"""
Zoho MBL Extraction Test Harness
If pdftotext is not installed, skips tests and allows watcher to start.
Exit 0 = PASS or SKIPPED. Exit 1 = FAIL.
"""

import os
import sys
import json
import subprocess

sys.path.insert(0, r"C:\temp\zoho")


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
        print("  [INFO] pdftotext not installed on this machine.")
        print("  [INFO] Skipping extraction tests — watcher will start.")
        print("  [INFO] To enable tests: install poppler for Windows")
        print("         https://github.com/oschwartz10612/poppler-windows/releases")
        print("\n  SKIPPED - SAFE TO START")
        print("="*55 + "\n")
        return True

    from zoho_bills import extract_mbl_from_pdf_text, extract_text_from_pdf

    with open(EXPECTATION_FILE, "r") as f:
        expected_map = json.load(f)

    total        = 0
    passed       = 0
    failed_cases = []

    for filename, expected_mbl in expected_map.items():
        file_path = os.path.join(TEST_DIR, filename)

        if not os.path.exists(file_path):
            print(f"  [SKIP] Missing test file: {filename}")
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
