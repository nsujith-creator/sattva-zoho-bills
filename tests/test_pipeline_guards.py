"""Tax route (audit A5), vendor-scoped duplicates (F05), multi-invoice PDFs (F06),
HSN/SAC codes (F07) and invoice-date exchange rates (F08).

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

    def test_name_signal_alone_goes_to_review(self):
        # A domestic contact with no GSTIN whose name contains an overseas signal word.
        route = zb.determine_tax_route(
            {"is_overseas": False, "currency_code": "INR", "line_items": [], "notes": "",
             "tax_amount_igst": 0, "tax_amount_cgst": 0, "tax_amount_sgst": 0},
            "AWS LOGISTICS", "")
        self.assertEqual((route["tax_mode"], route["review_required"]), ("unknown", True))
        self.assertEqual(route["review_reason"], "ambiguous_overseas_status")

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

    def test_one_valid_invoice_plus_malformed_entry_is_held(self):
        result = self.parse([approvable_invoice(), "not an invoice"])
        self.assertEqual(result["multi_invoice_count"], 2)
        self.assertEqual(result["invoices"][1], {"unparsed": "not an invoice"})

    def test_held_pdf_survives_cache_reload_and_old_entries_are_reparsed(self):
        held = zb._make_cache_entry({"confidence": "low", "multi_invoice_count": 2,
                                     "invoices": [{"bill_number": "A"}, {"bill_number": "B"}]}, "held")
        legacy = zb._make_cache_entry(approvable_invoice(), "legacy")
        legacy["_parser_version"] = 6  # may hold one invoice of several
        with tempfile.TemporaryDirectory(prefix="zoho-multi-test-") as tmp:
            cache_path = os.path.join(tmp, "parsed_cache.json")
            Path(cache_path).write_text(json.dumps({"held": held, "legacy": legacy}), encoding="utf-8")
            with patch.object(zb, "CACHE_PATH", cache_path), contextlib.redirect_stdout(io.StringIO()):
                loaded = zb._load_parse_cache()
        self.assertEqual(len(zb._cache_lookup(loaded, "held")["invoices"]), 2)
        self.assertIsNone(zb._cache_lookup(loaded, "legacy"))

    def test_single_element_list_is_a_normal_invoice(self):
        result = self.parse([approvable_invoice()])
        self.assertEqual(result["bill_number"], BILL_NUMBER)
        self.assertNotIn("multi_invoice_count", result)


class HsnTests(unittest.TestCase):
    def test_invalid_hsn_holds_the_bill(self):
        for code in ("1234567", "9965110000", "998313/996719"):
            with self.subTest(code=code):
                parsed = approvable_invoice()
                parsed["line_items"][0]["hsn_or_sac"] = code
                log, posts, _, _ = run_pipeline(parsed)
                self.assertEqual(posts, 0)
                self.assertEqual(log["awaiting_approval"], [])
                self.assertTrue(log["needs_review"][0]["reason"].startswith("hsn_or_sac_invalid:"))

    def test_valid_or_absent_hsn_reaches_approval(self):
        parsed = approvable_invoice()
        parsed["line_items"][0]["hsn_or_sac"] = "SAC 9967 19"
        with tempfile.TemporaryDirectory(prefix="zoho-hsn-test-") as folder:
            log, posts, _, _ = run_pipeline(parsed, folder=folder)
            queue = json.loads(Path(folder, "APPROVAL_QUEUE_PATH.json").read_text(encoding="utf-8"))
        self.assertEqual(posts, 0)
        self.assertEqual(len(log["awaiting_approval"]), 1)
        lines = next(iter(queue.values()))["payload"]["line_items"]
        self.assertEqual(lines[0]["hsn_or_sac"], "996719")
        self.assertNotIn("hsn_or_sac", lines[1])


class InvoiceDateFxTests(unittest.TestCase):
    VENDORS = {"ANTHROPIC PBC": {"id": "anthropic", "gst_no": ""}}

    def usd_invoice(self):
        return {"vendor_name": "ANTHROPIC PBC", "vendor_gstin": "", "bill_number": "INV-2026-0042",
                "bill_date": "2026-04-01", "due_date": None, "mbl_number": "", "currency_code": "USD",
                "is_overseas": True, "confidence": "high", "notes": "",
                "line_items": [{"description": "Claude Team plan", "quantity": 1, "rate": 100.0,
                                "hsn_or_sac": "998315"}],
                "subtotal": 100.0, "discounts_or_credits": 0, "tax_amount_igst": 0,
                "tax_amount_cgst": 0, "tax_amount_sgst": 0, "invoice_total": 100.0}

    def test_bill_uses_its_invoice_date_rate(self):
        dates = []

        def rate_on(date):
            dates.append(date)
            return 84.25

        with patch.object(zb, "fetch_usd_inr_rate_for_date", rate_on), \
                tempfile.TemporaryDirectory(prefix="zoho-fx-test-") as folder:
            log, posts, _, _ = run_pipeline(self.usd_invoice(), loaded_vendors=self.VENDORS, folder=folder)
            queue = json.loads(Path(folder, "APPROVAL_QUEUE_PATH.json").read_text(encoding="utf-8"))
        self.assertEqual(dates, ["2026-04-01"])
        self.assertEqual(len(log["awaiting_approval"]), 1)
        self.assertEqual(next(iter(queue.values()))["payload"]["line_items"][0]["rate"], 8425.0)

    def test_failed_invoice_date_rate_holds_the_bill(self):
        # The harness blocks the network, so the invoice-date fetch fails. Today's
        # rate must not be used instead.
        log, posts, _, _ = run_pipeline(self.usd_invoice(), loaded_vendors=self.VENDORS)
        self.assertEqual(posts, 0)
        self.assertEqual(log["awaiting_approval"], [])
        self.assertEqual(log["needs_review"][0]["reason"], "fx_rate_unavailable")


if __name__ == "__main__":
    unittest.main()
