# Cloud automation audit repairs, 28 October 2026

## Release boundary

Candidate branch: `codex/cloud-audit-fixes-20261028`. Repairs are based on PR24's exact source `8ef5c7a1709c2d09a5c1e73792ed8365bfab1886`; this is a proposed replacement release, not proof that the running service has these fixes. Main was `b8ec1f4880452c8a739c5c4d192c71b8717c4558` when the candidate was prepared. The original source archive and hashes are retained privately.

The owner approved conditional automatic creation for qualifying NEW cloud uploads. Per-invoice approval is not restored for that journey. Existing records, historical processing, payments, vendors, FX decisions and ITC remain outside that creation authority. The Windows program and parse cache are not part of this change.

## Findings addressed

| Audit finding | Candidate behavior | Reproducible evidence |
| --- | --- | --- |
| Two matching AI mistakes can pass | Remove owner GSTIN from model prompt; independently require readable PDF text with printed identity, registrations, labelled dates/totals/tax heads, charge amounts/rates and SAC. Reuse CMA/BL guards. Ambiguous/scanned text holds before paid calls. | `test_automation_recovery`, `test_source_text` |
| Missing printed supplier GSTIN | Domestic registration must be printed and match the unique vendor; never fill it from the vendor master. | `test_automation` |
| Meta aliases bypass duplicate check | Use the maintained core invoice key, including FBADS/ADS normalization. | `test_automation_recovery.ProviderRegressions` |
| Reused invoice number silently dropped | Reconcile the existing record's date, currency, supplier registration, amounts, tax heads and lines before calling it Duplicate; mismatches hold without modifying it. | `test_automation_recovery.AuditRegressions` |
| Tax response assumptions | Require core bill-level exact tax checks and line tax IDs; additionally verify per-line tax amounts when returned. | `test_automation`, `test_automation_recovery` |
| Lost post-create read/attachment | Persist create/attachment attempts and bill ID; bounded safe-read recovery and explicit retry retain all readings, never repeat an uncertain create, and check an existing attachment before another upload. | `test_automation_recovery`, `test_automation_http` |
| Zoho rejection called uncertain | Preserve sanitized HTTP/Zoho error code and reason; distinguish definitive rejection from unknown create outcome. | `test_automation_recovery` |
| Transient failure cannot resume | Existing authenticated status page offers CSRF-protected Retry for classified transient failures. A missing model reading needs explicit retry; completed readings are reused. Permanent source/financial mismatches remain held. | `test_automation_http`, `test_automation_recovery` |
| Unused reservations exhaust allowance | Settle completed calls to valid reported token usage; retain unresolved reservations and prior spending across restarts. No configured limit raised. | `test_automation_recovery.BudgetSettlement` |
| Worker fails silently | Keep the scan loop alive, expose worker failure through health503 and inbox warning, clear it only after a successful cycle. | `test_automation_http`, `test_automation_recovery` |
| Upload waits on provider work | Separate short journal lock from provider processing; enqueue does not wait on extraction. | `test_automation_recovery` |
| Attachment size / name agreement / org link | Limit automatic PDFs to5,000,000 bytes; compare supplier names across readings; include configured organisation in Zoho link. | `test_automation_http`, `test_automation_recovery` |

The original 14 independent audit probes were reproduced on the unmodified baseline with synthetic data and blocked network. They describe baseline defects, not tests expected to pass unchanged on repaired code.

## Checks actually performed

With Python3.12 on the authorized Windows laptop:

