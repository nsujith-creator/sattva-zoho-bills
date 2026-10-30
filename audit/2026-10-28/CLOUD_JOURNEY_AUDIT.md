# Cloud bill-automation journey audit, 28 October 2026

Auditor: Claude Code (cloud session), independent of the implementer. Audited release: PR #24 head `8ef5c7a1709c2d09a5c1e73792ed8365bfab1886` (verified by fetching `refs/pull/24/head`; PR open, unmerged; `main` is `b8ec1f4`). No deployment, merge, provider call, Zoho write, credential access or financial-record change was made by this audit. The probe tests use only synthetic data and fake providers, and they block network access.

## Verdict

**Question: can the owner currently upload a qualifying new invoice from a phone and have it correctly posted and verified in Zoho without further intervention? No.**

- No genuine new bill has ever been created by the cloud release. Financial creation is held off, and the live acceptance step is still pending.
- The deployed code has confirmed defects in duplicate detection, supplier-registration evidence, post-creation recovery and continuity, as listed below.
- The service's approved lifetime ends 4 November 2026 at 12:28:59 IST. If the configured AI allowance is the approved USD 0.50 (Notion event 021; the runtime value was not readable), it covers roughly 4–7 invoices.

Approval basis: per-invoice human approval is not a gate for this journey. The owner explicitly authorised conditional automatic creation of qualifying NEW cloud uploads after acceptance (Notion events 019 and 021; PR #24's `AGENTS.md` amendment, not yet on `main`). The Windows workflow keeps its approval step. This audit preserves that scope; it does not reinterpret it.

## Access limits of this audit

| Source | Result |
|---|---|
| GitHub repo, PR #24, review threads | Inspected directly |
| Notion Control Centre, shared instructions, project record, decision log | Fetched 2026-10-28 ~11:05 UTC and read in full |
| Live site `sattva-owner-bills-acceptance.up.railway.app` | **BLOCKED**: this session's network policy denies the host |
| Railway service, deployment `379b8fb4…`, update `c0d94895…`, volume journals, AI budget file | **BLOCKED**: Railway connector unauthenticated; no runtime access |
| Zoho Books API documentation (`www.zoho.com`) | **BLOCKED** by network policy |
| `C:\temp\zoho\ZOHO_BILLS_AUTOMATION_KNOWLEDGE.md`, laptop evidence, original PDFs | **BLOCKED**: laptop-only; not reachable from this cloud container |
| Canonical Zoho project skill | Not present in this session's skill list |

So the runtime state below (deployment SUCCESS, zero AI reservations, pending update APPLYING) comes from the implementer's Notion events 021–023. I could not observe it myself.

## Findings, ranked by financial harm or disruption to bill flow

Each probe ID refers to `repro/test_audit_journey.py`. All 14 probes ran and behaved as stated (output in `repro/run_output.txt`).

| # | Severity | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | Financial | Source accuracy rests only on two readings by the same model of the same PDF. A consistent misread of invoice number and date posts, and is shown as **Verified**. Nothing is checked against the PDF text layer. The Windows deterministic guards (pdftotext MBL / CMA CGM invoice number) are not in the cloud image. The prompt also states the owner GSTIN value (`automation.py:42`), so the buyer-GSTIN gate (`:269`) can be satisfied by a model echoing the prompt instead of the printed invoice | Probe D: misread `SYNTHETIC-00I`/wrong date → `verified`. Code: `automation.py:42,269,460` | CONFIRMED (mechanism); live misread rate UNVERIFIED |
| 2 | Financial / ITC | A blank printed supplier GSTIN still auto-posts. Tax route and registration come from the Zoho master contact, not from the invoice | Probe F → `verified`; `automation.py:309,527` skips the registration check when blank | CONFIRMED |
| 3 | Financial | Duplicate lookup regression: the Meta `FBADS-…` and `ADS…` forms are aliased in core `_bill_key`, but the cloud uses its own key. An already-booked Meta invoice can be created again | Probe M: cloud `find` → `None`; core keys equal | CONFIRMED |
| 4 | Bill flow / accounting | Same vendor and normalised number with a different date or total is marked **Duplicate** ("Already exists in Zoho"), with no comparison. Example: a supplier whose numbering resets each financial year. The new invoice is never booked, and the owner is told it already exists | Probe G | CONFIRMED |
| 5 | Bill flow | Every domestic forward-charge post requires `line_item_taxes` on each saved line (`automation.py:540–542`). Only the fake provider returns that shape. The core read-back, which has been checked against live data, uses the bill-level `taxes` array instead. The Codex review on PR #24 says Zoho's documented response lacks `line_item_taxes`. If so, every routine domestic bill is created, then left `needs_review` without its original attached | Probe L | Mechanism CONFIRMED; live response shape UNVERIFIED (Zoho docs blocked) |
| 6 | Bill flow | A transient GET or attachment failure after creation becomes terminal `needs_review`. The created bill stays unattached and is never revisited | Probe B: posts=1, attachments=0, terminal | CONFIRMED |
| 7 | Explanation | A definitive Zoho rejection (HTTP 4xx) is shown as "Previous creation outcome is uncertain". Zoho's actual reason is discarded (`json_request`, `Zoho.call`) | Probe C | CONFIRMED |
| 8 | Needs a developer | Transient holds are terminal: provider overload/HTTP 5xx, connection loss, interrupted parse, Zoho sign-in failure. There is no retry action, and re-uploading the identical PDF is refused (UNIQUE sha256 → 409). Recovery needs someone to edit server state | Probe J; `app.py:116,507` | CONFIRMED |
| 9 | Continuity | AI allowance: each call reserves `input×1.25 + 6,000 output tokens` and never releases the reservation. That is about USD 0.07–0.12 per invoice. At the approved USD 0.50 (if that is the configured value) that is ~4–7 invoices, and the code's hard maximum of USD 1.00 is ~8–14 invoices. After that, every upload holds. The four approved acceptance calls use part of it | Probe N; `automation.py:157–163` | CONFIRMED (arithmetic); actual `APP_AI_BUDGET_USD` value BLOCKED |
| 10 | Continuity | The approved service stops 4 Nov 2026 12:28:59 IST (Notion 017/021). Bill flow ends then. The expiry and backup launcher is private, not in GitHub, so this audit could not review it | Notion 017, 021 | Owner decision; launcher UNVERIFIED |
| 11 | Silent stop | An unexpected error in the worker's intent scan (anything but `sqlite3.DatabaseError`, e.g. disk full) kills the worker thread. `/healthz` still says `ok`, and uploads stay "Queued" forever | Probe I | CONFIRMED |
| 12 | Mobile UX | An upload request waits for the whole in-flight job, including two paid model calls, because `enqueue` shares the processing lock | Probe A: waited 5.8 s behind a 6 s synthetic job | CONFIRMED |
| 13 | Bill flow | Uploads up to 12 MB are accepted. The Codex review cites a 5 MB Zoho attachment limit. A larger PDF could create a bill and then fail the mandatory attachment | `app.py:23` | Limit UNVERIFIED |
| 14 | Minor | `vendor_name` is not compared between the two readings | Probe E | CONFIRMED |
| 15 | Minor | The "Open Zoho bill" link has no organisation ID (`books.zoho.in/app#/bills/<id>`), so it relies on the browser's default organisation | Rendered status page | Behaviour UNVERIFIED |
| 16 | Routine holds | A zero-rated/exempt line (`line_tax_percentage_unsupported`) or a printed round-off holds the whole invoice. How often real invoices have these is unmeasured | Probe H | CONFIRMED (behaviour) |

Release-governance observation: seven Codex review threads (six P1, one P2) were open on `8ef5c7a` from 09:04 UTC. That was before the deployment recorded in Notion event 021, and all seven are still unresolved. Findings 2–6, 13 and 14 are among them, and this audit independently reproduced 2, 3, 4, 6 and 14. The Notion note "no new blocker in the final source diff" refers to a separate local reviewer.

## What does pass (offline, synthetic, this audit)

- 70 cloud tests and 60 core tests pass at `8ef5c7a`. The core tests need a dummy `ANTHROPIC_API_KEY` in the environment, because the core otherwise reads `C:\temp\zoho\config.json`.
- The source/payload journal is written before the POST. A lost create response is reconciled by lookup plus a unique marker, and is never reposted. An uncertain create with no record holds without reposting.
- The AI cost reservation is fsynced before any paid call, is never reset on restart, and an exhausted budget sends no request.
- Exact-PDF re-upload is rejected. The same invoice in a different PDF goes to lookup, not a create (for non-aliased numbers).
- These are all held: USD, discounts, multi-invoice PDFs, low confidence or listed issues, wrong buyer GSTIN, arithmetic mismatch, Zoho tax-group rounding disagreement.
- Status, duplicate, hold, processing and inbox pages render at 360 and 390 px with no horizontal overflow.

## Gate status

| Gate | Status | Basis |
|---|---|---|
| 1. Extraction | **UNVERIFIED** for the cloud release; design FAIL (finding 1) | Zero live calls of the cloud prompt. The ten-field matches on two carrier originals were made by the laptop/earlier extractor and come from the implementer's report |
| 2. Overseas GST / vendor matching | **FAIL** for automatic posting (finding 2); live UNVERIFIED | Core routing tests pass. The one live source-backed case was laptop-side and reported |
| 3. Duplicate / accounting safeguards | **FAIL** (findings 3, 4) | Offline journal and lost-response safety PASS |
| 4. Checked automatic posting | **FAIL** (6, 7) and **UNVERIFIED/BLOCKED** (5, 13) | No live creation, read-back or attachment exists |
| 5. Cloud/mobile independence | **FAIL** (8–12) | Mobile access is owner-reported only. Rendering PASS (synthetic) |
| 6. Historical F20 (1,164 records) | **FAIL / incomplete**; BLOCKED for independent check | Implementer's own records: 1,114 source candidates, 50 missing originals, zero whole rows certified. No closure is claimed, so none is verified |

Not established by anyone yet:
- extraction accuracy of the cloud prompt;
- genuine new-bill creation;
- exact saved-record verification against a real cloud-created bill;
- original-attachment byte verification;
- upload-to-result time (no authorised real run exists).

## Smallest corrections

1. Remove the GSTIN value from the prompt, and keep the code-level equality gate. For deterministic corroboration, port the Windows text-layer checks. That needs `poppler-utils` in the image, which is a dependency decision for the owner, not a new AI or OCR provider.
2. Hold domestic automatic posting unless a printed `vendor_gstin` exists and equals the matched contact's `gst_no`.
3. Use `core._bill_key` in `Zoho.find`.
4. On finding an existing bill, read it (read-only) and compare date, total and currency. Report `duplicate` only on a match; otherwise hold with "same number, different invoice".
5. Verify GST heads from the bill-level `taxes` array plus each line's `tax_id`, as the live-checked core does. Use `line_item_taxes` only if Zoho returns it. Before enabling posting, confirm the live shape with one read-only GET of an existing domestic bill.
6. After `bill_id` is journaled, keep transport failures non-terminal (bounded read-only retries). Reserve `needs_review` for actual mismatches.
7. Read the HTTPError body, and show Zoho's code and message.
8. Add an owner "Retry" for holds classed as transient. It should reuse the saved parse and stay budget-guarded.
9. After each call, settle the reservation down to the provider-reported `usage`, and let the owner set the ceiling instead of the hard-coded 1.00.
10. Catch all exceptions in the worker loop, and report worker liveness in `/healthz`.
11. Create the queue file with `O_EXCL` outside the processing lock.
12. Cap automatic-processing uploads at Zoho's confirmed attachment limit, before any paid call.
13. Add the normalised supplier name to `source_signature`.

## Unavoidable owner actions / missing proof

- Decide whether the cloud service and its AI budget continue beyond 4 Nov 2026. Without that, bill flow stops on that date or when the allowance runs out, whichever comes first.
- Live proof still needed after the corrections: the approved acceptance run (four Haiku calls on the Maersk/CMA originals, which should end `duplicate`), then the first genuine new invoice. The new invoice must be reconciled field by field against its original and the saved Zoho record, with an attachment hash check. This audit did not consume or count the four approved calls, and it could not read the cloud journal to confirm they are unused.
- Network access for future audits: allow `sattva-owner-bills-acceptance.up.railway.app` and `www.zoho.com` in this environment, and authorise the Railway connector, if independent runtime inspection is wanted.
