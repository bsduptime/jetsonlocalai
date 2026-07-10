"""JSON schemas exposed to the LLM via ctx.register_tool().

Same division of labor as the familycal plugin: the model does the
*language* work — deciding that a message is actually a purchase request,
and turning "the thing Yannai puts on his cereal is finished" into an item
name — and the plugin does the *state* work (exact, deduped, persistent).
The capture policy lives HERE, in the descriptions, because tool
descriptions are what the model sees at decision time.
"""

from __future__ import annotations

_CAPTURE_POLICY = (
    "CAPTURE POLICY (matters in group chats): add something only when a "
    "message clearly asks for it to be bought or restocked — \"we're out of "
    "milk\", \"get dog food\", \"need batteries for the remote\". Do NOT add "
    "items from casual mentions of products (\"the milk in this cafe is "
    "great\"), from questions, or from messages about food that was eaten. "
    "If a message is ambiguous, ask briefly instead of adding. Use the item "
    "name people would say at the store, in the language the family used "
    "(Hebrew stays Hebrew), without filler words."
)

_LIST_PARAM = {
    "type": "string",
    "description": (
        "Which list. Omit for the default \"shopping\" list. Use another "
        "short name only when the family explicitly keeps a separate list "
        "(e.g. \"pharmacy\", \"hardware\")."
    ),
}

SHOPPING_ADD = {
    "name": "shopping_add",
    "description": (
        "Add item(s) to the family's persistent shopping list. The list is "
        "shared across every chat this agent is in and survives between "
        "conversations — anyone can add in the group and anyone can read it "
        "at the store. Adding an item that is already on the list is safe: "
        "it is reported as already_present (and a newly given quantity "
        "updates the existing entry), never duplicated. Returns the full "
        "updated list; confirm briefly what was added. " + _CAPTURE_POLICY
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "description": "The item(s) to add.",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {
                            "type": "string",
                            "description": "Store-name of the product, e.g. \"dog food\", \"חלב\".",
                        },
                        "qty": {
                            "type": "string",
                            "description": "Optional amount, e.g. \"2\", \"1kg\", \"large pack\".",
                        },
                        "added_by": {
                            "type": "string",
                            "description": "Optional: who asked for it, if known from the chat.",
                        },
                    },
                    "required": ["item"],
                    "additionalProperties": False,
                },
            },
            "list": _LIST_PARAM,
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

SHOPPING_LIST = {
    "name": "shopping_list",
    "description": (
        "Read a household list back, exactly as stored (items, optional "
        "quantities, who added them, when, and whether already checked off "
        "as bought — done=true). When presenting, show the OPEN items as "
        "the list and mention checked ones briefly (\"already picked up: "
        "…\"). Call this whenever someone asks what's on the list / what to "
        "buy, and ALWAYS call it before shopping_check or shopping_remove "
        "so you pass item names exactly as stored. Takes no required "
        "arguments."
    ),
    "parameters": {
        "type": "object",
        "properties": {"list": _LIST_PARAM},
        "additionalProperties": False,
    },
}

SHOPPING_CHECK = {
    "name": "shopping_check",
    "description": (
        "Check item(s) off a household list as BOUGHT / picked up — \"got "
        "the milk\", \"picked up the dog food\", or someone ticking things "
        "off mid-shop. The item stays on the list marked done (visible as "
        "checked, reversible), it is NOT deleted — use shopping_remove only "
        "when something is no longer needed at all. Pass done=false to "
        "un-check a mistake (\"actually I didn't get the milk\"). Matching "
        "is exact on the stored item name (case/whitespace don't matter): "
        "read the list first and pass names as shown there. Items not on "
        "the list come back in not_found — tell the user rather than "
        "guessing a different item."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Item names to (un)check, as stored on the list.",
            },
            "done": {
                "type": "boolean",
                "description": "Omit or true = bought. false = un-check a mistake.",
            },
            "list": _LIST_PARAM,
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

SHOPPING_REMOVE = {
    "name": "shopping_remove",
    "description": (
        "Delete item(s) from a household list because they are NO LONGER "
        "NEEDED — \"forget the batteries\", \"we don't need milk after all\". "
        "For things that were BOUGHT, use shopping_check instead (keeps a "
        "visible, reversible record of the trip). Matching is exact on the "
        "stored item name (case/whitespace don't matter): read the list "
        "first and pass names as shown there. Items that aren't on the list "
        "come back in not_found — tell the user rather than guessing a "
        "different item."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Item names to remove, as stored on the list.",
            },
            "list": _LIST_PARAM,
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

SHOPPING_CLEAR = {
    "name": "shopping_clear",
    "description": (
        "Tidy a household list in one go. scope=\"checked\" (the usual case, "
        "after a shopping trip: \"clear what we bought\") deletes only the "
        "checked-off items and needs no confirmation. scope=\"all\" wipes the "
        "entire list including open items — that discards real requests, so "
        "it requires confirm=true: ask the user first unless they clearly "
        "said to clear everything. For single items use shopping_check / "
        "shopping_remove."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["checked", "all"],
                "description": "\"checked\" = drop bought items only (default). \"all\" = wipe the list.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Required true for scope=\"all\" only, after the user clearly asked.",
            },
            "list": _LIST_PARAM,
        },
        "additionalProperties": False,
    },
}
