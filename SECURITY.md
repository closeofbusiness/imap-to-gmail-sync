# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, report privately using GitHub's built-in **"Report a vulnerability"** button on the repository's **Security** tab. This routes privately to the maintainers — no email or external contact needed.

We'll acknowledge within a reasonable time and coordinate a fix and disclosure.

## Scope notes

- This tool reads source-mailbox credentials and Gmail OAuth secrets from
  environment variables / a local `.env` file only. Never commit real
  credentials; `.gitignore` excludes `.env` and common secret file patterns
  by default.
- The IMAP connection used to **fetch** mail is always opened `readonly=True`
  — that path structurally cannot mark mail read, move it, or delete it.
  The one exception is opt-in: if `IMAP2GMAIL_SYNCED_FOLDER` is set, a
  message is moved (COPY + `\Deleted` + EXPUNGE) out of the source folder,
  but *only* after an independent Gmail-side re-fetch confirms the import
  genuinely landed (see README.md "Moving synced mail into a folder") —
  the import call's own return value is never trusted for this. With
  `IMAP2GMAIL_SYNCED_FOLDER` unset (the default), the tool is exactly as
  read-only as described above. If you find a code path that deletes or
  moves source mail *without* that independent verification, or that does
  so when `IMAP2GMAIL_SYNCED_FOLDER` is unset, that's a P0 report.
- State files under `~/.imap_to_gmail_sync/state/` contain only IMAP UIDs
  and a `Message-ID` dedup list — treat them as low-sensitivity, but they
  do reveal *which* messages were imported.
- This tool never sends mail and has no outbound-network surface beyond the
  source IMAP server and the Gmail API.
