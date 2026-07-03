"""Event planning: resolve datetimes + attendees into a concrete plan.

Pure functions, no side effects — the transport layer is what actually
writes anything. `build_plan` turns a validated request envelope into a
plan dict that both the dry-run renderer and the real Google transport
consume. This is where the participant-vs-FYI semantics live.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class PlanError(ValueError):
    """Raised for un-plannable requests (bad datetime, etc.). The server
    maps these to ok=false / invalid_input."""


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_local_dt(value: str, tz: str) -> datetime:
    """Parse an ISO-8601 datetime. If it's naive, attach the configured
    local timezone; if it already carries an offset, respect it."""
    if not isinstance(value, str) or not value.strip():
        raise PlanError("start_missing")
    s = value.strip().replace(" ", "T", 1) if " " in value and "T" not in value else value.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise PlanError("bad_datetime") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz))
    return dt


def resolve_attendee(ref: str, contacts: list) -> dict | None:
    """Match a raw ref ("elon", "Lihi", "a@b.com") to a contact.

    Returns a resolved contact dict, or None if unresolved. A ref that
    looks like an email but isn't in the contacts list resolves to a
    synthetic contact (the user gave an explicit address on purpose).
    """
    r = ref.strip().lower()
    for c in contacts:
        if c.get("email") and c["email"].strip().lower() == r:
            return c
        if r in (c.get("aliases") or []):
            return c
        name = (c.get("name") or "").strip().lower()
        if name and (r == name or r == name.split()[0]):
            return c
    if "@" in r and "." in r.split("@")[-1]:
        return {"email": ref.strip(), "name": None, "aliases": [],
                "default_role": None, "note": None, "_raw": True}
    return None


def build_plan(req: dict, cfg) -> dict:
    """Turn a validated create_event request into a concrete plan."""
    tz = cfg.tz
    start = parse_local_dt(req["start"], tz)

    if req.get("end"):
        end = parse_local_dt(req["end"], tz)
        if end <= start:
            raise PlanError("end_before_start")
    else:
        dur = req.get("duration_minutes")
        if not isinstance(dur, int) or dur <= 0:
            dur = cfg.default_duration_min
        end = start + timedelta(minutes=dur)

    label = cfg.calendar_label(req.get("calendar"))
    cal = cfg.calendars.get(label, {"id": None, "name": label.title()})

    invited, informed, unresolved = [], [], []
    for a in req.get("attendees") or []:
        # A direct socket client (bypassing the validating plugin) can send a
        # malformed attendee like {} — guard so it raises the handled PlanError
        # path instead of a KeyError that would crash the daemon.
        if not isinstance(a, dict) or not a.get("ref"):
            raise PlanError("invalid_attendee")
        ref = a["ref"]
        role = a.get("role") or "participant"
        contact = resolve_attendee(ref, cfg.contacts)
        if contact is None:
            unresolved.append({"ref": ref, "role": role})
            continue
        entry = {
            "ref": ref,
            "name": contact.get("name"),
            "email": contact.get("email") or None,
            "notify": bool(a.get("notify", True)),
        }
        if role == "participant":
            invited.append(entry)
        else:
            informed.append(entry)

    return {
        "calendar": {"label": label, "id": cal.get("id"), "name": cal.get("name")},
        "event": {
            "title": req["title"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "tz": tz,
            "location": req.get("location"),
            "notes": req.get("notes"),
        },
        "invited": invited,       # participants -> get an invite (real mode)
        "informed": informed,     # fyi -> told, NOT on the invite
        "unresolved": unresolved,
        "warnings": _warnings(invited, informed, unresolved),
    }


def _warnings(invited, informed, unresolved) -> list:
    w = []
    for e in invited + informed:
        if not e["email"]:
            w.append(f"{e['ref']}: no email on file — cannot notify")
    for u in unresolved:
        w.append(f"{u['ref']}: no matching contact — skipped")
    return w


def _fmt_when(iso_start: str, iso_end: str) -> str:
    s = datetime.fromisoformat(iso_start)
    e = datetime.fromisoformat(iso_end)
    day = s.strftime("%a %b %-d")
    return f"{day} {s.strftime('%H:%M')}–{e.strftime('%H:%M')}"


def render_summary(plan: dict, *, dry_run: bool) -> str:
    ev = plan["event"]
    when = _fmt_when(ev["start"], ev["end"])
    prefix = "\U0001F9EA DRY RUN — would create" if dry_run else "✅ Created"
    lines = [f"{prefix}: “{ev['title']}” on {plan['calendar']['name']} — {when}"]
    if ev.get("location"):
        lines[0] += f" @ {ev['location']}"

    def _who(e):
        label = e["name"] or e["ref"]
        addr = f" <{e['email']}>" if e["email"] else ""
        note = "notified" if e["notify"] and e["email"] else "not notified"
        return f"{label}{addr} ({note})"

    if plan["invited"]:
        lines.append("  Invited (attending): " + "; ".join(_who(e) for e in plan["invited"]))
    if plan["informed"]:
        lines.append("  FYI (optional — invited but not expected to attend): "
                     + "; ".join(_who(e) for e in plan["informed"]))
    if plan["unresolved"]:
        lines.append("  Unresolved (skipped): "
                     + "; ".join(u["ref"] for u in plan["unresolved"]))
    return "\n".join(lines)
