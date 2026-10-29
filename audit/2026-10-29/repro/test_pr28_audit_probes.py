"""Independent audit probes of PR28 (0a182105278a7968d5c454ab7b745aaadae2b3d3).

Synthetic data and fake providers only. No invoices, credentials or network.
Run: git fetch origin refs/pull/28/head
     git worktree add --detach /tmp/pr28 0a182105278a7968d5c454ab7b745aaadae2b3d3
     PR28_ROOT=/tmp/pr28 ANTHROPIC_API_KEY=offline-dummy python3 -W ignore audit/2026-10-29/repro/test_pr28_audit_probes.py
Each probe prints [ID] with the observed result. "FINDING" probes assert the defective
behaviour that exists at the pinned commit; "CONFIRM" probes assert a safeguard holds.
"""
import contextlib, copy, hashlib, json, os, socket, sqlite3, sys, tempfile, time, unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ['PR28_ROOT'])
sys.path.insert(0, str(ROOT/'cloud_review'))
import automation as a
import owner_start
from test_automation import PDF, DOC, GSTIN, FakeAI, FakeZoho, core_fixture, invoice, source_fixture

VENDORS = {'SYNTHETIC FREIGHT VENDOR': {'id': '123', 'gst_no': GSTIN}}
_connect = socket.socket.connect
def _no_network(self, address):
    raise AssertionError('external network forbidden in audit probes: %r' % (address,))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.folder = Path(self.tmp.name)
        self.db = self.folder/'review.sqlite3'
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT,pdf BLOB)')
            db.execute('INSERT INTO invoices VALUES(?,?,?,?)', (DOC, 's.pdf', hashlib.sha256(PDF).hexdigest(), PDF))
        self.text = source_fixture()
        self.ai = FakeAI(invoice()); self.zoho = FakeZoho(self.folder)
        self.engine = self.new_engine()
        self.engine.enqueue(DOC)
        self.patches = [patch.dict(os.environ, {'APP_AUTOMATION_POSTING': '1'}),
                        patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')),
                        patch.object(socket.socket, 'connect', _no_network)]
        for p in self.patches: p.start()
    def new_engine(self):
        return a.Engine(self.db, self.core, self.ai, self.zoho, source_reader=lambda pdf: self.text)
    def tearDown(self):
        self.engine.close()
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()
    def run_job(self, engine=None):
        engine = engine or self.engine
        due = (engine.read(DOC) or {}).get('retry_at', 0)
        with patch.object(a.time, 'time', return_value=max(time.time(), due)+.01):
            engine.process(DOC)
        return engine.read(DOC)


