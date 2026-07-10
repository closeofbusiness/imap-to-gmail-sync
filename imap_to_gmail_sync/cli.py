#!/usr/bin/env python3
"""
cli.py — run one sync pass. Intended to be invoked on a schedule (cron,
systemd timer, launchd, etc.) every 5-30 minutes.

Loads a .env file (if present) before reading config, so this works both
as a standalone cron job and inside a process that already has the vars
exported.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_dotenv_if_present(path: Path) -> int:
    """Minimal .env loader — no external dependency. Never overwrites a var
    that's already set in the real environment (explicit exports win)."""
    if not path.is_file():
        return 0
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = val.strip().strip('"').strip("'")
        n += 1
    return n


def main() -> int:
    env_path = Path(os.environ.get("IMAP2GMAIL_ENV_FILE", ".env"))
    n = _load_dotenv_if_present(env_path)
    if n:
        print(f"[imap-to-gmail-sync] loaded {n} vars from {env_path}")

    # Import after .env load so a fresh process picks up freshly-exported vars.
    from .core import load_config_from_env, run_sync

    parser = argparse.ArgumentParser(description="Sync an IMAP mailbox into Gmail")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and count messages without importing to Gmail",
    )
    args = parser.parse_args()

    cfg = load_config_from_env()
    if cfg is None:
        print(
            "[imap-to-gmail-sync] skipped — IMAP2GMAIL_SYNC_ENABLED not set "
            "or credentials missing (see README.md)"
        )
        return 0

    try:
        result = run_sync(cfg, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary = (
        f"{'dry-run: ' if args.dry_run else ''}"
        f"fetched={result.fetched} imported={result.imported} "
        f"dup_skip={result.skipped_duplicate} no_mid={result.skipped_no_message_id}"
    )
    if cfg.synced_folder:
        summary += f" moved={result.moved}"
    if result.errors:
        summary += f" errors={len(result.errors)}"
        for err in result.errors:
            print(f"  ! {err}", file=sys.stderr)

    print(summary)
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
