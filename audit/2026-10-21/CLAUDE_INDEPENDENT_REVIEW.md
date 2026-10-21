# Independent review of the 21 Oct 2026 forensic audit

**Reviewer:** Claude (Claude Code cloud session), 21 Oct 2026
**Input:** `SATTVA_ZOHO_FORENSIC_AUDIT_2026-10-21.zip` (6 files, committed unmodified alongside this file; hashes in `SHA256SUMS`)
**Verdict:** **FAIL stands.** Not because of the 155/157 count, which proves very little, but because F01 + F02 + F03 mean a bill marked "verified" is not actually verified, and F04 means nothing stopped those bills from being posted.

## 1. What this review could and could not do

The contract (`CLAUDE_INDEPENDENT_AUDIT_CONTRACT.md`) asks for independent reproduction against the live code, PDFs and Zoho. Almost none of that was possible from here:

| Contract item | Status | Reason |
|---|---|---|
| Reproduce all 157 cases against fingerprinted live code | **Not done** | Neither the source nor the harness that produced the 157 cases is in this repo. 155 of 157 rows record no input, expected value or actual value, so they cannot be reproduced even with the source. |
| Inspect F01–F20 in code | **Not done** (reasoned only) | Source not in repo. Line references in the report are unverified. |
| Test against real source PDFs | **Not done** | No PDFs supplied (correctly, per acceptance criterion 8). |
| Read-only Zoho GET | **Not done** | No read-only credential; `www.zohoapis.in` and `accounts.zoho.in` are denied by this environment's network policy. |
| Reassess S1/S2 severity and harm | **Done, provisionally** | Section 4. Based on the report text and domain reasoning. Each line states its basis. |
| Do not post, delete `parsed_cache.json`, expose secrets or mark PASS | **Complied** | Nothing outside this repo was touched. The package contains no tokens, keys or GSTINs (grep-checked). |

## 2. Package integrity (verified here)

- `EXECUTED_EDGE_CASES_157.csv` and `.json` are row-for-row identical: 157 unique IDs X001–X157, 155 PASS, 2 FAIL.
- All 5 entries in `LIVE_SOURCE_FINGERPRINTS.json` are well-formed SHA-256 (uppercase hex, consistent with PowerShell `Get-FileHash`). They can't be checked against anything until the source is pushed.
- The README's counts (157 cases, 155/2, 20 findings) match the files. Auditor split: 9 × S1 (F01–F09), 11 × S2 (F10–F20).

## 3. What the 157-case register actually proves

Very little, and the contract itself says not to accept summary counts as proof.

- **155 of 157 rows have empty `expected` and `actual`.** A row that says PASS with no recorded input or expectation is a label, not evidence.
- **90 rows are opaque numbered variants:** `vendor_match_00..19`, `payload_tax_00..19`, `payload_hsn_00..19`, `overseas_fx_00..09`, `rcm_id_00..09`, `verification_subtotal_00..09`. Another 20 are 10 vendor names × True/False for one overseas-detection function. The 157 cases exercise roughly **9 functions**: validation, overseas detection, invoice-number-from-filename, vendor match, tax payload, HSN payload, FX, RCM and `verify_created_bill`.
- **All 157 are labelled `S1` "if failed"**, so the severity column carries no information.
- **Only X156 and X157 carry data.** Both expect `verify_created_bill` → `False` on a bad bill and got `True`. If the harness is what it claims, these reproduce F01 and F02. They are the only two rows worth anything as evidence.
- *Inference, unconfirmed:* if `verification_subtotal_00..09` assert that small subtotal differences are accepted, they pass **because of** F02 and should start failing once F02 is fixed. Check this when the harness is available.
- The main `zoho_bills.py --test` reports 31 PASS while counting the known failure `watcher_lock_scope` as a PASS. A harness that reports green on a known failure can't be used as a gate (added below as A1).

## 4. Severity reassessment

Basis key: **R** = reasoned from report text and domain knowledge (no code seen), **P** = supported by package data, **Q** = cannot settle without code.

