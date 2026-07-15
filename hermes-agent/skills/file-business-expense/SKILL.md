---
name: file-business-expense
description: "File a supplier invoice or receipt (photo or PDF) that David sends as a business expense in Morning/GreenInvoice. Use whenever David shares an invoice/receipt and wants it recorded, filed, or 'entered as an expense'. Hebrew (חשבונית / קבלה) and English."
version: 1.0.0
author: David Klippel
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [expenses, accounting, greeninvoice, morning, invoice, receipt, bookkeeping]
---

# File a business expense from a receipt or invoice

Use this when David sends a photo or PDF of a **supplier invoice/receipt** and wants it
recorded as a business expense in Morning (GreenInvoice). The greeninvoice tools
(`gi_*`) are your interface; the Morning API key lives in a separate broker you never see.

## The one rule that matters
**Morning's OCR is the source of truth for the numbers, not your eyes.** You upload the
document, Morning parses it, and you file *those* parsed values. Your own reading of the
image is a cross-check, not the data. This keeps the ledger accurate and keeps the
original document attached — which the tax authority requires.

## Steps

1. **Look at the document.** Confirm it really is a supplier invoice or receipt. If it's
   clearly something else (a photo, a bank statement, a delivery note תעודת משלוח, a price
   quote הצעת מחיר, an ID), do **not** file it — say what it is and ask David what he wants.

2. **Upload it to Morning for OCR.** Call `gi_upload_expense_file(path=<local path of the
   attachment David sent>)`. This creates an expense **draft** with the source file
   attached. Do NOT skip this and type numbers from your own reading — the attached
   original is required for tax.

3. **Read the parsed draft.** Call `gi_search_expense_drafts` (filter by supplier/date) to
   get the fields Morning's OCR extracted. These are the authoritative numbers.

4. **Cross-check against the image.** Compare the draft's supplier, amount, VAT, date and
   document number to what you see in the photo. If they disagree, **flag the discrepancy
   to David** and let him decide — don't silently pick one.

5. **Check for duplicates.** Call `gi_search_expenses` with supplier + number + amount +
   date. If a matching expense already exists, tell David and **stop** — never double-file.

6. **Resolve the supplier.** `gi_search_suppliers` by name / taxId. If it exists, use its
   `id`. If not, `gi_create_supplier` with the supplier's name and taxId (ח.פ / ע.מ).

7. **Create the expense as OPEN.** Call `gi_create_expense` with the values **from the
   draft** — amount, supplier, vat, vatType, date, number, documentType. It is created
   **Open (status 10): recorded but NOT reported to tax.**
   - David will be asked to **confirm the numbers** in Telegram before it is written. This
     is expected and good. Present the supplier and amount clearly so he can check at a
     glance. If he declines, do not retry with the same numbers — ask what to change.

8. **Confirm back to David.** Once created: e.g. *"Filed as an Open expense: הערמונים שלי,
   ₪16.90, 30/06/2026. It stays Open for the monthly review."*

## Never do these on your own
- **Never report an expense to tax** (`gi_close_expense`). Closing is **irreversible** and
  only happens in the monthly review, per-item, when David explicitly approves it.
- **Never** create an expense from your own reading without uploading the source document.
- **Never** guess when a field is unreadable, a document is ambiguous, or it might be
  personal rather than business — ask David.

## If you can't read a field
Say which field and why (blurry, cropped, cut off). Morning's OCR draft may still have it;
if not, ask David to confirm that one value rather than guessing.
