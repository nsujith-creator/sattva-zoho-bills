# PR28 pre-deployment audit, 29 October 2026

Auditor: Claude Code cloud session, independent of the implementer and of the Codex reviewer. Audited exact candidate `0a182105278a7968d5c454ab7b745aaadae2b3d3`, fetched from `refs/pull/28/head`, against base `f5274d2ab1c3efa12f364f5ea8518cdee3570fd8`. Both matched the expected values. No deployment, merge, restart, provider call, Zoho read or write, or credential access was performed. Live checks were anonymous GET requests only.

## Verdicts

**Is this exact candidate source-ready for the approved deployment?** Yes, conditionally. There is one small release blocker (B1), which should be fixed before NEW-bill posting is enabled. There are also two deployment preconditions (P1, P2) that the deployer must check. I found no defect in the PR28 changes themselves that could create a wrong bill or a duplicate bill.

**Has the live upload-to-verified-Zoho journey been proved?** No. PR28 is undeployed. The last live acceptance (PR27, deployment `7f012598`) held both originals, and posting remained off. No genuine NEW bill has ever been created, read back or attachment-verified by the cloud. No upload-to-result time has been measured.

## Evidence examined and how

| Item | Status |
|---|---|
| PR28 head/base, 8 changed files | Verified (git). `src/`, `app.py`, `auth.py`, `extraction.py`, `review.py`, `container_start.py`, `Dockerfile` and `.dockerignore` are byte-identical to the base. The model, prompt and budget constants are untouched. |
| GitHub PR28 state | Verified: open, mergeable `clean`, 0 review threads, Codex bot review "no findings" on `0a18210`, no CI checks configured. |
| Full cloud suite | **Run here: 156/156 passed, twice**. Linux, Python 3.13, real `pdftotext`/`pdfinfo`, `env -i` with only a dummy key, non-loopback sockets blocked. |
| Core suite | Run here: 60 passed, 2 skipped (Windows-only lock tests). |
| Implementer's "156/156 / 83 focused" result, reviewer's "94 + 18" result, earlier WinError10053 runs | Claims; not observed directly. My Linux runs did not reproduce WinError10053. That supports, but does not prove, the "Windows loopback transport" explanation. Earlier failures stay on record. |
| Notion Control Centre, project record and decision log through event `039` | Read in full. The owner's later approvals (conditional NEW posting; the 29 Oct duplicate-only penny rule; PR28 release after this audit) supersede older per-invoice wording in `AGENTS.md` for this journey only. |
| `C:\temp\zoho\AGENTS.md`, `ZOHO_BILLS_AUTOMATION_KNOWLEDGE.md`, private originals and readings | **Inaccessible**: laptop-only. |
| Railway runtime, volume journals, AI ledger | **Inaccessible**: no Railway connector in this session. The "4 calls / USD 0.047410" figure is the implementer's report. |
| Live site (15:04 UTC) | Anonymous GETs: `/healthz` 200 `ok`; `/` 303 to `/login`; HSTS and `no-store` present. The running release identity is not observable this way. |
| Zoho API documentation | Inaccessible (network policy). The `is_item_level_tax_calc` contract is therefore taken from the implementer's report of the Zoho docs. |

## Findings

Probe IDs refer to `repro/test_pr28_audit_probes.py`. All 10 probes pass reproducibly at `0a18210`; output is in `repro/run_output.txt`.

### Release blocker

**B1 — A printed due date that both readings omit still posts as "Verified".** Medium severity. Pre-existing: the PR27 base behaves identically.
- **Where:** `automation.py:192-198` checks a due date only when the readings supply one. When both return `null`, nothing compares the printed `Due Date:` / `Payable by` row.
- **Trigger:** probe F1 adds `Due Date: 2026-11-27` to the source text, and both fake readings leave `due_date` null.
- **Observed:** status `verified`, one create, no `due_date` in the payload. Zoho then applies the vendor's default terms.
- **Business impact:** the saved payable date contradicts the original while the page says "Printed invoice fields … verified". That breaks the owner's accuracy definition (original = payload = saved fields).
- **Smallest fix:** in `corroborate_source`, if any labelled due-date row exists and `due_date` is None, raise `Hold`. Reuse the existing `due_rows` pattern. Add a one-case test.

### Deployment preconditions

**P1 — A hold on the retained readings takes the whole owner inbox offline, not just posting.** Medium severity (operational).
- **Where:** `owner_start.py:385-386`. `main()` calls `retained_case_jobs` and then `accepted_receipt` before `app.main()`, with no guard. Any reconciliation hold on the two retained readings becomes an uncaught `RuntimeError`, the process exits, and the platform restart-loops.
- **Probe F3:** the real function raises `RuntimeError('Retained source readings are incomplete or unsupported; no worker started')`, and nothing catches it before the server starts.
- **Why it matters:** this is fail-closed, as the owner asked. But under PR27 an acceptance hold only kept posting off. PR28 adds new parsing (`cma_printed_charge_taxes`) whose behaviour on the real CMA and Maersk text has not been shown against the retained readings. If the real layout doesn't match the pattern, the phone inbox goes down.
- **Required before switching:**
  - Run `reconcile_readings` and `validate(check_zoho_rounding=False)` on the four retained readings and the two private PDFs, locally, with posting off. This needs no inference and no Zoho write.
  - Keep `7f012598` ready as the rollback target.
  - Optional code alternative: catch this in `main()`, record `all_pass:false` and serve the inbox with posting held.

