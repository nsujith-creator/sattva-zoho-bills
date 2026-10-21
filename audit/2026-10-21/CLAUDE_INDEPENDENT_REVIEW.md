# Independent review of the 21 Oct 2026 forensic audit

**Reviewer:** Claude (Claude Code cloud session), 21 Oct 2026. Version 2.
**Inputs:** the auditor's package (unmodified in this folder, hashes in `SHA256SUMS`) and the audited source in `src/` from PR #2 (commit `600f3b6`).
**Verdict:** **FAIL stands, now on executed evidence.** The real pipeline, run offline with Zoho and Claude faked, auto-posts a bill with no approval. It then "verifies" that bill although Zoho's total is ₹6.46 lakh off. Separately, a bill that fails verification is still reported to the watcher as created.

### Changes from version 1

- The source is now in the repo. All five SHA-256 values were recomputed here and match `LIVE_SOURCE_FINGERPRINTS.json`.
- The 157 cases, the built-in suites and a new reproduction script (`repro/reproduce_findings.py`) were all run against `src/`.
- **Corrections to v1:**
  - F08 is not S1 for shipping-line invoices with a printed exchange rate. The extraction prompt keeps those in INR and they are never converted; the real shipping-line risk is A5.
  - F09 drops to S3: the risky cases were executed and go to review.
  - F14 is reframed, and F15 drops to S3.
- **New findings:**
  - A5 (S1): Indian vendors can be auto-posted as reverse-charge imports.
  - A6: a PDF that replaces an older file with the same name is ignored.
  - A7: which Zoho org and credentials the pipeline posts with is decided by another project's `.env` file.
  - A4: every line posts to one hard-coded expense account.
  - A1 is worse than reported.

## 1. Contract status

| Contract item | Status | Notes |
|---|---|---|
| Reproduce all 157 cases against the fingerprinted code | **Done** | 155 PASS / 2 FAIL, identical to the audit. Section 3 covers what those rows do and don't test. |
| Inspect F01–F20 in code | **Done** | Section 4, with `src/` line references. 11 findings were reproduced by running the code. |
| Test against real source PDFs | **Not done** | No PDFs supplied. |
| Read-only Zoho GET | **Not done** | `repro/export_zoho_bills_readonly.py` is ready to run on the Windows machine. This cloud environment can't reach `zohoapis.in`. |
| Reassess S1/S2 severity and harm | **Done** | Section 4. Each line states whether it was reproduced, shown fail-safe, or read from code. |
| Do not post, delete `parsed_cache.json`, expose secrets or mark PASS | **Complied** | Every run was offline: network calls were blocked and state was written to temporary directories. `src/` has no credential literals; the only GSTINs are synthetic ones in tests. |

## 2. Source integrity

