# Cloud rounding/source repair candidate

The live PR27 acceptance run held both existing originals. Investigation found offsetting same-rate line tax allocations and source GST rows requiring exact group arithmetic; both raw model readings also shared a charge-tax misread. The owner approved only duplicate penny allocations, with exact GST-head/grand totals and no financial changes.

The candidate preserves raw readings, derives a separate checked result from unique printed CMA tax rows, chooses an exact documented calculation mode, and records the bounded duplicate allocation policy. Windows/core, auth, provider/model, credentials, budget and expiry are unchanged. Owner startup reuses retained readings only, backs up prior processing state, and blocks missing/unsafe evidence before worker construction.

New records retain strict per-line checks. NEW group-rounded invoices requiring an unproved charge allocation remain held before creation. No claim of universal coverage or live activation.

Initial candidate fa5d3ba validation on Windows/Python 3.12, with a dummy key, real credentials removed and external socket connections blocked:

- Initial focused source/rounding/worker/startup suite: 73 tests passed. Two additional startup/evidence guards were then added and passed in the final full run.
- Final full cloud run: 148 cases, 147 passed and one transport error. The upload journey's oversized-request rejection hit `ConnectionAbortedError [WinError 10053]`; that exact test passed on its isolated rerun (1 test, 3.297 seconds). All final rounding, raw-evidence, backup and startup checks passed.
- The first full run and a targeted auth run also encountered WinError 10053, in different unchanged rejected-request tests. `auth.py`, `app.py` and `test_auth.py` match deployed/main commit `f5274d2ab1c3efa12f364f5ea8518cdee3570fd8` byte for byte. Its isolated baseline auth suite passed all 18 tests; the final candidate run also passed those 18. This is intermittent Windows transport evidence, not proof of an entirely passing full run or permission to weaken authentication tests.
- Python AST parsing, whitespace checks and protected-source hash comparison passed. The unchanged core suite was not repeated. No runtime image build or live candidate run has occurred.

Reproduce from this branch: `python -m unittest discover -s cloud_review -p 'test_*.py' -v`. Narrow scope: `python -m unittest discover -s cloud_review -p 'test_rounding.py' -v`; startup: `python -m unittest discover -s cloud_review -p 'test_owner_start.py' -v`. Use offline fixtures/dummy configuration, with external network blocked; never supply production secrets to a test process. The independent reviewer must assess the exact candidate commit and its dependencies, not infer a pass from this report.

Independent review reproduced three gaps in that first candidate: empty/partial tax arrays counted as complete duplicate-allocation evidence; nonempty low-confidence readings passed startup preflight; and signed printed tax amounts lost their minus during deterministic correction. The revision requires explicit charged heads on every line before an offset, complete high-confidence/issue-free readings before worker construction, and holds signed/credit-formatted relevant monetary rows. All 78 focused tests passed after these fixes, including the three independent reproductions. Final full-suite rerun and independent re-review of the revised head are pending.

No live file, job, bill or platform setting was changed by preparing this branch. No extra inference was used. Source review and production approval are still required. Live proof must reuse the four retained readings, compare both existing Zoho records unchanged, and enable qualifying NEW-upload creation only after every check passes. A first genuine new bill still needs exact read-back, original attachment hash and elapsed-time proof before that journey can be called verified.
