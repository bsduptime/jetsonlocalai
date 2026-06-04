"""JSON schemas exposed to the LLM via ctx.register_tool().

Document types this tool understands:
  305 — tax invoice (חשבונית מס): records income + VAT, demands payment.
  320 — invoice/receipt (חשבונית מס/קבלה): invoice AND receipt in one.
  400 — receipt (קבלה): acknowledges payment; link it to the 305 it pays via
        `linkedDocumentId` when a 305 is later paid.
  300 — proforma/"deal" account, 10 — price quote: DRAFT-ONLY (previewable,
        never issued through this tool).
"""

from __future__ import annotations

# Reusable sub-schemas -------------------------------------------------------

_CLIENT_BLOCK = {
    "type": "object",
    "description": (
        "Who the document bills. Either reference an existing client by "
        "`id` (resolve it first with gi_search_clients), or supply an inline "
        "client with at least `name`. Set `add: true` on an inline client to "
        "save it to the client list; otherwise it is a one-off."
    ),
    "properties": {
        "id": {"type": "string", "description": "Existing client id. If set, other fields are optional."},
        "name": {"type": "string", "description": "Client display name."},
        "emails": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "Client email(s). Only used to DISTRIBUTE the document, and "
                "only when issuing with email_to_client=true. Ignored on drafts."
            ),
        },
        "taxId": {"type": "string", "description": "Client tax id / ת.ז / ח.פ."},
        "address": {"type": "string"},
        "city": {"type": "string"},
        "zip": {"type": "string"},
        "country": {"type": "string", "description": "2-letter ISO country code, e.g. IL."},
        "phone": {"type": "string"},
        "mobile": {"type": "string"},
        "add": {"type": "boolean", "description": "Persist an inline client to the client list (default false)."},
    },
    "additionalProperties": False,
}

_INCOME_ROW = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "Line item description."},
        "quantity": {"type": "number", "description": "Quantity (> 0)."},
        "price": {"type": "number", "description": "Unit price (>= 0), before VAT, in `currency`."},
        "currency": {"type": "string", "description": "3-letter currency code, e.g. ILS, USD."},
        "vatType": {"type": "integer", "description": "VAT handling: 0 = default (apply VAT). Leave 0 unless told otherwise."},
        "catalogNum": {"type": "string", "description": "Optional catalog/SKU number."},
    },
    "required": ["description", "quantity", "price", "currency"],
    "additionalProperties": False,
}

_DOC_FIELDS = {
    "type": {"type": "integer", "description": "Document type code (see tool description). Drafts: 10/300/305/320/400. Issue: 305/320/400."},
    "client": _CLIENT_BLOCK,
    "income": {"type": "array", "items": _INCOME_ROW, "description": "Line items (at least one)."},
    "currency": {"type": "string", "description": "Document currency (3-letter). Default ILS."},
    "vatType": {"type": "integer", "description": "Document-level VAT type. Default 0."},
    "lang": {"type": "string", "enum": ["he", "en"], "description": "Document language. Default he."},
    "date": {"type": "string", "description": "Issue date YYYY-MM-DD. Default: today (server-side)."},
    "dueDate": {"type": "string", "description": "Payment due date YYYY-MM-DD (optional)."},
    "description": {"type": "string", "description": "Short document description (optional)."},
    "remarks": {"type": "string", "description": "Free-text remarks shown on the document (optional)."},
    "footer": {"type": "string", "description": "Footer text (optional)."},
    "linkedDocumentId": {"type": "string", "description": "For a 400 receipt: the id of the 305 invoice it pays off (optional)."},
}


def _doc_params(required):
    return {
        "type": "object",
        "properties": dict(_DOC_FIELDS),
        "required": required,
        "additionalProperties": False,
    }


# Tool schemas ---------------------------------------------------------------

GI_DRAFT_INVOICE = {
    "name": "gi_draft_invoice",
    "description": (
        "Render a PREVIEW of an invoice/document WITHOUT creating it. Nothing "
        "is recorded in GreenInvoice, no number is burned, and no email is "
        "sent. The rendered PDF is written to a file on disk and the result "
        "gives you `preview_pdf_path` (an absolute path) plus "
        "`preview_pdf_bytes` — the PDF is NOT returned inline, so do not "
        "expect base64 content. To show David the draft, hand that path to "
        "your delivery channel (attach the file via the mailer, or send it "
        "over Telegram); do not read the file's bytes into your reply. When "
        "he approves, call gi_issue_invoice with the same fields to create the "
        "real document. Loosely rate-limited (anti-spam only)."
    ),
    "parameters": _doc_params(["type", "client", "income"]),
}