class Findings(Base):
    def test_F1_printed_due_date_omitted_by_both_readings_still_posts(self):
        # Both readings return due_date null although the PDF prints a labelled due date.
        self.text = source_fixture().replace('Currency INR\n', 'Currency INR\nDue Date: 2026-11-27\n')
        job = self.run_job()
        print('\n[F1] printed "Due Date: 2026-11-27", both readings null ->', job['status'],
              '| payload due_date:', job.get('payload', {}).get('due_date'), '| posts:', self.zoho.posts)
        self.assertEqual(('verified', 1), (job['status'], self.zoho.posts))
        self.assertNotIn('due_date', job['payload'])

    def test_F2_payload_validated_before_upgrade_is_held_as_changed(self):
        # A NEW upload validated by PR27 (posting off) has a payload without is_item_level_tax_calc.
        with patch.dict(os.environ, {'APP_AUTOMATION_POSTING': '0'}):
            job = self.run_job()
        self.assertEqual('validated', job['status'])
        legacy = dict(job['payload']); legacy.pop('is_item_level_tax_calc')
        self.engine.save(job, 'validated', payload=legacy)        # what PR27 would have persisted
        job = self.run_job()                                     # PR28 with posting enabled
        print('\n[F2] PR27-validated job after PR28 posting enabled ->', job['status'], '|', job['message'],
              '| posts:', self.zoho.posts)
        self.assertEqual(('needs_review', 0), (job['status'], self.zoho.posts))
        self.assertIn('changed after the previous attempt', job['message'])

    def test_F3_retained_reading_hold_aborts_owner_startup_before_inbox_serves(self):
        # owner_start.main() calls retained_case_jobs() with no guard (owner_start.py:385).
        # A reconcile Hold on the retained readings becomes an uncaught RuntimeError: the process
        # exits before app.main(), so the whole owner inbox is unavailable, not just posting.
        source = Path(owner_start.__file__).read_text()
        main_body = source[source.index('def main():'):]
        unguarded = 'retained_case_jobs(folder, cases, automation)' in main_body and 'try:' not in main_body.split('app.main()')[0]
        # Real function, synthetic retained state: complete high-confidence readings whose
        # printed evidence no longer reconciles (here: an original with no text layer).
        owner = self.folder/'owner'; (owner/'automation').mkdir(parents=True)
        pdf = b'%PDF-1.4\nno text layer\n%%EOF'; digest = hashlib.sha256(pdf).hexdigest(); doc = 'b'*32
        with contextlib.closing(sqlite3.connect(owner/'review.sqlite3')) as db, db:
            db.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT,created_at TEXT,pdf BLOB)')
            db.execute('CREATE TABLE suggestions(invoice_id TEXT,payload_json TEXT,created_at TEXT)')
            db.execute('INSERT INTO invoices VALUES(?,?,?,?,?)', (doc, 'x.pdf', digest, 'now', pdf))
            db.execute('INSERT INTO suggestions VALUES(?,?,?)', (doc, json.dumps({'kind': 'automatic_upload_intent',
                       'policy': a.POLICY, 'id': doc, 'pdf_sha256': digest}), 'now'))
        (owner/'automation'/(doc+'.json')).write_text(json.dumps({'id': doc, 'policy': a.POLICY, 'pdf_sha256': digest,
            'status': 'needs_review', 'parsed': invoice(), 'source_check': invoice()}))
        case = {'case': 'c1', 'bill_id': '1', 'sha256': digest, 'pdf': pdf, 'expected': {'bill_number': 'SYNTHETIC-001'}}
        with self.assertRaises(RuntimeError) as raised:
            owner_start.retained_case_jobs(owner, [case], a)
        print('\n[F3] retained_case_jobs ->', type(raised.exception).__name__, '|', raised.exception,
              '| main() guards it before app.main():', not unguarded)
        self.assertTrue(unguarded)

    def test_F4_credit_suffix_on_cma_tax_row_is_read_as_positive(self):
        line = dict(invoice()['line_items'][0])
        rows = 'CGST @ 9% DOC 996713 GH 100.00 9.00 CR\nSGST @ 9% DOC 996713 GH 100.00 9.00 CR\n'
        found = a.cma_printed_charge_taxes(line, rows)
        print('\n[F4] "9.00 CR" rows accepted as', {k: v['amount'] for k, v in found.items()})
        self.assertEqual(found['cgst']['amount'], '9.00')

    def test_F5_new_bill_payload_requests_group_calculation_by_default(self):
        # Strict NEW validation already proves item (per-line) arithmetic, yet the payload asks
        # Zoho for bill-level calculation whenever groups also agree.
        payload = a.validate(invoice(), self.core, VENDORS)
        p = invoice()
        p['line_items'] = [dict(p['line_items'][0], description='Charge A', rate='100.05'),
                           dict(p['line_items'][0], description='Charge B', rate='100.07')]
        p.update(subtotal='200.12', tax_amount_cgst='18.01', tax_amount_sgst='18.01', invoice_total='236.14')
        residue = a.validate(p, self.core, VENDORS)
        print('\n[F5] is_item_level_tax_calc for ordinary NEW bill:', payload['is_item_level_tax_calc'],
              '| residue-cancelling NEW bill:', residue['is_item_level_tax_calc'])
        self.assertIs(payload['is_item_level_tax_calc'], False)
        self.assertIs(residue['is_item_level_tax_calc'], False)


