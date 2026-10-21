# sattva-zoho-bills
Sattva Global Logistics vendor bill automation, audit and cloud transition

## Audit
- [`audit/2026-10-21/`](audit/2026-10-21/): forensic audit package (verdict FAIL) and [independent review](audit/2026-10-21/CLAUDE_INDEPENDENT_REVIEW.md)
- [`audit/2026-10-21/repro/`](audit/2026-10-21/repro/): offline reproduction of the findings against `src/`, and a read-only Zoho export for the reconciliation

## Approving bills

The watcher never posts. A bill that passes every check is queued in `C:\temp\zoho\approval_queue.json` with the exact payload, status `awaiting_approval`. From `C:\temp\zoho`:

1. Review: `python zoho_bills.py --file "C:\temp\bills\<file>.pdf" --preview`
2. Approve: `python zoho_bills.py --file "C:\temp\bills\<file>.pdf" --approve --approver "Your Name"`

The approve run posts only if it rebuilds the identical payload for the identical PDF. Before posting, it appends approver, time, PDF SHA-256 and payload SHA-256 to `C:\temp\zoho\approvals.json`. Only `verified` (posted and read back) counts as done; `post_verify_failed` means the bill is in Zoho but needs checking.

Before a bill reaches the approval queue:
- **Duplicates are checked per vendor.** The same number from the same vendor is skipped. The same number written differently (`INV-001` vs `INV001`), or held by a bill with no vendor on record, goes to review. Another vendor's bill with the same number is not a duplicate.
- **A PDF that holds several invoices goes to review whole,** with every invoice kept.
- **A vendor with an Indian GSTIN is never treated as a reverse-charge import.** If its invoice looks overseas (USD, an overseas flag, or a vendor-name signal), it goes to review.

Read-back requires the Zoho bill's currency, every line's amount and tax code, and `sub_total` to equal what was posted exactly. `tax_total` and `total` must equal the printed invoice within ₹0.01 per line (paise rounding of computed tax). For reverse-charge bills, `total` must equal the pre-tax payable, and each line's reverse-charge tax must equal 18%.
