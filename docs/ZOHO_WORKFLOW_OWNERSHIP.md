# Sattva Zoho Books workflow responsibilities

## Scope and existing instructions

This document records workflow responsibilities. It supplements [AGENTS.md](../AGENTS.md); it does not supersede it or the project's existing master instructions in `C:\temp\zoho\ZOHO_BILLS_AUTOMATION_KNOWLEDGE.md`. Read those instructions before operational work. Preserve established field mappings, duplicate detection, idempotency, source traceability and approval boundaries. Surface any conflict with its source and date rather than silently replacing an established instruction with an assumption.

## Responsibilities

**Sujith:** purchasing, receiving original supplier invoices, entering and reviewing purchase bills in Zoho, banking and payments, supplying bank/FX evidence, and approving accounting changes and return filings. Ownership of these activities does not authorize an AI agent to change records or vendor masters.

**Tanvi:** IFF/GST-return filing using Zoho entries and GST portal records available to her, within the owner's QRMP workflow. Professional sign-off belongs to an appropriately qualified professional; do not infer a CA credential from her role. Supplier-invoice investigation, purchase-bill entry or correction, bank/FX reconstruction and automation diagnosis are not part of her assigned filing remit. Do not assume she has received original invoices or knows about invoices never supplied to her.

Involve Tanvi when there is an identified GST-filing question or need for return/portal evidence. Investigate records already controlled by the owner first. Do not manufacture Tanvi-specific packs, spreadsheets or explanations, or shift internal investigations to her merely because an audit found a discrepancy. Sujith decides what information or documents to share for a genuine filing purpose and authorizes external communication. There is no blanket prohibition on discussing automation with Tanvi, and no file type or row count is inherently prohibited.

**ChatGPT and Claude:** support the owner's investigations by comparing original invoices, complete Zoho records and available bank/card, FX and GST-return evidence; investigate invoice identity, amounts, taxes, missing bills and automation defects; document evidence privately and identify genuine filing questions. AI findings are unverified input until reconciled and reviewed, and do not replace professional judgment or owner approval.

## Evidence and approval controls

- Distinguish tax printed on an invoice, tax booked in Zoho, GSTR-2B appearance, ITC claimed and tax actually paid. An estimate or a missing piece of evidence does not establish unpaid tax or a missing bill.
- A partial export cannot establish absence from the full Zoho ledger. A bank debit establishes an amount paid, not automatically the applicable accounting FX rate.
- Reconcile invoice number, vendor/GSTIN, date, taxable amount, tax components, total, currency and original source. Flag ambiguity instead of inventing values.
- Obtain Sujith's explicit approval before financial entries or corrections, changes to tax treatment or vendor masters, return filings or amendments, IMS actions, production restarts, or external communication. A filing question or audit finding is not approval to act.
- Keep confidential invoices, GSTINs and credentials off GitHub. Use redacted, evidence-backed updates on Issue #12.

## Findings and continuity

Keep filing status, individual corrections, amounts, audit verdicts and production status in dated evidence or issue updates with their source and verification limits. Re-check them before relying on them; they are not permanent workflow rules.

Read this document alongside the existing project instructions when continuing the audit. If Claude updates persistent notes or an existing scheduled task, link to the current document and verify the actual update before claiming it was saved. A GitHub comment alone does not prove that Claude's memory or scheduler changed.
