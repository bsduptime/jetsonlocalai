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

from .config import (
    EXPENSE_DOCUMENT_TYPES,
    EXPENSE_VAT_TYPES,
    ISSUABLE_DOCUMENT_TYPES,
)
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

# Receipt logic. A receipt (400 קבלה) and an invoice+receipt (320) record HOW
# money was received via `payment` rows; a standalone receipt may carry no
# line items. A plain tax invoice (305) has income only — payment belongs on
# its receipt, not on it.
PAYMENT_REQUIRED_TYPES = {320, 400}
INCOME_OPTIONAL_TYPES = {400}
# GreenInvoice PaymentType: -1 not-paid, 0 deduction-at-source, 1 cash,
# 2 cheque, 3 credit-card, 4 bank-transfer, 5 paypal, 10 payment-app, 11 other.
VALID_PAYMENT_TYPES = {-1, 0, 1, 2, 3, 4, 5, 10, 11}
# For a payment-app payment (type 10), `appType` names the app:
# 1 Bit, 2 Pepper Pay (discontinued), 3 PayBox. 0 = unspecified.
VALID_APP_TYPES = {0, 1, 2, 3}
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


def _build_payment(rows: Any) -> list[dict]:
    """Validate + build the `payment` rows of a receipt (how money came in)."""
    if not isinstance(rows, list) or not rows:
        raise InvalidInput("missing_field", "payment")
    if len(rows) > MAX_INCOME_ROWS:
        raise InvalidInput("too_many_payment_rows", "payment")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InvalidInput("invalid_payment_row", str(i))
        date = _date(row, "date", required=True)
        ptype = row.get("type")
        if isinstance(ptype, bool) or not isinstance(ptype, int) \
                or ptype not in VALID_PAYMENT_TYPES:
            raise InvalidInput("invalid_payment_type", f"payment[{i}].type")
        price = _num(row.get("price"), f"payment[{i}].price")
        if price < 0 or price > MAX_UNIT_PRICE:
            raise InvalidInput("invalid_price", f"payment[{i}].price")
        item = {
            "date": date,
            "type": ptype,
            "price": price,
            "currency": _currency(row.get("currency"), f"payment[{i}].currency"),
        }
        # Optional method-specific details (cheque no., bank, card, txn ref).
        for opt in ("bankName", "bankBranch", "bankAccount", "chequeNum",
                    "transactionId", "cardNum", "accountId"):
            v = _req_str(row, opt, maxlen=64, required=False)
            if v is not None:
                item[opt] = v
        for opt_int in ("cardType", "dealType", "numPayments", "firstPayment"):
            if row.get(opt_int) is not None:
                item[opt_int] = _int_field(row, opt_int)
        # Payment-app details: `appType` names the app (1=Bit, 3=PayBox).
        if row.get("appType") is not None:
            app = _int_field(row, "appType")
            if app not in VALID_APP_TYPES:
                raise InvalidInput("invalid_app_type", f"payment[{i}].appType")
            item["appType"] = app
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
    }

    # Income (line items): required for everything except a standalone
    # receipt (400), which may acknowledge a payment with no line items.
    income_rows = args.get("income")
    if doc_type in INCOME_OPTIONAL_TYPES and not income_rows:
        pass
    else:
        body["income"] = _build_income(income_rows)

    # Payment (how money was received): required for receipt-bearing docs
    # (400 receipt, 320 invoice+receipt); rejected on documents that don't
    # record payment (e.g. a plain 305 tax invoice) so it can't be smuggled on.
    payment_rows = args.get("payment")
    if doc_type in PAYMENT_REQUIRED_TYPES:
        body["payment"] = _build_payment(payment_rows)
    elif payment_rows:
        raise InvalidInput("payment_not_allowed_for_type", str(doc_type))

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


# ---- expense validation --------------------------------------------------

# Vendor-side "accounting classification" (the expense category). We forward
# only these identifying/echo fields; the caller cannot smuggle arbitrary keys.
_CLASSIFICATION_FIELDS = ("id", "key", "code", "title", "irsCode", "type")


