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

Read-back requires the Zoho bill's currency, every line's amount and tax code, and `sub_total` to equal what was posted exactly. `tax_total` and `total` must equal the printed invoice within ₹0.01 per line (paise rounding of computed tax). For reverse-charge bills, `total` must equal the pre-tax payable, and each line's reverse-charge tax must equal 18%.
