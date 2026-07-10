"""household-board — a tappable Telegram checklist over the household store.

Why a separate bot: Hermes' Telegram gateway renders inline keyboards only
for its own features (clarify/approvals) and silently drops unknown
callback prefixes — there is no plugin hook. Rather than fork the adapter,
this tiny service runs beside Elena with its OWN bot token: it posts one
pinned board message per chat, every item is a button (tap = bought,
tap again on ✅ = un-bought), and it shares the plugin's JSON store — so
"Elena, add milk" shows up on the board, and a tap shows up when anyone
asks Elena what's left. Cross-process safety comes from the store's file
lock.

Privacy: the bot works with Telegram privacy mode ON — it receives only
/list commands and button taps, never the family's messages. Access is
gated by an explicit chat-id allowlist (fail closed).

Stdlib only (urllib), same as the relays — nothing to pip-install on the
Jetson or the Windows box.

Env:
  HOUSEHOLD_BOT_TOKEN        (required) BotFather token for the board bot
  HOUSEHOLD_ALLOWED_CHAT_IDS (required) comma-separated Telegram chat ids
  HOUSEHOLD_STATE_DIR        shared list store (must match the plugin's)
  HOUSEHOLD_BOARD_STATE_DIR  board registry (default: <state>/board)
  HOUSEHOLD_POLL_TIMEOUT     getUpdates long-poll seconds (default 10)
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("household_board")

MAX_BUTTON_ITEMS = 45  # inline keyboards get unwieldy past this; overflow noted in text


# --- shared store (the plugin's _store.py, loaded by path) -----------------

def load_store():
    explicit = os.environ.get("HOUSEHOLD_STORE_PATH")
    path = (Path(explicit) if explicit
            else Path(__file__).resolve().parents[1] / "plugins" / "household" / "_store.py")
    spec = importlib.util.spec_from_file_location("household_store", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load store module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["household_store"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- rendering (pure — unit-tested) ----------------------------------------

def item_hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def render_board(list_name: str, items: list[dict]) -> tuple[str, dict]:
    """Returns (text, reply_markup). One button per item: tap toggles bought."""
    open_items = [it for it in items if not it.get("done")]
    done_items = [it for it in items if it.get("done")]
    stamp = time.strftime("%H:%M")
    text = (f"🛒 {list_name} — {len(open_items)} to get, "
            f"{len(done_items)} in the cart  ({stamp})")
    if not items:
        text = f"🛒 {list_name} — empty  ({stamp})\nTell Elena when you need something."

    rows = []
    shown = 0
    for it in open_items + done_items:
        if shown >= MAX_BUTTON_ITEMS:
            text += f"\n…and {len(items) - shown} more (ask Elena for the full list)"
            break
        label = it["item"]
        if it.get("qty"):
            label += f" × {it['qty']}"
        mark = "✅" if it.get("done") else "⬜"
        # normalize_key(item) reproduces the store key for stored display text
        h = item_hash(_norm(it["item"]))
        rows.append([{"text": f"{mark} {label}",
                      "callback_data": f"it:{list_name}:{h}"}])
        shown += 1
    footer = []
    if done_items:
        footer.append({"text": "🧹 clear bought", "callback_data": f"cb:{list_name}"})
    footer.append({"text": "🔄", "callback_data": f"rf:{list_name}"})
    rows.append(footer)
    return text, {"inline_keyboard": rows}


def _norm(item: str) -> str:
    import re
    return re.sub(r"\s+", " ", item).strip().casefold()


def parse_callback(data: str) -> tuple[str, str, str] | None:
    """'it:<list>:<h>' -> ("it", list, h); 'cb:<list>'/'rf:<list>' -> (op, list, "")."""
    parts = data.split(":", 2)
    if len(parts) >= 2 and parts[0] in ("it", "cb", "rf"):
        return parts[0], parts[1], parts[2] if len(parts) == 3 else ""
    return None


# --- Telegram Bot API (thin, injectable) ------------------------------------

class Api:
    def __init__(self, token: str, timeout: int = 65):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def call(self, method: str, **params) -> dict:
        req = urllib.request.Request(
            f"{self.base}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "description": f"http_{e.code}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"ok": False, "description": f"network:{type(e).__name__}"}


# --- the service -------------------------------------------------------------

class Board:
    def __init__(self, api, store, allowed_chat_ids: set[str],
                 registry_path: Path):
        self.api = api
        self.store = store
        self.allowed = allowed_chat_ids
        self.registry_path = registry_path
        self.registry = self._load_registry()   # chat_id -> {message_id, list}
        self._mtimes: dict[str, float] = {}

    # registry: which chat has a board message for which list
    def _load_registry(self) -> dict:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_registry(self) -> None:
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.registry_path.with_name(self.registry_path.name + ".tmp")
            tmp.write_text(json.dumps(self.registry), encoding="utf-8")
            os.replace(tmp, self.registry_path)
        except OSError as e:
            log.warning("could not save board registry: %s", e)

    # --- board lifecycle ---
    def post_board(self, chat_id: str, list_name: str) -> None:
        items = self.store.read(list_name)["items"]
        text, markup = render_board(list_name, items)
        resp = self.api.call("sendMessage", chat_id=chat_id, text=text,
                             reply_markup=markup)
        if not resp.get("ok"):
            log.warning("sendMessage failed for %s: %s", chat_id,
                        resp.get("description"))
            return
        message_id = resp["result"]["message_id"]
        old = self.registry.get(chat_id)
        self.registry[chat_id] = {"message_id": message_id, "list": list_name}
        self._save_registry()
        # best-effort: pin the new board, unpin the previous one
        self.api.call("pinChatMessage", chat_id=chat_id, message_id=message_id,
                      disable_notification=True)
        if old and old.get("message_id"):
            self.api.call("deleteMessage", chat_id=chat_id,
                          message_id=old["message_id"])

    def refresh_board(self, chat_id: str) -> None:
        reg = self.registry.get(chat_id)
        if not reg:
            return
        list_name = reg["list"]
        items = self.store.read(list_name)["items"]
        text, markup = render_board(list_name, items)
        resp = self.api.call("editMessageText", chat_id=chat_id,
                             message_id=reg["message_id"], text=text,
                             reply_markup=markup)
        if not resp.get("ok"):
            desc = (resp.get("description") or "").lower()
            if "not modified" in desc:
                return
            # board message was deleted / too old to edit — post a fresh one
            log.info("edit failed (%s), re-posting board in %s", desc, chat_id)
            self.post_board(chat_id, list_name)

    def refresh_all(self) -> None:
        for chat_id in list(self.registry):
            self.refresh_board(chat_id)

    # --- update handling ---
    def handle_message(self, msg: dict) -> None:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        # Telegram delivers /commands even with privacy mode ON — so /add
        # works while the bot stays deaf to the actual conversation.
        cmd = text.split()[0].split("@")[0] if text.startswith("/") else ""
        if cmd not in ("/list", "/add"):
            return
        if chat_id not in self.allowed:
            log.warning("ignoring %s from non-allowlisted chat %s — add it "
                        "to HOUSEHOLD_ALLOWED_CHAT_IDS to enable it", cmd, chat_id)
            return

        if cmd == "/add":
            self._handle_add(chat_id, text)
            return

        parts = text.split()
        list_name = parts[1].casefold() if len(parts) > 1 else \
            (self.registry.get(chat_id) or {}).get("list", "shopping")
        try:
            self.store.read(list_name)   # validates the name
        except self.store.StoreError:
            self.api.call("sendMessage", chat_id=chat_id,
                          text=f"'{parts[1]}' is not a valid list name")
            return
        self.post_board(chat_id, list_name)

    def _handle_add(self, chat_id: str, text: str) -> None:
        """/add milk, dog food, 2 eggs — comma-separated items onto this
        chat's board list (default shopping). Deterministic: no language
        smarts here; nuanced capture stays with the agent."""
        payload = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not payload:
            self.api.call("sendMessage", chat_id=chat_id,
                          text="usage: /add milk, dog food, batteries")
            return
        list_name = (self.registry.get(chat_id) or {}).get("list", "shopping")
        entries = [{"item": p.strip()} for p in payload.split(",") if p.strip()]
        try:
            out = self.store.add(list_name, entries)
        except self.store.StoreError as e:
            self.api.call("sendMessage", chat_id=chat_id,
                          text=f"could not add: {e.reason}")
            return
        bits = []
        if out["added"]:
            bits.append("added: " + ", ".join(out["added"]))
        if out["already_present"]:
            bits.append("already on it: " + ", ".join(out["already_present"]))
        self.api.call("sendMessage", chat_id=chat_id,
                      text="  ·  ".join(bits) or "nothing to add")
        if self.registry.get(chat_id):
            self.refresh_board(chat_id)
        else:
            self.post_board(chat_id, list_name)

    def handle_callback(self, cq: dict) -> None:
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        parsed = parse_callback(cq.get("data") or "")

        def answer(text=""):
            if cq_id:
                self.api.call("answerCallbackQuery", callback_query_id=cq_id,
                              text=text)

        if chat_id not in self.allowed or parsed is None:
            answer()
            return
        op, list_name, arg = parsed
        try:
            if op == "it":
                items = self.store.read(list_name)["items"]
                match = next((it for it in items
                              if item_hash(_norm(it["item"])) == arg), None)
                if match is None:
                    answer("that item is gone — refreshing")
                else:
                    now_done = not match.get("done")
                    self.store.check(list_name, [match["item"]], done=now_done)
                    answer(f"✅ {match['item']}" if now_done
                           else f"⬜ {match['item']} is back on the list")
            elif op == "cb":
                out = self.store.clear(list_name, "checked")
                answer(f"cleared {out['cleared']} bought item(s)")
            else:  # rf
                answer("refreshed")
        except self.store.StoreError as e:
            answer(f"error: {e.reason}")
        self.refresh_board(chat_id)

    # --- store watching (Elena's writes appear without a tap) ---
    def poll_store_changes(self) -> None:
        changed = False
        for chat_id, reg in self.registry.items():
            path = self.store.state_dir() / f"{reg['list']}.json"
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(reg["list"]) not in (None, mtime):
                changed = True
            self._mtimes[reg["list"]] = mtime
        if changed:
            self.refresh_all()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("HOUSEHOLD_BOARD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = (os.environ.get("HOUSEHOLD_BOT_TOKEN") or "").strip()
    allowed = {c.strip() for c in
               (os.environ.get("HOUSEHOLD_ALLOWED_CHAT_IDS") or "").split(",")
               if c.strip()}
    if not token:
        log.error("HOUSEHOLD_BOT_TOKEN unset")
        return 1
    if not allowed:
        # Discovery mode — still fail-closed (an empty allowlist serves and
        # reveals nothing), but keep running so /list attempts get their chat
        # id logged. That log line IS the setup flow for finding the ids.
        log.warning("HOUSEHOLD_ALLOWED_CHAT_IDS empty — discovery mode: "
                    "serving NO chats; send /list in the target chat and "
                    "copy the id this log prints")

    store = load_store()
    registry_path = Path(
        os.environ.get("HOUSEHOLD_BOARD_STATE_DIR")
        or (store.state_dir() / "board")
    ) / "boards.json"
    poll_timeout = int(os.environ.get("HOUSEHOLD_POLL_TIMEOUT") or "10")

    api = Api(token, timeout=poll_timeout + 15)
    board = Board(api, store, allowed, registry_path)
    log.info("household-board up: %d allowed chat(s), state=%s",
             len(allowed), store.state_dir())

    offset = 0
    while True:
        resp = api.call("getUpdates", offset=offset, timeout=poll_timeout,
                        allowed_updates=["message", "callback_query"])
        if not resp.get("ok"):
            log.warning("getUpdates failed: %s", resp.get("description"))
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = max(offset, upd["update_id"] + 1)
            try:
                if "callback_query" in upd:
                    board.handle_callback(upd["callback_query"])
                elif "message" in upd:
                    board.handle_message(upd["message"])
            except Exception:
                log.exception("error handling update %s", upd.get("update_id"))
        board.poll_store_changes()
    return 0


if __name__ == "__main__":
    sys.exit(main())
