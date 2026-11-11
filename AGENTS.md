# Zoho Bills automation repository instructions

Inherit the global Codex instructions.

## Product boundary

This workflow extracts, validates, deduplicates, and prepares vendor bills for Zoho Books. AI extraction is unverified input, not accounting truth. Human approval and reconciliation are mandatory before posting.

## Authority

Use the canonical Zoho Bills project memory or project-specific authoritative document when available. Do not silently override established field mappings, duplicate rules, approval boundaries, or accounting decisions with assumptions from code or old notes.

## Owner-approved business-expense routing

Any invoice billed to GSTIN 27AEFPN9399L1ZI belongs in Sattva's business-expense processing workflow, regardless of the personal name or billing contact displayed. Do not reject it as personal for that reason. The GSTIN must be evidenced on the source invoice, not inferred from the vendor's registration or an AI assertion. Business-expense intake does not imply automatic tax-credit eligibility or permission to post; preserve reconciliation, duplicate detection, reverse-charge treatment, and per-invoice human approval.

## Required constraints

### Owner-authorized cloud automatic processing, 28 October 2026

The owner explicitly instructed: cloud browser upload → Haiku parsing → automatic Zoho posting, with no manual re-entry or laptop step. For that cloud journey, this later instruction authorizes conditional automatic creation after source checks, exact vendor registration, arithmetic/tax validation, duplicate checks and durable source/payload journaling. It supersedes the per-invoice approval requirement for qualifying new cloud uploads only. It does not authorize bulk historical processing, existing-bill edits, vendor-master changes, invented FX or tax decisions, payments, or exceeding the existing spending ceiling. Extraction alone never authorizes posting. Failed or ambiguous checks hold the invoice with its reason. Completion requires exact Zoho read-back and original-PDF verification. The Windows workflow retains its existing approval behavior.

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


### Owner-approved duplicate penny allocation, 29 October 2026

For duplicate verification only, the owner permits offsetting one-paise line allocations within the SAME GST head and rate when source bases, quantities, descriptions, SAC, vendor registration, tax IDs, every GST-head total and invoice total match exactly. Each line difference is at most one paise; each same-head/rate group must net to zero with complete line evidence. Cross-head/cross-rate offsets, larger differences, missing evidence or any total tolerance remain held. This does not authorize existing-bill edits or relax new-bill read-back. Preserve raw extraction evidence and separately record any PDF-derived reconciliation. Additional AI calls and deployment require their existing approvals.

### Owner-approved Total Transport rounding, 30 October 2026

Total Transport Systems Limited (PAN AAACT3276C) prints per-line rounded GST, while Zoho saves its own rounding. For NEW cloud bills from that supplier only, Zoho's saved tax is accepted as verified when each GST head, the tax total, the payable total and every line tax are within INR 1 of the printed invoice. Taxable subtotal, line amounts, tax IDs, identity, dates and the original-attachment check stay exact. The accepted differences are recorded on the job. Other suppliers keep exact read-back.

### Owner-approved bank-debit booking for foreign-currency overseas invoices, 3 November 2026

For NEW cloud uploads from overseas service suppliers (for example Anthropic, OpenAI, Railway, Cloudflare, ShipsGo and future ones) whose invoice is printed only in a foreign currency with no INR conversion, the app asks the owner for the two bank debits: the payment and the bank charges. It books the bill in INR: service lines scaled to sum exactly to the payment debit, with reverse charge; one separate bank-charges line with no GST and no reverse charge. The original currency amount, effective rate and charges are written in the bill notes; the printed reading is kept unchanged. Effective rate must fall in the configured plausibility band (USD 70-130) and charges may not exceed 10% of the payment, otherwise the owner is asked again. All other source, duplicate, vendor (existing Zoho vendor only), read-back and original-PDF checks stay mandatory. This does not authorize invented FX rates, vendor creation, or edits to existing bills.

### Owner-approved expense account routing, 12 November 2026

New cloud bills route each line to a Zoho account by the matched Zoho vendor, never by AI choice: Anthropic, OpenAI, Railway, Cloudflare and ShipsGo to Software Subscription Charger (914109000000994119); Sendinblue/Brevo to Advertising And Marketing (914109000000000501); Google India by printed service code (998319/998315 to Software Subscription Charger, 99836x to Advertising And Marketing); the bank-charges line on bank-debit bookings to Bank Fees and Charges (914109000000000507); every other line stays on COGS (914109000000000567). Existing bills are not edited. Duplicate verification compares source facts, not this booking choice.
