"""Transports: how a plan becomes reality (or a rendered rehearsal).

DryRunTransport writes nothing and sends nothing — it records the plan to
the state dir for inspection (mirrors the mailer writing a .eml in dry-run).
It is the DEFAULT.

GoogleTransport does real writes via the Google Calendar API:
  - participants -> required attendees
  - fyi people   -> OPTIONAL attendees (they see it / get notified, but are
                    marked "not expected to attend" — the clean mapping for
                    "she won't come but should know", no separate channel
                    needed)
  - sendUpdates="all" so everyone is actually notified by Google.
It is only constructed when CALENDAR_DRY_RUN=false. Google libraries are
imported lazily so the dry-run path has zero third-party dependencies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class TransportError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class DryRunTransport:
    name = "dryrun"

    def __init__(self, state_dir: str):
        self.state_dir = state_dir

    def execute(self, plan: dict, *, request_id: str) -> dict:
        record = {"request_id": request_id, "plan": plan}
        try:
            outdir = Path(self.state_dir) / "dryrun"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / f"{request_id}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # inspection artifact is best-effort; never fail the call on it
        return {"dry_run": True, "calendar_url": None, "event_id": None}


class GoogleTransport:
    """Real Google Calendar writes.

    Config (env):
      CALENDAR_GOOGLE_CREDENTIALS  path to the OAuth client secrets json
      CALENDAR_GOOGLE_TOKEN        path to the cached user token json
                                   (produced by setup-google-auth.py)
    The token is created once via the installed-app OAuth flow; here we
    only load it (refreshing if needed). No browser interaction at runtime.
    """

    name = "google"

    def __init__(self, state_dir: str, calendars: dict):
        self.state_dir = state_dir
        self.calendars = calendars
        self.token_path = os.environ.get("CALENDAR_GOOGLE_TOKEN")
        self.creds_path = os.environ.get("CALENDAR_GOOGLE_CREDENTIALS")
        if not self.token_path or not Path(self.token_path).exists():
            raise TransportError(
                "google_not_configured",
                "no cached token — run setup-google-auth.py and set "
                "CALENDAR_GOOGLE_TOKEN",
            )
        self._service = None

    def _svc(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError as e:
            raise TransportError(
                "google_libs_missing",
                "pip install -r calendar-relay/requirements.txt",
            ) from e
        try:
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    Path(self.token_path).write_text(creds.to_json())
                else:
                    raise TransportError("google_token_invalid",
                                         "re-run setup-google-auth.py")
            self._service = build("calendar", "v3", credentials=creds,
                                  cache_discovery=False)
        except TransportError:
            raise
        except Exception as e:
            raise TransportError("google_auth_failed", type(e).__name__) from e
        return self._service

    def execute(self, plan: dict, *, request_id: str) -> dict:
        cal_id = plan["calendar"].get("id") or "primary"
        ev = plan["event"]

        attendees = []
        for p in plan["invited"]:
            if p.get("email"):
                attendees.append({"email": p["email"]})
        for f in plan["informed"]:
            if f.get("email"):
                attendees.append({"email": f["email"], "optional": True})

        body = {
            "summary": ev["title"],
            "start": {"dateTime": ev["start"], "timeZone": ev["tz"]},
            "end": {"dateTime": ev["end"], "timeZone": ev["tz"]},
        }
        if ev.get("location"):
            body["location"] = ev["location"]
        if ev.get("notes"):
            body["description"] = ev["notes"]
        if attendees:
            body["attendees"] = attendees

        try:
            created = self._svc().events().insert(
                calendarId=cal_id,
                body=body,
                sendUpdates="all",   # participants + optional attendees get notified
            ).execute()
        except TransportError:
            raise
        except Exception as e:
            # google HttpError and friends -> structured failure, never raise raw
            detail = getattr(e, "reason", None) or type(e).__name__
            raise TransportError("google_insert_failed", str(detail)[:120]) from e

        return {
            "dry_run": False,
            "calendar_url": created.get("htmlLink"),
            "event_id": created.get("id"),
        }


def make_transport(cfg) -> DryRunTransport | GoogleTransport:
    if cfg.dry_run:
        return DryRunTransport(cfg.state_dir)
    return GoogleTransport(cfg.state_dir, cfg.calendars)
