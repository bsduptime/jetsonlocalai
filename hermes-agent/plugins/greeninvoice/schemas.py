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

_PAYMENT_ROW = {
    "type": "object",
    "description": "One payment received (for receipts: type 400 and 320).",
    "properties": {
        "date": {"type": "string", "description": "Date money was received, YYYY-MM-DD."},
        "type": {"type": "integer", "description": "Payment method: 1=cash, 2=cheque, 3=credit card, 4=bank transfer, 5=paypal, 10=payment app (also set appType: e.g. Bit), 11=other, 0=deduction at source, -1=not paid."},
        "price": {"type": "number", "description": "Amount received in `currency`."},
        "currency": {"type": "string", "description": "3-letter currency code, e.g. ILS."},
        "bankName": {"type": "string"},
        "bankBranch": {"type": "string"},
        "bankAccount": {"type": "string"},
        "chequeNum": {"type": "string", "description": "Cheque number (for type 2)."},
        "transactionId": {"type": "string", "description": "Transfer/transaction reference."},
        "cardType": {"type": "integer"},
        "cardNum": {"type": "string", "description": "Last digits of the card (for type 3)."},
        "numPayments": {"type": "integer", "description": "Number of card installments."},
        "appType": {"type": "integer", "description": "Which payment app (use with type 10): 1=Bit, 2=Pepper Pay (discontinued), 3=PayBox. Use 1 for money received via Bit."},
    },
    "required": ["date", "type", "price", "currency"],
    "additionalProperties": False,
}

