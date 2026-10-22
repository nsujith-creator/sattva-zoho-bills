# Sattva Zoho Books workflow responsibilities

## Scope and existing instructions

This document records workflow responsibilities. It supplements [AGENTS.md](../AGENTS.md); it does not supersede it or the project's existing master instructions in `C:\temp\zoho\ZOHO_BILLS_AUTOMATION_KNOWLEDGE.md`. Read those instructions before operational work. Preserve established field mappings, duplicate detection, idempotency, source traceability and approval boundaries. Surface any conflict with its source and date rather than silently replacing an established instruction with an assumption.

## Responsibilities

**Sujith:** purchasing, receiving original supplier invoices, entering and reviewing purchase bills in Zoho, banking and payments, supplying bank/FX evidence, and approving accounting changes and return filings. Ownership of these activities does not authorize an AI agent to change records or vendor masters.

**Tanvi:** IFF/GST-return filing using Zoho entries and GST portal records available to her, within the owner's QRMP workflow. Professional sign-off belongs to an appropriately qualified professional; do not infer a CA credential from her role. Supplier-invoice investigation, purchase-bill entry or correction, bank/FX reconstruction and automation diagnosis are not part of her assigned filing remit. Do not assume she has received original invoices or knows about invoices never supplied to her.

Involve Tanvi when there is an identified GST-filing question or need for return/portal evidence. Investigate records already controlled by the owner first. Do not manufacture Tanvi-specific packs, spreadsheets or explanations, or shift internal investigations to her merely because an audit found a discrepancy. Sujith decides what information or documents to share for a genuine filing purpose and authorizes external communication. There is no blanket prohibition on discussing automation with Tanvi, and no file type or row count is inherently prohibited.

## AI responsibilities and execution lanes

**ChatGPT: project execution lead.** Own the working delivery plan, implementation/integration coordination, defect remediation, realistic cloud architecture and budget check, and the Railway-based private invoice-upload/review-only MVP. Account for actual code, test evidence and deployed outcomes, not handoffs or intentions. Independently perform read-only historical Zoho/PDF reconciliation as resources permit. The existing 08:00 audit task is for historical F20 and is **not** a cloud-build task. Do not claim unscheduled background execution or deployed functionality.

**Claude: independent auditor and adversarial reviewer.** Reproduce defects, challenge accounting conclusions, check edge cases and regressions on proposed fixes and cloud release, and continue its own historical invoice investigation. Review the specific implementation delivered by ChatGPT, not a competing product roadmap. Claude usage/scheduler access is independent of ChatGPT; neither can silently launch the other's session.

**Owner (Sujith):** product priorities, business rules, financial and deployment approvals. The owner is not responsible for carrying messages between agents or inventing engineering acceptance criteria.

**Parallel tracks:** (1) contain and reconcile the old Windows/Zoho system and financial exceptions; (2) implement a separate **nonposting** cloud review interface. A full historical audit is not a prerequisite for building or demonstrating a cloud review-only interface, but financial posting must remain disabled until relevant end-to-end safety acceptance, integration checks and explicit owner approval. Never alter the existing Sattva CRM for this initiative without separate authorization.

**Bridge behavior:** GitHub Issue #12 is the asynchronous shared mailbox for the historical F20 investigation. It is **not** an agent-to-agent live connection, scheduler or cloud delivery tracker. Each agent checks new evidence during an actual authorized run, posts only meaningful redacted evidence or a precise blocker, and picks up assigned items when next able to execute. Code delivery and independent acceptance belong in GitHub commits/PRs with reproducible tests, not a chain of circular Issue comments. A scheduled task showing a last-run time is not evidence that useful work was completed; verify artifacts. If the work has not run or no evidence exists, say so explicitly. Neither agent should make the owner mediate this handoff.

Both agents may inspect original PDFs, complete Zoho records and available bank/card, FX and GST-return evidence to check invoice identity, amounts, taxes, missing bills and automation defects. AI findings are unverified input until reconciled and reviewed, and do not replace professional judgment or owner approval.

## Evidence and approval controls

- Distinguish tax printed on an invoice, tax booked in Zoho, GSTR-2B appearance, ITC claimed and tax actually paid. An estimate or a missing piece of evidence does not establish unpaid tax or a missing bill.
- A partial export cannot establish absence from the full Zoho ledger. A bank debit establishes an amount paid, not automatically the applicable accounting FX rate.
- Reconcile invoice number, vendor/GSTIN, date, taxable amount, tax components, total, currency and original source. Flag ambiguity instead of inventing values.
- Obtain Sujith's explicit approval before financial entries or corrections, changes to tax treatment or vendor masters, return filings or amendments, IMS actions, production restarts, or external communication. A filing question or audit finding is not approval to act.
- Keep confidential invoices, GSTINs and credentials off GitHub. Use redacted, evidence-backed updates on Issue #12.

## Findings and continuity

Keep filing status, individual corrections, amounts, audit verdicts and production status in dated evidence or issue updates with their source and verification limits. Re-check them before relying on them; they are not permanent workflow rules.

Read this document alongside the existing project instructions when continuing the audit. If Claude updates persistent notes or an existing scheduled task, link to the current document and verify the actual update before claiming it was saved. A GitHub comment alone does not prove that Claude's memory or scheduler changed.
