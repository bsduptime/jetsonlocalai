"""Input validation + GreenInvoice request-body builders.

Pure functions: no I/O, no network. Each raises InvalidInput (with a
stable machine token) on the first problem. The handler turns those into
error responses. Building the API body here — rather than trusting the
caller to send a ready-made GreenInvoice payload — means a prompt-injected
caller cannot smuggle arbitrary fields (e.g. flipping a document into a
type we never want issued, or injecting hundreds of line items).
"""

from __future__ import annotations

from typing import Any

from .config import ISSUABLE_DOCUMENT_TYPES
from .errors import InvalidInput

# Drafts/previews may render a wider set than we will ever *issue*: price
# quotes (10) and proforma/"deal" accounts (300) are useful to preview.
DRAFTABLE_DOCUMENT_TYPES = {10, 300} | ISSUABLE_DOCUMENT_TYPES

MAX_INCOME_ROWS = 100
MAX_STR = 500
MAX_DESC = 1000
MAX_EMAILS = 10
# Sane business bounds so a prompt-injected caller (even past the
# confirmation gate + rate limit) can't mint an absurd invoice amount.
MAX_QUANTITY = 1_000_000
MAX_UNIT_PRICE = 100_000_000
DATE_RE_FIELDS = ("date", "dueDate")