```powershell
$env:ANTHROPIC_API_KEY='offline-test'
python -m unittest discover -s cloud_review -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

- Cloud: **99 tests passed**,39.036 seconds. Core: **60 tests passed**,7.118 seconds. Local private logs retain earlier failures as well as the final passing run. One earlier run had a transient Windows loopback connection abort plus a new source-check fixture exposing ambiguity between charge-level and total tax labels; the latter was fixed and the full suite passed. No test was removed to obtain the pass.
- Generated synthetic PDF: actual `pdfinfo`/`pdftotext` extraction passed, including split GST charge rows and abbreviated CMA-style summary rows.
- Both approved original PDFs passed the deterministic source guard using retained local parsed facts and original PDF text. These were **not** new Haiku calls and do not prove the cloud model's extraction accuracy. Originals, text and amounts remain private.
- Two fresh read-only Zoho record reads showed that these actual bills do return `line_item_taxes`. The audit's conditional claim that this field may be absent on every domestic bill was not established for these records. The candidate supports bill-level verification while checking optional line details whenever supplied.
- Browser: at360px, owner Retry resumed an interrupted synthetic second reading, retained the first reading, and finished with exactly3 fake AI attempts,1 fake create and1 fake attachment. Real model/financial calls:0. Status and inbox layouts were measured at360,390,768 and desktop widths without horizontal overflow.
- A browser file-picker check stalled until the bounded loopback server closed; that browser-upload check is **inconclusive**. The actual authenticated HTTP upload/re-upload/retry paths passed the automated tests. Do not claim browser upload acceptance from the successful Retry check.

## Deployment and live proof still outstanding

### R16 independent review and corrections

After explicit owner consent, one independent Codex reviewer examined only the12 pinned source/test/documentation files, without invoices, credentials, external connectors or production access. It independently demonstrated five remaining source-admission bypasses: PO number substituted for invoice number, shipper registration substituted for buyer registration, reference INR substituted for USD invoice currency, one printed charge reused twice, and an invented due date. All five were fixed. Follow-up within the same review also caught staggered-column buyer leakage and conflicting currency labels on one row; both were fixed.

The reviewer's12 isolated fake-core probes now produce the intended results:7 unsafe cases rejected and5 positive controls accepted. It identified no further supported blocker within its bounded source review. This is not a certification of unseen core/provider/container/live behavior. Reviewed automation.py SHA256 `cc55958e312a9c90d41898542b94fd6e19f62158a7822c7c35adbf2e04b683a8`, test_source_text.py `1603f349bdcad440a3f848d3cca1b95b7d8f68e841507b520ed30b0f434f6d87`.

Current full suite: **106 cloud tests passed**,40.043 seconds; **60 core tests passed**,7.140 seconds. Both originals also passed the stricter deterministic guard using retained local facts, including their printed due dates. No model inference or financial write. All620 cached extractions and10 protected Windows hashes remain unchanged; candidate credential scan0 matches. The earlier99-test result above is retained as dated iteration evidence, not the current count.

At the later R16 read, Railway no longer reported the old pending patch. The live deployment was still379b8fb4 at8ef5c7a, no newer deployment appeared, and no original-acceptance log was returned. The disappearance of the pending patch does not establish a successful deployment or completed acceptance. No changes were resubmitted. This corrected candidate still needs explicit merge/deployment approval and actual acceptance.

No new candidate build, merge, production deployment, paid inference or financial write was performed. Docker is unavailable locally; the new Debian image/package installation still requires a real build. At13:49UTC, Railway's earlier acceptance patch `c0d94895-9d59-427a-85d8-8403e689b8d3` remained APPLYING with no new deployment. It was not resubmitted or modified beneath that operation. The live deployment remains the older8ef5c7a release.

The four original-invoice Haiku checks are still unused. They must run against the approved release after its build/deployment and independent review, followed by a genuine qualifying NEW invoice, exact Zoho read-back, attachment hash verification and measured upload-to-result time. An existing mismatching invoice must hold, not be force-labelled Duplicate. No synthetic financial record may be created in the live books to manufacture this proof.

## Dependency, cost and remaining scope

The owner approved Debian `poppler-utils` for this candidate. It supplies local PDF text extraction, is GPL-licensed, has no licence/subscription fee and needs normal container package/security maintenance. No pip dependency, OCR service or additional model provider is introduced. References: [Debian package](https://packages.debian.org/bookworm/poppler-utils), [licensing](https://sources.debian.org/src/poppler/20.09.0-3.1%2Bdeb11u1/debian/copyright/), [Zoho attachment help](https://www.zoho.com/in/books/help/bills/other-actions.html).

The owner allows efficient budgets above the former INR500 planning ceiling for starting, running and maintaining this automation, **after approving the concrete budget**. This is not an unlimited allowance. No new numerical allocation, service extension or purchase has been approved by that statement alone; the USD0.50 AI allowance and4November2026 12:28:59PMIST expiry remain pending a separate operating-budget decision.

Unsupported zero/exempt mappings, printed adjustments and FX remain explicit holds; these repairs do not invent accounting treatment. Source-text checks are a conservative admission guard, not a proof of universal extraction accuracy. Full historical F20 certification, missing originals, ITC/FX evidence and the Maersk restoration exception remain open, separate from new-upload delivery.