- **Hashes:** `zoho_bills.py`, `watcher.py`, `watchdog.py`, `tax_rules.json` and `vendor_rules.json` all match their fingerprints. Line endings are mixed (`zoho_bills.py` and `vendor_rules.json` are CRLF, the rest LF), and `.gitattributes` keeps them byte-exact.
- **The test files don't run from the committed layout:**
  - `tests/REPRODUCE_157_CASES.py` hardcodes `C:\temp\zoho\` and `C:\Users\...` paths.
  - `tests/test_vendor_matching.py` imports `zoho_bills` from the repo root, but the code now lives in `src/`.
  - `tests/run_tests.py` needs `test_invoices/` and `test_expectations.json`, which aren't in the repo.

  All three ran here after path adjustments.

## 3. Test results (Python 3.13, Linux, offline)

| Suite | Result | What it means |
|---|---|---|
| `REPRODUCE_157_CASES.py` (paths adapted) | 155 PASS / 2 FAIL, identical IDs | Reproduced. See below for what the 155 rows prove. |
| `zoho_bills.py --test` | `SUMMARY: PASS 31 tests` | One of the 31 is `watcher_lock_scope`, whose body is just `assert True` (A1). |
| `watcher.py --test` | **FAIL** at the preflight assertion | Reproduces F10. |
| `tests/test_vendor_matching.py` | 16/16 PASS | GSTIN-first vendor matching holds up. |
| `tests/run_tests.py` | Cannot run (fixtures absent) | When `pdftotext` is missing it prints "SKIPPED - SAFE TO START" and exits 0 (F10). |
| `repro/reproduce_findings.py` (new) | 11 defects reproduced, F09 fail-safe | Exits 1 while any defect remains, so it can serve as the acceptance gate once fixes land. |

**What the 157 rows actually test.** The harness is now visible:
- **About 86 distinct inputs, not 157.** `payload_tax_00..19` repeats a single route. `vendor_match_00..19` covers 10 inputs twice each. `fx`, `rcm` and `verification_subtotal` loop over 2 inputs each.
- **It doesn't test the production config.** It replaces the live tax configuration before running: overseas signals cut to 5, fake tax IDs, no IGST vendor list, no aliases.
- **At least 26 PASS rows encode the behaviour this audit flags as defective:**
  - 5 `verification_subtotal` rows require verification to accept a ₹0.50 subtotal and ₹18.50 total discrepancy (F01/F02).
  - 10 `payload_hsn` rows require a 7-digit HSN to be dropped (F07).
  - 10 `overseas` rows require a vendor-name match to override an AI "domestic" flag on an INR invoice (A5).
  - `validate_INR_line_on_USD` requires a USD invoice with an INR line to be accepted.

## 4. Findings verified against `src/`

**Evidence key:**
- **RUN:** reproduced by executing the code (`repro/reproduce_findings.py`; *e2e* means the real `zoho_bills.main()` and, where noted, `watcher.run_for_file()`).
- **SAFE:** executed and shown to route to review.
- **CODE:** read from source, not executed.

| ID | Audit | Review | Evidence | What the code does |
|---|---|---|---|---|
| F01 | S1 | **S1** | RUN e2e | `verify_created_bill` reads Zoho `total` (`zoho_bills.py:1060`) and never compares it. In the e2e run, Zoho total ₹9,99,999 against printed ₹3,54,000 still gives `POST_VERIFY_PASSED`. Domestic taxes *are* reconciled exactly before posting (`1596–1651`), so the unchecked risk sits on the Zoho side: a wrong tax-ID mapping in Zoho, tax-inclusive settings, or rounding. None of these is read back. |
| F02 | S1 | **S1** | RUN e2e | `tolerance = max(1.0, expected_subtotal * 0.01)` (`1062`). A ₹2,900 subtotal error on a ₹3,00,000 bill passes. |
| F03 | S1 | **S1** | RUN e2e + watcher | `created` is logged (`2329`) before verification (`2330`), and `run_log["created"]` keeps the bill either way. The watcher returns `created` (`watcher.py:706`), and `should_mark_seen("created")` is True. Startup reconciliation skips any file whose latest status isn't `created` (`watcher.py:596`), so a `post_verify_failed` file stays marked done forever. |
| F04 | S1 | **S1** | RUN e2e | Only GSTIN-tier vendor matches and AI-vs-keyword tax-rate disagreements stop for approval (`2297–2313`). Exact or alias matches POST straight away. This contradicts `AGENTS.md`: "Human approval and reconciliation are mandatory before posting." |
| F05 | S1 | **S1** | RUN e2e + watcher | `fetch_existing_bills` keeps bill numbers only, with no vendor (`1104–1117`). The check runs before vendor matching (`2164`). Another vendor's `INV-1001` makes this bill `skipped_duplicate`, and the watcher marks it done. Formatting variants (`INV-001` vs `INV001`) also slip past, so duplicates can happen the other way too. |
| F06 | S1 | **S1** | RUN | A multi-invoice PDF keeps only the invoice with the highest line total (`1427–1444`). The result is cached by file hash (`2113`), so the dropped invoices never return, and the only trace is a stdout `PARSE_MULTI_INVOICE` line. The code comment cites Blinkit product plus platform-fee invoices. |
| F07 | S1 | **S2** | RUN | Invalid HSN/SAC is dropped (`1667–1672`). New detail: the code truncates to 8 digits *before* the length check, so a 10-digit code posts as a wrong 8-digit code. The amounts payable don't change. |
| F08 | S1 | **S2** (corrects v1) | RUN | Exchange-rate conversion applies only to bills classified overseas and in USD (`1663`). The watcher never passes `--use-invoice-date-fx` (`watcher.py:678`), so these bills use the live processing-day rate. v1's S1 for printed-ROE shipping-line invoices was wrong: prompt rules 1–5 keep those in INR. |
| F09 | S1 | **S3** | SAFE e2e | A 12% line raises `line_tax_percentage_unsupported` and goes to review (`1591`, `2285`). An intra-state GSTIN with printed IGST fails the reconciliation, which matches every tax component to the paisa (`1636–1647`), and also goes to review. Intra/inter-state comes from the GSTIN prefix `27` (`1542`), which is right for B2B services to a Maharashtra registration. *Rate and place-of-supply conclusions are my reading of GST law; confirm with your CA.* |
| F10 | S2 | **S2** | RUN | `enforce_test_gate()` is commented out in the watcher's entry point (`watcher.py:1383`, "was spawning visible startup subprocesses"), yet the call already uses `CREATE_NO_WINDOW` (`watcher.py:105`). Re-enabling it alone isn't enough: the gate passes with `pdftotext` missing, and with zero fixtures. |
| F11 | S2 | **S2** | CODE | `pdftotext` isn't only for tests. Production uses it for the MBL regex (`2120`) and the CMA CGM invoice-number override (`2141`). Without it, `extract_text_from_pdf` falls back to a raw-bytes regex that finds little in compressed PDFs, so both deterministic checks go quiet and the AI's values stand. **Check `where pdftotext` on the Windows machine.** |
| F12 | S2 | **S2** | CODE | `MAX_LOG_ENTRIES = 500` (`101`, `503`). |
| F13 | S2 | Out of scope | — | Missing feature, not a defect. |
| F14 | S2 | **S2, reframed** | CODE | A replayed POST is blocked on the next run by the bill-number set. The real gap is different: a POST that times out after Zoho created the bill is logged `failed`, retried, then `skipped_duplicate`. The bill exists, was never verified, and is labelled skipped. |
| F15 | S2 | **S3** | CODE | `run_log.json` is matched by filename with no run_id check (`watcher.py:699–721`). A stale read needs the subprocess to exit without writing `run_log`. Every path I traced ends in `failed` or `needs_review`, not a false `created`. |
| F16 | S2 | Not a defect | — | Runtime state, and currently the safety net. |
| F17 | S2 | **S2** | CODE | If `parsed_cache` and `seen_files` are both lost, every PDF is re-parsed. Non-deterministic AI output plus exact-string dedupe can then produce duplicate bills. |
| F18 | S2 | **S2** | CODE | The domestic path checks the AI's arithmetic exactly, which is stronger than the report implies. The overseas path reconciles nothing (`1599–1600`). Neither compares against the PDF. |
| F19 | S2 | **S2** | CODE | No attachment upload anywhere in `zoho_bills.py`. |
| F20 | S2 | **The gate** | — | Still open. See section 6. |

**Added by this review**

| ID | Severity | Evidence | Finding |
|---|---|---|---|
| A1 | S2 | RUN | The "known open" `watcher_lock_scope` self-test is `assert True` (`3017–3018`) and counts toward `PASS 31`. |
| A2 | Evidence quality | RUN | The 157-row register: about 86 distinct inputs, replaced config, and at least 26 rows that assert the flagged behaviour (section 3). |
| A3 | Resolved | — | The source is now in the repo (PR #2). |
| A4 | S2, confirm intent | RUN | Every line posts to `account_id` `914109000000000567` (`1666`), and the AI's `account_name` is ignored. Unless that account is a deliberate catch-all, SaaS, clearance and freight costs all land in one P&L head. |
| A5 | **S1 (new)** | RUN e2e | Overseas status ignores the vendor's GSTIN (`1484–1506`). Any of three things marks a bill overseas: a non-INR currency, an AI `is_overseas=true`, or a vendor name containing a signal substring (`AWS` matches `DAWSON`). The bill is then auto-posted as a reverse-charge (RCM) import with an exchange-rate conversion whenever no supplier GST is printed. In the e2e run, an Indian-GSTIN vendor's USD invoice, which the AI called domestic, was posted with RCM at 18% IGST on a ₹83,000 base. The only guard is that a printed supplier GST sends the bill to review. |
| A6 | S2 (new) | CODE | The watcher tracks files by name (`watcher.py:813`). A new PDF saved over an older one with the same name is never processed. |
| A7 | **S1 if ever triggered, else S2 (new)** | CODE | Posting credentials and org come from `C:\sattva\books_recon\.env` (`590–675`). Any `BOOKS_RECON_ZOHO_SANDBOX_*` key makes the pipeline post with sandbox credentials and org. Any `..._READ_*` key makes it post with the "read" client. The access token is saved to `config.json` and reused by any run within 50 minutes, whichever credentials created it (`760–816`). **Bills logged `created` may not be in the production org.** Check now: does that `.env` define SANDBOX or READ keys, and what `source_module=` do the `[zoho-auth]` lines in `watcher.log` show? |

**Net:**
- **S1 (7):** F01–F06 and A5, plus A7 if the sandbox override was ever active.
- **S2 (12):** F07, F08, F10, F11, F12, F14, F17, F18, F19, A1, A4 and A6.
- **S3 (2):** F09 and F15.
- **Not defects (3):** F13, F16 and F20.

**What holds up well:** the domestic route.
- Intra/inter-state is set from the GSTIN.
- Line, tax and total are reconciled to the paisa, and unsupported rates or a mismatched tax split go to review.
- Vendor matching never falls back to a conflicting name when the invoice carries a GSTIN (`1868`).
- Low-confidence parses, ambiguous overseas status and overseas vendors charging Indian GST all go to review.

## 5. Fix order (cheapest risk reduction first)

1. **Keep the scheduled tasks disabled. Check A7 today:** look in the `.env` without sharing the values, and grep `watcher.log`.
2. **F04:** require approval for every bill. Remove the exact/alias auto-post at `2297`, and record approver, timestamp and PDF hash.
3. **F03:** log `created` and append to `run_log["created"]` only after verification passes. Add a `verified` status, and make the watcher treat only `verified` as done (`watcher.py:341`, `706`).
4. **F01/F02:** compare `total`, `tax_total`, `sub_total` and each line's `tax_id` and rate to ₹0.01. Allow ₹1 only for an explicit round-off line. Remove the 1% tolerance at `1062`.
5. **A5:** a vendor with an Indian GSTIN is never overseas, and non-INR currency on such a vendor goes to review. Replace substring signals with exact vendor IDs.
6. **F05:** dedupe on `(vendor_id, normalised bill number)`. Keep `vendor_id` in `fetch_existing_bills`, and run the check after vendor matching.
7. **F06:** send multi-invoice PDFs to review and cache every invoice.
8. **F07:** send invalid HSN/SAC to review, and remove the `[:8]` truncation.
9. **F10/F11:** install poppler on the Windows machine. Re-enable `enforce_test_gate()`, and make a missing `pdftotext` or missing fixtures a failure.
10. **F08:** have the watcher pass `--use-invoice-date-fx`, and send the bill to review on a rate-fetch failure instead of falling back.

`repro/reproduce_findings.py` asserts the correct behaviour for each of these, so it goes green as they land.

## 6. Remaining blocker: reconciliation (F20)

`repro/export_zoho_bills_readonly.py` runs on the Windows machine. It uses `zoho_bills.py`'s own auth with `zoho_post` disabled, and writes one JSON file containing:

- **Auth and org:** which credentials and org it read from (checks A7).
- **Matched entries:** every `created`, `post_verify_failed`, `failed` and `skipped_duplicate` entry in `invoice_log.json`, matched to the Zoho bills with the same number. This checks F05 vendor collisions and F14 timeouts.
- **Bill detail:** trimmed details for each matched bill (line taxes, RCM, `documents`).
- **PDFs needed:** the list of source PDFs to upload.

Upload that JSON and those PDFs to the review session, without committing either. Every posted bill then gets a field-by-field comparison against its PDF. One limit remains: `invoice_log.json` retains only 500 entries (F12), so bills older than that window need a Zoho-side date-range pass.

## 7. Updates after version 2

- **A7 is not active.** Checked on the Windows machine on 21 Oct:
  - `C:\sattva\books_recon\.env` does not exist, and no override variables are set in the process, user or machine environment.
  - Of 3,907 authentication log entries, 3,881 use the legacy config path and 26 an older inline loader aligned with it.

  A7 is now **S3, latent**: a future `.env` with SANDBOX or READ keys would still redirect posting silently.
- **F11 is confirmed in production.** `pdftotext` is not installed on the Windows machine, so the MBL regex and the CMA CGM invoice-number override have been inert.
- **The read-only export ran** (exit 0, legacy config):
  - 1,160 Zoho bills listed.
  - 97 invoice_log entries checked, all matched to a Zoho bill by number.
  - 80 bill details fetched, with 0 errors.
  - All 60 PDFs it lists are present in `C:\temp\bills`.

  Matching is by number only, so PDF-to-Zoho parity (F20) is still open.
- **F03 and F04 are fixed.**
  - Posting now requires a recorded approval bound to the reviewed payload.
  - `verified` is the only success status.

  `repro/reproduce_findings.py` reports both as FAIL-SAFE, and `tests/test_approval_boundary.py` covers the AGENTS.md cases.
- **F01 and F02 are fixed.**
  - Read-back now compares currency, each line's amount and tax code, and `sub_total` exactly against the posted payload.
  - It compares `tax_total` and `total` against the printed invoice within ₹0.01 per line.
  - For reverse-charge bills, it checks `total` against the pre-tax payable and each line's 18% reverse-charge amount.
  - The old `max(₹1, 1%)` tolerance is gone. `repro/reproduce_findings.py` reports both as FAIL-SAFE.
- **The RCM total assumption is confirmed.** All 16 reverse-charge bills in the read-only export have `total = sub_total`.
- **A5, F05 and F06 are fixed.**
  - **A5:** an Indian GSTIN on the vendor master or the invoice blocks reverse charge (review instead), and vendor-name signals match whole words only.
  - **F05:** duplicates are keyed by vendor and bill number. A format variant, or a bill with no vendor on record, goes to review.
  - **F06:** a multi-invoice PDF is held for review with every invoice kept, including in the parse cache.

  `repro/reproduce_findings.py` now reports A5, F01–F06 and F09 as FAIL-SAFE.
- **Still open: 25 `skipped_duplicate` entries whose PDF total did not match Zoho.** Under the old global dedupe, each one's Zoho "match" may belong to another vendor. Any whose Zoho vendor differs from the PDF's supplier is a bill that was never entered.

