"""
core.py — Incremental IMAP -> Gmail import.

Syncs mail from any IMAP mailbox into a Gmail account via the Gmail REST API
(messages.import + OAuth), so you can keep receiving mail sent to an old/
legacy address after your provider retires "check mail via POP from other
accounts" (Google deprecated this for Gmail; this is the IMAP-based
replacement).

Read-only on the source IMAP server by default. Never sends mail. If
IMAP2GMAIL_SYNCED_FOLDER is set, successfully-imported-and-verified messages
are moved (COPY + \\Deleted + EXPUNGE) from the source folder into that
folder, so it's visually clear what's already been handled. This is the one
intentional write path, and it only ever touches a message AFTER an
independent Gmail-side verification confirms the import actually landed
(never trust the import call's return value alone for something that
deletes-on-source — see README.md "Moving synced mail into a folder").
"""
from __future__ import annotations

import base64
import email
import imaplib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_UA = "imap-to-gmail-sync/0.2"
MAX_IMPORTED_IDS = 500


@dataclass(frozen=True)
class SyncConfig:
    account_id: str
    src_host: str
    src_user: str
    src_pass: str
    src_folder: str
    gmail_label: str
    after_date: str | None
    state_path: Path
    enabled: bool = True
    synced_folder: str | None = None


@dataclass
class SyncResult:
    fetched: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    skipped_no_message_id: int = 0
    moved: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def default_state_dir() -> Path:
    """Where per-account sync state (last UID, dedup ring buffer) is kept.

    Override with IMAP2GMAIL_STATE_DIR; defaults to ~/.imap_to_gmail_sync/state.
    """
    override = os.environ.get("IMAP2GMAIL_STATE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".imap_to_gmail_sync" / "state"


def load_config_from_env() -> SyncConfig | None:
    """Build a SyncConfig from environment variables, or None if disabled/
    incomplete. See README.md for the full variable list."""
    if os.environ.get("IMAP2GMAIL_SYNC_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return None

    host = os.environ.get("IMAP2GMAIL_SRC_HOST", "").strip()
    user = os.environ.get("IMAP2GMAIL_SRC_USER", "").strip()
    password = os.environ.get("IMAP2GMAIL_SRC_PASS", "").strip()
    if not all([host, user, password]):
        return None

    account_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", user.split("@")[0])[:40] or "default"
    after = os.environ.get("IMAP2GMAIL_AFTER_DATE", "").strip() or None
    synced_folder = os.environ.get("IMAP2GMAIL_SYNCED_FOLDER", "").strip() or None

    return SyncConfig(
        account_id=account_id,
        src_host=host,
        src_user=user,
        src_pass=password,
        src_folder=os.environ.get("IMAP2GMAIL_SRC_FOLDER", "INBOX").strip() or "INBOX",
        gmail_label=os.environ.get("IMAP2GMAIL_GMAIL_LABEL", "Imported").strip() or "Imported",
        after_date=after,
        state_path=default_state_dir() / f"{account_id}.json",
        synced_folder=synced_folder,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _gmail_access_token() -> str:
    client_id = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError(
            "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN required "
            "(see README.md for how to obtain an OAuth Desktop client + refresh token)"
        )
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        headers={"User-Agent": DEFAULT_UA},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _gmail_request(
    method: str,
    path: str,
    *,
    token: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict | None:
    resp = httpx.request(
        method,
        f"{GMAIL_API}/{path}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": DEFAULT_UA},
        params=params,
        json=json_body,
        timeout=30,
    )
    if resp.status_code == 204:
        return {}
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def ensure_gmail_label(token: str, label_name: str) -> str:
    data = _gmail_request("GET", "labels", token=token) or {}
    for lbl in data.get("labels", []):
        if lbl.get("name") == label_name:
            return lbl["id"]

    created = _gmail_request(
        "POST",
        "labels",
        token=token,
        json_body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    )
    if not created or "id" not in created:
        raise RuntimeError(f"Failed to create Gmail label {label_name!r}")
    return created["id"]


def gmail_has_message_id(token: str, message_id: str) -> bool:
    clean = message_id.strip().strip("<>").strip()
    if not clean:
        return False
    query = f"rfc822msgid:{clean}"
    data = _gmail_request(
        "GET",
        "messages",
        token=token,
        params={"q": query, "maxResults": 1},
    )
    return bool(data and data.get("messages"))


def verify_gmail_import(token: str, gmail_id: str) -> bool:
    """Independent post-import check: re-fetch the message by its returned id
    and confirm it's really retrievable, before we let anything (e.g. a
    source-mailbox move) depend on the import having succeeded. Cheap
    (metadata only) and deliberately distrusts the import call's own return
    value — see README.md "Moving synced mail into a folder" for why."""
    try:
        data = _gmail_request(
            "GET",
            f"messages/{gmail_id}",
            token=token,
            params={"format": "minimal"},
        )
    except httpx.HTTPStatusError:
        return False
    return bool(data and data.get("id") == gmail_id)


def import_rfc822_to_gmail(token: str, label_id: str, raw_bytes: bytes) -> str | None:
    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    data = _gmail_request(
        "POST",
        "messages/import",
        token=token,
        json_body={
            "raw": encoded,
            # INBOX is required alongside the custom label: Gmail only shows
            # a message in the primary inbox list if it carries INBOX.
            # Without it, imported mail is invisible except via the custom
            # label or a label: search.
            "labelIds": [label_id, "INBOX"],
            "internalDateSource": "dateHeader",
        },
    )
    if not data:
        return None
    return data.get("id")


def extract_message_id(raw_bytes: bytes) -> str | None:
    msg = email.message_from_bytes(raw_bytes)
    mid = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    return mid or None


def _imap_since_clause(after_date: str | None) -> str | None:
    if not after_date:
        return None
    dt = datetime.strptime(after_date, "%Y-%m-%d")
    return dt.strftime("%d-%b-%Y")


def fetch_new_messages(
    cfg: SyncConfig,
    state: dict[str, Any],
) -> tuple[list[tuple[int, bytes]], dict[str, Any]]:
    last_uid = int(state.get("last_uid", 0))
    last_uidvalidity = state.get("uidvalidity")

    conn = imaplib.IMAP4_SSL(cfg.src_host)
    try:
        conn.login(cfg.src_user, cfg.src_pass)
        status, _ = conn.select(f'"{cfg.src_folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP select failed for folder {cfg.src_folder!r}")

        _, data = conn.status(cfg.src_folder, "(UIDVALIDITY)")
        uidvalidity = None
        if data and data[0]:
            m = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
            if m:
                uidvalidity = int(m.group(1))

        if uidvalidity is None:
            raise RuntimeError("Could not read UIDVALIDITY from source folder")

        if last_uidvalidity is None or uidvalidity != last_uidvalidity:
            last_uid = 0

        criteria = f"UID {last_uid + 1}:*" if last_uid else "ALL"
        since = _imap_since_clause(cfg.after_date)
        if since:
            if last_uid:
                status, uid_data = conn.uid("search", None, f"(UID {last_uid + 1}:*)", f"SINCE {since}")
            else:
                status, uid_data = conn.uid("search", None, f"SINCE {since}")
        else:
            status, uid_data = conn.uid("search", None, criteria)

        if status != "OK":
            raise RuntimeError(f"IMAP UID search failed: {status}")

        uid_list = []
        if uid_data and uid_data[0]:
            uid_list = [int(u) for u in uid_data[0].split() if u]
        uid_list = sorted(u for u in uid_list if u > last_uid)

        messages: list[tuple[int, bytes]] = []
        for uid in uid_list:
            status, fetched = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            part = fetched[0]
            if isinstance(part, tuple) and len(part) >= 2:
                messages.append((uid, part[1]))

        new_state = {
            "last_uid": max((u for u, _ in messages), default=last_uid),
            "uidvalidity": uidvalidity,
            "imported_message_ids": state.get("imported_message_ids", []),
        }
        return messages, new_state
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def move_messages_to_folder(cfg: SyncConfig, uids: list[int]) -> tuple[int, list[str]]:
    """Move verified-imported messages from cfg.src_folder into
    cfg.synced_folder on the source IMAP server (COPY + \\Deleted + EXPUNGE —
    the universally-compatible pattern, not the RFC 6851 MOVE extension, so
    this works even on IMAP servers that don't advertise MOVE).

    Only ever called with uids whose Gmail import was independently verified
    (see verify_gmail_import) — this is the one write path against the
    source mailbox, and it is deliberately last, after everything else
    already succeeded.
    """
    if not uids or not cfg.synced_folder:
        return 0, []

    moved = 0
    errors: list[str] = []
    conn = imaplib.IMAP4_SSL(cfg.src_host)
    try:
        conn.login(cfg.src_user, cfg.src_pass)

        # Best-effort folder creation; IMAP servers vary in how they report
        # "already exists" so we don't treat a CREATE failure as fatal here —
        # the SELECT right after is the real check that the folder is usable.
        try:
            conn.create(f'"{cfg.synced_folder}"')
        except Exception:
            pass

        status, _ = conn.select(f'"{cfg.src_folder}"', readonly=False)
        if status != "OK":
            return 0, [f"move: could not open {cfg.src_folder!r} writable"]

        for uid in uids:
            status, _ = conn.uid("copy", str(uid), f'"{cfg.synced_folder}"')
            if status != "OK":
                errors.append(f"move: uid={uid} COPY to {cfg.synced_folder!r} failed")
                continue
            status, _ = conn.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
            if status != "OK":
                errors.append(f"move: uid={uid} copied but could not mark \\Deleted on source")
                continue
            moved += 1

        conn.expunge()
        return moved, errors
    except Exception as exc:  # noqa: BLE001 - best-effort, never fail the sync over this
        errors.append(f"move: {exc}")
        return moved, errors
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def run_sync(cfg: SyncConfig, *, dry_run: bool = False) -> SyncResult:
    result = SyncResult()
    state = load_state(cfg.state_path)
    messages, new_state = fetch_new_messages(cfg, state)
    result.fetched = len(messages)

    if dry_run:
        result.imported = len(messages)
        return result

    token = _gmail_access_token()
    label_id = ensure_gmail_label(token, cfg.gmail_label)
    imported_ids: list[str] = list(new_state.get("imported_message_ids", []))
    verified_uids: list[int] = []

    for uid, raw in messages:
        mid = extract_message_id(raw)
        if not mid:
            result.skipped_no_message_id += 1
            new_state["last_uid"] = max(new_state.get("last_uid", 0), uid)
            continue

        if mid in imported_ids or gmail_has_message_id(token, mid):
            result.skipped_duplicate += 1
            new_state["last_uid"] = max(new_state.get("last_uid", 0), uid)
            continue

        try:
            gmail_id = import_rfc822_to_gmail(token, label_id, raw)
        except httpx.HTTPStatusError as exc:
            result.errors.append(f"uid={uid} import failed: {exc}")
            continue

        if gmail_id:
            result.imported += 1
            imported_ids.append(mid)
            imported_ids = imported_ids[-MAX_IMPORTED_IDS:]
            new_state["last_uid"] = max(new_state.get("last_uid", 0), uid)
            # Only queue for a source-mailbox move once Gmail independently
            # confirms the message is retrievable — never trust the import
            # call's return value alone for something that deletes-on-source.
            if cfg.synced_folder and verify_gmail_import(token, gmail_id):
                verified_uids.append(uid)
        else:
            result.errors.append(f"uid={uid} import returned no id")

    new_state["imported_message_ids"] = imported_ids
    save_state(cfg.state_path, new_state)

    if verified_uids:
        moved, move_errors = move_messages_to_folder(cfg, verified_uids)
        result.moved = moved
        result.errors.extend(move_errors)

    return result
