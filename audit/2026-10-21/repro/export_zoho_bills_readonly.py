r"""Read-only Zoho Books export for the audit reconciliation (F20, F05, F14, A8).

Run on the Windows machine that runs the pipeline, from the repository root:
    python audit\2026-10-21\repro\export_zoho_bills_readonly.py

It loads src/zoho_bills.py (hash-checked against LIVE_SOURCE_FINGERPRINTS.json)
and uses only its own auth and GET helpers. It never posts, never runs the
pipeline and never reads PDFs. Its only writes are one JSON file in the current
directory and the access-token refresh zoho_bills.py always persists to its
config. Upload the JSON and the PDFs it lists to the review session. Do not
commit either.
"""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FINGERPRINTS = ROOT / "audit" / "2026-10-21" / "LIVE_SOURCE_FINGERPRINTS.json"

# invoice_log statuses whose bill must be checked in Zoho, and why.
STATUSES = {
    "created": "posted by the pipeline: needs PDF parity (F20)",
    "verified": "posted with an approval and passed read-back: needs PDF parity (F20)",
    "posted_unverified": "posted; read-back never completed (F03)",
    "post_verify_failed": "posted but failed read-back (F03)",
    "failed": "POST may have succeeded before a timeout (F14)",
    "skipped_duplicate": "matched an existing number, possibly another vendor's (F05)",
}
BILL_FIELDS = (
    "bill_id", "bill_number", "vendor_id", "vendor_name", "gst_no", "gst_treatment",
    "source_of_supply", "destination_of_supply", "date", "due_date", "status",
    "currency_code", "exchange_rate", "is_reverse_charge_applied", "is_inclusive_tax",
    "sub_total", "tax_total", "total", "taxes", "reference_number", "notes",
    "documents", "created_time", "last_modified_time",
)
LINE_FIELDS = (
    "description", "account_id", "account_name", "hsn_or_sac", "quantity", "rate",
    "item_total", "tax_id", "tax_name", "tax_percentage",
    "reverse_charge_tax_id", "reverse_charge_tax_amount",
)


def load_pipeline(path):
    expected = json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["zoho_bills.py"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    if actual != expected:
        print(f"NOTE: {path} differs from the audited fingerprint (expected once fixes are merged)")
    spec = importlib.util.spec_from_file_location("zoho_bills_readonly", path)
    zb = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(zb)

    def refuse_post(*args, **kwargs):
        raise RuntimeError("read-only export: zoho_post is disabled")

    zb.zoho_post = refuse_post
    return zb, actual


def trim_bill(bill):
    out = {k: bill.get(k) for k in BILL_FIELDS if k in bill}
    out["line_items"] = [{k: li.get(k) for k in LINE_FIELDS if k in li}
                         for li in bill.get("line_items", [])]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zoho-bills", type=Path, default=ROOT / "src" / "zoho_bills.py")
    parser.add_argument("--pause", type=float, default=0.7,
                        help="seconds between bill detail calls (Zoho allows 100/min)")
    args = parser.parse_args()

    zb, source_sha = load_pipeline(args.zoho_bills)
    zb._run_id_ctx = "readonly-audit-export-" + zb.generate_run_id()
    with contextlib.redirect_stdout(io.StringIO()):
        zb.refresh_token()
    auth = {
        "source_module": zb.get_auth_source_module(),
        "source_file": zb.get_auth_source_file(),
        "books_domain": zb.get_zoho_domain(),
        "organization_id_masked": zb.mask_value(zb.get_org_id()),
    }
    print(f"Auth: {auth['source_module']} | org {auth['organization_id_masked']}")
    if not auth["source_module"].endswith("legacy Zoho config path"):
        print("WARNING: not the legacy config. The pipeline posts with this same "
              "resolution, so check which org these bills went to (finding A8).")

    zoho_bills = []
    page = 1
    while True:
        data = zb.zoho_get("bills", {"page": page, "per_page": 200})
        zoho_bills.extend(data.get("bills", []))
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    by_number = {}
    for b in zoho_bills:
        by_number.setdefault(str(b.get("bill_number", "")).strip().upper(), []).append(b)
    print(f"Zoho bills listed: {len(zoho_bills)}")

    with open(zb.INVOICE_LOG_PATH, encoding="utf-8") as f:
        invoice_log = json.load(f)
    entries = [e for e in invoice_log if e.get("status") in STATUSES and e.get("bill_number")]

    details, matches = {}, []
    for entry in entries:
        found = by_number.get(str(entry["bill_number"]).strip().upper(), [])
        for b in found:
            bill_id = b.get("bill_id")
            if bill_id and bill_id not in details:
                try:
                    details[bill_id] = trim_bill(zb.zoho_get(f"bills/{bill_id}").get("bill", {}))
                except Exception as e:
                    details[bill_id] = {"bill_id": bill_id, "error": str(e)[:200]}
                time.sleep(args.pause)
        matches.append({"invoice_log": entry, "why": STATUSES[entry["status"]],
                        "zoho_bill_ids": [b.get("bill_id") for b in found]})

    pdfs = sorted({m["invoice_log"]["file"] for m in matches
                   if m["invoice_log"]["status"] in ("created", "verified", "posted_unverified",
                                                   "post_verify_failed", "failed")
                   and m["zoho_bill_ids"]})
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path.cwd() / f"zoho_audit_export_{stamp}.json"
    out_path.write_text(json.dumps({
        "generated_at": stamp, "zoho_bills_sha256": source_sha, "auth": auth,
        "invoice_log_entries": len(invoice_log),
        "status_counts": {s: sum(1 for e in invoice_log if str(e.get("status")) == s)
                          for s in sorted({str(e.get("status")) for e in invoice_log})},
        "zoho_bill_count": len(zoho_bills),
        "matches": matches, "zoho_bill_details": details, "pdfs_needed": pdfs,
    }, indent=2, default=str), encoding="utf-8")

    errors = sum(1 for d in details.values() if "error" in d)
    print(f"Checked {len(matches)} invoice_log entries, fetched {len(details)} bills "
          f"({errors} errors). PDFs needed: {len(pdfs)}")
    print(f"Wrote {out_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
