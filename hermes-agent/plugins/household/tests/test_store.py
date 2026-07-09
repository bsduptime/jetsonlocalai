"""Deterministic store behavior: dedup, persistence, atomicity, containment."""

from __future__ import annotations

import json

import pytest


def test_add_and_read_roundtrip(store):
    out = store.add("shopping", [{"item": "milk"}, {"item": "dog food", "qty": "2"}])
    assert out["added"] == ["milk", "dog food"]
    read = store.read("shopping")
    assert [i["item"] for i in read["items"]] == ["milk", "dog food"]
    assert read["items"][1]["qty"] == "2"
    # the internal dedup key never leaks to the model
    assert all("key" not in i for i in read["items"])


def test_dedup_casefold_and_whitespace(store):
    store.add("shopping", [{"item": "Dog Food"}])
    out = store.add("shopping", [{"item": "  dog   food "}])
    assert out["added"] == []
    assert out["already_present"] == ["Dog Food"]
    assert len(out["items"]) == 1


def test_duplicate_add_with_qty_updates_existing(store):
    store.add("shopping", [{"item": "milk"}])
    out = store.add("shopping", [{"item": "Milk", "qty": "2"}])
    assert out["already_present"] == ["milk"]
    assert store.read("shopping")["items"][0]["qty"] == "2"


def test_hebrew_items(store):
    store.add("shopping", [{"item": "חלב"}, {"item": "אוכל לכלב", "qty": "שק גדול"}])
    out = store.add("shopping", [{"item": "חלב "}])
    assert out["already_present"] == ["חלב"]
    assert len(store.read("shopping")["items"]) == 2


def test_remove_exact_and_not_found(store):
    store.add("shopping", [{"item": "milk"}, {"item": "eggs"}])
    out = store.remove("shopping", ["MILK", "batteries"])
    assert out["removed"] == ["milk"]
    assert out["not_found"] == ["batteries"]
    assert [i["item"] for i in out["items"]] == ["eggs"]


def test_clear(store):
    store.add("shopping", [{"item": "milk"}, {"item": "eggs"}])
    out = store.clear("shopping")
    assert out["cleared"] == 2
    assert store.read("shopping")["items"] == []


def test_persists_across_module_state(store, state_dir):
    store.add("shopping", [{"item": "milk"}])
    raw = json.loads((state_dir / "shopping.json").read_text(encoding="utf-8"))
    assert raw["v"] == 1
    assert raw["items"][0]["item"] == "milk"
    assert raw["items"][0]["added_at"]


def test_named_lists_are_separate(store):
    store.add("shopping", [{"item": "milk"}])
    store.add("pharmacy", [{"item": "plasters"}])
    assert [i["item"] for i in store.read("shopping")["items"]] == ["milk"]
    assert [i["item"] for i in store.read("pharmacy")["items"]] == ["plasters"]


def test_list_name_traversal_rejected(store):
    for bad in ("../etc", "a/b", "a\\b", "..", "x.json", "", "A B"):
        with pytest.raises(store.StoreError) as e:
            store.read(bad)
        assert e.value.reason == "invalid_list_name"


def test_list_name_casefolds(store):
    store.add("Shopping", [{"item": "milk"}])
    assert [i["item"] for i in store.read("SHOPPING")["items"]] == ["milk"]


def test_corrupt_state_surfaces_cleanly(store, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "shopping.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(store.StoreError) as e:
        store.read("shopping")
    assert e.value.reason == "state_corrupt"


def test_no_tmp_file_left_behind(store, state_dir):
    store.add("shopping", [{"item": "milk"}])
    assert [p.name for p in state_dir.iterdir()] == ["shopping.json"]


def test_item_limits(store):
    with pytest.raises(store.StoreError) as e:
        store.add("shopping", [{"item": "x" * 200}])
    assert e.value.reason == "value_too_long"
    with pytest.raises(store.StoreError) as e:
        store.add("shopping", [{"item": "   "}])
    assert e.value.reason == "empty_value"


def test_list_full(store):
    store.add("shopping", [{"item": f"item {i}"} for i in range(store.MAX_ITEMS)])
    with pytest.raises(store.StoreError) as e:
        store.add("shopping", [{"item": "one too many"}])
    assert e.value.reason == "list_full"
