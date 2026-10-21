"""Offline reproduction of the 21-Oct-2026 audit findings against src/.

Runs the real pipeline code with Zoho, Anthropic and FX calls replaced by
in-memory fakes. Network access is blocked for the whole run, nothing is
posted anywhere, and all state files are written to a temporary directory.

Each check asserts the behaviour the business needs, not the current one:
  DEFECT     the code does something unsafe (counts toward exit code 1)
  FAIL-SAFE  the code routes the risky case to review (finding downgraded)
  INFO       behaviour worth knowing that is not by itself a defect

Usage (from the repository root, Linux/macOS only for pipeline checks):
  python3 audit/2026-10-21/repro/reproduce_findings.py
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"

# Importing zoho_bills requires an Anthropic key; no request is ever sent.
os.environ.setdefault("ANTHROPIC_API_KEY", "audit-repro-not-a-key")


def _blocked_urlopen(*args, **kwargs):
    raise RuntimeError("network disabled in audit reproduction")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SRC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


zb = _load("zoho_bills")
watcher = _load("watcher")

# main() hardcodes r"C:\temp\zoho\zoho.lock". On Windows that is the live lock
# file, so the end-to-end pipeline checks only run where it resolves to a
# harmless relative filename inside the temporary directory.
PIPELINE_SAFE = os.name != "nt"

RESULTS = []
TEMP_DIRS = []


def record(finding, verdict, summary, evidence):
    RESULTS.append({"finding": finding, "verdict": verdict,
                    "summary": summary, "evidence": evidence})


VENDOR_GSTIN = "27ABCDE1234F1Z5"
VENDORS = {
    "SAMPLE CLEARING SERVICES PVT LTD": {"id": "9001", "gst_no": VENDOR_GSTIN},
    "SAMPLE SHIPPING AGENCIES PVT LTD": {"id": "9002", "gst_no": "27FGHIJ5678K1Z2"},
}


def domestic_invoice(**overrides):
    parsed = {
        "vendor_name": "SAMPLE CLEARING SERVICES PVT LTD",
        "vendor_gstin": VENDOR_GSTIN,
        "vendor_found_in_zoho": True,
        "bill_number": "SCS/2026/0417",
        "bill_date": "2026-10-01",
        "due_date": None,
        "currency_code": "INR",
        "is_overseas": False,
        "line_items": [{
            "description": "Customs clearance and documentation service",
            "quantity": 1, "rate": 300000.00, "hsn_or_sac": "996719",
            "tax_name": "CGST 9%+SGST 9%", "tax_percentage": 18,
        }],
        "subtotal": 300000.00, "discounts_or_credits": 0,
        "tax_amount_igst": 0, "tax_amount_cgst": 27000.00, "tax_amount_sgst": 27000.00,
        "invoice_total": 354000.00,
        "mbl_number": "", "notes": "", "confidence": "high", "issues": "",
    }
    parsed.update(overrides)
    return parsed


def zoho_bill_for(parsed, vendor_id, **overrides):
    """A Zoho GET /bills/{id} response that matches the parsed invoice."""
    tax = sum(float(parsed.get(f) or 0) for f in ("tax_amount_igst", "tax_amount_cgst", "tax_amount_sgst"))
    bill = {
        "bill_id": "900000000001", "bill_number": parsed["bill_number"],
        "vendor_id": vendor_id, "date": parsed["bill_date"], "currency_code": "INR",
        "sub_total": parsed["subtotal"], "tax_total": round(tax, 2), "total": parsed["invoice_total"],
        "is_reverse_charge_applied": False,
        "line_items": [{"rate": li["rate"], "quantity": 1, "tax_id": zb.get_tax_id("cgst_sgst_18")}
                       for li in parsed["line_items"]],
    }
    bill.update(overrides)
    return bill


def run_pipeline(parsed, zoho_bill, existing_bills=(), usd_rate=83.0, approver=None, tmp=None):
    """Run zoho_bills.main() for one PDF with every external call faked.

    Pass the previous result's tmp to keep state (approval queue, logs) and the
    same PDF across runs; approver adds --approve --approver NAME.
    """
    if tmp is None:
        tmp = tempfile.mkdtemp(prefix="zoho_audit_repro_")
        TEMP_DIRS.append(tmp)
    pdf = os.path.join(tmp, "invoice.pdf")
    if not os.path.exists(pdf):
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 audit placeholder " + os.urandom(8))
    posts = []

    def fake_post(endpoint, payload):
        posts.append({"endpoint": endpoint, "payload": copy.deepcopy(payload)})
        return {"bill": {"bill_id": "900000000001"}}

    state = {name: os.path.join(tmp, name + ".json") for name in (
        "run_log", "parsed_cache", "invoice_log", "failed_invoices",
        "run_manifest", "audit_baseline", "daily_metrics", "system_health",
        "approval_queue", "approvals")}
    fakes = {
        "LOG_PATH": state["run_log"], "CACHE_PATH": state["parsed_cache"],
        "INVOICE_LOG_PATH": state["invoice_log"],
        "FAILED_INVOICES_PATH": state["failed_invoices"],
        "RUN_MANIFEST_PATH": state["run_manifest"],
        "AUDIT_BASELINE_PATH": state["audit_baseline"],
        "DAILY_METRICS_PATH": state["daily_metrics"],
        "SYSTEM_HEALTH_PATH": state["system_health"],
        "APPROVAL_QUEUE_PATH": state["approval_queue"],
        "APPROVALS_PATH": state["approvals"],
        "TAX_RULES_PATH": str(SRC / "tax_rules.json"),
        "VENDOR_RULES_PATH": str(SRC / "vendor_rules.json"),
        "refresh_token": lambda: None,
        "fetch_tax_ids_from_zoho": lambda rules, run_id="unset": rules,
        "fetch_existing_bills": lambda: set(existing_bills),
        "fetch_vendors": lambda: copy.deepcopy(VENDORS),
        "parse_pdf_with_claude": lambda fp, fn, vl: copy.deepcopy(parsed),
        "extract_text_from_pdf": lambda fp: "",
        "get_usd_to_inr": lambda: usd_rate,
        "zoho_post": fake_post,
        "zoho_get": lambda endpoint, params=None: {"bill": copy.deepcopy(zoho_bill)},
        "notify": lambda *a, **k: None,
    }
    out = io.StringIO()
    cwd = os.getcwd()
    with contextlib.ExitStack() as stack:
        for attr, value in fakes.items():
            stack.enter_context(mock.patch.object(zb, attr, value))
        stack.enter_context(mock.patch.object(zb.time, "sleep", lambda s: None))
        argv = ["zoho_bills.py", "--file", pdf, "--run-id", "AUDIT-REPRO"]
        argv += ["--approve", "--approver", approver] if approver else []
        stack.enter_context(mock.patch.object(sys, "argv", argv))
        os.chdir(tmp)
        try:
            with contextlib.redirect_stdout(out):
                zb.main()
        finally:
            os.chdir(cwd)

    def read(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return default

    run_log = read(state["run_log"], {})
    return {"tmp": tmp, "file": "invoice.pdf", "posts": posts, "run_log": run_log,
            "invoice_log": read(state["invoice_log"], []), "stdout": out.getvalue(),
            "approval_queue": read(state["approval_queue"], {})}


def run_approved(parsed, zoho_bill, **kwargs):
    """Queue the proposal, then approve it: the only path to zoho_post after F04."""
    first = run_pipeline(parsed, zoho_bill, **kwargs)
    return run_pipeline(parsed, zoho_bill, approver="Audit Reviewer", tmp=first["tmp"], **kwargs)


def watcher_status(result):
    """What watcher.run_for_file() reports for the file just processed."""
    # run_for_file reads the literal path r"C:\temp\zoho\run_log.json"; off
    # Windows that is a plain filename in the current directory.
    run_log_name = r"C:\temp\zoho\run_log.json"

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    cwd = os.getcwd()
    os.chdir(result["tmp"])
    try:
        with open(run_log_name, "w", encoding="utf-8") as f:
            json.dump(result["run_log"], f)
        with mock.patch.object(watcher.subprocess, "Popen", lambda *a, **k: FakeProc()), \
                mock.patch.object(watcher, "log", lambda msg: None):
            return watcher.run_for_file(result["file"], "AUDIT-REPRO")
    finally:
        os.chdir(cwd)


def statuses(result):
    return [e.get("status") for e in result["invoice_log"]]


# ── Pipeline checks (real main(), faked externals) ─────────────────────────────
def verify_errors(r):
    return " ".join(e.get("error", "") for e in r["invoice_log"] if e.get("status") == "post_verify_failed")


def check_f04_f01_f02():
    parsed = domestic_invoice()
    exact = zoho_bill_for(parsed, "9001")
    unapproved = run_pipeline(parsed, exact)
    # Simulate a re-extraction that yields a different payload for the same PDF.
    os.remove(os.path.join(unapproved["tmp"], "parsed_cache.json"))
    changed = run_pipeline(dict(parsed, due_date="2026-12-31"), exact,
                           approver="Audit Reviewer", tmp=unapproved["tmp"])
    posted = len(unapproved["posts"]) + len(changed["posts"])
    record("F04", "DEFECT" if posted else "FAIL-SAFE",
           "Exact-name, reconciled bill is POSTed with no human approval step",
           f"no approval -> posts={len(unapproved['posts'])} status={statuses(unapproved)}; "
           f"approval of a payload that changed since review -> posts={len(changed['posts'])}")

    control = "POST_VERIFY_PASSED" in run_approved(parsed, exact)["stdout"]
    # Zoho stores a total ₹6,45,999 off the printed ₹3,54,000.
    f01 = run_approved(parsed, zoho_bill_for(parsed, "9001", total=999999.00))
    f01_passed = "POST_VERIFY_PASSED" in f01["stdout"]
    record("F01", "DEFECT" if f01_passed or not control else "FAIL-SAFE",
           "Read-back verification passes although Zoho total differs from printed total",
           f"exact read-back verified={control}; printed total=354000.00 zoho total=999999.00 -> "
           f"verify passed={f01_passed}; errors={verify_errors(f01)[:90]!r}")
    # ₹2,900 on a ₹3,00,000 bill sat inside the old 1% band.
    f02 = run_approved(parsed, zoho_bill_for(parsed, "9001", sub_total=302900.00, total=356900.00))
    f02_passed = "POST_VERIFY_PASSED" in f02["stdout"]
    record("F02", "DEFECT" if f02_passed or not control else "FAIL-SAFE",
           "Subtotal off by ₹2,900 on a ₹3,00,000 bill is accepted (1% tolerance)",
           f"expected sub_total=300000.00 zoho sub_total=302900.00 -> verify passed={f02_passed}; "
           f"errors={verify_errors(f02)[:90]!r}")


def check_f03():
    parsed = domestic_invoice(bill_number="SCS/2026/0418")
    bill = zoho_bill_for(parsed, "9001", date="2026-09-01")  # wrong date in Zoho
    r = run_approved(parsed, bill)
    log = statuses(r)
    success_keys = [k for k in ("created", "verified")
                    if any(e.get("file") == r["file"] for e in r["run_log"].get(k, []))]
    w_status, w_detail = watcher_status(r)
    defect = (bool(r["posts"]) and (bool(success_keys) or {"created", "verified"} & set(log)
                                    or watcher.should_mark_seen(w_status)))
    record("F03", "DEFECT" if defect else "FAIL-SAFE",
           "A bill that fails read-back verification is reported 'created' and marked done",
           f"posts={len(r['posts'])}; invoice_log={log}; run_log success lists with file={success_keys}; "
           f"watcher status={w_status!r} -> should_mark_seen={watcher.should_mark_seen(w_status)}")


def check_f05():
    # Zoho already holds bill 'INV-1001' from some other vendor.
    parsed = domestic_invoice(bill_number="INV-1001")
    r = run_pipeline(parsed, zoho_bill_for(parsed, "9001"), existing_bills={"INV-1001"})
    w_status, _ = watcher_status(r)
    skipped = statuses(r) == ["skipped_duplicate"] and not r["posts"]
    record("F05", "DEFECT" if skipped else "FAIL-SAFE",
           "A different vendor's bill with the same number is silently skipped as a duplicate",
           f"existing bill numbers carry no vendor: {sorted(set(['INV-1001']))}; "
           f"posts={len(r['posts'])}; invoice_log={statuses(r)}; watcher status={w_status!r} "
           f"-> should_mark_seen={watcher.should_mark_seen(w_status)}")


def check_a5_usd_domestic_vendor():
    parsed = domestic_invoice(
        vendor_name="SAMPLE SHIPPING AGENCIES PVT LTD", vendor_gstin="27FGHIJ5678K1Z2",
        bill_number="SSA-EXP-7731", currency_code="USD", is_overseas=False,
        line_items=[{"description": "Ocean freight Nhava Sheva to Jebel Ali",
                     "quantity": 1, "rate": 1000.00, "hsn_or_sac": "996521",
                     "tax_percentage": 0}],
        subtotal=1000.00, tax_amount_cgst=0, tax_amount_sgst=0, invoice_total=1000.00)
    bill = zoho_bill_for(parsed, "9002", sub_total=83000.00, total=83000.00,
                         is_reverse_charge_applied=True,
                         line_items=[{"reverse_charge_tax_id": "914109000000014234",
                                      "reverse_charge_tax_amount": 14940.0}])
    r = run_pipeline(parsed, bill, usd_rate=83.0)
    proposals = list(r["approval_queue"].values())
    payload = r["posts"][0]["payload"] if r["posts"] else (proposals[0]["payload"] if proposals else {})
    rcm = payload.get("is_reverse_charge_applied") is True
    record("A5", "DEFECT" if rcm else "FAIL-SAFE",
           "Indian-GSTIN vendor's USD invoice is proposed for posting as an RCM import of service",
           f"vendor GSTIN=27FGHIJ5678K1Z2, AI is_overseas=False, currency=USD -> "
           f"posts={len(r['posts'])}, status={statuses(r)}, "
           f"is_reverse_charge_applied={payload.get('is_reverse_charge_applied')}, "
           f"line rate={payload.get('line_items', [{}])[0].get('rate')} (USD 1000 x live rate 83.0)")


def check_f09_failsafe():
    parsed = domestic_invoice(bill_number="SCS/2026/0419")
    parsed["line_items"][0]["tax_percentage"] = 12
    r1 = run_pipeline(parsed, zoho_bill_for(parsed, "9001"))
    parsed2 = domestic_invoice(bill_number="SCS/2026/0420", tax_amount_igst=54000.00,
                               tax_amount_cgst=0, tax_amount_sgst=0)
    r2 = run_pipeline(parsed2, zoho_bill_for(parsed2, "9001"))
    safe = not r1["posts"] and not r2["posts"] and \
        statuses(r1) == ["needs_review"] and statuses(r2) == ["needs_review"]
    reasons = [e.get("error", "")[:70] for e in r1["invoice_log"] + r2["invoice_log"]]
    record("F09", "FAIL-SAFE" if safe else "DEFECT",
           "Unsupported GST rate and a route/printed-tax mismatch both go to review, not Zoho",
           f"12% line -> posts={len(r1['posts'])}; intra-state vendor with printed IGST -> "
           f"posts={len(r2['posts'])}; reasons={reasons}")


# ── Unit checks ────────────────────────────────────────────────────────────────
def check_f06():
    two_invoices = [
        {"vendor_name": "A", "bill_number": "PRODUCT-1", "line_items": [{"rate": 4200.0}]},
        {"vendor_name": "A", "bill_number": "PLATFORM-FEE-1", "line_items": [{"rate": 15.0}]},
    ]
    body = json.dumps({"content": [{"text": json.dumps(two_invoices)}], "usage": {}}).encode()

    class Resp:
        def read(self):
            return body

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 placeholder")
    try:
        with mock.patch.object(zb.urllib.request, "urlopen", lambda *a, **k: Resp()), \
                contextlib.redirect_stdout(io.StringIO()):
            result = zb.parse_pdf_with_claude_once(f.name, "multi.pdf", ["A"])
    finally:
        os.unlink(f.name)
    kept = result.get("bill_number") if isinstance(result, dict) else None
    record("F06", "DEFECT" if isinstance(result, dict) else "FAIL-SAFE",
           "A PDF with two invoices yields one bill; the other is dropped without review",
           f"extractor returned 2 invoices -> kept {kept!r}, dropped 'PLATFORM-FEE-1'")


def payload_line(**item):
    parsed = domestic_invoice()
    parsed["line_items"] = [dict(parsed["line_items"][0], **item)]
    route = {"is_overseas": False, "tax_mode": "cgst_sgst", "tax_id_key": "cgst_sgst_18"}
    return zb.build_payload(parsed, "9001", route)["line_items"][0]


def check_f07():
    seven = payload_line(hsn_or_sac="1234567").get("hsn_or_sac")
    ten = payload_line(hsn_or_sac="9965110000").get("hsn_or_sac")
    record("F07", "DEFECT" if seven is None or ten == "99651100" else "FAIL-SAFE",
           "Invalid HSN/SAC is silently dropped or truncated instead of routed to review",
           f"7-digit '1234567' -> {seven!r}; 10-digit '9965110000' -> {ten!r}")


def check_a4_account():
    saas = payload_line(description="Claude Team subscription", hsn_or_sac="998315")
    freight = payload_line(description="Customs clearance and documentation service")
    same = saas["account_id"] == freight["account_id"]
    record("A4", "INFO",
           "Every bill line posts to one hard-coded expense account",
           f"SaaS line account_id={saas['account_id']}, clearance line account_id="
           f"{freight['account_id']} (same={same}); extracted account_name is ignored")


def check_a5_substring():
    route = zb.determine_tax_route(
        {"currency_code": "INR", "is_overseas": False, "line_items": [], "notes": "",
         "tax_amount_igst": 0, "tax_amount_cgst": 0, "tax_amount_sgst": 0},
        "DAWSON LOGISTICS PVT LTD", vendor_gst_no="27AAAAA0000A1Z5")
    record("A5", "DEFECT" if route["tax_mode"] == "rcm" else "FAIL-SAFE",
           "Overseas signal is a substring test on the vendor name and ignores the GSTIN",
           f"'DAWSON' contains 'AWS' -> tax_mode={route['tax_mode']}, "
           f"review_required={route['review_required']} despite Indian GSTIN 27AAAAA0000A1Z5")


def check_f08():
    rate, _ = zb.resolve_effective_usd_rate(
        {"currency_code": "USD"}, {"is_overseas": True}, "2026-04-01", 83.0, False)
    spawn = re.search(r'\[PYTHON_EXE, MAIN_SCRIPT, "--file"[^\]]*\]',
                      (SRC / "watcher.py").read_text(encoding="utf-8")).group(0)
    uses_flag = "--use-invoice-date-fx" in spawn
    record("F08", "DEFECT" if not uses_flag and rate == 83.0 else "FAIL-SAFE",
           "Watcher runs use the processing-day USD rate, not the invoice-date rate",
           f"watcher spawn args: {spawn}; without the flag a 2026-04-01 bill uses "
           f"the run-level rate {rate}")


def check_overseas_skips_reconcile():
    check = zb.reconcile_payload_taxes({"invoice_total": 1}, {"line_items": []},
                                       {"is_overseas": True})
    record("RCM", "INFO",
           "Overseas/RCM bills skip line, tax and total reconciliation entirely",
           f"reconcile_payload_taxes(overseas, empty payload) -> ok={check['ok']}")


def check_a1_f10():
    src = (SRC / "zoho_bills.py").read_text(encoding="utf-8")
    body = re.search(r"def t_watcher_scope_deferred\(\):\s*\n\s*(.+)", src).group(1).strip()
    record("A1", "DEFECT" if body == "assert True" else "INFO",
           "The 'known open' watcher_lock_scope self-test is a placeholder that always passes",
           f"t_watcher_scope_deferred body: {body!r}; counted in 'SUMMARY: PASS 31'")
    w = (SRC / "watcher.py").read_text(encoding="utf-8")
    main_block = w[w.index('if __name__ == "__main__":'):]
    gate_off = "# enforce_test_gate() disabled" in main_block and \
        re.search(r"^\s+enforce_test_gate\(\)", main_block, re.M) is None
    tests = (ROOT / "tests" / "run_tests.py").read_text(encoding="utf-8")
    vacuous = "SKIPPED - SAFE TO START" in tests
    record("F10", "DEFECT" if gate_off else "FAIL-SAFE",
           "Watcher preflight gate is disabled, and would pass vacuously if re-enabled",
           f"enforce_test_gate() call commented out in watcher __main__={gate_off}; "
           f"run_tests.py returns success when pdftotext is missing={vacuous}")


def main():
    with mock.patch("urllib.request.urlopen", _blocked_urlopen):
        unit_checks = [check_f06, check_f07, check_a4_account, check_a5_substring,
                       check_f08, check_overseas_skips_reconcile, check_a1_f10]
        pipeline_checks = [check_f04_f01_f02, check_f03, check_f05,
                           check_a5_usd_domestic_vendor, check_f09_failsafe]
        for check in unit_checks:
            check()
        try:
            if PIPELINE_SAFE:
                for check in pipeline_checks:
                    check()
            else:
                print("Pipeline checks skipped on Windows: main() hardcodes the live lock path.")
        finally:
            for tmp in TEMP_DIRS:
                shutil.rmtree(tmp, ignore_errors=True)

    for r in RESULTS:
        print(f"[{r['verdict']:<9}] {r['finding']:<4} {r['summary']}")
        print(f"             {r['evidence']}")
    defects = sorted({r["finding"] for r in RESULTS if r["verdict"] == "DEFECT"})
    safe = sorted({r["finding"] for r in RESULTS if r["verdict"] == "FAIL-SAFE"})
    print(f"\nDEFECTS REPRODUCED: {', '.join(defects) or 'none'}")
    print(f"FAIL-SAFE CONFIRMED: {', '.join(safe) or 'none'}")
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
