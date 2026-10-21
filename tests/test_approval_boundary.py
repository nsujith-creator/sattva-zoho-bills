"""Approval boundary (audit F04) and verified-only success (audit F03).

Runs the real main() loop offline for the AGENTS.md verification cases: clean,
poor-quality, duplicate, tax mismatch, missing field and retry after partial
failure, and checks that nothing reaches zoho_post without a recorded approval.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vendor_matching import GSTIN, MAERSK_ID, approvable_invoice, run_pipeline, zb

SRC = Path(__file__).resolve().parents[1] / "src"
APPROVER = "Test Approver"
BILL_ID = "900000000001"

_spec = importlib.util.spec_from_file_location("watcher_under_test", SRC / "watcher.py")
watcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watcher)


def readback(parsed, **overrides):
    """GET bills/{id} response matching what the pipeline posts for the parsed invoice."""
    payload = zb.build_payload(parsed, MAERSK_ID, zb.determine_tax_route(parsed, "MAERSK A/S", GSTIN))
    tax = sum(parsed[f] for f in ("tax_amount_igst", "tax_amount_cgst", "tax_amount_sgst"))
    bill = {"bill_id": BILL_ID, "bill_number": parsed["bill_number"], "vendor_id": MAERSK_ID,
            "date": parsed["bill_date"], "currency_code": "INR", "sub_total": parsed["subtotal"],
            "tax_total": round(tax, 2), "total": parsed["invoice_total"],
            "line_items": [{"rate": line["rate"], "quantity": line["quantity"], "tax_id": line["tax_id"]}
                           for line in payload["line_items"]]}
    bill.update(overrides)
    return bill


def watcher_status(folder, run_log, filename, popen_calls=None):
    """What watcher.run_for_file() reports, with its hardcoded paths redirected."""
    folder = Path(folder)
    run_log_file = folder / "watcher_run_log.json"
    run_log_file.write_text(json.dumps(run_log), encoding="utf-8")
    redirect = {r"C:\temp\zoho\run_log.json": str(run_log_file),
                r"C:\temp\zoho\invoice_log.json": str(folder / "INVOICE_LOG_PATH.json")}
    real_open, real_exists = open, watcher.os.path.exists

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def fake_popen(args, **kwargs):
        if popen_calls is not None:
            popen_calls.append(list(args))
        return FakeProc()

    with patch.object(watcher, "open", lambda p, *a, **k: real_open(redirect.get(p, p), *a, **k),
                      create=True), \
            patch.object(watcher.os.path, "exists", lambda p: real_exists(redirect.get(p, p))), \
            patch.object(watcher.subprocess, "Popen", fake_popen), \
            patch.object(watcher, "log", lambda message: None):
        return watcher.run_for_file(filename, "TEST-RUN")[0]


class ApprovalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="zoho-approval-test-")
        self.folder = Path(self._tmp.name)
        self.posted = []

    def tearDown(self):
        self._tmp.cleanup()

    def pipeline(self, parsed, **kwargs):
        return run_pipeline(parsed, folder=self.folder, **kwargs)

    def state(self, name, default=None):
        path = self.folder / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def statuses(self):
        return [entry["status"] for entry in self.state("INVOICE_LOG_PATH", [])]

    def pdf_hash(self):
        return hashlib.sha256((self.folder / "sample.pdf").read_bytes()).hexdigest()

    def accept_post(self, endpoint, payload):
        # The approval must already be on disk when the POST is sent.
        approvals = self.state("APPROVALS_PATH", [])
        self.assertTrue(any(a["payload_sha256"] == zb.payload_digest(payload) for a in approvals))
        self.posted.append(copy.deepcopy(payload))
        return {"bill": {"bill_id": BILL_ID}}

    # AGENTS.md case 1: clean invoice
    def test_clean_invoice_waits_for_approval_then_posts_verified(self):
        parsed = approvable_invoice()
        log, posts, _, output = self.pipeline(parsed)
        self.assertEqual(posts, 0)
        self.assertEqual(log["awaiting_approval"][0]["reason"], "awaiting_approval")
        self.assertIn("--approve --approver", output)
        self.assertIsNone(self.state("APPROVALS_PATH"))
        queued = self.state("APPROVAL_QUEUE_PATH")[self.pdf_hash()]
        status = watcher_status(self.folder, log, "sample.pdf")
        self.assertEqual(status, "awaiting_approval")
        self.assertFalse(watcher.should_mark_seen(status))
        for key, value in {"subtotal": 348267.25, "tax_amount_cgst": 10670.69,
                           "tax_amount_sgst": 10670.69, "tax_amount_igst": 0,
                           "invoice_total": 369608.63}.items():
            self.assertEqual(queued["reconciliation"]["amounts"][key],
                             {"expected": value, "calculated": value})

        log, posts, _, _ = self.pipeline(parsed, approver=APPROVER, post=self.accept_post,
                                         readback=readback(parsed))
        self.assertEqual(posts, 1)
        self.assertEqual(self.posted, [queued["payload"]])
        [approval] = self.state("APPROVALS_PATH")
        self.assertEqual((approval["approved_by"], approval["pdf_sha256"], approval["payload_sha256"]),
                         (APPROVER, self.pdf_hash(), queued["payload_sha256"]))
        self.assertTrue(approval["approved_at"])
        self.assertEqual(log["verified"][0]["bill_id"], BILL_ID)
        self.assertEqual(self.statuses(), ["awaiting_approval", "posted_unverified", "verified"])
        self.assertNotIn(self.pdf_hash(), self.state("APPROVAL_QUEUE_PATH"))
        status = watcher_status(self.folder, log, "sample.pdf")
        self.assertEqual(status, "verified")
        self.assertTrue(watcher.should_mark_seen(status))

    # AGENTS.md cases 2-5: an approval never overrides a failed check
    def test_approval_cannot_override_failed_checks(self):
        cases = {
            "poor_quality": (lambda p: p.update(confidence="low"), {}, "needs_review"),
            "duplicate": (lambda p: None, {"existing": {"MH27IN1000763792"}}, "skipped_duplicate"),
            "tax_mismatch": (lambda p: p.update(tax_amount_cgst=p["tax_amount_cgst"] + 100), {},
                             "needs_review"),
            "missing_field": (lambda p: p.update(bill_date=""), {}, "needs_review"),
        }
        for name, (mutate, extra, bucket) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory(prefix="zoho-approval-test-") as folder:
                parsed = approvable_invoice()
                mutate(parsed)
                for approver in (None, APPROVER):
                    log, posts, _, _ = run_pipeline(parsed, folder=folder, approver=approver,
                                                    post=self.accept_post, **extra)
                    self.assertEqual(posts, 0)
                    self.assertEqual(len(log[bucket]), 1)
                self.assertFalse((Path(folder) / "APPROVALS_PATH.json").exists())

    # AGENTS.md case 6: retry after partial failure
    def test_failed_readback_is_never_success(self):
        parsed = approvable_invoice()
        self.pipeline(parsed)
        log, posts, _, _ = self.pipeline(parsed, approver=APPROVER, post=self.accept_post,
                                         readback=readback(parsed, date="2026-09-01"))
        self.assertEqual(posts, 1)
        self.assertEqual(log["verified"], [])
        self.assertIn("bill_date_mismatch", log["post_verify_failed"][0]["error"])
        self.assertEqual(self.statuses(), ["awaiting_approval", "posted_unverified", "post_verify_failed"])
        status = watcher_status(self.folder, log, "sample.pdf")
        self.assertEqual(status, "post_verify_failed")
        self.assertFalse(watcher.should_mark_seen(status))

    def test_readback_must_match_printed_amounts(self):
        # Audit F01/F02: each of these passed the old subtotal-only, 1% check.
        parsed = approvable_invoice()
        cases = {
            "total_mismatch": {"total": parsed["invoice_total"] + 0.50},
            "tax_total_mismatch": {"tax_total": 21341.38 + 0.50, "total": parsed["invoice_total"] + 0.50},
            "sub_total_mismatch": {"sub_total": parsed["subtotal"] + 2900},
        }
        for error, overrides in cases.items():
            with self.subTest(error), tempfile.TemporaryDirectory(prefix="zoho-approval-test-") as folder:
                run_pipeline(parsed, folder=folder)
                log, posts, _, _ = run_pipeline(parsed, folder=folder, approver=APPROVER,
                                                post=lambda endpoint, payload: {"bill": {"bill_id": BILL_ID}},
                                                readback=readback(parsed, **overrides))
                self.assertEqual(posts, 1)
                self.assertEqual(log["verified"], [])
                self.assertIn(error, log["post_verify_failed"][0]["error"])

    def test_approval_is_bound_to_the_reviewed_payload(self):
        self.pipeline(approvable_invoice())
        changed = approvable_invoice()
        changed["due_date"] = "2026-11-30"
        log, posts, _, _ = self.pipeline(changed, approver=APPROVER, post=self.accept_post)
        self.assertEqual(posts, 0)
        self.assertEqual(log["awaiting_approval"][0]["reason"],
                         "approval_rejected:payload_changed_since_review")
        self.assertIsNone(self.state("APPROVALS_PATH"))
        self.assertEqual(self.state("APPROVAL_QUEUE_PATH")[self.pdf_hash()]["payload"]["due_date"],
                         "2026-11-30")

    def test_approval_without_reviewed_proposal_does_not_post(self):
        log, posts, _, _ = self.pipeline(approvable_invoice(), approver=APPROVER, post=self.accept_post)
        self.assertEqual(posts, 0)
        self.assertEqual(log["awaiting_approval"][0]["reason"], "approval_rejected:no_reviewed_proposal")

    def test_unreadable_approval_ledger_blocks_post_and_is_kept(self):
        parsed = approvable_invoice()
        self.pipeline(parsed)
        ledger = self.folder / "APPROVALS_PATH.json"
        ledger.write_text("{not json", encoding="utf-8")
        log, posts, _, _ = self.pipeline(parsed, approver=APPROVER, post=self.accept_post)
        self.assertEqual(posts, 0)
        self.assertEqual(log["failed"][0]["error"], "approval_ledger_write_failed")
        self.assertEqual(ledger.read_text(encoding="utf-8"), "{not json")

    def test_approve_needs_one_file_and_a_named_approver(self):
        for argv in (["--file", "x.pdf", "--approve"],
                     ["--file", "x.pdf", "--approve", "--approver"],
                     ["--approve", "--approver", "X"],
                     ["--file", "x.pdf", "--approve", "--approver", "X", "--preview"],
                     ["--file", "a.pdf", "--file", "b.pdf", "--approve", "--approver", "X"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with patch.object(sys, "argv", ["zoho_bills.py"] + argv), \
                        patch.object(zb, "zoho_post", side_effect=AssertionError("POST")), \
                        patch("builtins.open", side_effect=AssertionError("no lock or state file")), \
                        contextlib.redirect_stdout(out):
                    zb.main()
                self.assertIn("[APPROVE]", out.getvalue())

    def test_watcher_never_passes_approve(self):
        calls = []
        watcher_status(self.folder, {"run_id": "x"}, "sample.pdf", popen_calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--approve", calls[0])
        self.assertNotIn("--approver", calls[0])


if __name__ == "__main__":
    unittest.main()
