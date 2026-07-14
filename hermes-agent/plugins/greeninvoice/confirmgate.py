"""confirmgate — human confirmation of the numbers before an expense is written.

The single real risk in the receipt→expense flow is that a WRONG NUMBER lands in David's
real Morning books. `gi_create_expense` takes the amount, supplier, VAT, date and document
number as MODEL-supplied arguments (schemas.py), and the broker posts them straight to
Morning (validate.build_expense) — Morning's OCR only produced a *draft*; nothing binds the
created expense to it. So whatever Elena read (via tesseract) and typed becomes the ledger
entry. A misread is a wrong Open expense.

So we intercept `gi_create_expense` and make David confirm the exact payload in Telegram
before it is written. There is no vision model here — Elena does the reading, David checks
the numbers, the broker writes only on his yes.

Why this is enough, and why we don't gate the rest:
  - upload_expense_file makes a reversible OCR draft with no numbers → not gated.
  - close_expense (report to tax, irreversible) is already confirm-gated in the broker.
  - delete_expense removes an Open row (reversible direction) → not gated.
  - create_supplier writes a supplier record, but a supplier is INERT until attached to an
    expense — and that expense is gated here, where David sees the supplier name next to the
    amount. So a stray supplier changes nothing on the books on its own → not gated.

THE ALLOWLIST-GRAIN SUBTLETY (this is the crux; get it wrong and the gate leaks):
Hermes' approval prompt offers [o]nce/[s]ession/[a]lways. The `[a]lways` grain is keyed by
`rule_key`. If we OMIT rule_key, the resolver substitutes the bare tool name
(`hermes_cli/plugins.py:2264  rule_key=details.rule_key or tool_name`), so one "always"
would blanket-approve EVERY future create_expense — the exact failure this gate exists to
prevent. So we SET an explicit rule_key = a hash of the canonical payload. Then "always"
only ever whitelists that one exact supplier+amount+date+number, which essentially never
recurs (and if it does, it's a duplicate). Distinct expenses always get a fresh prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

# Operator escape hatch — NOT model-reachable (env only). Default on.
ENABLED = os.environ.get("GI_EXPENSE_CONFIRM", "1") != "0"

_DOC_TYPES = {10: "invoice", 20: "receipt", 30: "invoice+receipt", 40: "other"}
_PAY_TYPES = {-1: "unpaid", 0: "deduction-at-source", 1: "cash", 2: "cheque", 3: "card",
              4: "transfer", 5: "paypal", 10: "payment-app", 11: "other"}

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
# Also neutralise Telegram/Markdown formatting chars: the values are David's own data, but
# if the gateway renders the prompt as Markdown a stray * _ ` [ could mangle it. Cheap
# insurance; these fields never legitimately contain markup.
_MD = re.compile(r"[*_`\[\]()~>#+=|{}]")


def _clean(s, cap: int = 80, strip_md: bool = False) -> str:
    """Cap length + strip control chars before display in the HUMAN prompt.

    `strip_md` also removes Markdown formatting chars — use it ONLY for free-text fields
    (supplier name, description) where markup could mangle rendering and is never
    legitimate. Do NOT use it for structured identifiers (supplier id, doc number): those
    can legitimately contain `_`/`-`, and mangling them would defeat the whole point of
    showing the id (David must see the EXACT id that gets attached)."""
    if s is None:
        return ""
    s = _CTRL.sub(" ", str(s))
    if strip_md:
        s = _MD.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:cap]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _supplier_line(args) -> str:
    """Render the supplier as David will see it. Crucially, if the payload references a
    supplier by ID, SHOW that id — the broker attaches the expense by id (validate.py), so a
    prompt that showed only a model-supplied `name` could display 'Rami Levi' while a
    different supplier id is what actually gets attached. Surfacing the id closes that
    display-vs-reality gap."""
    sup = args.get("supplier")
    if not isinstance(sup, dict):
        return _clean(sup, 60) if sup else "?"
    name = _clean(sup.get("name") or sup.get("taxId") or "?", 60, strip_md=True)
    sid = sup.get("id")
    if sid:
        return f"{name}  (attaches to existing supplier id {_clean(sid, 40)})"
    return name


def summary(args: dict) -> str:
    """The human-facing confirmation David sees in Telegram. Enums + numbers + cleaned
    strings only. Never raises — callers depend on that (see hooks.pre_tool_call)."""
    cur = _clean(args.get("currency") or "ILS", 8)
    amount = _num(args.get("amount"))
    vat = _num(args.get("vat"))
    lines = ["Create this expense in your REAL Morning books?",
             f"  Supplier: {_supplier_line(args)}"]
    lines.append(f"  Amount:   {amount:.2f} {cur}" if amount is not None
                 else "  Amount:   (missing!)")
    if vat is not None:
        lines.append(f"  VAT:      {vat:.2f} {cur}")
    if args.get("date"):
        lines.append(f"  Date:     {_clean(args.get('date'), 16)}")
    if args.get("number"):
        lines.append(f"  Doc no.:  {_clean(args.get('number'), 40)}")
    dt = args.get("documentType")
    pt = args.get("paymentType")
    tags = []
    if dt in _DOC_TYPES:
        tags.append(_DOC_TYPES[dt])
    if pt in _PAY_TYPES:
        tags.append(f"paid: {_PAY_TYPES[pt]}")
    if tags:
        lines.append("  (" + ", ".join(tags) + ")")
    lines.append("Approve = created OPEN (not yet reported to tax; you review monthly).")
    return "\n".join(lines)


def payload_key(args: dict) -> str:
    """Deterministic per-payload rule_key so an [a]lways approval is scoped to THIS exact
    expense, not to all future creates.

    We hash the WHOLE create payload, not a hand-picked subset. The failure we must avoid is
    two MATERIALLY DIFFERENT expenses sharing a key (then 'always' on one auto-approves the
    other — a leak). Hashing every field is the only way to guarantee that: any change to
    anything the broker writes (amount, vat, vatType, date, reportingDate, classification,
    paymentType, currencyRate, ...) yields a new key and a fresh prompt. Erring toward MORE
    prompts is harmless; erring toward fewer is the leak. So this is deliberately maximal.

    Determinism: JSON with sorted keys. args holds only JSON-native types (it came off the
    wire as tool args), so this is stable across calls for an identical payload."""
    try:
        canon = json.dumps(args, sort_keys=True, ensure_ascii=True, default=str)
    except Exception:
        canon = repr(sorted((str(k), str(v)) for k, v in (args or {}).items()))
    return "gi_create_expense:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