| ID | Auditor | Review | Basis | Reasoning |
|---|---|---|---|---|
| F01 gross payable not compared | S1 | **S1** | P (X156), R | Zoho derives the total from lines, tax_id, RCM flag and adjustments. Skipping the total check means a wrong tax route, wrong rate, missing RCM or bad round-off can all pass verification. |
| F02 subtotal tolerance max(₹1, 1%) | S1 | **S1** | P (X157), R | On a ₹3 lakh subtotal (Maersk-sized), 1% lets roughly ₹3,000 per bill through unflagged. Matching to ₹0.01 is a one-line fix. |
| F03 `created` logged before read-back; watcher checks `created` first | S1 | **S1** | R | A bill that fails verification is treated as done and never returns for review. The retained log already holds 2 `post_verify_failed`. Cheapest S1 to fix. |
| F04 auto-post despite AGENTS.md approval rule | S1 | **S1 (multiplier)** | R, P (log) | This is a control failure, and it amplifies every other S1. The retained log shows 403/500 (81%) already go to `needs_review` and only 60 were `created`, so gating every post costs about 60 extra approvals per 500 bills. Cheap to fix and removes a lot of risk. |
| F05 global invoice-number-only dedupe | S1 | **S1, measurable now** | R, P (log) | A false positive silently drops a real bill: the payable goes unrecorded and the ITC is missed. Short freight invoice numbers collide across vendors. **The log has 35 `skipped_duplicate` entries. Check each one's vendor against the bill it matched.** |
| F06 multi-invoice PDF collapsed to largest | S1 | **S1** | R | Whole bills go missing (unrecorded payable, lost ITC). Detectable from `parsed_cache.json`: look for PDFs where extraction saw more than one invoice. |
| F07 invalid-length HSN/SAC silently dropped | S1 | **S2** | R, Q | A missing HSN on an *inward* bill changes neither the payable nor the tax. HSN summary reporting falls on outward supplies. The real risk is that a malformed code can be a symptom of a misparsed line, so it should route to review instead of being stripped silently. Also check whether valid 4-digit HSNs are being treated as "invalid". |
| F08 FX at processing-day rate / fallback | S1 | **S1** for INR-payable invoices with a printed ex-rate (shipping lines); **S2** for foreign SaaS | R | When an Indian vendor bills in USD and prints its exchange rate and INR payable, that printed INR figure is the amount owed, so any other rate breaks parity. For foreign SaaS under RCM, the rate should be the transaction-date rate per accounting policy. Processing-day drift is usually under 1%, but it is systematic and it also shifts the RCM IGST base. |
| F09 GST: supplier-state routing, 5%/18% only | S1 | **S2, pending Q** | R, Q | *My understanding, confirm with your CA:* (a) For B2B services to a registered recipient, IGST Act s.12(2)(a) puts place of supply at the recipient's location. For most of what a forwarder buys, IGST vs CGST+SGST therefore reduces to "supplier state ≠ Sattva's registered state". The exceptions are immovable-property and hotel services (s.12(3)), restaurant (s.12(4)) and event admission (s.12(6)). (b) Since the rate rationalisation effective 22 Sep 2025, 5% and 18% (plus 40%) are the main slabs, so a 5/18 whitelist fits current bills better than the report implies. The gaps are exempt/nil/0% lines and pre-22-Sep-2025 invoices at 12%/28%. **What sets the severity is whether an unsupported rate routes to review (fail-safe → S3) or gets coerced to 5/18 (→ S1).** |
| F10 watcher self-test fails at preflight | S2 | **S2: the test is right, the code is wrong** | R | The real finding is that the preflight safety gate is disabled. Fix it by restoring the gate, not by editing the assertion. |
| F11 PDF extraction suite skipped | S2 | **S3** as a defect | R | Fixed by installing `poppler-utils`. The evidence gap remains until the suite actually runs. |
| F12 500-entry rolling log | S2 | **S2** | R | It also means **reconciliation has to start from Zoho, not the log**: list the bills created by the integration's API user since go-live. |
| F13 local-only, no cloud/mobile | S2 | **Out of scope** | R | This is a missing feature, not a defect, and the report itself puts it out of scope. It should not count toward the FAIL. |
| F14 POST-timeout replay idempotency | S2 | **S2** | Q | The duplicate-bill path depends on how F05's dedupe check sequences against an uncertain POST. Settle it with the code. |
| F15 watcher reads stale `run_log.json` by filename | S2 | **S2, S1 if confirmed** | Q | If a stale `created` from an earlier run can mark a new, unposted file as done, the bill is silently missed. That is the same harm as F05/F06. |
| F16 all three scheduled tasks Disabled | S2 | **Not a defect: the current safety state** | P (report) | With the system off, no new harm is accruing. **Keep the tasks disabled** until F01–F04 are fixed. |
| F17 parse-cache recovery unproven | S2 | **S2** | R | Agree. |
| F18 LLM extraction has no independent truth anchor | S2 | **S2, root cause of F20** | R | With F01/F02 relaxed, verification compares Zoho against the LLM's output, not against the PDF, and a self-consistent wrong extraction passes. A cheap deterministic anchor: cross-check invoice number, GSTIN and gross total against the `pdftotext` text layer. |
| F19 PDF and Zoho attachment not proved together | S2 | **S2** | R | Agree. |
| F20 no end-to-end PDF → Zoho parity | S2 | **The gate** | R | Not a defect but the outstanding evidence. The system can't move from FAIL without it, whatever else is fixed. |

