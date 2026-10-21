"""Tax route (audit A5), vendor-scoped duplicates (F05) and multi-invoice PDFs (F06).

Runs the real main() loop offline through the shared harness and checks that each
risky case stops before the approval queue, while legitimate bills still reach it.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vendor_matching import MAERSK_ID, approvable_invoice, run_pipeline, zb

BILL_NUMBER = "MH27IN1000763792"


class TaxRouteTests(unittest.TestCase):
    def test_indian_gstin_vendor_usd_invoice_is_held_not_reverse_charged(self):
        parsed = approvable_invoice()
        parsed["currency_code"] = "USD"
        log, posts, _, _ = run_pipeline(parsed)
        self.assertEqual(posts, 0)
        self.assertEqual(log["awaiting_approval"], [])
        self.assertEqual(log["needs_review"][0]["reason"],
                         "tax_route_uncertain:indian_gstin_vendor_with_overseas_signal")

    def test_ai_overseas_flag_cannot_override_invoice_gstin(self):
        route = zb.determine_tax_route(
            {"is_overseas": True, "currency_code": "INR", "vendor_gstin": "27FGHIJ5678K1Z2",
             "line_items": [], "notes": ""}, "SOME AGENT", "")
        self.assertEqual(route["review_reason"], "indian_gstin_vendor_with_overseas_signal")

    def test_substring_no_longer_triggers_reverse_charge(self):
        route = zb.determine_tax_route(
            {"is_overseas": False, "currency_code": "INR", "line_items": [], "notes": "",
             "tax_amount_igst": 0, "tax_amount_cgst": 0, "tax_amount_sgst": 0},
            "DAWSON LOGISTICS PVT LTD", "27AAAAA0000A1Z5")
        self.assertEqual(route["tax_mode"], "cgst_sgst")

    def test_foreign_vendor_without_indian_gstin_stays_reverse_charge(self):
        route = zb.determine_tax_route(
            {"is_overseas": True, "currency_code": "USD", "line_items": [], "notes": "",
             "tax_amount_igst": 0, "tax_amount_cgst": 0, "tax_amount_sgst": 0},
            "ANTHROPIC", "")
        self.assertEqual((route["tax_mode"], route["review_required"]), ("rcm", False))


class DuplicateTests(unittest.TestCase):
    def test_other_vendors_bill_with_same_number_is_not_a_duplicate(self):
        other = {"bill_number": BILL_NUMBER, "vendor_id": "some-other-vendor"}
        log, posts, _, _ = run_pipeline(approvable_invoice(), existing=[other])
        self.assertEqual(log["skipped_duplicate"], [])
        self.assertEqual(log["awaiting_approval"][0]["bill_number"], BILL_NUMBER)
        self.assertEqual(posts, 0)

    def test_same_vendor_same_number_is_skipped(self):
        log, posts, _, _ = run_pipeline(approvable_invoice(), existing={BILL_NUMBER})
        self.assertEqual(log["skipped_duplicate"][0]["vendor_id"], MAERSK_ID)
        self.assertEqual(posts, 0)

    def test_format_variant_or_vendorless_match_goes_to_review(self):
        for existing in ([{"bill_number": "MH27-IN-1000763792", "vendor_id": MAERSK_ID}],
                         [{"bill_number": BILL_NUMBER, "vendor_id": ""}]):
            with self.subTest(existing=existing):
                log, posts, _, _ = run_pipeline(approvable_invoice(), existing=existing)
                self.assertEqual(posts, 0)
                self.assertEqual(log["skipped_duplicate"], [])
                self.assertTrue(log["needs_review"][0]["reason"].startswith("possible_duplicate:"))


class MultiInvoiceTests(unittest.TestCase):
    def parse(self, invoices):
        body = json.dumps({"content": [{"text": json.dumps(invoices)}], "usage": {}}).encode()

        class Resp:
            def read(self):
                return body

        with tempfile.TemporaryDirectory(prefix="zoho-multi-test-") as tmp:
            pdf = os.path.join(tmp, "multi.pdf")
            Path(pdf).write_bytes(b"%PDF offline fixture")
            with patch.object(zb.urllib.request, "urlopen", lambda *a, **k: Resp()), \
                    contextlib.redirect_stdout(io.StringIO()):
                return zb.parse_pdf_with_claude_once(pdf, "multi.pdf", ["A"])

    def test_every_invoice_is_kept_and_the_pdf_is_held(self):
        first, second = approvable_invoice(), approvable_invoice()
        second["bill_number"], second["line_items"] = "PLATFORM-FEE-1", [{"rate": 15.0}]
        result = self.parse([first, second])
        self.assertEqual(result["multi_invoice_count"], 2)
        self.assertEqual([i["bill_number"] for i in result["invoices"]], [BILL_NUMBER, "PLATFORM-FEE-1"])
        self.assertEqual(result["confidence"], "low")

        cache_entry = zb._make_cache_entry(dict(result), "hash")
        self.assertEqual(zb._cache_lookup({"hash": cache_entry}, "hash")["invoices"], result["invoices"])

        log, posts, _, _ = run_pipeline({k: v for k, v in result.items() if not k.startswith("_")})
        self.assertEqual(posts, 0)
        self.assertEqual(log["awaiting_approval"], [])
        self.assertEqual(log["needs_review"][0]["reason"],
                         f"multi_invoice_pdf:2:{BILL_NUMBER},PLATFORM-FEE-1")

    def test_single_element_list_is_a_normal_invoice(self):
        result = self.parse([approvable_invoice()])
        self.assertEqual(result["bill_number"], BILL_NUMBER)
        self.assertNotIn("multi_invoice_count", result)


if __name__ == "__main__":
    unittest.main()
