"""Relay configuration: env knobs + contacts + calendars.

Stdlib-only. Contacts can come from (in priority order):
  1. an injected Python list (used by the demo/tests),
  2. a YAML file if PyYAML is installed,
  3. a JSON file.
This keeps the dry-run demo dependency-free while allowing the Jetson
install to use a human-friendly YAML contacts file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _as_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _as_int(val: str | None, default: int) -> int:
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    dry_run: bool = True
    tz: str = "Asia/Jerusalem"
    default_calendar: str = "family"
    default_duration_min: int = 60
    daily_limit: int = 20
    socket_path: str = "/run/hermes-calendar/sock"
    state_dir: str = "/tmp/hermes-calendar-state"
    # label -> {"id": <google calendar id or None>, "name": <display>}
    calendars: dict = field(default_factory=dict)
    # list of {"email", "name", "aliases":[...], "default_role", "note"}
    contacts: list = field(default_factory=list)

    def calendar_label(self, label: str | None) -> str:
        return (label or self.default_calendar).strip().lower()


def _load_contacts_file(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dep
        except ImportError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                f"contacts file {path} is YAML but PyYAML is not installed; "
                "install pyyaml or use a .json contacts file"
            ) from e
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    contacts = data.get("contacts", []) if isinstance(data, dict) else []
    return contacts


def _normalize_contacts(raw: list) -> list:
    out = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        email = (c.get("email") or "").strip()
        name = c.get("name")
        aliases = [str(a).strip().lower() for a in (c.get("aliases") or []) if str(a).strip()]
        role = c.get("default_role")
        if role not in ("participant", "fyi"):
            role = None
        out.append({
            "email": email,
            "name": name,
            "aliases": aliases,
            "default_role": role,
            "note": c.get("note"),
        })
    return out


def load_config(env: dict | None = None, *, contacts: list | None = None,
                contacts_path: str | None = None,
                calendars: dict | None = None) -> Config:
    env = env if env is not None else os.environ
    cfg = Config(
        dry_run=_as_bool(env.get("CALENDAR_DRY_RUN"), True),
        tz=env.get("CALENDAR_TZ") or "Asia/Jerusalem",
        default_calendar=(env.get("CALENDAR_DEFAULT") or "family").lower(),
        default_duration_min=_as_int(env.get("CALENDAR_DEFAULT_DURATION_MIN"), 60),
        daily_limit=_as_int(env.get("CALENDAR_DAILY_LIMIT"), 20),
        socket_path=env.get("HERMES_CALENDAR_SOCKET") or "/run/hermes-calendar/sock",
        state_dir=env.get("CALENDAR_STATE_DIR") or "/tmp/hermes-calendar-state",
    )
    if contacts is not None:
        cfg.contacts = _normalize_contacts(contacts)
    else:
        path = contacts_path or env.get("CALENDAR_CONTACTS_FILE")
        cfg.contacts = _normalize_contacts(_load_contacts_file(path)) if path else []

    if calendars is not None:
        cfg.calendars = calendars
    else:
        # A minimal default so a fresh install still resolves the "family" label.
        cfg.calendars = {cfg.default_calendar: {"id": None, "name": cfg.default_calendar.title()}}
        # Live: point the "family" label at a real Google calendar id.
        fam_id = env.get("CALENDAR_FAMILY_ID")
        if fam_id:
            cfg.calendars[cfg.default_calendar] = {"id": fam_id, "name": "Family"}
    return cfg
