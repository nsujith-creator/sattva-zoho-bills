"""
Zoho MBL Extraction Test Harness (the watcher's preflight gate)
Exit 0 only if pdftotext is installed and every expected fixture extracts
correctly. A missing tool, expectation file or fixture fails, and so does
running zero tests (audit F10). Exit 1 = FAIL.
"""

import os
import sys
import json
import subprocess

sys.path.insert(0, r"C:\temp\zoho")
# Repository layout: tests/run_tests.py next to src/zoho_bills.py.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


def pdftotext_available():
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


TEST_DIR         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_invoices")
EXPECTATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_expectations.json")


def run_tests() -> bool:
    print("\n" + "="*55)
    print("  ZOHO MBL EXTRACTION TEST HARNESS")
    print("="*55 + "\n")

    if not pdftotext_available():
        print("  FAIL - pdftotext is not installed, so production MBL and CMA CGM")
        print("         checks cannot run. Install poppler for Windows and add its")
        print("         bin folder to PATH:")
        print("         https://github.com/oschwartz10612/poppler-windows/releases")
        print("\n  TESTS FAILED - DO NOT START")
        print("="*55 + "\n")
        return False

    if not os.path.exists(EXPECTATION_FILE):
        print(f"  FAIL - expectation file missing: {EXPECTATION_FILE}")
        print("\n  TESTS FAILED - DO NOT START")
        print("="*55 + "\n")
        return False

    from zoho_bills import extract_mbl_from_pdf_text, extract_text_from_pdf

    with open(EXPECTATION_FILE, "r") as f:
        expected_map = json.load(f)

    total        = 0
    passed       = 0
    failed_cases = []

    for filename, expected_mbl in expected_map.items():
        file_path = os.path.join(TEST_DIR, filename)

        if not os.path.exists(file_path):
            print(f"  FAIL {filename}")
            print(f"       Fixture missing: {file_path}")
            total += 1
            failed_cases.append((filename, expected_mbl, "<fixture missing>"))
            continue

        pdf_text = extract_text_from_pdf(file_path)
        result   = extract_mbl_from_pdf_text(pdf_text) or ""
        total   += 1

        if result == expected_mbl:
            print(f"  OK   {filename}")
            print(f"       -> {result}")
            passed += 1
        else:
            print(f"  FAIL {filename}")
            print(f"       Expected : {expected_mbl!r}")
            print(f"       Got      : {result!r}")
            failed_cases.append((filename, expected_mbl, result))

        print()

    print("="*55)
    print(f"  Results: {passed}/{total} passed")

    if total == 0:
        print("\n  FAIL - no extraction tests ran")
        print("  TESTS FAILED - DO NOT START")
        print("="*55 + "\n")
        return False

    if failed_cases:
        print(f"  Failed : {len(failed_cases)}")
        print("\n  TESTS FAILED - DO NOT DEPLOY")
        print("="*55 + "\n")
        return False
    else:
        print("\n  ALL TESTS PASSED - SAFE TO DEPLOY")
        print("="*55 + "\n")
        return True


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