def _req_str(d: dict, key: str, *, maxlen: int = MAX_STR, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None or v == "":
        if required:
            raise InvalidInput("missing_field", key)
        return None
    if not isinstance(v, str):
        raise InvalidInput("invalid_field_type", key)
    if "\x00" in v:
        raise InvalidInput("null_byte_in_field", key)
    if len(v) > maxlen:
        raise InvalidInput("field_too_long", key)
    return v


def _num(v: Any, key: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InvalidInput("invalid_number", key)
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        raise InvalidInput("invalid_number", key)
    return float(v)


def _int_field(d: dict, key: str, *, default: int | None = None) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise InvalidInput("invalid_int", key)
    return v


def _date(d: dict, key: str, *, required: bool) -> str | None:
    v = d.get(key)
    if v is None or v == "":
        if required:
            raise InvalidInput("missing_field", key)
        return None
    if not isinstance(v, str) or len(v) != 10 or v[4] != "-" or v[7] != "-":
        raise InvalidInput("invalid_date", key)
    y, m, dd = v[:4], v[5:7], v[8:10]
    if not (y.isdigit() and m.isdigit() and dd.isdigit()):
        raise InvalidInput("invalid_date", key)
    if not (1 <= int(m) <= 12 and 1 <= int(dd) <= 31):
        raise InvalidInput("invalid_date", key)
    return v


def _currency(v: Any, key: str = "currency") -> str:
    if not isinstance(v, str) or len(v) != 3 or not v.isalpha():
        raise InvalidInput("invalid_currency", key)
    return v.upper()


def _emails(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise InvalidInput("invalid_field_type", "emails")
    if len(v) > MAX_EMAILS:
        raise InvalidInput("too_many_emails", "emails")
    out = []
    for i, e in enumerate(v):
        if not isinstance(e, str) or "@" not in e or "\x00" in e or len(e) > 254:
            raise InvalidInput("invalid_email", f"emails[{i}]")
        if "\r" in e or "\n" in e:
            raise InvalidInput("invalid_email", f"emails[{i}]")
        out.append(e)
    return out


# ---- client validation ---------------------------------------------------

def build_client_create(args: dict) -> dict:
    """Validate + build a POST /clients body."""
    if not isinstance(args, dict):
        raise InvalidInput("invalid_field_type", "client")
    name = _req_str(args, "name")
    body: dict[str, Any] = {"name": name}
    if "emails" in args and args["emails"] is not None:
        body["emails"] = _emails(args["emails"])
    for opt in ("taxId", "address", "city", "zip", "phone", "mobile", "fax", "country"):
        val = _req_str(args, opt, required=False)
        if val is not None:
            body[opt] = val
    if "country" in body and (len(body["country"]) != 2 or not body["country"].isalpha()):
        raise InvalidInput("invalid_country", "country")
    return body


def build_client_update(args: dict) -> tuple[str, dict]:
    """Validate + build (client_id, PUT /clients/{id} body)."""
    client_id = _req_str(args, "id", maxlen=64)
    body = build_client_create(args)  # same field rules; name required on update too
    return client_id, body


# ---- document validation -------------------------------------------------

def _build_income(rows: Any) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise InvalidInput("missing_field", "income")
    if len(rows) > MAX_INCOME_ROWS:
        raise InvalidInput("too_many_income_rows", "income")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InvalidInput("invalid_income_row", str(i))
        desc = _req_str(row, "description", maxlen=MAX_DESC)
        qty = _num(row.get("quantity"), f"income[{i}].quantity")
        if qty <= 0 or qty > MAX_QUANTITY:
            raise InvalidInput("invalid_quantity", f"income[{i}].quantity")
        price = _num(row.get("price"), f"income[{i}].price")
        if price < 0 or price > MAX_UNIT_PRICE:
            raise InvalidInput("invalid_price", f"income[{i}].price")
        currency = _currency(row.get("currency"), f"income[{i}].currency")
        item = {
            "description": desc,
            "quantity": qty,
            "price": price,
            "currency": currency,
            "vatType": _int_field(row, "vatType", default=0),
        }
        if row.get("catalogNum") is not None:
            item["catalogNum"] = _req_str(row, "catalogNum", maxlen=64, required=False)
        out.append(item)
    return out


def _resolve_client_block(args: dict, *, email_to_client: bool,
                          for_issue: bool) -> dict:
    """Build the document's `client` block.

    Either an existing client by id, or an inline client. `email_to_client`
    controls whether the client's emails are populated on the document:
    GreenInvoice emails the document to whatever emails appear here, so
    stripping them is how we issue WITHOUT distributing.

    Strict booleans throughout: a real JSON `true` is required to enable a
    side effect. `add` (persist an inline client) is honored ONLY when
    issuing — a draft/preview must never create a client record.
    """
    raw = args.get("client")
    if not isinstance(raw, dict):
        raise InvalidInput("missing_field", "client")
    client_id = raw.get("id")
    if client_id is not None:
        if not isinstance(client_id, str) or not client_id or len(client_id) > 64:
            raise InvalidInput("invalid_client_id", "client.id")
        block: dict[str, Any] = {"id": client_id}
        name = _req_str(raw, "name", required=False)
        if name:
            block["name"] = name
    else:
        # Inline client. `add` is honored only on issue, and only for a
        # literal `true`; otherwise it stays a one-off (never persisted).
        block = {
            "name": _req_str(raw, "name"),
            "add": (raw.get("add") is True) and for_issue,
        }
        for opt in ("taxId", "address", "city", "zip", "phone", "mobile", "country"):
            val = _req_str(raw, opt, required=False)
            if val is not None:
                block[opt] = val

    # Email control. The caller's `client.emails` is only honored when the
    # caller also set email_to_client=True; otherwise we force no emails so
    # no distribution happens on issue.
    if email_to_client:
        emails = raw.get("emails")
        if not emails:
            raise InvalidInput("email_to_client_without_emails", "client.emails")
        block["emails"] = _emails(emails)
    else:
        block["emails"] = []
    return block


def build_document(args: dict, *, for_issue: bool) -> dict:
    """Validate + build a POST /documents (or /documents/preview) body.

    `for_issue` tightens the allowed document types to the issuable set and
    requires the explicit confirmation flag (checked by the handler, not
    here). Previews allow a slightly wider type set and never email.
    """
    if not isinstance(args, dict):
        raise InvalidInput("invalid_field_type", "document")

    doc_type = _int_field(args, "type")
    allowed = ISSUABLE_DOCUMENT_TYPES if for_issue else DRAFTABLE_DOCUMENT_TYPES
    if doc_type not in allowed:
        raise InvalidInput("document_type_not_allowed", str(doc_type))

    # Strict identity check: a literal JSON `true` is required to email the
    # client. Never email on a draft/preview.
    email_to_client = (args.get("email_to_client") is True) if for_issue else False

    body: dict[str, Any] = {
        "type": doc_type,
        "client": _resolve_client_block(args, email_to_client=email_to_client,
                                        for_issue=for_issue),
        "currency": _currency(args.get("currency", "ILS")),
        "lang": _lang(args.get("lang", "he")),
        "vatType": _int_field(args, "vatType", default=0),
        "income": _build_income(args.get("income")),
    }

    date = _date(args, "date", required=False)
    if date:
        body["date"] = date
    due = _date(args, "dueDate", required=False)
    if due:
        body["dueDate"] = due

    for opt, maxlen in (("description", MAX_STR), ("remarks", MAX_DESC),
                        ("footer", MAX_STR), ("emailContent", MAX_DESC)):
        val = _req_str(args, opt, maxlen=maxlen, required=False)
        if val is not None:
            body[opt] = val

    # Receipt (400) linked to an invoice it pays off.
    linked = args.get("linkedDocumentId")
    if linked is not None:
        if not isinstance(linked, str) or not linked or len(linked) > 64:
            raise InvalidInput("invalid_linked_document_id", "linkedDocumentId")
        body["linkedDocumentIds"] = [linked]

    return body


def _lang(v: Any) -> str:
    if not isinstance(v, str) or v.lower() not in {"he", "en"}:
        raise InvalidInput("invalid_lang", "lang")
    return v.lower()


# ---- search validation ---------------------------------------------------

def build_search(args: dict, *, allowed_fields: set[str]) -> dict:
    """Build a conservative search body. Only whitelisted scalar/string
    filters pass through; pagination is clamped."""
    body: dict[str, Any] = {}
    if isinstance(args, dict):
        for k in allowed_fields:
            if k in args and args[k] is not None:
                v = args[k]
                if isinstance(v, str):
                    if "\x00" in v or len(v) > MAX_STR:
                        raise InvalidInput("invalid_search_field", k)
                    body[k] = v
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    body[k] = v
                else:
                    raise InvalidInput("invalid_search_field", k)
    page = args.get("page") if isinstance(args, dict) else None
    pagesize = args.get("pageSize") if isinstance(args, dict) else None
    body["page"] = page if isinstance(page, int) and 1 <= page <= 10_000 else 1
    body["pageSize"] = pagesize if isinstance(pagesize, int) and 1 <= pagesize <= 100 else 25
    return body
