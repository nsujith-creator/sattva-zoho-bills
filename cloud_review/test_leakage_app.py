"""Leakage monitor, page and routes. Synthetic data only; no Zoho, email or network calls."""
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import leakage as L
import leakage_app as A
from test_leakage import FakeZoho, CONFIG, bill, invoice, evidence

IST = A.IST


def monitor(folder, docs=None, env=None, sender=None, now=None, factory=None):
    db = str(Path(folder)/'review.sqlite3')
    docs = docs if docs is not None else evidence([bill('1', d='2026-10-01')])
    factory = factory or (lambda: L.ReadOnlyZoho(CONFIG, transport=FakeZoho(docs), token_source=lambda c: 't', pace=0))
    return A.Monitor(db, folder, env=env or {}, client_factory=factory, sender=sender,
                     now=now or (lambda: datetime(2026, 11, 20, 9, 0, tzinfo=IST)))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_run_stores_report_and_reports_unbilled(self):
        m = monitor(self.tmp.name)
        m.run('manual')
        report, started, email = m.latest_report()
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual([i['type'] for i in report['confirmed_leakage']], ['UNBILLED'])
        self.assertEqual(email, 'not configured')

    def test_decision_needs_reason_and_valid_ids_then_applies(self):
        m = monitor(self.tmp.name)
        with self.assertRaises(ValueError):
            m.decide('write_off', {'ids': '1', 'reason': ''})
        with self.assertRaises(ValueError):
            m.decide('write_off', {'ids': '1;DROP', 'reason': 'Absorbed fee'})
        with self.assertRaises(ValueError):
            m.decide('nonsense', {'ids': '1', 'reason': 'Absorbed fee'})
        m.decide('write_off', {'ids': '1', 'reason': 'Spot booking no-show fee'})
        m.run('manual')
        report = m.latest_report()[0]
        self.assertEqual(report['confirmed_leakage'], [])
        self.assertEqual([i['type'] for i in report['accepted_losses']], ['written off (unbilled)'])
        did = m.decision_rows()[0][0]
        self.assertEqual(m.revoke(did), 1)
        m.run('manual')
        self.assertEqual([i['type'] for i in m.latest_report()[0]['confirmed_leakage']], ['UNBILLED'])

    def test_decision_applies_at_once_without_reading_zoho(self):
        calls = []
        docs = evidence([bill('1', d='2026-10-01')])
        def factory():
            calls.append(1)
            return L.ReadOnlyZoho(CONFIG, transport=FakeZoho(docs), token_source=lambda c: 't', pace=0)
        m = monitor(self.tmp.name, factory=factory)
        self.assertFalse(m.reapply())  # nothing read yet
        m.run('manual')
        m.decide('write_off', {'ids': '1', 'reason': 'No-show fee, absorbed'})
        self.assertTrue(m.reapply())
        report = m.latest_report()[0]
        self.assertEqual((report['confirmed_leakage'], len(calls)), ([], 1))
        self.assertEqual([i['type'] for i in report['accepted_losses']], ['written off (unbilled)'])
        self.assertEqual(m.runs(1)[0][1], 'decision')
        self.assertIsNone(m.previous_report())  # a re-apply is not a new Zoho read
        out = A.page(report, m.latest_report()[1], m.latest_report()[2], m.runs(), m.decision_rows(), 'c')
        self.assertIn('decisions applied', out)
        with m.lock:
            self.assertFalse(m.reapply())  # a full check in progress picks decisions up itself

    def test_email_failure_reports_status_and_body(self):
        import io, urllib.error
        env = {'RESEND_API_KEY': 'k', 'LEAKAGE_EMAIL_TO': 'owner@example.com', 'LEAKAGE_EMAIL_FROM': 'books@example.com'}
        def boom(*a):
            raise urllib.error.HTTPError('https://api.resend.com/emails', 403, 'Forbidden', {}, io.BytesIO(b'{"message": "domain not verified"}'))
        m = monitor(self.tmp.name, env=env, sender=boom)
        m.run('manual')
        self.assertEqual(m.latest_report()[2], 'failed: HTTP 403 {"message": "domain not verified"}')

    def test_link_by_invoice_number(self):
        docs = evidence([bill('1', ref='')], [invoice('9', text='no key')])
        m = monitor(self.tmp.name, docs)
        m.decide('link', {'ids': '1', 'invoice_number': 'INV-9', 'reason': 'Billed on house B/L'})
        m.run('manual')
        report = m.latest_report()[0]
        self.assertEqual(report['confirmed_leakage']+[i for i in report['potential_issues'] if i['type'] != 'invoice with no vendor cost recorded'], [])

    def test_email_always_sent_when_configured_even_incomplete(self):
        sent = []
        env = {'RESEND_API_KEY': 'k', 'LEAKAGE_EMAIL_TO': 'owner@example.com', 'LEAKAGE_EMAIL_FROM': 'books@example.com'}

        def broken():
            raise L.Incomplete('Zoho connection failed: synthetic')
        m = monitor(self.tmp.name, env=env, sender=lambda *a: sent.append(a), factory=broken)
        m.run('daily')
        report, _, email = m.latest_report()
        self.assertEqual((report['status'], email), ('INCOMPLETE', 'sent'))
        self.assertIn('INCOMPLETE', sent[0][3])

    def test_config_error_is_incomplete_with_reason(self):
        def bad():
            raise L.ConfigError('ZOHO_RECON_REFRESH_TOKEN looks like a template placeholder')
        m = monitor(self.tmp.name, factory=bad)
        m.run('manual')
        report = m.latest_report()[0]
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertIn('placeholder', report['incomplete_reasons'][0])

    def test_interrupted_run_is_marked_incomplete_on_restart(self):
        m = monitor(self.tmp.name)
        with sqlite3.connect(m.db_path) as db:
            db.execute("INSERT INTO leakage_runs(trigger, started_at, status) VALUES ('daily','2026-11-20T08:00:00+05:30','RUNNING')")
        m2 = monitor(self.tmp.name)
        self.assertEqual(m2.runs(1)[0][4], 'INCOMPLETE')

    def test_daily_schedule(self):
        clock = {'t': datetime(2026, 11, 20, 7, 59, tzinfo=IST)}
        m = monitor(self.tmp.name, now=lambda: clock['t'])
        self.assertFalse(m.due())
        clock['t'] = datetime(2026, 11, 20, 8, 1, tzinfo=IST)
        self.assertTrue(m.due())
        m.run('daily')
        self.assertFalse(m.due())
        clock['t'] = datetime(2026, 11, 21, 8, 0, tzinfo=IST)
        self.assertTrue(m.due())

    def test_one_run_at_a_time(self):
        m = monitor(self.tmp.name)
        with m.lock:
            self.assertIsNone(m.run('manual'))
            self.assertFalse(m.run_now())


