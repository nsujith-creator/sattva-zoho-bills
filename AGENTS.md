# Zoho Bills automation repository instructions

Inherit the global Codex instructions.

## Product boundary

This workflow extracts, validates, deduplicates, and prepares vendor bills for Zoho Books. AI extraction is unverified input, not accounting truth. Human approval and reconciliation are mandatory before posting.

## Authority

Use the canonical Zoho Bills project memory or project-specific authoritative document when available. Do not silently override established field mappings, duplicate rules, approval boundaries, or accounting decisions with assumptions from code or old notes.

## Required constraints

- Never auto-post a vendor bill solely because extraction completed.
- Preserve duplicate detection and idempotency.
- Reconcile invoice number, vendor, date, taxable amount, tax components, total, currency, and source document.
- Flag ambiguity rather than inventing a value.
- Keep original source evidence linked or traceable.
- Do not change Zoho organisation settings, tax treatment, chart of accounts, vendor master, credentials, or production records without explicit approval.
- Do not add another OCR or LLM provider without a measured extraction failure, sample comparison, cost, and rollback plan.

## Verification boundary

For extraction or posting changes, test representative cases:
1. Clean invoice.
2. Poor-quality or ambiguous invoice.
3. Duplicate invoice.
4. Tax mismatch.
5. Missing mandatory field.
6. Retry after partial failure.

Completion requires showing which cases passed, which fields were reconciled, and that no bill was posted without the required approval boundary.
