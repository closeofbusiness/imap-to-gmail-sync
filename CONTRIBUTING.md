# Contributing to imap-to-gmail-sync

Thanks for your interest! This tool is intentionally small and single-purpose — contributions should keep it that way.

## Ground rules

- **Stay narrow.** This tool does one thing: IMAP fetch → Gmail import (+ an opt-in verified move on the source). Resist scope creep into a general mail client or a two-way sync.
- **The fetch path stays read-only, always.** The IMAP `SELECT` used to fetch mail must stay `readonly=True`, full stop. The *only* sanctioned write path against the source mailbox is the existing opt-in move-to-folder feature, and any change to it must preserve its invariant: a message is moved only after an independent Gmail-side re-fetch confirms the import genuinely succeeded — never trust an API call's own return value for anything that deletes-on-source. No new feature may write to the source mailbox outside that one already-reviewed path.
- **Never send mail.** This is an import tool, not a mail client.
- **Keep it small.** Prefer clarity over cleverness; the whole tool should stay readable in one sitting.
- **No telemetry.** No feature may phone home or report usage anywhere beyond stdout/stderr.

## How to contribute

1. Open an issue describing the change before large work, so we can agree on scope.
2. Fork, branch, and keep pull requests focused.
3. Add/update tests in `tests/` — `python3 -m pytest` (or `unittest discover`) must pass.
4. Follow the existing style (PEP 8, type hints, docstrings).

## Developer Certificate of Origin (DCO)

By submitting a pull request, you certify the [DCO](https://developercertificate.org/): that you wrote the code or otherwise have the right to submit it under the project's MIT license. Sign off each commit:

```bash
git commit -s -m "your message"
```

which adds a `Signed-off-by: Your Name <you@example.com>` line.

## PR checklist

- [ ] Change is focused and described in an issue/PR
- [ ] Source mailbox stays read-only; no new outbound-send capability
- [ ] Tests added/updated and passing
- [ ] Docs/README updated if behaviour or env vars changed
- [ ] Commits signed off (`-s`)
