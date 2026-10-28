"""Offline extraction safety checks. No supplier PDFs or provider calls."""
import unittest
import json
from unittest.mock import patch
from extraction import normalize_suggestions, request_anthropic, ExtractionError

PDF=b"%PDF-1.4\nsynthetic\n%%EOF"

def local_invoice(**changes):
    d={"supplier":"Synthetic CMA CGM billing agent","gstin":"27AABCC9048G1ZL",
       "invoice_number":"INEMHC27098373","invoice_date":"2026-09-28","currency":"INR",
       "subtotal":"27579.52","cgst":"2482.16","sgst":"2482.16","igst":"0",
       "discount":"0","total":"32543.84","multi_invoice":False,"issues":[]}
    d.update(changes)
    return d

class ExtractionContract(unittest.TestCase):
    def test_maharashtra_tax_heads_unchanged(self):
        x=normalize_suggestions(local_invoice())
        self.assertEqual("suggestions_only",x["status"])
        self.assertEqual("2482.16",x["fields"]["cgst"])
        self.assertEqual("2482.16",x["fields"]["sgst"])
        self.assertEqual("0.00",x["fields"]["igst"])
        self.assertFalse(x["source_verified"])

    def test_multi_invoice_hold(self):
        x=normalize_suggestions(local_invoice(multi_invoice=True))
        self.assertEqual("multi_invoice_hold",x["status"])
        self.assertEqual({},x["fields"])

    def test_overseas_usd_without_invented_gst_or_fx(self):
        x=normalize_suggestions(local_invoice(gstin=None,currency="USD",
                    subtotal="20",cgst="0",sgst="0",igst="0",total="20"))
        self.assertNotIn("gstin",x["fields"])
        self.assertNotIn("fx",x["fields"])
        self.assertEqual("USD",x["fields"]["currency"])

    def test_mismatch_flagged_not_recomputed(self):
        x=normalize_suggestions(local_invoice(total="34000.00"))
        self.assertEqual("34000.00",x["fields"]["total"])
        self.assertIn("do not match", " ".join(x["issues"]))

    def test_provider_error_no_default_values(self):
        class Broken:
            def __enter__(self):return self
            def __exit__(self,*a):return None
            def read(self,n):return b"bad-json"
        with self.assertRaises(ExtractionError):
            request_anthropic(PDF,"synthetic.pdf","test",transport=lambda *a,**k:Broken())

    def test_cma_bill_of_lading_is_withheld_without_inventing_invoice_number(self):
        x=normalize_suggestions(local_invoice(invoice_number="AMC1234567"))
        self.assertNotIn("invoice_number",x["fields"])
        self.assertIn("AMC1234567", " ".join(x["issues"]))
        self.assertIn("bill-of-lading", " ".join(x["issues"]))
        self.assertFalse(x["source_verified"])

    def test_same_prefix_for_other_supplier_is_not_rewritten(self):
        x=normalize_suggestions(local_invoice(supplier="Synthetic local supplier",invoice_number="AMC1234567"))
        self.assertEqual("AMC1234567",x["fields"]["invoice_number"])

    def test_unknown_tax_and_credit_are_flagged_and_never_defaulted_to_zero(self):
        x=normalize_suggestions(local_invoice(igst=None,discount=None))
        self.assertNotIn("igst",x["fields"])
        self.assertNotIn("discount",x["fields"])
        self.assertIn("igst", " ".join(x["issues"]))
        self.assertIn("discount", " ".join(x["issues"]))

    def test_docker_copy_inputs_survive_context_filter(self):
        import fnmatch
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        patterns=(root/".dockerignore").read_text().splitlines()
        copies=[line for line in (root/"cloud_review"/"Dockerfile").read_text().splitlines() if line.startswith("COPY ")]
        for filename in [name for line in copies for name in line.split()[1:-1]]:
            with self.subTest(filename=filename):
                included=True
                for rule in patterns:
                    if fnmatch.fnmatchcase(filename,rule.lstrip("!")):
                        included=rule.startswith("!")
                self.assertTrue(included, filename+" is excluded from Docker context")
                self.assertTrue((root/filename).is_file())
        for filename in ('config.json','.env','parsed_cache.json','cloud_review/config.json','src/config.json'):
            included=True
            for rule in patterns:
                if fnmatch.fnmatchcase(filename,rule.lstrip('!')):included=rule.startswith('!')
            self.assertFalse(included,filename+' must not enter the image context')

if __name__=="__main__":unittest.main()