class Confirmations(Base):
    def test_C1_consistent_misread_identity_or_buyer_registration_holds(self):
        for change in ({'bill_number': 'SYNTHETIC-00I'}, {'buyer_gstin': '27AEFPN9399L1Z1'},
                       {'bill_date': '2026-10-23'}):
            p = invoice(); p.update(change)
            with self.subTest(change=change), self.assertRaises(a.Hold):
                a.reconcile_readings(p, copy.deepcopy(p), self.text, self.core)
        print('\n[C1] consistent misreads of number / buyer GSTIN / date held by PDF text')

    def test_C2_restart_after_unanswered_create_never_posts_twice(self):
        def crash_during_create(endpoint, params=None, payload=None):
            if payload:
                self.zoho.posts += 1
                raise SystemExit('container killed while create in flight')
            return {'code': 0, 'bill': self.zoho.bill}
        original = self.zoho.call; self.zoho.call = crash_during_create
        with self.assertRaises(SystemExit):
            self.run_job()
        self.engine.close(); self.zoho.call = original
        engine = self.new_engine()                        # restart: same volume, new process
        try:
            for _ in range(5):
                job = self.run_job(engine)
            engine.retry(DOC); job = self.run_job(engine)  # owner presses Retry
            print('\n[C2] restart after unanswered create ->', job['status'], '| posts:', self.zoho.posts)
            self.assertEqual(1, self.zoho.posts)
            self.assertIn(job['status'], ('retryable', 'retry_wait'))
        finally:
            engine.close(); self.engine = self.new_engine()

    def test_C3_reused_number_with_different_date_is_not_called_duplicate(self):
        payload = a.validate(invoice(), self.core, VENDORS)
        self.zoho.bill = {**payload, 'bill_id': '999', 'date': '2025-04-02', 'sub_total': 100, 'tax_total': 18,
                          'total': 118, 'gst_no': GSTIN,
                          'taxes': [{'tax_name': 'CGST9', 'tax_amount': 9}, {'tax_name': 'SGST9', 'tax_amount': 9}]}
        job = self.run_job()
        print('\n[C3] same number, different date ->', job['status'], '| posts:', self.zoho.posts)
        self.assertEqual(('needs_review', 0), (job['status'], self.zoho.posts))

    def test_C4_meta_alias_uses_core_key(self):
        rows = [{'vendor_id': '55', 'bill_number': 'FBADS-190-12345', 'bill_id': '1'}]
        transport = lambda url, headers, payload=None, timeout=30: {'code': 0, 'bills': rows, 'page_context': {'has_more_page': False}}
        with patch.dict(os.environ, {'ZOHO_ORGANIZATION_ID': '1'}):
            z = a.Zoho(transport); z.expires = 1e18; z.token = 'x'; z.core = self.core
            self.assertEqual('1', z.find('55', 'ADS190-12345')['bill_id'])

    def test_C5_penny_rule_counterexamples_hold(self):
        p = invoice()
        p['line_items'] = [dict(p['line_items'][0], description='A'), dict(p['line_items'][0], description='B')]
        p.update(subtotal='200.00', tax_amount_cgst='18.00', tax_amount_sgst='18.00', invoice_total='236.00')
        payload = a.validate(copy.deepcopy(p), self.core, VENDORS, check_zoho_rounding=False)
        def bill(heads):
            b = dict(copy.deepcopy(payload), bill_id='9', gst_no=GSTIN, sub_total='200.00', tax_total='36.00', total='236.00',
                     taxes=[{'tax_name': 'CGST', 'tax_amount': '18.00'}, {'tax_name': 'SGST', 'tax_amount': '18.00'}])
            for line, taxes in zip(b['line_items'], heads):
                if taxes is not None: line['line_item_taxes'] = taxes
            return b
        T = lambda c, s: [{'tax_name': 'CGST', 'tax_amount': c}, {'tax_name': 'SGST', 'tax_amount': s}]
        ok = a.verify_detail(self.core, '9', payload, p, bill([T('9.01', '9.01'), T('8.99', '8.99')]), owned=False)
        # Genuine cross-head offset: CGST +1 paisa against SGST -1 paisa (each head's own group is off).
        cases = {'cross_head': [T('9.01', '9.00'), T('9.00', '8.99')],
                 'two_paise': [T('9.02', '9.02'), T('8.98', '8.98')],
                 'empty_array': [[], T('9.00', '9.00')],
                 'partial_array': [[{'tax_name': 'CGST', 'tax_amount': '9.01'}], T('8.99', '8.99')],
                 'absent_array': [T('9.01', '9.01'), None]}
        results = {k: a.verify_detail(self.core, '9', payload, p, bill(v), owned=False)['ok'] for k, v in cases.items()}
        owned = a.verify_detail(self.core, '9', payload, p, bill([T('9.01', '9.01'), T('8.99', '8.99')]), owned=True)['ok']
        # Within-head swap (CGST and SGST each net to zero separately) is inside the owner's rule.
        swap = a.verify_detail(self.core, '9', payload, p, bill([T('9.01', '8.99'), T('8.99', '9.01')]), owned=False)['ok']
        print('\n[C5] same-head/rate net-zero duplicate ok:', ok['ok'], '| within-head swap ok:', swap,
              '| counterexamples ok-flags:', results, '| same offsets on NEW bill ok:', owned)
        self.assertTrue(ok['ok']); self.assertTrue(swap)
        self.assertFalse(any(results.values())); self.assertFalse(owned)


if __name__ == '__main__':
    unittest.main(verbosity=2)