**Added by this review**

| ID | Severity | Finding |
|---|---|---|
| A1 | S2 | `zoho_bills.py --test` counts the known failure `watcher_lock_scope` as PASS. A green self-test can't be used as a release gate until known failures report as failures. |
| A2 | n/a (evidence quality) | The 157-case register isn't reproducible: 155 rows lack input/expected/actual and the harness isn't in the package. Future registers must record inputs and expectations per row. |
| A3 | n/a (process) | The fingerprints can be checked only on the Windows machine. Until the source is in this repo, nobody else can confirm which code was audited. |

**Net:** 7 S1 (F01–F06, F08 printed-rate case), 1 S1-or-S3 pending code (F09), 3 reclassified as not defects (F13, F16, F20), plus A1–A3.

## 5. Recommended order (cheapest risk reduction first)

1. **Keep all three scheduled tasks disabled** (F16).
2. **Turn off auto-post (F04).** Every bill goes through approval. 81% already do.
3. **Fix verification (F01, F02, F03):** exact subtotal, tax amounts and gross total to ₹0.01; compare the GST route and RCM flag; log `created` only after read-back passes; the watcher treats only `verified` as success.
4. **Read-only reconciliation, Zoho-side:** list every bill created by the integration since go-live, compare each against its source PDF, and start with the 2 `post_verify_failed` and the Maersk `MH27IN1000763792` (₹369,608.63).
5. **Audit the 35 `skipped_duplicate` entries** for cross-vendor collisions, then make dedupe vendor/GSTIN-scoped (F05).
6. **Hold multi-invoice PDFs for review** (F06), and route unsupported GST rates and invalid HSN to review rather than coercing or stripping them (F07, F09).
7. Restore the watcher preflight gate (F10), make known failures fail (A1), and install `poppler-utils` and run the PDF suite (F11).

## 6. What's needed to finish the contract

1. **Push the five fingerprinted files** (`zoho_bills.py`, `watcher.py`, `watchdog.py`, `tax_rules.json`, `vendor_rules.json`), plus `AGENTS.md`, `test/run_tests.py` and the script that generated the 157 cases. Grep the source for hardcoded credentials first. This repo's `.gitattributes` stores these files byte-exact so the SHA-256 values still match after a commit from Windows, and `.gitignore` blocks `config.json`, PDFs and state JSON.
2. **A Zoho read path**, one of:
   - (a) a read-only Zoho Books OAuth client (bills/contacts READ scopes) stored as environment secrets, plus `www.zohoapis.in` and `accounts.zoho.in` added to the environment's allowed domains, or
   - (b) a read-only export script run on the Windows machine, with its output uploaded to the session.
3. **Source PDFs for the posted bills**, uploaded to the session and never committed.
