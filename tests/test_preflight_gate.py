"""The watcher's preflight gate, tests/run_tests.py (audit F10).

Runs the harness as the watcher does, in a scratch folder laid out like
C:\\temp\\zoho\\test, and checks that it passes only when pdftotext is present and
every expected fixture extracts correctly.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"


def minimal_pdf(text):
    """A one-page PDF whose text layer is `text`."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


class PreflightGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="zoho-preflight-test-")
        self.test_dir = Path(self._tmp.name) / "test"
        (self.test_dir / "test_invoices").mkdir(parents=True)
        shutil.copy(TESTS / "run_tests.py", self.test_dir / "run_tests.py")

    def tearDown(self):
        self._tmp.cleanup()

    def expect(self, mapping):
        (self.test_dir / "test_expectations.json").write_text(json.dumps(mapping), encoding="utf-8")

    def fixture(self, name, text):
        (self.test_dir / "test_invoices" / name).write_bytes(minimal_pdf(text))

    def run_gate(self, path=None):
        env = dict(os.environ, PYTHONPATH=str(SRC),
                   ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY", "offline-test-not-a-key"))
        if path is not None:
            env["PATH"] = path
        proc = subprocess.run([sys.executable, str(self.test_dir / "run_tests.py")],
                              capture_output=True, text=True, env=env, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    def test_missing_pdftotext_fails(self):
        self.expect({"bl.pdf": "MAEU123456789"})
        self.fixture("bl.pdf", "Bill of Lading: MAEU123456789")
        code, output = self.run_gate(path=str(Path(self._tmp.name) / "empty-bin"))
        self.assertEqual(code, 1, output)
        self.assertIn("pdftotext is not installed", output)

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
    def test_missing_expectations_fixture_or_zero_tests_fails(self):
        code, output = self.run_gate()
        self.assertEqual(code, 1, output)
        self.assertIn("expectation file missing", output)

        self.expect({"bl.pdf": "MAEU123456789"})
        code, output = self.run_gate()
        self.assertEqual(code, 1, output)
        self.assertIn("Fixture missing", output)

        self.expect({})
        code, output = self.run_gate()
        self.assertEqual(code, 1, output)
        self.assertIn("no extraction tests ran", output)

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
    def test_real_fixture_passes_and_wrong_value_fails(self):
        self.fixture("bl.pdf", "Bill of Lading: MAEU123456789")
        self.expect({"bl.pdf": "MAEU123456789"})
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)
        self.assertIn("1/1 passed", output)

        self.expect({"bl.pdf": "MAEU000000000"})
        code, output = self.run_gate()
        self.assertEqual(code, 1, output)


if __name__ == "__main__":
    unittest.main()