_ISSUE_FIELDS = dict(_DOC_FIELDS)
_ISSUE_FIELDS["email_to_client"] = {
    "type": "boolean",
    "description": (
        "If true, GreenInvoice emails the issued document to the client's "
        "`emails` (which must then be provided). If false/omitted, the "
        "document is issued but NOT distributed to the client."
    ),
}
_ISSUE_FIELDS["confirm"] = {
    "type": "boolean",
    "description": (
        "Must be explicitly true to issue. This is a REAL, IRREVERSIBLE "
        "accounting/tax document — confirm with David before setting it."
    ),
}

GI_ISSUE_INVOICE = {
    "name": "gi_issue_invoice",
    "description": (
        "Create a REAL, IRREVERSIBLE GreenInvoice document (305 tax invoice, "
        "320 invoice+receipt, or 400 receipt). Increments the document number "
        "sequence; cannot be deleted (only cancelled via a credit note in the "
        "dashboard). Requires confirm=true and is tightly rate-limited "
        "(3/hour, 10/day). Set email_to_client=true to also email it to the "
        "client. ALWAYS draft with gi_draft_invoice and get David's explicit "
        "go-ahead first. Returns the created document id/number plus remaining "
        "quota."
    ),
    "parameters": {
        "type": "object",
        "properties": _ISSUE_FIELDS,
        "required": ["type", "client", "income", "confirm"],
        "additionalProperties": False,
    },
}

GI_GET_DOCUMENT = {
    "name": "gi_get_document",
    "description": "Retrieve a single document by its GreenInvoice id. Read-only, unlimited.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Document id."}},
        "required": ["id"],
        "additionalProperties": False,
    },
}

GI_SEARCH_DOCUMENTS = {
    "name": "gi_search_documents",
    "description": (
        "Search documents. Read-only, unlimited. Filter by `type` (e.g. 305), "
        "`status`, `fromDate`/`toDate` (YYYY-MM-DD), `clientId`, or free `text`. "
        "Paginate with `page` and `pageSize` (<=100)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {"type": "integer"},
            "status": {"type": "integer"},
            "fromDate": {"type": "string"},
            "toDate": {"type": "string"},
            "clientId": {"type": "string"},
            "text": {"type": "string"},
            "page": {"type": "integer"},
            "pageSize": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

GI_DOCUMENT_DOWNLOAD_LINKS = {
    "name": "gi_document_download_links",
    "description": "Get signed PDF download links for a document by id. Read-only, unlimited.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Document id."}},
        "required": ["id"],
        "additionalProperties": False,
    },
}

_CLIENT_WRITE_PROPS = {
    "name": {"type": "string", "description": "Client display name."},
    "emails": {"type": "array", "items": {"type": "string"}, "description": "Client email(s)."},
    "taxId": {"type": "string"},
    "address": {"type": "string"},
    "city": {"type": "string"},
    "zip": {"type": "string"},
    "country": {"type": "string", "description": "2-letter ISO country code."},
    "phone": {"type": "string"},
    "mobile": {"type": "string"},
    "fax": {"type": "string"},
}

GI_CREATE_CLIENT = {
    "name": "gi_create_client",
    "description": (
        "Create a client that documents can bill. Loosely rate-limited. "
        "`name` is required. Returns the new client id."
    ),
    "parameters": {
        "type": "object",
        "properties": dict(_CLIENT_WRITE_PROPS),
        "required": ["name"],
        "additionalProperties": False,
    },
}

GI_UPDATE_CLIENT = {
    "name": "gi_update_client",
    "description": (
        "Update an existing client by id. Loosely rate-limited. Clients can "
        "be created and updated but NEVER deleted through this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Client id to update."}, **_CLIENT_WRITE_PROPS},
        "required": ["id", "name"],
        "additionalProperties": False,
    },
}

GI_GET_CLIENT = {
    "name": "gi_get_client",
    "description": "Retrieve a single client by id. Read-only, unlimited.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Client id."}},
        "required": ["id"],
        "additionalProperties": False,
    },
}

GI_SEARCH_CLIENTS = {
    "name": "gi_search_clients",
    "description": (
        "Search clients by `name`, `taxId`, `email`, or free `text`. "
        "Read-only, unlimited. Use this to resolve a name to a client id "
        "before drafting or issuing a document."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "taxId": {"type": "string"},
            "email": {"type": "string"},
            "text": {"type": "string"},
            "page": {"type": "integer"},
            "pageSize": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

GI_QUOTA = {
    "name": "gi_quota",
    "description": (
        "Report remaining rate-limit budget for each action class (issue, "
        "draft, client_write), the current environment (sandbox/production), "
        "and whether the broker is in dry-run. Read-only. Call this before "
        "issuing if unsure whether you're near the cap."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

ALL_SCHEMAS = [
    GI_DRAFT_INVOICE, GI_ISSUE_INVOICE, GI_GET_DOCUMENT, GI_SEARCH_DOCUMENTS,
    GI_DOCUMENT_DOWNLOAD_LINKS, GI_CREATE_CLIENT, GI_UPDATE_CLIENT,
    GI_GET_CLIENT, GI_SEARCH_CLIENTS, GI_QUOTA,
]
