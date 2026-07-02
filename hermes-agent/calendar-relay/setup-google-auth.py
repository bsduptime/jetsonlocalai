#!/usr/bin/env python3
"""One-time Google Calendar authorization for the calendar relay.

What it does:
  * runs the standard installed-app OAuth flow in your browser,
  * caches the resulting user token to a token file the relay reads,
  * (with --list-calendars) prints your calendars + their IDs so you can
    grab the "Family" calendar id for CALENDAR_FAMILY_ID.

Prereqs:
  1. A Google Cloud project with the "Google Calendar API" enabled.
  2. An OAuth client of type "Desktop app" — download its JSON.
  3. pip install -r requirements.txt  (ideally in a venv)

Usage:
  export CALENDAR_GOOGLE_CREDENTIALS=~/secrets/gcal-oauth-client.json
  export CALENDAR_GOOGLE_TOKEN=~/secrets/gcal-token.json
  python3 setup-google-auth.py                 # authorize (opens browser)
  python3 setup-google-auth.py --list-calendars  # then find your Family cal id
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.readonly"]


def _paths() -> tuple[str, str]:
    creds = os.environ.get("CALENDAR_GOOGLE_CREDENTIALS")
    token = os.environ.get("CALENDAR_GOOGLE_TOKEN") or str(
        Path.home() / ".config" / "hermes-calendar" / "gcal-token.json")
    if not creds:
        sys.exit("error: set CALENDAR_GOOGLE_CREDENTIALS to your OAuth client "
                 "secrets json (Desktop app type).")
    creds = str(Path(creds).expanduser())
    token = str(Path(token).expanduser())
    if not Path(creds).exists():
        sys.exit(f"error: credentials file not found: {creds}")
    return creds, token


def _load_libs():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        return Credentials, Request, InstalledAppFlow, build
    except ImportError:
        sys.exit("error: Google libs missing. Run:\n"
                 "  pip install -r requirements.txt")


def authorize() -> str:
    Credentials, Request, InstalledAppFlow, _ = _load_libs()
    creds_path, token_path = _paths()

    creds = None
    if Path(token_path).exists():
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            creds = None
    if creds and creds.valid:
        print(f"already authorized — token at {token_path}")
        return token_path
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        print("opening a browser to authorize Google Calendar access…")
        creds = flow.run_local_server(port=0)

    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(token_path).write_text(creds.to_json())
    os.chmod(token_path, 0o600)
    print(f"✓ authorized. token cached at {token_path}")
    print("  now run:  python3 setup-google-auth.py --list-calendars")
    return token_path


def list_calendars() -> None:
    Credentials, Request, _, build = _load_libs()
    _, token_path = _paths()
    if not Path(token_path).exists():
        sys.exit("no token yet — run without --list-calendars first to authorize.")
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        Path(token_path).write_text(creds.to_json())
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    items = svc.calendarList().list().execute().get("items", [])
    print("\nYour calendars (use the id for CALENDAR_FAMILY_ID):\n")
    for c in items:
        access = c.get("accessRole", "")
        star = " *writable*" if access in ("owner", "writer") else ""
        print(f"  {c.get('summary','(no name)')!r}")
        print(f"      id: {c['id']}{star}")
    print()


if __name__ == "__main__":
    if "--list-calendars" in sys.argv:
        list_calendars()
    else:
        authorize()