**P2 — Jobs validated before the upgrade are held as "changed".** Low severity.
- **Where:** `automation.py:1032`. PR28 adds `is_item_level_tax_calc` to every domestic payload. Any job PR27 saved as `validated` (posting off) carries the old payload.
- **Probe F2:** after posting is enabled, such a job becomes `needs_review` with "Supplier or proposed bill changed after the previous attempt." Zero creates.
- **Impact:** safe, but a legitimate pre-release upload is stuck, and re-uploading the same PDF only reopens it. Notion `035` recorded no such jobs (only the two originals), but that is now hours old.
- **Required:** confirm zero `validated` jobs on the volume before deploying, or compare payloads with the new key ignored when the stored payload lacks it.

### Nonblocking observations

**O1 — A "CR"/"DR" suffix on a CMA tax row is read as a positive amount.** Low severity.
- **Where:** `automation.py:587-591` detects a leading `-`/`+`/`−`, brackets and a trailing `-`, but not a trailing `CR`/`DR`.
- **Probe F4:** `9.00 CR` is accepted as `9.00`.
- **Risk today:** header arithmetic and `amount()` (which rejects negatives) would likely stop a real credit downstream. Still, add `\s*(?:CR|DR)\b` to the signed-format hold.

**O2 — NEW bills request Zoho's bill-level ("group") tax calculation by default.** Hardening.
- **Probe F5:** `is_item_level_tax_calc` is `False` for ordinary NEW invoices, and also for a 100.05 + 100.07 invoice whose rounding residues cancel.
- **Why it matters:** strict NEW validation already requires the printed GST heads to equal per-line arithmetic, so item mode is always proven for a NEW bill. Group mode adds a dependency on Zoho's undocumented per-line allocation. If Zoho returns different `line_item_taxes`, a real bill is created and then held, without its attachment.
- **Fix:** set item mode for every NEW bill (one line in the NEW branch). Leave duplicate handling unchanged.

**O3 — An omitted BL/reference also passes when both readings leave `mbl_number` empty** (`automation.py:327-331`). This is a non-financial reference field. Hold only if the owner wants it matched.

### Confirmed safeguards (all probes pass at `0a18210`)

- **Source truth:**
  - Consistent misreads of invoice number, buyer GSTIN or invoice date in both readings are held by PDF-text corroboration (C1).
  - Raw readings stay byte-identical. CMA corrections are only taken from a unique row matching both SAC and base, with no reuse of a tax row across charges.
  - Signed and bracketed amounts, repeated rows and empty or invalid charge lists are held (candidate tests, re-run here).
  - Name normalisation only strips a trailing `C/O CCAI`, and only when both readings share the same printed supplier GSTIN.
- **Rounding:**
  - The duplicate penny rule passes the owner's same-head/rate net-zero case, including a within-head swap.
  - It fails a genuine cross-head offset, a 2-paise difference, empty, partial or absent line-tax arrays, and the same offsets on a NEW bill (C5).
  - Cross-rate cancellation selects item mode, and a NEW group-rounded invoice without per-line proof is held before creation (candidate tests).
- **Exactly one bill:**
  - A container killed during an unanswered create, then restarted and retried by the owner, produces exactly one POST; recovery only looks up (C2).
  - A reused number with a different date is not called a duplicate (C3).
  - The Meta `FBADS`/`ADS` alias is found through the core `_bill_key` (C4).
  - "Verified" requires an exact read-back plus the attachment hash.
- **Startup:** retained acceptance re-derives evidence before the worker starts, makes an fsynced O_EXCL backup of the prior jobs and receipt, forces posting off and makes no model call.

## Operational continuity (owner decision, not a code defect)

- **Service window:** the approved window ends **4 Nov 2026 12:28:59 IST** (epoch 1793775539). After that, `prepare_owner` refuses to start, so bill flow stops.
- **AI allowance:** at the reported USD 0.047410 for 4 calls (about USD 0.012 per call), the remaining USD 0.50 covers roughly **17 more two-reading invoices**. This is an estimate, and larger PDFs cost more. After that, every upload holds.
- **Continuing:** either limit needs a new owner-approved numerical proposal.

## What the approved deployment must observe afterwards

1. **Startup proof:**
   - Release `0a182105…` with these image hashes:
     - `automation.py` `c2207174df3a1ea09390e0297deaa7e9e5e33d916cc6154b1d09a02587d6a02b`
     - `owner_start.py` `59b63dc1b810b969a36478baf7c59f0d06cf8bd2dc009314ccd329fbf93f3874`
     - `zoho_core.py` `0ee596104519552698e2d22c453500433393e03a3b554f3454a91f1f17079a9c`
     - `app.py` `8bdf7501…`, `auth.py` `9ecd4df8…`, `extraction.py` `b57a430e…`, `review.py` `1d92710e…` and `container_start.py` `de8c47fd…`, all unchanged from the base.
   - A new `pre-owner-launch-0a182105278a.sqlite3` backup that passes its integrity check.
   - A new `acceptance-jobs-before-0a18210….json`.
2. **Original acceptance:**
   - Both retained originals end `duplicate`, with `all_pass:true`.
   - Each passes its full existing-record before/after comparison.
   - The AI ledger still reads `calls_reserved:4`, so zero extra inference.
   - `create_attempted` stays false.
3. **First genuine qualifying NEW upload:**
   - Exactly one Zoho bill.
   - Field-by-field reconciliation of original, payload and saved record.
   - Downloaded attachment SHA-256 equal to the upload's.
   - Measured upload-to-`Verified` time.

   Until then, the journey is unproved.

   Do not create a fictional payable or rerun paid extraction to manufacture a pass.