class PageTests(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(A.inr('369608.63'), '3,69,608')
        self.assertEqual(A.inr('5917048.57', True), '59,17,048.57')
        self.assertEqual(A.inr('994'), '994')

    def test_page_escapes_zoho_text(self):
        with tempfile.TemporaryDirectory() as d:
            m = monitor(d, evidence([bill('1', vendor='<script>alert(1)</script>')]))
            m.run('manual')
            report, started, email = m.latest_report()
            out = A.page(report, started, email, m.runs(), m.decision_rows(), 'csrf-token')
        self.assertNotIn('<script>alert(1)</script>', out)
        self.assertIn('&lt;script&gt;', out)
        self.assertIn('is leaking across 1 shipment.', out)

    def test_records_link_to_zoho_only_with_numeric_ids(self):
        with tempfile.TemporaryDirectory() as d:
            m = monitor(d, evidence([bill('4600000123', d='2026-10-01')]))
            m.run('manual')
            report, started, email = m.latest_report()
            out = A.page(report, started, email, m.runs(), m.decision_rows(), 'c', zoho_org='60012345')
            bare = A.page(report, started, email, m.runs(), m.decision_rows(), 'c')
        self.assertIn('href="https://books.zoho.in/app/60012345#/bills/4600000123"', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertNotIn('books.zoho.in', bare)
        self.assertEqual(A.zoho_link('60012345', 'bills', '1"><script>', 'x'), 'x')

    def test_filter_bar_has_a_chip_per_type_and_rows_carry_filter_data(self):
        with tempfile.TemporaryDirectory() as d:
            m = monitor(d, evidence([bill('1', d='2026-10-01', vendor='Maersk <India>')]))
            m.run('manual')
            report, started, email = m.latest_report()
            out = A.page(report, started, email, m.runs(), m.decision_rows(), 'c')
        self.assertIn('data-f="UNBILLED"', out)
        self.assertIn('data-type="UNBILLED" data-amt="10000.00" data-date="2026-10-01"', out)
        self.assertIn('maersk &lt;india&gt;', out)  # search text is escaped
        self.assertIn('1 Oct 2026', out)
        self.assertLess(out.index('id="decide"'), out.index("getElementById('filters')"))  # script runs after the lists

    def test_new_since_last_check_bulk_data_party_and_age(self):
        with tempfile.TemporaryDirectory() as d:
            clock = {'t': datetime(2026, 11, 20, 9, 0, tzinfo=IST)}
            docs = evidence([bill('1', d='2026-10-01')])
            m = monitor(d, docs, now=lambda: clock['t'])
            m.run('manual')
            self.assertIsNone(m.previous_report())
            docs['bills']['2'] = dict(bill('2', d='2026-10-02', ref='MBL: MSCU1234567890'))
            m.run('manual')
            report, started, email = m.latest_report()
            prev = m.previous_report()
            self.assertEqual(len(prev['confirmed_leakage']), 1)
            out = A.page(report, started, email, m.runs(), m.decision_rows(), 'c', previous=prev)
        rows = re.findall(r'<li data-type="UNBILLED"[^>]*>', out)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum('data-new="1"' in r for r in rows), 1)
        self.assertIn('data-new="1"', out); self.assertIn('class="newtag"', out)
        self.assertIn('data-acts="write_off" data-bills="2"', out)
        self.assertIn('data-kind="decide"', out)
        self.assertIn('at risk · 49 days', out)  # 20 Nov report vs 2 Oct bill
        self.assertIn('id="bulk"', out); self.assertIn('data-new="1" aria-pressed', out)

    def test_bulk_write_off_stores_every_bill(self):
        with tempfile.TemporaryDirectory() as d:
            m = monitor(d)
            self.assertEqual(m.decide('write_off', {'ids': '11,12,13', 'reason': 'Maersk no-show fees, absorbed'}), 3)

    def test_incomplete_is_never_presented_as_clean(self):
        report = A.Monitor._failed_report(datetime(2026, 11, 20, tzinfo=IST), 'Zoho HTTP 503')
        out = A.page(report, '2026-11-20T08:00:00+05:30', 'not configured', [], [], 'c')
        self.assertIn('This check is incomplete', out)
        self.assertNotIn('Nothing is leaking', out)


class RouteTests(unittest.TestCase):
    PASSWORD = 'local-test-password-with-entropy-12345'

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        s = socket.socket(); s.bind(('127.0.0.1', 0)); cls.port = s.getsockname()[1]; s.close()
        env = os.environ.copy()
        env.update(APP_USERNAME='owner', APP_PASSWORD=cls.PASSWORD, DATA_DIR=cls.folder.name, PORT=str(cls.port), BIND='127.0.0.1',
                   APP_ALLOW_LOCAL_HTTP='1', ZOHO_ORGANIZATION_ID='1', ZOHO_CLIENT_ID='1000.SYN', ZOHO_CLIENT_SECRET='syn',
                   ZOHO_RECON_REFRESH_TOKEN='1000.syn.syn', LEAKAGE_DAILY_HOUR_IST='23')
        env.pop('APP_PASSWORD_HASH', None)
        cls.p = subprocess.Popen([sys.executable, str(Path(__file__).with_name('app.py'))], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                if cls.req('GET', '/healthz')[0] == 200:
                    break
            except OSError:
                time.sleep(.05)
        status, body, headers = cls.req('GET', '/login')
        nonce_cookie = headers['Set-Cookie'].split(';')[0]
        nonce = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
        status, body, headers = cls.req('POST', '/login', urlencode({'password': cls.PASSWORD, 'csrf': nonce}),
                                        {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': nonce_cookie})
        cls.auth = headers['Set-Cookie'].split(';')[0]
        page = cls.req('GET', '/leakage', headers={'Cookie': cls.auth})[1]
        cls.csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate(); cls.p.wait(timeout=4); cls.p.stderr.close(); cls.folder.cleanup()

    @classmethod
    def req(cls, method, path, data=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=5)
        c.request(method, path, data, headers or {})
        r = c.getresponse()
        out = r.status, r.read().decode('utf8', 'replace'), dict(r.getheaders())
        c.close()
        return out

    def form(self, path, fields):
        return self.req('POST', path, urlencode(fields), {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': self.auth})

    def test_page_requires_sign_in(self):
        status, _, headers = self.req('GET', '/leakage')
        self.assertEqual(status, 303)
        self.assertIn('next=%2Fleakage', headers['Location'])
        status, _, headers = self.req('POST', '/leakage/run', 'csrf=x', {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual((status, headers['Location']), (303, '/login?next=%2Fleakage'))

    def test_page_renders_and_allows_self_fonts_only(self):
        status, body, headers = self.req('GET', '/leakage', headers={'Cookie': self.auth})
        self.assertEqual(status, 200)
        self.assertIn('No check has run yet.', body)
        self.assertIn("font-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn('googleapis', body)

    def test_static_assets_allowlisted(self):
        status, _, headers = self.req('GET', '/static/fonts/poppins-latin-700-normal.woff2')
        self.assertEqual((status, headers['Content-Type']), (200, 'font/woff2'))
        for bad in ('/static/../app.py', '/static/fonts/../../leakage.py', '/static/OFL-Poppins.txt', '/static/'):
            self.assertEqual(self.req('GET', bad)[0], 404, bad)

    def test_decision_needs_csrf_and_reason(self):
        self.assertEqual(self.form('/leakage/decide/write_off', {'ids': '1', 'reason': 'Absorbed'})[0], 403)
        status, body, _ = self.form('/leakage/decide/write_off', {'csrf': self.csrf, 'ids': '1', 'reason': ''})
        self.assertEqual(status, 400)
        self.assertIn('Give a reason', body)
        status, _, headers = self.form('/leakage/decide/write_off', {'csrf': self.csrf, 'ids': '123', 'reason': 'Absorbed no-show fee'})
        self.assertEqual((status, headers['Location']), (303, '/leakage?m=saved'))
        body = self.req('GET', '/leakage?m=saved', headers={'Cookie': self.auth})[1]
        self.assertIn('Decision saved', body)
        self.assertEqual(self.form('/leakage/decide/delete_everything', {'csrf': self.csrf})[0], 404)


if __name__ == '__main__':
    unittest.main()


class ReleasePinCoverage(unittest.TestCase):
    def test_every_python_module_in_the_image_is_pinned(self):
        import owner_start
        root = Path(__file__).resolve().parents[1]
        shipped = set()
        for line in (root/'cloud_review'/'Dockerfile').read_text().splitlines():
            if line.startswith('COPY '):
                *sources, target = line.split()[1:]
                for src in sources:
                    if src.endswith('.py'):
                        shipped.add(target.rsplit('/', 1)[1] if not target.endswith('/') else src.rsplit('/', 1)[1])
        self.assertEqual(shipped, owner_start.SOURCE_NAMES, 'startup manifest must cover every Python file in /app')
        pin = (root/'tools'/'release_pin.py').read_text()
        for name in shipped - {'zoho_core.py'}:
            self.assertIn("'"+name+"'", pin, name+' missing from tools/release_pin.py')
