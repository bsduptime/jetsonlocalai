"""calendar plugin handler — thin shim over the hermes-calendar daemon.

All policy (contact allowlist, rate limit, dry-run vs. real transport,
Google credentials) lives in a separate `hermes-calendar.service` reached
over a Unix socket. This handler never sees credentials; it validates
field types up front so simple LLM mistakes don't reach the socket, hands
the request to `_client`, and ALWAYS returns a JSON string, never raises.
"""

from __future__ import annotations

import json

from . import _client


def _bad(reason: str, detail: str = "") -> str:
    return json.dumps({
        "ok": False, "error": "invalid_input",
        "reason": reason, "detail": detail,
    })


def create_event(args: dict, **_kwargs) -> str:
    try:
        title = args.get("title")
        start = args.get("start")
        end = args.get("end") or None
        duration = args.get("duration_minutes")
        calendar = args.get("calendar") or None
        location = args.get("location") or None
        notes = args.get("notes") or None
        attendees = args.get("attendees") or []
    except Exception as e:
        return _bad("args_unreadable", type(e).__name__)

    if not isinstance(title, str) or not title.strip():
        return _bad("invalid_field_type", "title")
    if not isinstance(start, str) or not start.strip():
        return _bad("invalid_field_type", "start")
    if duration is not None and not isinstance(duration, int):
        return _bad("invalid_field_type", "duration_minutes")
    if not isinstance(attendees, list):
        return _bad("invalid_field_type", "attendees")

    norm_attendees = []
    for i, a in enumerate(attendees):
        if not isinstance(a, dict):
            return _bad("attendee_not_object", f"attendees[{i}]")
        ref = a.get("ref")
        role = a.get("role")
        if not isinstance(ref, str) or not ref.strip():
            return _bad("attendee_missing_ref", f"attendees[{i}]")
        if role not in ("participant", "fyi"):
            return _bad("attendee_bad_role", f"attendees[{i}]")
        notify = a.get("notify")
        norm_attendees.append({
            "ref": ref.strip(),
            "role": role,
            "notify": True if notify is None else bool(notify),
        })

    try:
        resp = _client.create_event(
            title=title.strip(),
            start=start.strip(),
            end=end,
            duration_minutes=duration,
            calendar=calendar,
            location=location,
            notes=notes,
            attendees=norm_attendees,
        )
    except _client.DaemonUnreachable as e:
        return json.dumps({
            "ok": False, "error": "transport_failed",
            "reason": "daemon_unreachable", "detail": f"{e.reason}: {e.detail}",
        })
    except Exception as e:
        return _bad("internal_error", type(e).__name__)

    if isinstance(resp, dict) and "v" in resp:
        resp = {k: v for k, v in resp.items() if k != "v"}
    return json.dumps(resp)


def list_contacts(args: dict, **_kwargs) -> str:
    """Return the known calendar contacts so the agent can resolve names/
    aliases to addresses + role hints before calling create_event. Read-only.
    Always returns JSON, never raises."""
    try:
        resp = _client.list_contacts()
    except _client.DaemonUnreachable as e:
        return json.dumps({
            "ok": False, "error": "transport_failed",
            "reason": "daemon_unreachable", "detail": f"{e.reason}: {e.detail}",
        })
    except Exception as e:
        return _bad("internal_error", type(e).__name__)
    if isinstance(resp, dict) and "v" in resp:
        resp = {k: v for k, v in resp.items() if k != "v"}
    return json.dumps(resp)