def _build_supplier_block(raw: Any) -> dict:
    """Build the expense's `supplier` block: either an existing supplier by
    `id`, or an inline supplier with at least `name`."""
    if not isinstance(raw, dict):
        raise InvalidInput("missing_field", "supplier")
    supplier_id = raw.get("id")
    if supplier_id is not None:
        if not isinstance(supplier_id, str) or not supplier_id or len(supplier_id) > 64:
            raise InvalidInput("invalid_supplier_id", "supplier.id")
        block: dict[str, Any] = {"id": supplier_id}
        name = _req_str(raw, "name", required=False)
        if name:
            block["name"] = name
        return block
    block = {"name": _req_str(raw, "name")}
    if "emails" in raw and raw["emails"] is not None:
        block["emails"] = _emails(raw["emails"])
    for opt in ("taxId", "address", "city", "zip", "phone", "mobile", "fax",
                "country", "contactPerson", "accountingKey", "department"):
        val = _req_str(raw, opt, required=False)
        if val is not None:
            block[opt] = val
    if "country" in block and (len(block["country"]) != 2 or not block["country"].isalpha()):
        raise InvalidInput("invalid_country", "supplier.country")
    return block


def _build_classification(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise InvalidInput("invalid_field_type", "accountingClassification")
    out: dict[str, Any] = {}
    for k in _CLASSIFICATION_FIELDS:
        v = raw.get(k)
        if v is None:
            continue
        if k in ("irsCode", "type"):
            out[k] = _int_field(raw, k)
        else:
            s = _req_str(raw, k, maxlen=120, required=False)
            if s is not None:
                out[k] = s
    if not out:
        raise InvalidInput("empty_classification", "accountingClassification")
    return out


def build_expense(args: dict) -> dict:
    """Validate + build a POST /expenses body. Builds an expense that is
    CREATED OPEN (status 10). This builder structurally cannot report/close an
    expense: there is no path here to status 20 — that lives solely in the
    confirm-gated close_expense op (POST /expenses/{id}/close)."""
    if not isinstance(args, dict):
        raise InvalidInput("invalid_field_type", "expense")

    doc_type = _int_field(args, "documentType", default=40)
    if doc_type not in EXPENSE_DOCUMENT_TYPES:
        raise InvalidInput("expense_type_not_allowed", str(doc_type))

    amount = _num(args.get("amount"), "amount")
    if amount < 0 or amount > MAX_UNIT_PRICE:
        raise InvalidInput("invalid_amount", "amount")

    body: dict[str, Any] = {
        "documentType": doc_type,
        "amount": amount,
        "currency": _currency(args.get("currency", "ILS")),
        "supplier": _build_supplier_block(args.get("supplier")),
        # Always active: an expense must stay visible for the monthly review.
        # Not caller-controllable (a hidden/inactive expense could dodge review).
        "active": True,
    }

    if args.get("vat") is not None:
        vat = _num(args.get("vat"), "vat")
        if vat < 0 or vat > MAX_UNIT_PRICE:
            raise InvalidInput("invalid_vat", "vat")
        body["vat"] = vat
    if args.get("vatType") is not None:
        vt = _int_field(args, "vatType")
        if vt not in EXPENSE_VAT_TYPES:
            raise InvalidInput("invalid_vat_type", "vatType")
        body["vatType"] = vt
    if args.get("currencyRate") is not None:
        rate = _num(args.get("currencyRate"), "currencyRate")
        if rate <= 0 or rate > 1_000_000:
            raise InvalidInput("invalid_currency_rate", "currencyRate")
        body["currencyRate"] = rate
    if args.get("paymentType") is not None:
        ptype = args.get("paymentType")
        if isinstance(ptype, bool) or not isinstance(ptype, int) \
                or ptype not in VALID_PAYMENT_TYPES:
            raise InvalidInput("invalid_payment_type", "paymentType")
        body["paymentType"] = ptype

    for date_key in ("date", "dueDate", "reportingDate"):
        d = _date(args, date_key, required=False)
        if d:
            body[date_key] = d

    num = _req_str(args, "number", maxlen=64, required=False)
    if num is not None:
        body["number"] = num
    for opt, maxlen in (("description", MAX_STR), ("remarks", MAX_DESC)):
        val = _req_str(args, opt, maxlen=maxlen, required=False)
        if val is not None:
            body[opt] = val

    if args.get("accountingClassification") is not None:
        body["accountingClassification"] = _build_classification(
            args["accountingClassification"])

    return body


def build_supplier_create(args: dict) -> dict:
    """Validate + build a POST /suppliers body (mirror of build_client_create)."""
    if not isinstance(args, dict):
        raise InvalidInput("invalid_field_type", "supplier")
    body: dict[str, Any] = {"name": _req_str(args, "name")}
    if "emails" in args and args["emails"] is not None:
        body["emails"] = _emails(args["emails"])
    for opt in ("taxId", "address", "city", "zip", "phone", "mobile", "fax",
                "country", "contactPerson", "accountingKey", "department", "remarks"):
        val = _req_str(args, opt, required=False)
        if val is not None:
            body[opt] = val
    if "country" in body and (len(body["country"]) != 2 or not body["country"].isalpha()):
        raise InvalidInput("invalid_country", "country")
    return body


# ---- upload metadata validation ------------------------------------------

# Invoice attachments Elena may upload. Kept tight — these are business
# receipts, not arbitrary files.
UPLOAD_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "heic", "heif", "gif"}
UPLOAD_CONTENT_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/webp",
    "image/heic", "image/heif", "image/gif",
}
MAX_FILENAME = 255


