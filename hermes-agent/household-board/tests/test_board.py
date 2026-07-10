"""Board logic against a fake Telegram API and the real shared store."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BOARD_DIR = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


board_mod = _load("board_under_test", BOARD_DIR / "board.py")


class FakeApi:
    def __init__(self):
        self.calls = []
        self.fail_edit_with = None
        self._next_message_id = 100

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self._next_message_id += 1
            return {"ok": True, "result": {"message_id": self._next_message_id}}
        if method == "editMessageText" and self.fail_edit_with:
            return {"ok": False, "description": self.fail_edit_with}
        return {"ok": True, "result": {}}

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOUSEHOLD_STATE_DIR", str(tmp_path / "household"))
    return board_mod.load_store()


@pytest.fixture
def board(store, tmp_path):
    api = FakeApi()
    b = board_mod.Board(api, store, allowed_chat_ids={"111"},
                        registry_path=tmp_path / "board" / "boards.json")
    return b, api


def test_render_marks_and_footer():
    items = [{"item": "milk", "qty": "2"}, {"item": "bread", "done": True}]
    text, markup = board_mod.render_board("shopping", items)
    rows = markup["inline_keyboard"]
    assert "1 to get" in text and "1 in the cart" in text
    assert rows[0][0]["text"] == "⬜ milk × 2"
    assert rows[1][0]["text"] == "✅ bread"
    footer = [b["text"] for b in rows[-1]]
    assert "🧹 clear bought" in footer
    # callback data stays under Telegram's 64-byte cap
    assert all(len(b["callback_data"].encode()) <= 64
               for row in rows for b in row)


def test_render_empty_list():
    text, markup = board_mod.render_board("shopping", [])
    assert "empty" in text
    assert markup["inline_keyboard"][-1][0]["text"] == "🔄"


def test_parse_callback():
    assert board_mod.parse_callback("it:shopping:abc123") == ("it", "shopping", "abc123")
    assert board_mod.parse_callback("cb:shopping") == ("cb", "shopping", "")
    assert board_mod.parse_callback("update_prompt:y") is None
    assert board_mod.parse_callback("") is None


def test_list_command_posts_and_pins(board, store):
    b, api = board
    store.add("shopping", [{"item": "milk"}])
    b.handle_message({"chat": {"id": 111}, "text": "/list"})
    assert len(api.of("sendMessage")) == 1
    assert len(api.of("pinChatMessage")) == 1
    assert b.registry["111"]["list"] == "shopping"


def test_non_allowlisted_chat_ignored(board, store):
    b, api = board
    b.handle_message({"chat": {"id": 999}, "text": "/list"})
    assert api.of("sendMessage") == []
    # a tap from a foreign chat answers the callback but touches nothing
    b.handle_callback({"id": "cq1", "data": "it:shopping:deadbeef00",
                       "message": {"chat": {"id": 999}}})
    assert store.read("shopping")["items"] == []


def test_tap_toggles_item_and_edits_board(board, store):
    b, api = board
    store.add("shopping", [{"item": "milk"}])
    b.handle_message({"chat": {"id": 111}, "text": "/list"})
    h = board_mod.item_hash("milk")

    b.handle_callback({"id": "cq1", "data": f"it:shopping:{h}",
                       "message": {"chat": {"id": 111}}})
    assert store.read("shopping")["items"][0]["done"] is True
    assert any("✅ milk" in p.get("text", "") for p in api.of("answerCallbackQuery"))
    assert len(api.of("editMessageText")) == 1

    # tap again -> un-bought
    b.handle_callback({"id": "cq2", "data": f"it:shopping:{h}",
                       "message": {"chat": {"id": 111}}})
    assert "done" not in store.read("shopping")["items"][0]


def test_clear_bought_button(board, store):
    b, api = board
    store.add("shopping", [{"item": "milk"}, {"item": "eggs"}])
    store.check("shopping", ["milk"])
    b.handle_message({"chat": {"id": 111}, "text": "/list"})
    b.handle_callback({"id": "cq1", "data": "cb:shopping",
                       "message": {"chat": {"id": 111}}})
    items = store.read("shopping")["items"]
    assert [i["item"] for i in items] == ["eggs"]


def test_stale_hash_answers_gracefully(board, store):
    b, api = board
    store.add("shopping", [{"item": "milk"}])
    b.handle_message({"chat": {"id": 111}, "text": "/list"})
    b.handle_callback({"id": "cq1", "data": "it:shopping:0000000000",
                       "message": {"chat": {"id": 111}}})
    assert any("gone" in p.get("text", "")
               for p in api.of("answerCallbackQuery"))
    assert store.read("shopping")["items"][0].get("done") is None


def test_edit_failure_reposts_board(board, store):
    b, api = board
    store.add("shopping", [{"item": "milk"}])
    b.handle_message({"chat": {"id": 111}, "text": "/list"})
    api.fail_edit_with = "Bad Request: message to edit not found"
    b.refresh_board("111")
    assert len(api.of("sendMessage")) == 2   # original + repost
    assert b.registry["111"]["message_id"] == 102


def test_store_change_triggers_refresh(board, store):
    b, api = board
    store.add("shopping", [{"item": "milk"}])
    b.handle_message({"chat": {"id": 111}, "text": "/list"})
    b.poll_store_changes()                       # records baseline mtime
    edits_before = len(api.of("editMessageText"))

    import os, time
    path = store.state_dir() / "shopping.json"
    store.add("shopping", [{"item": "eggs"}])
    os.utime(path, (time.time() + 5, time.time() + 5))  # force distinct mtime
    b.poll_store_changes()
    assert len(api.of("editMessageText")) > edits_before


def test_named_list_command(board, store):
    b, api = board
    store.add("pharmacy", [{"item": "plasters"}])
    b.handle_message({"chat": {"id": 111}, "text": "/list pharmacy"})
    assert b.registry["111"]["list"] == "pharmacy"
    b.handle_message({"chat": {"id": 111}, "text": "/list not/valid"})
    assert any("not a valid list name" in p.get("text", "")
               for p in api.of("sendMessage"))
