"""Handler contract: always a JSON string, never a raise, confirm gate."""

from __future__ import annotations

import json


def _call(fn, args):
    out = fn(args)
    assert isinstance(out, str)
    return json.loads(out)


def test_add_list_remove_flow(handler):
    out = _call(handler.shopping_add,
                {"items": [{"item": "milk"}, {"item": "dog food", "qty": "2"}]})
    assert out["ok"] is True
    assert out["added"] == ["milk", "dog food"]

    out = _call(handler.shopping_list, {})
    assert out["ok"] is True
    assert [i["item"] for i in out["items"]] == ["milk", "dog food"]

    out = _call(handler.shopping_remove, {"items": ["milk"]})
    assert out["ok"] is True
    assert out["removed"] == ["milk"]


def test_add_invalid_items(handler):
    for bad in ({}, {"items": []}, {"items": "milk"}, {"items": [42]}):
        out = _call(handler.shopping_add, bad)
        assert out["ok"] is False
        assert out["error"] in ("invalid_input", "state", "internal")


def test_remove_requires_string_items(handler):
    out = _call(handler.shopping_remove, {"items": [{"item": "milk"}]})
    assert out["ok"] is False
    assert out["reason"] == "invalid_field_type"


def test_clear_requires_confirm(handler):
    _call(handler.shopping_add, {"items": [{"item": "milk"}]})
    out = _call(handler.shopping_clear, {})
    assert out["ok"] is False
    assert out["reason"] == "confirmation_required"
    # list untouched
    assert len(_call(handler.shopping_list, {})["items"]) == 1

    out = _call(handler.shopping_clear, {"confirm": True})
    assert out["ok"] is True
    assert out["cleared"] == 1


def test_named_list_passthrough(handler):
    _call(handler.shopping_add, {"items": [{"item": "plasters"}], "list": "pharmacy"})
    out = _call(handler.shopping_list, {"list": "pharmacy"})
    assert [i["item"] for i in out["items"]] == ["plasters"]
    assert _call(handler.shopping_list, {})["items"] == []


def test_bad_list_name_is_structured_error(handler):
    out = _call(handler.shopping_list, {"list": "../etc"})
    assert out["ok"] is False
    assert out["error"] == "state"
    assert out["reason"] == "invalid_list_name"


def test_hebrew_stays_hebrew_in_json(handler):
    _call(handler.shopping_add, {"items": [{"item": "חלב"}]})
    raw = handler.shopping_list({})
    assert "חלב" in raw  # ensure_ascii=False — no ח escapes for the model
