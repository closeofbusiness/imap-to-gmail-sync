# imap-to-gmail-sync

Incrementally syncs mail from **any IMAP mailbox** into **Gmail**, via the Gmail
REST API (`messages.import` + OAuth) — no app passwords, no IMAP access to
Gmail itself.

**Why this exists:** Google deprecated Gmail's POP "check mail from other
accounts" feature, which many people used to consolidate an old/legacy mail
address (a self-hosted mailcow box, a family domain, an old work account,
etc.) into their primary Gmail inbox. This tool replaces that workflow: a
small script you run on a schedule (cron, systemd timer, launchd — anywhere)
pulls new mail from the source mailbox over IMAP and imports it into Gmail
under a label you choose.

Not affiliated with Google. No warranty — see [LICENSE](LICENSE).

## What it does

- Connects **read-only by default** to the source IMAP mailbox (`readonly=True`
  SELECT for fetching — it never marks source mail as read, never deletes,
  never writes to the source server at all, unless you explicitly opt into
  the move-to-folder feature below).
- Tracks progress incrementally via IMAP `UID` + `UIDVALIDITY`, so each run
  only fetches mail newer than the last successful sync.
- Deduplicates by `Message-ID` two ways: a local ring buffer of recently
  imported IDs, and a live Gmail `rfc822msgid:` search — so it's safe to
  re-run, and safe even if some of the mail already reached Gmail through
  another path (e.g. the POP fetch you're retiring).
- Imports via `messages.import` with a Gmail label you choose (auto-created
  if it doesn't exist) **and** `INBOX`, preserving the original `Date:`
  header — both labels matter: Gmail only shows a message in the default
  inbox view if it carries `INBOX`, so without it, correctly-imported mail
  is invisible except via the custom label or a `label:` search.
- Optionally moves synced mail into a separate folder on the source server
  once it's independently verified present in Gmail — see below.
- Ships as both a **library** (`imap_to_gmail_sync.core`) and a **CLI**
  (`imap-to-gmail-sync`).

## What it does not do

- It does not send mail.
- It does not touch the source mailbox beyond reading, unless you opt into
  `IMAP2GMAIL_SYNCED_FOLDER` (see below) — and even then, only ever a
  verified-safe move, never a delete.
- It does not handle the Gmail *OAuth setup* for you — you create a Desktop
  OAuth client in Google Cloud Console yourself (see below). This keeps the
  tool from ever needing your Google account password.

## Install

```bash
pip install imap-to-gmail-sync
# or, from source:
pip install -e .
```

## Set up Gmail OAuth (one-time)

1. [Google Cloud Console](https://console.cloud.google.com) → new project →
   enable the **Gmail API**.
2. **OAuth consent screen** → User type **External** → add yourself as a test
   user (or publish — see Google's docs on
   [production readiness](https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance)
   if you want a refresh token that doesn't expire after ~7 days).
3. **Credentials** → **Create OAuth client ID** → **Desktop app**. Note the
   `client_id` and `client_secret`.
4. Scope needed: `https://www.googleapis.com/auth/gmail.modify` (covers
   import + label creation; nothing broader).
5. Run any standard OAuth "installed app" flow once to get a `refresh_token`
   (e.g. `google-auth-oauthlib`'s `InstalledAppFlow.run_local_server()`, or
   any Gmail-API quickstart) — this repo doesn't ship a bootstrap script
   because most people already have a preferred way to do this, but a
   ~15-line example is in [`docs/oauth_bootstrap_example.py`](docs/oauth_bootstrap_example.py).

## Configure

Set these as environment variables (a `.env` file in the working directory
is also picked up automatically by the CLI — see `IMAP2GMAIL_ENV_FILE` below):

```bash
# Enable + source mailbox
IMAP2GMAIL_SYNC_ENABLED=1
IMAP2GMAIL_SRC_HOST=mail.example.com
IMAP2GMAIL_SRC_USER=you@example.com
IMAP2GMAIL_SRC_PASS=<mailbox password>
IMAP2GMAIL_SRC_FOLDER=INBOX               # optional, default INBOX
IMAP2GMAIL_GMAIL_LABEL=Imported           # optional, default "Imported"
# IMAP2GMAIL_AFTER_DATE=2026-01-01        # optional YYYY-MM-DD IMAP SINCE filter,
#                                            useful on first run for a large mailbox
# IMAP2GMAIL_STATE_DIR=~/.imap_to_gmail_sync/state  # optional, this is the default
# IMAP2GMAIL_ENV_FILE=.env                # optional, path to a .env file the CLI loads
# IMAP2GMAIL_SYNCED_FOLDER=Synced         # optional, see "Moving synced mail into a folder" below

# Destination Gmail account OAuth
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
```

## Run

```bash
# See what would be imported, without writing anything to Gmail:
imap-to-gmail-sync --dry-run

# Actually import:
imap-to-gmail-sync
```

Schedule it (cron example, every 5 minutes):

```cron
*/5 * * * * cd /path/to/your/config && /usr/bin/env imap-to-gmail-sync >> sync.log 2>&1
```

## First run on a large/old mailbox

The very first run with no prior state does an IMAP `ALL` search on the
source folder — on an old mailbox this could mean thousands of messages. Set
`IMAP2GMAIL_AFTER_DATE=YYYY-MM-DD` (today, or wherever you want the cutover
point) for the first run to only pull mail from that date forward, then
unset it (or leave it — it's harmless once `last_uid` state exists, since the
UID-based incremental fetch takes priority).

## Moving synced mail into a folder

By default the source mailbox just accumulates forever with no visual
distinction between "already synced" and "not yet synced." If you want that
distinction, set `IMAP2GMAIL_SYNCED_FOLDER` (e.g. `Synced`) — the folder is
auto-created if it doesn't exist.

**How it stays safe:** a message is only ever moved *after* an independent
Gmail-side check confirms it's genuinely retrievable there — the tool
deliberately does not trust the import API call's own "success" response for
something that's about to be deleted from the source. Concretely, per
message: import → **separate** `GET` re-fetch by the returned id, confirming
the id really exists → only then `UID COPY` into the target folder, `UID
STORE +FLAGS (\Deleted)` on the original, `EXPUNGE`. Any failure at the
move step (folder unreachable, COPY rejected, etc.) is non-fatal and just
leaves that message where it was, to be retried next run — a move failure
never affects whether the Gmail import itself is considered successful, and
sync state is saved before any move is attempted.

**This is opt-in and only affects messages processed while it's enabled.**
Mail already imported in earlier runs (before you set this, or before this
feature existed) stays wherever it is — the dedup logic will always
correctly skip it as "already imported," and duplicates are never queued
for a move. If you're turning this on for a mailbox that already has synced
mail sitting in it, you'll need a one-time script to independently
re-verify each already-imported message via `gmail_has_message_id` and then
call `move_messages_to_folder` directly with the confirmed UIDs — this is a
deliberate manual step, not automated, so you can't accidentally mass-move
mail you haven't actually double-checked.

## Security notes

- Source mailbox credentials and Gmail OAuth secrets are read from the
  environment only — never logged, never written to state files.
- State files (`~/.imap_to_gmail_sync/state/*.json`) contain only IMAP UIDs,
  `UIDVALIDITY`, and a ring buffer of recent `Message-ID` values — no
  credentials, no message content.
- Report vulnerabilities per [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
