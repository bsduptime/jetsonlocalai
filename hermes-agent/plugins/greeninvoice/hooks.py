"""greeninvoice pre_tool_call hook — confirm the numbers before writing an expense.

Gates `gi_create_expense` (the step that writes model-supplied amount/supplier/date to
David's real Morning books) via Hermes' human approval prompt in Telegram. No vision model.
See confirmgate.py for the reasoning and the allowlist-grain subtlety.

CRITICAL failure semantics (verified against hermes source):
  - If this hook RAISES, Hermes catches it and PROCEEDS with the tool call ungated
    (hermes_cli/plugins.py:1892-1927, model_tools.py:1181-1199). A raise is therefore a
    silent bypass of a money-writing gate. So this hook must never raise.
  - Returning {"action":"approve", "message":...} escalates to the human gate. Returning
    {"action":"block", "message":...} vetoes outright and the message becomes the tool
    result the model sees.
  - Therefore, on ANY internal error we return BLOCK (not approve): if we cannot render the
    numbers for David to check, we must not let the write proceed on trust. Fail closed.
"""

from __future__ import annotations

import logging

from . import confirmgate as cg

log = logging.getLogger("hermes.plugins.greeninvoice.hooks")

GATED_TOOLS = {"gi_create_expense"}

_announced = False


def _announce_once() -> None:
    # Prove the gate is live from a hook CALL, not from registration: plugin registration
    # runs before Hermes attaches its logging handlers, so anything logged there is
    # swallowed (learned the hard way with the previous gate).
    global _announced
    if _announced:
        return
    _announced = True
    log.warning("GI_EXPENSE_CONFIRM live gate=%s enabled=%s",
                "gi_create_expense", cg.ENABLED)


def pre_tool_call(tool_name: str = "", args=None, **_kw):
    """Confirm the expense payload with David before gi_create_expense writes it."""
    if not cg.ENABLED or tool_name not in GATED_TOOLS:
        return None
    try:
        _announce_once()   # inside the try: a raise here would be swallowed → ungated write
        payload = args if isinstance(args, dict) else {}
        message = cg.summary(payload)
        key = cg.payload_key(payload)
        log.warning("GI_EXPENSE_CONFIRM asking tool=%s key=%s", tool_name, key)
        # Explicit rule_key = payload hash. If omitted, Hermes substitutes the bare tool
        # name and one [a]lways would blanket-approve every future create — see confirmgate.
        return {"action": "approve", "message": message, "rule_key": key}
    except Exception:
        # Never raise (a raise = silent ungated write). Never approve on incomplete info
        # for a money write. Block with a generic, non-echoing message.
        log.exception("GI_EXPENSE_CONFIRM failed to render — blocking the write")
        return {"action": "block",
                "message": ("BLOCKED: could not render the expense confirmation safely. "
                            "No expense was created. Retry, or check the details manually.")}
