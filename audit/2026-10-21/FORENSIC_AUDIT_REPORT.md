# Sattva Global Logistics Zoho vendor-bill forensic audit

**Inspection date:** 21 October 2026  
**Verdict:** **FAIL** for a clean accounting-accuracy certification. **Completed:** read-only source inspection, live file fingerprinting, offline built-in suites, 157-case synthetic exercise, log/status review and private GitHub repository creation. **Not completed:** full live Zoho/PDF parity or cloud deployment. No real bill has been created or changed by this audit.

## Actual evidence and test results

The live Windows source `C:\temp\zoho\zoho_bills.py` has **3,128 lines** and SHA-256 `DB420557D29C27D7F865B49F2ABFA19D0D0C1F7BB172D5207B5D4ACA4986C578`. `watcher.py` has 1,387 lines. This differs from the September 2,574-line source. All five fingerprints are in `LIVE_SOURCE_FINGERPRINTS.json`.

- Supplementary **157 scenarios executed against imported live code**: **155 PASS and 2 FAIL**; test result file contains IDs, the named input scenario and outcome. This is synthetic component testing, not production PDF or Zoho API reconciliation.
- Main `zoho_bills.py --test`: reports **31 PASS, zero failures**. A named known-failure case `watcher_lock_scope` is counted as PASS by its own harness, so the printed result overstates independent confidence in that check.
- `watcher.py --test`: **FAIL** on `AssertionError` around line **1326**. Preflight test expects a subprocess, but the preflight gate is disabled.
- `test/run_tests.py`: **SKIPPED**, because `pdftotext` is not installed. Its output says it is safe to start but its PDF extraction tests did not run.
- Python 3.12 AST source parse: main, watcher and watchdog **PASS**.
- `invoice_log.json` has exactly **500 retained entries**: **403 needs_review**, **60 created**, **35 skipped_duplicate**, **2 post_verify_failed**. These are statuses within a capped rolling log, not 500 distinct invoices or all historical transactions.
- `seen_files.json` had 612 entries, `parsed_cache.json` 616, and `pending_files.json` 16 at inspection. They were not deleted or reset.
- Windows scheduled tasks `Zoho Bills Watchdog`, `ZohoBillsWatcher`, `ZohoWatcher` were all **Disabled** at inspection.
- `C:\temp\zoho` is **not** an initialized Git repository. A separate **private** repository `https://github.com/nsujith-creator/sattva-zoho-bills` was created; sanitized source-history migration has not been verified.

## Findings register

| ID | Severity | Evidence class | Finding | Reference |
|---|---|---|---|---|
| F01 | S1 | Reproduced synthetic + code-supported | Final payable is not compared with printed gross total. Zoho `total` can differ wildly while `verify_created_bill` returns success. | Source lines 925-1094, test X156 |
| F02 | S1 | Reproduced synthetic + code-supported | Subtotal comparison uses `max(1 INR, 1% of expected subtotal)` tolerance, which accepts real monetary discrepancies. | Source lines 1062-1088, test X157 |
| F03 | S1 | Code-supported | Bill is logged `created` **before** read-back validation; `post_verify_failed` can coexist with `created`, and watcher checks `created` first. | Main 2322-2342; watcher 698-738 |
| F04 | S1 | Code-supported | `AGENTS.md` requires human approval before every post, but the live main loop auto-posts exact/alias matched bills. | Main 2297-2322 |
| F05 | S1 | Code-supported | Global invoice number-only duplicate check can suppress different vendors using same invoice number. | Main 1104-1118, 2164-2171 |
| F06 | S1 | Code-supported | Multi-invoice PDFs are collapsed to the one highest-value invoice; remaining invoices are ignored. | Main 1425-1447 |
| F07 | S1 | Code-supported | Invalid-length HSN/SAC codes are silently removed instead of triggering a review. | Main 1654-1700 |
| F08 | S1 | Code-supported | Historical foreign-currency bills can use processing-day USD rate or fall back to it if invoice-date FX lookup fails, rather than matching supplier-specific exchange basis. | Main 831-882, 2265-2296 |
| F09 | S1 | Code-supported | GST rules assume supplier GSTIN state code implies place of supply and only accept 5%/18% domestic tax rates; this is not general tax correctness. | Main 1508-1652 |
| F10 | S2 | Observed | Watcher self-test fails at its preflight assertion. | `watcher.py` 1326 |
| F11 | S2 | Observed | PDF MBL extraction test suite skipped because `pdftotext` unavailable. | `test/run_tests.py` execution |
| F12 | S2 | Observed + code-supported | 500-entry retained logs overwrite older evidence; lifetime result reconciliation cannot rely on the current JSON file. | State records, `_append_json_list` |
| F13 | S2 | Code-supported | Local-only folder processing does not satisfy private mobile cloud upload or laptop-loss resilience. | Main and watcher path constants |
| F14 | S2 | Code-supported | Uncertain POST timeout and replay path does not demonstrate atomic vendor-scoped idempotency. | Main `zoho_post`, watcher retries |
| F15 | S2 | Code-supported | Watcher result attribution reads shared `run_log.json` by filename, without matching current run_id, risking stale result acceptance. | Watcher 698-739 |
| F16 | S2 | Observed | All three installed Windows watcher scheduled tasks are Disabled. | Read-only Windows task inspection |
| F17 | S2 | Code-supported | Recovery from corrupted/missing parse-cache state needs backup/restore proof before trusting resumption. | `parsed_cache.json`, source cache helpers |
| F18 | S2 | Code-supported | Core classification and reconstruction rely on LLM output with limited independent PDF truth verification; arithmetic agreement is not source accuracy. | Main parser and payload reconciliation |
| F19 | S2 | Code-supported | Source invoice and Zoho attachments are not proved persisted together, so recovery traceability is incomplete. | Main posting path |
| F20 | S2 | Unverified | No independently observed end-to-end original PDF to Zoho GET parity was executed for every posted historical bill. | Verification gap |

