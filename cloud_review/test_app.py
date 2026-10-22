import base64
import hashlib
import hmac
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP = Path(__file__).with_name("app.py")
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
PASSWORD = "local-test-password-with-entropy-12345"

class InboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        env = os.environ.copy()
        env.update(APP_USERNAME="owner", APP_PASSWORD=PASSWORD, DATA_DIR=cls.folder.name,
                   PORT=str(cls.port), BIND="127.0.0.1")
        cls.p = subprocess.Popen([sys.executable, str(APP)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            if cls.p.poll() is not None:
                raise RuntimeError(cls.p.stderr.read().decode())
            try:
                c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                c.request("GET", "/healthz")
                r = c.getresponse()
                r.read()
                c.close()
                if r.status == 200:
                    break
            except OSError:
                time.sleep(.05)
        else:
            raise RuntimeError("Server did not start")
        cls.auth = "Basic " + base64.b64encode(b"owner:" + PASSWORD.encode()).decode()
        cls.csrf = hmac.new(PASSWORD.encode(), b"sattva-review-upload-v1", hashlib.sha256).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate()
        cls.p.wait(timeout=4)
        cls.p.stderr.close()
        cls.folder.cleanup()

    def request(self, method, path, data=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, data, headers or {})
        r = c.getresponse()
        result = r.status, r.read()
        c.close()
        return result

    def headers(self):
        return {"Authorization": self.auth, "Content-Type": "application/pdf",
                "X-Filename": "example.pdf", "X-CSRF-Token": self.csrf}

    def test_01_auth_required(self):
        self.assertEqual(401, self.request("GET", "/")[0])
        self.assertEqual(401, self.request("POST", "/api/invoices", PDF)[0])

    def test_02_empty_inbox(self):
        status, body = self.request("GET", "/", headers={"Authorization":self.auth})
        self.assertEqual(200, status)
        self.assertIn(b"No posting", body)

    def test_03_validation(self):
        h = self.headers()
        h.pop("X-CSRF-Token")
        self.assertEqual(403, self.request("POST", "/api/invoices", PDF, h)[0])
        self.assertEqual(400, self.request("POST", "/api/invoices", b"not a pdf", self.headers())[0])
        h = self.headers()
        h["Content-Type"] = "text/plain"
        self.assertEqual(415, self.request("POST", "/api/invoices", PDF, h)[0])

    def test_04_upload_and_duplicate(self):
        import json
        status, body = self.request("POST", "/api/invoices", PDF, self.headers())
        self.assertEqual(201, status)
        doc = json.loads(body)["id"]
        self.assertEqual(401, self.request("GET", "/invoice/" + doc)[0])
        self.assertEqual((200, PDF), self.request("GET", "/invoice/" + doc, headers={"Authorization":self.auth}))
        self.assertEqual(409, self.request("POST", "/api/invoices", PDF, self.headers())[0])

    def test_05_no_path_lookup(self):
        self.assertEqual(404, self.request("GET", "/invoice/../something",
                         headers={"Authorization":self.auth})[0])

    def test_06_insecure_startup_refused(self):
        env = os.environ.copy()
        env.update(APP_USERNAME="owner", APP_PASSWORD="short", DATA_DIR=self.folder.name)
        p = subprocess.run([sys.executable, str(APP)], env=env, capture_output=True, timeout=3)
        self.assertNotEqual(0, p.returncode)


    def test_07_review_save_and_validation(self):
        import json
        pdf = PDF + b'\nreview-case\n'
        code, body = self.request("POST", "/api/invoices", pdf, self.headers())
        self.assertEqual(201, code, body)
        doc = json.loads(body)["id"]
        self.assertEqual(401, self.request("GET", "/review/" + doc)[0])
        self.assertEqual(403, self.request("POST", "/api/reviews/"+doc, b"{}", {"Authorization":self.auth,"Content-Type":"application/json"})[0])
        fields = dict(supplier="Synthetic carrier", gstin="", invoice_number="TEST-7",
                      invoice_date="2026-10-20",currency="INR",subtotal="100.00",
                      cgst="9.00",sgst="9.00",igst="0",discount="0",total="118.00",notes="")
        headers={"Authorization":self.auth,"Content-Type":"application/json","X-CSRF-Token":self.csrf}
        code, body = self.request("POST", "/api/reviews/"+doc, json.dumps(fields).encode(), headers)
        self.assertEqual(200,code,body)
        self.assertEqual(False,json.loads(body)["pdf_verified"])
        code, body = self.request("GET","/review/"+doc,headers={"Authorization":self.auth})
        self.assertEqual(200, code)
        self.assertIn(b'118.00',body)
        fields["total"]="119.00"
        code, body = self.request("POST","/api/reviews/"+doc,json.dumps(fields).encode(),headers)
        self.assertEqual(400,code)
        self.assertIn(b"don't add up",body)
        code, body = self.request("GET","/review/"+doc,headers={"Authorization":self.auth})
        self.assertIn(b'118.00',body)

if __name__ == "__main__":
    unittest.main()
