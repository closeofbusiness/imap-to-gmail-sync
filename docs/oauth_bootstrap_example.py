#!/usr/bin/env python3
"""
oauth_bootstrap_example.py — one-time helper to obtain a Gmail refresh token.

Not part of the installed package; run it once by hand, then copy the
printed values into your environment / .env file and delete/ignore this
script's local credentials.json + token.json.

Requires (not a package dependency, install ad hoc):
    pip install google-auth-oauthlib

Usage:
    1. Download your OAuth Desktop client's credentials.json from
       Google Cloud Console (Credentials -> your Desktop client -> Download JSON)
       and place it next to this script.
    2. python3 oauth_bootstrap_example.py
    3. A browser opens; sign in and grant access.
    4. The script prints GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN.
"""
from __future__ import annotations

import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main() -> None:
    creds_path = Path(__file__).parent / "credentials.json"
    if not creds_path.exists():
        raise SystemExit(
            f"Missing {creds_path}. Download it from Google Cloud Console "
            "(Credentials -> your Desktop OAuth client -> Download JSON)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    client_config = json.loads(creds_path.read_text())["installed"]
    print("\nAdd these to your environment / .env:\n")
    print(f"GMAIL_CLIENT_ID={client_config['client_id']}")
    print(f"GMAIL_CLIENT_SECRET={client_config['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print(
        "\nRefresh tokens issued while your OAuth consent screen is in "
        "'Testing' publishing status expire after ~7 days. Move to "
        "'In production' for a long-lived token (see Google's docs linked "
        "from the main README)."
    )


if __name__ == "__main__":
    main()