**Severity meaning:** S1 = plausible materially incorrect financial record; S2 = substantial operational/recovery failure; S3 = limited friction. In the 157-case register, S1 means *impact if that assertion fails*, not 157 confirmed high-severity defects. Source-supported risks are not labeled production-observed without live comparison.

## Expected and actual journeys

**Expected:** upload one private PDF, preserve original/hash, identify all invoices including multi-invoice PDFs, extract and reconcile every taxable line and grand total, uniquely resolve supplier/GSTIN, require named human approval, prevent duplicate posting across retries, create the bill once, verify every Zoho field including gross payable and RCM/attachment, persist immutable evidence and meaningful status, and recover after laptop loss.

**Observed from source:** local Windows watcher polls `C:\temp\bills`, launches Claude extraction/cache, vendor/tax routing, partially gates review, may POST without approval, verifies selected Zoho fields (but not final payable), writes bounded logs, and reports a created result before verification is guaranteed.

## Acceptance criteria and priority

1. **Block live POST** until source PDF identity, vendor/GSTIN, invoice number/date, all line items, tax components and gross total reconcile; approval identity, timestamp and PDF digest recorded.
2. Correct the verification function to demand exact printed document-level payable to acceptable minor currency rounding, compare tax amounts and GST route and RCM, check source attachment and ensure only `verified` is a success status. Simulate Zoho GET discrepancy; test failures must be honest failures.
3. Make duplicate identity vendor/GSTIN scoped and durable across retries and uncertain POST responses. Test two distinct vendors both using `INV001` and repeated same-vendor PDF.
4. Preserve *all* invoices in multi-invoice PDFs or hold entire input for explicit splitting, not silently choose one.
5. Replace unsupported GST and HSN shortcuts with review blocks; verify 0%, 5%, 12%, 18%, 28%, exempt and mixed tax only where actually supported by current law and Zoho configuration. Do not assume one rule universally.
6. Fix watcher test failure, run PDF extraction harness with working text dependency, and test four simultaneous upload/kill/restart/retry races in an isolated sandbox.
7. Reconcile existing Zoho vendor bills read-only with source PDFs, especially the two retained `post_verify_failed`, every retained `created`, and recent Maersk example `MH27IN1000763792.PDF` (₹369,608.63 payable). A live GET comparison is **required and still outstanding**.
8. Establish sanitized source history in the private repo only after secret scanning and credential rotation review. Never upload `config.json`, tokens, raw invoice PDFs or local cache/log JSON.
9. Cloud migration and mobile single-picker user flow remain **out of scope for this source audit** and **not built**. Require private storage, authenticated roles, backups, restore drill, rate limits and zero unsafe auto-posts before production launch.

## Explicit evidence boundaries

This report is an honest **audit FAIL verdict** identifying real observed test failures and source-supported accounting vulnerabilities. It is *not* proof that every posted record is wrong, nor a certification that all live bills are correct. No live Zoho writes were performed. No unattended destructive changes were made. Claude has not been engaged.
