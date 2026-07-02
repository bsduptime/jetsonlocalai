"""JSON schemas exposed to the LLM via ctx.register_tool().

The design mirrors the mailer plugin: the model does the *language* work
(resolve "tomorrow at 11am" to an absolute datetime; decide who is a real
participant vs. who is only being kept informed) and the relay does the
*policy + side-effect* work (contact allowlist, rate limit, actually
writing the event / sending invites). Keeping datetime resolution in the
model — which knows today's date — keeps the relay free of fragile natural-
language date parsing.
"""

from __future__ import annotations

LIST_CONTACTS = {
    "name": "list_contacts",
    "description": (
        "List the known calendar contacts. Returns an array `contacts`, each "
        "with `email`, `name`, `aliases` (lowercase things the user might say, "
        "e.g. \"elon\", \"lihi\", \"my wife\", \"me\"), `default_role` (a hint: "
        "\"participant\" or \"fyi\"), and `note`. Call this FIRST whenever the "
        "user names people instead of giving email addresses (\"invite Elon, "
        "let Lihi know\"): match each name/alias to a contact and pass that "
        "contact's exact details to create_event. If a named person matches no "
        "contact, still pass them in create_event's attendees with the raw name "
        "as `ref` — the relay reports them as unresolved rather than guessing an "
        "address. Takes no arguments."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


CREATE_EVENT = {
    "name": "create_event",
    "description": (
        "Create a calendar event on a shared calendar and, optionally, invite "
        "or inform people. IMPORTANT distinction the user cares about:\n"
        "  - role=\"participant\": the person is actually attending. In real "
        "mode they receive a calendar INVITE they can RSVP to.\n"
        "  - role=\"fyi\": the person is NOT attending but should be told it's "
        "happening (e.g. \"she won't come but she should know\"). They are NOT "
        "added to the invite; they get a heads-up notification only.\n"
        "Resolve relative dates using today's date and pass `start` as an "
        "absolute local ISO-8601 datetime. By DEFAULT the relay is in dry-run "
        "mode: it returns exactly what it WOULD create and who it WOULD "
        "invite/inform, without writing to any real calendar or sending "
        "anything. Returns ok=true with a structured plan plus a human-readable "
        "`summary`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Event title, e.g. \"Climbing with Elon\".",
                "maxLength": 300,
            },
            "start": {
                "type": "string",
                "description": (
                    "Event start as an absolute local ISO-8601 datetime, e.g. "
                    "\"2026-07-04T11:00\". Resolve relative expressions like "
                    "\"tomorrow at 11am\" yourself using today's date before "
                    "calling. Do not pass a timezone offset — the relay applies "
                    "the configured local timezone."
                ),
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Event length in minutes. Default 60.",
                "minimum": 5,
                "maximum": 1440,
                "default": 60,
            },
            "end": {
                "type": "string",
                "description": (
                    "Optional explicit end as local ISO-8601 datetime. If given, "
                    "it overrides duration_minutes."
                ),
            },
            "calendar": {
                "type": "string",
                "description": (
                    "Which calendar to put it on, by label (e.g. \"family\", "
                    "\"work\"). Defaults to the configured default calendar."
                ),
                "default": "family",
            },
            "attendees": {
                "type": "array",
                "description": (
                    "People to invite or inform. Omit for a private event with "
                    "no notifications."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": (
                                "What the user called the person (\"elon\", "
                                "\"Lihi\", or a raw email). The relay resolves it "
                                "against the contacts list."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "enum": ["participant", "fyi"],
                            "description": (
                                "\"participant\" = attending, gets an invite. "
                                "\"fyi\" = not attending, just informed."
                            ),
                        },
                        "notify": {
                            "type": "boolean",
                            "description": (
                                "Whether to send this person a notification. "
                                "Default true."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["ref", "role"],
                    "additionalProperties": False,
                },
                "default": [],
            },
            "location": {
                "type": "string",
                "description": "Optional location.",
            },
            "notes": {
                "type": "string",
                "description": "Optional description/notes for the event body.",
            },
        },
        "required": ["title", "start"],
    },
}
