"""Offline regressions for vendor identity and the existing pipeline boundaries."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import zoho_bills as zb


GSTIN = "27AAJCM4693D1Z8"
MAERSK_ID = "914109000000024331"
SAMUDERA_GSTIN = "27AABCS1234A1Z5"


def vendors():
    return {
        "MAERSK A/S": {
            "id": MAERSK_ID, "gst_no": GSTIN,
            "duplicates": [
                {"id": "maersk-gujarat", "gst_no": "24AAJCM4693D1ZE"},
                {"id": "maersk-tamilnadu", "gst_no": "33AAJCM4693D1Z5"},
            ],
        },
        "SAMUDERA SHIPPING LINE (INDIA) PVT LTD": {
            "id": "samudera", "gst_no": SAMUDERA_GSTIN,
        },
    }


def invoice():
    return {
        "vendor_name": "Maersk Line India Pvt. Ltd.", "vendor_gstin": GSTIN,
        "bill_number": "MH27IN1000763792", "bill_date": "2026-10-17",
        "due_date": "2026-10-18", "mbl_number": "277529380",
        "currency_code": "INR", "is_overseas": False, "confidence": "high",
        "line_items": [
            {"description": "Equipment Positioning Service Export(CY)", "quantity": 1, "rate": 3855.18, "tax_percentage": 18},
            {"description": "Export Service", "quantity": 1, "rate": 1060.17, "tax_percentage": 18},
            {"description": "Terminal Handling Service - Origin", "quantity": 1, "rate": 20200.0, "tax_percentage": 18},
            {"description": "Emergency Bunker Surcharge", "quantity": 1, "rate": 19275.87, "tax_percentage": 5},
            {"description": "Basic Ocean Freight", "quantity": 1, "rate": 298776.03, "tax_percentage": 5},
            {"description": "Documentation Fee- Origin", "quantity": 1, "rate": 5100.0, "tax_percentage": 18},
        ],
        "subtotal": 348267.25, "discounts_or_credits": 0,
        "tax_amount_igst": 0, "tax_amount_cgst": 10670.69,
        "tax_amount_sgst": 10670.69, "invoice_total": 369608.63, "notes": "",
    }


def approvable_invoice():
    """Exact master name and keyword-consistent rates: the path that used to auto-post."""
    parsed = invoice()
    parsed["vendor_name"] = "MAERSK A/S"
    parsed["line_items"][3]["description"] = "Ocean Freight - Bunker"
    return parsed


def run_pipeline(parsed, existing=None, preview=False, post=None, loaded_vendors=None,
                 source_path=None, approver=None, folder=None, readback=None):
    """Run the actual main loop with isolated state and blocked network.

    folder keeps state (approval queue, ledger, logs) across runs; approver adds
    --approve --approver NAME; readback is the bill returned by GET bills/{id}.
    """
    with (contextlib.nullcontext(folder) if folder else
          tempfile.TemporaryDirectory(prefix="zoho-vendor-test-")) as tmp:
        folder = Path(tmp)
        source = Path(source_path) if source_path else folder / "sample.pdf"
        if not source_path:
            source.write_bytes(b"%PDF offline fixture; extraction supplied from cache")
        file_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        cache = {file_hash: zb._make_cache_entry(copy.deepcopy(parsed), file_hash)}
        state_paths = {name: str(folder / (name + ".json")) for name in (
            "LOG_PATH", "CACHE_PATH", "INVOICE_LOG_PATH", "FAILED_INVOICES_PATH",
            "RUN_MANIFEST_PATH", "AUDIT_BASELINE_PATH", "DAILY_METRICS_PATH", "SYSTEM_HEALTH_PATH",
            "APPROVAL_QUEUE_PATH", "APPROVALS_PATH")}
        real_open, real_remove = open, os.remove
        lock_path = r"C:\temp\zoho\zoho.lock"

        def isolated_open(path, *args, **kwargs):
            return real_open(str(folder / "zoho.lock") if str(path) == lock_path else path,
                             *args, **kwargs)

        def isolated_remove(path, *args, **kwargs):
            return real_remove(str(folder / "zoho.lock") if str(path) == lock_path else path,
                               *args, **kwargs)

        tax_rules = {"tax_id_map": zb._DEFAULT_TAX_ID_MAP,
                     "igst_vendors": zb._DEFAULT_IGST_VENDORS,
                     "overseas_vendor_signals": list(zb._DEFAULT_OVERSEAS_VENDOR_SIGNALS),
                     "_source": "offline_test"}
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.multiple(zb, **state_paths))
            stack.enter_context(patch("builtins.open", side_effect=isolated_open))
            stack.enter_context(patch.object(zb.os, "remove", side_effect=isolated_remove))
            stack.enter_context(patch.object(zb.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")))
            stack.enter_context(patch.object(zb, "refresh_token"))
            stack.enter_context(patch.object(zb, "notify"))
            stack.enter_context(patch.object(zb.time, "sleep"))
            stack.enter_context(patch.object(zb, "load_tax_rules", return_value=tax_rules))
            stack.enter_context(patch.object(zb, "load_vendor_rules", return_value={"vendor_alias_map": {}, "_source": "offline_test"}))
            stack.enter_context(patch.object(zb, "fetch_tax_ids_from_zoho", side_effect=lambda rules, **kw: rules))
            # Exercise real duplicate loading rather than assuming the returned set.
            def fake_get(endpoint, params=None):
                if endpoint == "bills":
                    return {"bills": [{"bill_number": number} for number in (existing or [])],
                            "page_context": {"has_more_page": False}}
                if readback is not None and endpoint.startswith("bills/"):
                    return {"bill": copy.deepcopy(readback)}
                raise AssertionError(endpoint)
            stack.enter_context(patch.object(zb, "zoho_get", side_effect=fake_get))
            stack.enter_context(patch.object(zb, "fetch_vendors", return_value=loaded_vendors or vendors()))
            stack.enter_context(patch.object(zb, "_load_parse_cache", return_value=cache))
            stack.enter_context(patch.object(zb, "extract_text_from_pdf", return_value=""))
            stack.enter_context(patch.object(zb, "get_usd_to_inr", return_value=None))
            parser = stack.enter_context(patch.object(zb, "parse_pdf_with_claude", return_value=copy.deepcopy(parsed)))
            poster = stack.enter_context(patch.object(zb, "zoho_post", side_effect=post or AssertionError("Unapproved POST forbidden")))
            args = [zb.__file__, "--file", str(source)] + (["--preview"] if preview else [])
            args += ["--approve", "--approver", approver] if approver else []
            stack.enter_context(patch.object(sys, "argv", args))
            stack.enter_context(contextlib.redirect_stdout(output))
            zb.main()
            log = json.loads(Path(zb.LOG_PATH).read_text(encoding="utf-8"))
            return log, poster.call_count, parser.call_count, output.getvalue()


class VendorMatchingTests(unittest.TestCase):
    def test_fetch_keeps_same_name_registrations_across_pages(self):
        responses = [
            {"contacts": [{"contact_name": "MAERSK A/S", "contact_id": MAERSK_ID, "gst_no": GSTIN}],
             "page_context": {"has_more_page": True}},
            {"contacts": [
                {"contact_name": "MAERSK A/S", "contact_id": "gj", "gst_no": "24AAJCM4693D1ZE"},
                {"contact_name": "MAERSK A/S", "contact_id": "tn", "gst_no": "33AAJCM4693D1Z5"}],
             "page_context": {"has_more_page": False}},
        ]
        with patch.object(zb, "zoho_get", side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
            loaded = zb.fetch_vendors()
        self.assertEqual(len(loaded["MAERSK A/S"]["duplicates"]), 2)
        for name in ("MAERSK A/S", "Maersk Line India Pvt. Ltd."):
            self.assertEqual(zb.match_vendor_tiered(name, loaded, GSTIN)[0], MAERSK_ID)

    def test_unique_gstin_resolves_indian_agent_name(self):
        self.assertEqual(zb.match_vendor_tiered(invoice()["vendor_name"], vendors(), GSTIN),
                         (MAERSK_ID, "MAERSK A/S", "gstin", GSTIN))

    def test_ambiguous_missing_and_conflicting_identity(self):
        self.assertEqual(zb.match_vendor_tiered("MAERSK A/S", vendors())[2], "ambiguous")
        self.assertIsNone(zb.match_vendor_tiered("Maersk Line India Pvt. Ltd.", vendors())[0])
        self.assertEqual(zb.match_vendor_tiered("MAERSK A/S", vendors(), "bad-gstin")[2], "invalid_gstin")
        self.assertEqual(zb.match_vendor_tiered("MAERSK A/S", vendors(), "27ABCDE1234F1Z5")[2], "gstin_unmatched")
        duplicate_gst = vendors()
        duplicate_gst["OTHER CONTACT"] = {"id": "another-id", "gst_no": GSTIN}
        self.assertEqual(zb.match_vendor_tiered("MAERSK A/S", duplicate_gst, GSTIN)[2], "ambiguous")

    def test_existing_exact_and_alias_matches_still_work(self):
        self.assertEqual(zb.match_vendor_tiered("MAERSK A/S", vendors(), GSTIN)[2], "exact")
        with patch.object(zb, "VENDOR_ALIAS_MAP", {"AGENT": "MAERSK A/S"}):
            self.assertEqual(zb.match_vendor_tiered("Agent", vendors(), GSTIN)[0:3],
                             (MAERSK_ID, "MAERSK A/S", "alias"))

    def run_pipeline(self, *args, **kwargs):
        return run_pipeline(*args, **kwargs)

    def test_clean_invoice_resolves_vendor_but_requires_approval(self):
        parsed = invoice()
        self.assertIsNone(zb.validate_before_post(parsed, MAERSK_ID))
        log, posts, parses, _ = self.run_pipeline(parsed)
        self.assertEqual(log["needs_review"][0]["reason"], "vendor_gstin_match_requires_approval")
        self.assertEqual(log["needs_review"][0]["vendor_id"], MAERSK_ID)
        self.assertEqual((posts, parses), (0, 0))

    def test_poor_quality_invoice_remains_held(self):
        parsed = invoice()
        parsed["confidence"] = "low"
        log, posts, _, _ = self.run_pipeline(parsed)
        self.assertIn("low_confidence", log["needs_review"][0]["reason"])
        self.assertEqual(posts, 0)

    def test_duplicate_invoice_is_not_posted(self):
        log, posts, _, _ = self.run_pipeline(invoice(), existing={invoice()["bill_number"]})
        self.assertEqual(len(log["skipped_duplicate"]), 1)
        self.assertEqual(posts, 0)

    def test_tax_mismatch_remains_held(self):
        parsed = invoice()
        parsed["tax_amount_cgst"] += 100
        log, posts, _, _ = self.run_pipeline(parsed)
        self.assertIn("total_reconciliation_failed", log["needs_review"][0]["reason"])
        self.assertEqual(posts, 0)

    def test_missing_mandatory_field_remains_held(self):
        parsed = invoice()
        parsed["bill_date"] = ""
        log, posts, _, _ = self.run_pipeline(parsed)
        self.assertEqual(log["needs_review"][0]["reason"], "bill_date empty")
        self.assertEqual(posts, 0)

    def test_retry_after_remote_success_is_duplicate(self):
        # Exercise a response lost AFTER the simulated server has stored the bill.
        # Reaching the fake server now takes a queued proposal plus an approval.
        parsed = approvable_invoice()
        remote_bills = set()

        def store_then_disconnect(endpoint, payload):
            self.assertEqual(endpoint, "bills")
            remote_bills.add(payload["bill_number"])
            raise TimeoutError("response lost after server committed")

        with tempfile.TemporaryDirectory(prefix="zoho-vendor-test-") as folder:
            log, posts, _, _ = self.run_pipeline(parsed, folder=folder)
            self.assertEqual((posts, len(log["awaiting_approval"])), (0, 1))
            log, posts, parses, _ = self.run_pipeline(parsed, existing=remote_bills, post=store_then_disconnect,
                                                      approver="Test Approver", folder=folder)
        self.assertEqual((posts, parses), (1, 0))
        self.assertEqual(log["verified"], [])
        self.assertIn("response lost", log["failed"][0]["error"])
        self.assertEqual(remote_bills, {parsed["bill_number"]})
        for approver in (None, "Test Approver"):
            log, posts, parses, _ = self.run_pipeline(parsed, existing=remote_bills, approver=approver)
            self.assertEqual(len(log["skipped_duplicate"]), 1)
            self.assertEqual((posts, parses), (0, 0))

    def test_real_invoice_preview_reconciles_every_component(self):
        parsed = invoice()
        log, posts, parses, output = self.run_pipeline(parsed, preview=True)
        self.assertIn('"vendor_id": "' + MAERSK_ID + '"', output)
        self.assertIn('[PREVIEW] would POST:', output)
        self.assertEqual((posts, parses), (0, 0))
        self.assertEqual(log["verified"], [])
        route = zb.determine_tax_route(parsed, "MAERSK A/S", GSTIN)
        payload = zb.build_payload(parsed, MAERSK_ID, route)
        self.assertEqual(parsed["line_items"][3]["tax_percentage"], 5)
        self.assertEqual([line["tax_id"] for line in payload["line_items"]],
                         [zb.get_tax_id("cgst_sgst_" + str(rate)) for rate in (18, 18, 18, 5, 5, 18)])
        self.assertEqual(payload["bill_number"], parsed["bill_number"])
        self.assertEqual(payload["date"], parsed["bill_date"])
        self.assertEqual(payload["currency_code"], "INR")
        self.assertEqual(payload["due_date"], "2026-10-18")
        self.assertEqual(payload["reference_number"], "277529380")
        self.assertEqual(round(sum(line["rate"] for line in payload["line_items"]), 2), 348267.25)
        check = zb.reconcile_payload_taxes(parsed, payload, route)
        self.assertTrue(check["ok"], check)
        self.assertTrue(check["approval_required"])
        for key, value in {"subtotal": 348267.25, "tax_amount_cgst": 10670.69,
                           "tax_amount_sgst": 10670.69, "tax_amount_igst": 0,
                           "invoice_total": 369608.63}.items():
            self.assertEqual(check["amounts"][key], {"expected": value, "calculated": value})

    def test_exact_name_cannot_bypass_changed_tax_approval(self):
        parsed = invoice()
        parsed["vendor_name"] = "MAERSK A/S"
        log, posts, parses, _ = self.run_pipeline(parsed)
        self.assertEqual(log["needs_review"][0]["reason"], "line_tax_match_requires_approval")
        self.assertEqual((posts, parses), (0, 0))

    def test_balanced_total_cannot_hide_wrong_components_or_line_rates(self):
        for mutation in ("components", "line_rate", "missing_rate"):
            with self.subTest(mutation=mutation):
                parsed = invoice()
                parsed["vendor_name"] = "MAERSK A/S"
                if mutation == "components":
                    parsed["tax_amount_cgst"] += 1
                    parsed["tax_amount_sgst"] -= 1
                elif mutation == "line_rate":
                    parsed["line_items"][3]["tax_percentage"] = 18
                else:
                    parsed["line_items"][3].pop("tax_percentage")
                self.assertIsNone(zb.validate_before_post(parsed, MAERSK_ID))
                log, posts, parses, output = self.run_pipeline(parsed, preview=True)
                self.assertIn("payload_tax_amount_cgst_mismatch", log["needs_review"][0]["reason"])
                self.assertNotIn("[PREVIEW] would POST:", output)
                self.assertEqual((posts, parses), (0, 0))

    def test_unsupported_rates_and_missing_tax_ids_hold(self):
        for rate in (0, 12, "garbage", "NaN", "Infinity"):
            with self.subTest(rate=rate):
                parsed = invoice()
                parsed["line_items"][3]["tax_percentage"] = rate
                log, posts, parses, _ = self.run_pipeline(parsed, preview=True)
                self.assertIn("line_tax_percentage_", log["needs_review"][0]["reason"])
                self.assertEqual((posts, parses), (0, 0))
        parsed = invoice()
        route = zb.determine_tax_route(parsed, "MAERSK A/S", GSTIN)
        with patch.object(zb, "_TAX_ID_MAP", {"cgst_sgst_5": "five"}):
            with self.assertRaisesRegex(ValueError, "tax_id_missing:cgst_sgst_18"):
                zb.build_payload(parsed, MAERSK_ID, route)

    def test_ambiguous_gstin_is_held_in_actual_pipeline(self):
        contacts = vendors()
        contacts["OTHER CONTACT"] = {"id": "different-contact", "gst_no": GSTIN}
        log, posts, parses, _ = self.run_pipeline(invoice(), loaded_vendors=contacts)
        self.assertIn("vendor_match_ambiguous", log["needs_review"][0]["reason"])
        self.assertEqual((posts, parses), (0, 0))

    def test_reconciliation_rejects_tampered_payload_and_missing_source(self):
        parsed = invoice()
        route = zb.determine_tax_route(parsed, "MAERSK A/S", GSTIN)
        payload = zb.build_payload(parsed, MAERSK_ID, route)
        for field in ("subtotal", "tax_amount_cgst", "tax_amount_sgst", "tax_amount_igst", "invoice_total"):
            missing = copy.deepcopy(parsed)
            missing.pop(field)
            self.assertFalse(zb.reconcile_payload_taxes(missing, payload, route)["ok"])
        for field, value in (("tax_id", zb.get_tax_id("cgst_sgst_18")), ("rate", 1)):
            tampered = copy.deepcopy(payload)
            tampered["line_items"][3][field] = value
            self.assertFalse(zb.reconcile_payload_taxes(parsed, tampered, route)["ok"])


if __name__ == "__main__":
    unittest.main()