def build_upload_meta(args: dict, *, max_file_bytes: int) -> dict:
    """Validate the metadata for an upload_expense_file request. The raw file
    bytes ride the framed body, not the JSON; here we validate filename,
    content_type and the declared byte length against the cap."""
    if not isinstance(args, dict):
        raise InvalidInput("invalid_field_type", "upload")
    filename = _req_str(args, "filename", maxlen=MAX_FILENAME)
    # No path separators, and nothing that could inject a multipart
    # Content-Disposition header (quotes, CR/LF, other control chars).
    if any(c in filename for c in '/\\"') or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise InvalidInput("invalid_filename", "filename")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in UPLOAD_EXTENSIONS:
        raise InvalidInput("unsupported_file_type", "filename")
    content_type = _req_str(args, "content_type", maxlen=100)
    # Normalise to the validated BASE type only — discard any parameters. This
    # is what gets written into the multipart Content-Type header, so it must
    # not carry `;params`, CR/LF, or other control chars (header injection).
    base_ct = content_type.split(";", 1)[0].strip().lower()
    if base_ct not in UPLOAD_CONTENT_TYPES:
        raise InvalidInput("unsupported_content_type", "content_type")
    content_type = base_ct
    byte_len = args.get("byte_len")
    if isinstance(byte_len, bool) or not isinstance(byte_len, int):
        raise InvalidInput("invalid_byte_len", "byte_len")
    if byte_len <= 0 or byte_len > max_file_bytes:
        raise InvalidInput("file_too_large", "byte_len")
    return {"filename": filename, "content_type": content_type, "byte_len": byte_len}


# ---- search validation ---------------------------------------------------

def build_search(args: dict, *, allowed_fields: set[str],
                 bool_fields: set[str] = frozenset()) -> dict:
    """Build a conservative search body. Only whitelisted scalar/string
    filters pass through; pagination is clamped. Fields named in `bool_fields`
    additionally accept a JSON boolean."""
    body: dict[str, Any] = {}
    if isinstance(args, dict):
        for k in allowed_fields:
            if k in args and args[k] is not None:
                v = args[k]
                if k in bool_fields and isinstance(v, bool):
                    body[k] = v
                elif isinstance(v, str):
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