_DOC_FIELDS = {
    "type": {"type": "integer", "description": "Document type code (see tool description). Drafts: 10/300/305/320/400. Issue: 305/320/400."},
    "client": _CLIENT_BLOCK,
    "income": {"type": "array", "items": _INCOME_ROW, "description": "Line items. Required for 305/320 (and quotes/proformas). Optional for a standalone 400 receipt."},
    "payment": {"type": "array", "items": _PAYMENT_ROW, "description": "Payments received. REQUIRED for a receipt (400) and an invoice+receipt (320); must NOT be set on a plain 305 tax invoice. Each row says how the money came in (method, amount, date)."},
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
        "real document. Loosely rate-limited (anti-spam only).\n"
        "IMPORTANT — wording: the invoice CONTENT (description, line items, "
        "remarks, footer) must be the FINAL, real wording. NEVER insert labels "
        "like 'Draft', 'Example', 'Sample', or 'Test' to mark it as "
        "provisional — this preview IS how you make a draft for review, and "
        "issuing reuses these exact fields, so any such word would persist into "
        "the real tax document. (Real work descriptions that happen to contain "
        "those words — e.g. 'draft a project plan', 'build a test suite' — are "
        "fine; the rule is about not LABELLING the invoice as a draft.)"
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
        "client. ALWAYS draft with gi_draft_invoice first. Before issuing, "
        "RESTATE the key points to David — client, document type, total amount, "
        "currency, and each line item — and get his explicit confirmation. "
        "Sanity-check it is a complete, correct invoice: right client and "
        "amounts, and NO leftover label wording (e.g. 'Draft', 'Example', "
        "'Sample') sitting in the description/remarks/footer, since that would "
        "ship in the real tax document. Only then set confirm=true. Returns the "
        "created document id/number plus remaining quota.\n"
        "To issue a RECEIPT for money already received, use type 400 with a "
        "`payment` array (method/amount/date) and no line items needed; set "
        "`linkedDocumentId` to the invoice it pays off if it settles a prior "
        "305. For invoice-and-receipt-in-one (paid on the spot), use type 320 "
        "with both `income` and `payment`."
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

# ---- expenses (vendor-side ledger) ----------------------------------------
#
# Expense doc types: 10 invoice, 20 receipt, 30 invoice+receipt, 40 other.
# Expense statuses: 10 Open (default on create), 20 Reported (locked to tax).
# We create expenses OPEN and NEVER auto-report; close_expense (confirm-gated)
# is the only path to Reported.

_SUPPLIER_BLOCK = {
    "type": "object",
    "description": (
        "The vendor. Either reference an existing supplier by `id` (resolve "
        "first with gi_search_suppliers), or supply an inline supplier with at "
        "least `name`."
    ),
    "properties": {
        "id": {"type": "string", "description": "Existing supplier id."},
        "name": {"type": "string", "description": "Supplier name."},
        "taxId": {"type": "string", "description": "Supplier tax id / ח.פ / ע.מ."},
        "emails": {"type": "array", "items": {"type": "string"}},
        "address": {"type": "string"},
        "city": {"type": "string"},
        "zip": {"type": "string"},
        "country": {"type": "string", "description": "2-letter ISO code, e.g. IL."},
        "phone": {"type": "string"},
        "mobile": {"type": "string"},
        "contactPerson": {"type": "string"},
    },
    "additionalProperties": False,
}

_CLASSIFICATION_BLOCK = {
    "type": "object",
    "description": (
        "Optional accounting classification (expense category). Resolve options "
        "with gi_get_classifications; pass at least its `id`."
    ),
    "properties": {
        "id": {"type": "string"},
        "key": {"type": "string"},
        "code": {"type": "string"},
        "title": {"type": "string"},
    },
    "additionalProperties": False,
}

GI_UPLOAD_EXPENSE_FILE = {
    "name": "gi_upload_expense_file",
    "description": (
        "Upload an invoice/receipt image or PDF to Morning so it OCRs the "
        "document and creates an expense DRAFT with the source file attached. "
        "Pass `path` = the local file path of the photo/PDF the user sent (the "
        "path Hermes gives you for a received attachment). Only files inside "
        "Hermes' allowed media dirs can be read. This does NOT create the final "
        "expense: after uploading, find the parsed draft with "
        "gi_search_expense_drafts, sanity-check the fields, run gi_search_expenses "
        "to make sure it isn't a duplicate, then create the OPEN expense with "
        "gi_create_expense. Rate-limited."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local path to the invoice image/PDF."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

GI_CREATE_EXPENSE = {
    "name": "gi_create_expense",
    "description": (
        "Create a business expense in Morning. It is created OPEN (status 10) "
        "and is NOT reported to tax — it stays reviewable/editable until the "
        "monthly review. ALWAYS run gi_search_expenses first (same supplier + "
        "number + amount + date) to avoid duplicates. Resolve the supplier with "
        "gi_search_suppliers (or gi_create_supplier). Loosely rate-limited. "
        "Returns the new expense id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "documentType": {"type": "integer", "description": "10 invoice, 20 receipt, 30 invoice+receipt, 40 other (default 40)."},
            "amount": {"type": "number", "description": "Total amount in `currency` (incl. VAT unless vatType says otherwise)."},
            "vat": {"type": "number", "description": "VAT amount (optional)."},
            "vatType": {"type": "integer", "description": "0 before-VAT, 1 VAT-included, 2 exempt (optional)."},
            "currency": {"type": "string", "description": "3-letter code. Default ILS."},
            "currencyRate": {"type": "number", "description": "FX rate to ILS if not ILS (optional)."},
            "paymentType": {"type": "integer", "description": "How it was paid: 1 cash, 2 cheque, 3 card, 4 transfer, 5 paypal, 10 app, 11 other, 0 deduction, -1 unpaid (optional)."},
            "date": {"type": "string", "description": "Document date YYYY-MM-DD (optional)."},
            "dueDate": {"type": "string", "description": "Due date YYYY-MM-DD (optional)."},
            "reportingDate": {"type": "string", "description": "Tax reporting period date YYYY-MM-DD (optional)."},
            "number": {"type": "string", "description": "The supplier's invoice/receipt number (optional but recommended — used for dedup)."},
            "description": {"type": "string", "description": "Short description (optional)."},
            "remarks": {"type": "string", "description": "Free-text remarks (optional)."},
            "supplier": _SUPPLIER_BLOCK,
            "accountingClassification": _CLASSIFICATION_BLOCK,
        },
        "required": ["amount", "supplier"],
        "additionalProperties": False,
    },
}

GI_SEARCH_EXPENSES = {
    "name": "gi_search_expenses",
    "description": (
        "Search expenses. Read-only, unlimited. Use this to CHECK FOR "
        "DUPLICATES before creating (filter by `supplierId`/`supplierName`, "
        "`number`, `minAmount`/`maxAmount`, `fromDate`/`toDate`) and for the "
        "monthly review (`fromDate`/`toDate`, `reported: false`). `paid` and "
        "`reported` are booleans."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "supplierId": {"type": "string"},
            "supplierName": {"type": "string"},
            "number": {"type": "string"},
            "fromDate": {"type": "string"},
            "toDate": {"type": "string"},
            "minAmount": {"type": "number"},
            "maxAmount": {"type": "number"},
            "description": {"type": "string"},
            "accountingClassificationId": {"type": "string"},
            "paid": {"type": "boolean"},
            "reported": {"type": "boolean"},
            "page": {"type": "integer"},
            "pageSize": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

GI_GET_EXPENSE = {
    "name": "gi_get_expense",
    "description": "Retrieve a single expense by id. Read-only, unlimited.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}

GI_DELETE_EXPENSE = {
    "name": "gi_delete_expense",
    "description": (
        "Delete an OPEN expense by id (e.g. one the user rejected in the "
        "monthly review). Only works while the expense is Open — a reported "
        "(status 20) expense cannot be deleted. Loosely rate-limited."
    ),
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}

GI_CLOSE_EXPENSE = {
    "name": "gi_close_expense",
    "description": (
        "Report an expense to tax: moves it from Open (10) to Reported (20). "
        "This is REAL and IRREVERSIBLE — a reported expense is locked and "
        "cannot be edited or deleted. Requires confirm=true and is tightly "
        "rate-limited (shares the invoice-issue budget: 3/hour, 10/day). Before "
        "closing, RESTATE the expense(s) to David — supplier, amount, date, "
        "number — and get his explicit confirmation. Only then set confirm=true. "
        "Do NOT close expenses on your own initiative; the default is to leave "
        "them Open for David / the accountant to file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Expense id to report."},
            "confirm": {"type": "boolean", "description": "Must be explicitly true. Irreversible tax report."},
        },
        "required": ["id", "confirm"],
        "additionalProperties": False,
    },
}

GI_SEARCH_EXPENSE_DRAFTS = {
    "name": "gi_search_expense_drafts",
    "description": (
        "Search expense DRAFTS created by file upload (the OCR-parsed, "
        "not-yet-confirmed expenses). Read-only, unlimited. Use after "
        "gi_upload_expense_file to read the parsed fields before creating the "
        "real expense."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "supplierId": {"type": "string"},
            "supplierName": {"type": "string"},
            "fromDate": {"type": "string"},
            "toDate": {"type": "string"},
            "description": {"type": "string"},
            "page": {"type": "integer"},
            "pageSize": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

_SUPPLIER_WRITE_PROPS = {
    "name": {"type": "string", "description": "Supplier name."},
    "emails": {"type": "array", "items": {"type": "string"}},
    "taxId": {"type": "string"},
    "address": {"type": "string"},
    "city": {"type": "string"},
    "zip": {"type": "string"},
    "country": {"type": "string", "description": "2-letter ISO code."},
    "phone": {"type": "string"},
    "mobile": {"type": "string"},
    "fax": {"type": "string"},
    "contactPerson": {"type": "string"},
    "accountingKey": {"type": "string"},
    "department": {"type": "string"},
    "remarks": {"type": "string"},
}

GI_CREATE_SUPPLIER = {
    "name": "gi_create_supplier",
    "description": (
        "Create a supplier (vendor) that expenses are attributed to. Loosely "
        "rate-limited. `name` is required. Returns the new supplier id. "
        "Suppliers can be created but never deleted through this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": dict(_SUPPLIER_WRITE_PROPS),
        "required": ["name"],
        "additionalProperties": False,
    },
}

GI_SEARCH_SUPPLIERS = {
    "name": "gi_search_suppliers",
    "description": (
        "Search suppliers by `name`, `email`, `contactPerson`, or `active`. "
        "Read-only, unlimited. Resolve a vendor name to a supplier id before "
        "creating an expense."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
            "contactPerson": {"type": "string"},
            "active": {"type": "boolean"},
            "page": {"type": "integer"},
            "pageSize": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

GI_GET_CLASSIFICATIONS = {
    "name": "gi_get_classifications",
    "description": (
        "List the accounting classifications (expense categories) available to "
        "this business. Read-only, unlimited. Use to pick a classification when "
        "creating an expense."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

ALL_SCHEMAS = [
    GI_DRAFT_INVOICE, GI_ISSUE_INVOICE, GI_GET_DOCUMENT, GI_SEARCH_DOCUMENTS,
    GI_DOCUMENT_DOWNLOAD_LINKS, GI_CREATE_CLIENT, GI_UPDATE_CLIENT,
    GI_GET_CLIENT, GI_SEARCH_CLIENTS, GI_QUOTA,
    # expenses
    GI_UPLOAD_EXPENSE_FILE, GI_CREATE_EXPENSE, GI_SEARCH_EXPENSES,
    GI_GET_EXPENSE, GI_DELETE_EXPENSE, GI_CLOSE_EXPENSE,
    GI_SEARCH_EXPENSE_DRAFTS, GI_CREATE_SUPPLIER, GI_SEARCH_SUPPLIERS,
    GI_GET_CLASSIFICATIONS,
]
