"""Independent audit probes of PR24 (8ef5c7a). Synthetic data, fake providers, no network.

Run: git worktree add /tmp/pr24 8ef5c7a1709c2d09a5c1e73792ed8365bfab1886
     PR24_ROOT=/tmp/pr24 python3 -W ignore audit/2026-10-28/repro/test_audit_journey.py
"""
import contextlib, copy, hashlib, json, os, sqlite3, sys, tempfile, threading, time, unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ['PR24_ROOT'])
sys.path.insert(0, str(ROOT/'cloud_review'))
import automation as a
from test_automation import PDF, DOC, GSTIN, FakeAI, FakeZoho, core_fixture, invoice

VENDORS = [{'contact_name':'SYNTHETIC FREIGHT VENDOR','contact_id':'123','gst_no':GSTIN}]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.folder = Path(self.tmp.name)
        self.db = self.folder/'review.sqlite3'
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT UNIQUE,pdf BLOB)')
            db.execute('INSERT INTO invoices VALUES(?,?,?,?)',(DOC,'s.pdf',hashlib.sha256(PDF).hexdigest(),PDF))
        self.ai = FakeAI(invoice()); self.zoho = FakeZoho(self.folder)
        self.engine = a.Engine(self.db, self.core, self.ai, self.zoho); self.engine.enqueue(DOC)
        self.env = patch.dict(os.environ, {'APP_AUTOMATION_POSTING':'1'}); self.env.start()
        self.net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')); self.net.start()
    def tearDown(self):
        self.engine.close(); self.net.stop(); self.env.stop(); self.tmp.cleanup()
    def run_once(self):
        self.engine.process(DOC); return self.engine.read(DOC)


class Findings(Base):
    def test_A_upload_enqueue_blocks_while_worker_holds_lock(self):
        # Worker holds Engine.lock for the whole job, including both paid calls.
        slow = threading.Event()
        def extract(pdf, second=False):
            slow.wait(3); return copy.deepcopy(invoice())
        self.ai.extract = extract
        t = threading.Thread(target=self.engine.process, args=(DOC,)); t.start(); time.sleep(0.2)
        start = time.monotonic(); self.engine.enqueue('b'*32); waited = time.monotonic()-start
        t.join()
        print(f'\n[A] enqueue of a second upload waited {waited:.2f}s behind one job')
        self.assertGreater(waited, 2.5)

    def test_B_transient_readback_failure_after_create_is_terminal_and_unattached(self):
        orig = self.zoho.call; n = {'get':0}
        def flaky(endpoint, params=None, payload=None):
            if not payload and endpoint.startswith('bills/'):
                n['get'] += 1
                if n['get'] == 1: raise a.Hold('Provider connection or response failed. No automatic retry.')
            return orig(endpoint, params, payload)
        self.zoho.call = flaky
        job = self.run_once(); job2 = self.run_once()
        print(f"\n[B] after one transient GET failure: status={job2['status']} posts={self.zoho.posts} attachments={self.zoho.attachments} msg={job2['message']!r}")
        self.assertEqual('needs_review', job2['status']); self.assertEqual((1,0),(self.zoho.posts,self.zoho.attachments))

    def test_C_definitive_zoho_rejection_is_reported_as_uncertain(self):
        def reject(endpoint, params=None, payload=None):
            if payload: self.zoho.posts += 1; raise a.Hold('Provider HTTP 400. No automatic retry.')
            return {'code':0,'bill':None}
        self.zoho.call = reject
        self.run_once(); job = self.run_once()
        print(f"\n[C] Zoho 400 rejection shown as: {job['status']} — {job['message']!r}")
        self.assertIn('uncertain', job['message'])

    def test_D_two_agreeing_wrong_readings_post_and_verify(self):
        wrong = invoice(); wrong.update(bill_number='SYNTHETIC-00I', bill_date='2026-10-23')
        self.ai.parsed = wrong
        job = self.run_once()
        print(f"\n[D] consistent misread posted as {job['parsed']['bill_number']} {job['parsed']['bill_date']}: status={job['status']}")
        self.assertEqual('verified', job['status'])

    def test_E_vendor_name_disagreement_not_in_signature(self):
        second = invoice(); second['vendor_name'] = 'DIFFERENT SUPPLIER PVT LTD'
        self.assertEqual(a.source_signature(invoice()), a.source_signature(second))

    def test_F_missing_printed_supplier_gstin_posts_with_master_registration(self):
        p = invoice(); p['vendor_gstin'] = ''
        self.ai.parsed = p
        job = self.run_once()
        print(f"\n[F] blank printed supplier GSTIN: status={job['status']} (route from master GSTIN)")
        self.assertEqual('verified', job['status'])

    def test_G_same_number_different_financials_marked_duplicate_without_reconciling(self):
        self.zoho.bill = {'bill_id':'999','bill_number':'SYNTHETIC-001','date':'2025-04-02','total':5000}
        job = self.run_once()
        print(f"\n[G] existing bill with other date/total: status={job['status']} msg={job['message']!r}")
        self.assertEqual('duplicate', job['status'])

    def test_H_zero_rated_line_and_round_off_are_held(self):
        vendors = {'SYNTHETIC FREIGHT VENDOR':{'id':'123','gst_no':GSTIN}}
        p = invoice(); p['line_items'].append({'description':'Reimbursement','quantity':1,'rate':'50.00','tax_percentage':'0','hsn_or_sac':'996713'})
        p.update(subtotal='150.00', invoice_total='168.00')
        with self.assertRaises(a.Hold) as c1: a.validate(p, self.core, vendors)
        q = invoice(); q.update(subtotal='100.40', invoice_total='118.00', tax_amount_cgst='9.04', tax_amount_sgst='9.04')
        q['line_items'][0]['rate'] = '100.40'
        with self.assertRaises(a.Hold) as c2: a.validate(q, self.core, vendors)
        print(f"\n[H] zero-rated line: {c1.exception}\n[H] printed round-off: {c2.exception}")

    def test_I_worker_thread_dies_silently_on_unexpected_error(self):
        with patch.object(self.engine, 'recover_uploads', side_effect=OSError('No space left on device')):
            self.engine.start(); time.sleep(1.6)
            alive = self.engine.worker.is_alive()
        print(f'\n[I] worker alive after OSError in intent scan: {alive}')
        self.assertFalse(alive)

    def test_J_interrupted_parse_is_terminal_and_same_pdf_cannot_be_resubmitted(self):
        job = self.engine.read(DOC); self.engine.save(job, 'parsing')
        job = self.run_once()
        with self.assertRaises(sqlite3.IntegrityError):
            with contextlib.closing(sqlite3.connect(self.db)) as db, db:
                db.execute('INSERT INTO invoices VALUES(?,?,?,?)',('c'*32,'s.pdf',hashlib.sha256(PDF).hexdigest(),PDF))
        print(f"\n[J] interrupted parse -> {job['status']}; identical re-upload rejected by UNIQUE(sha256)")
        self.assertIn(job['status'], a.TERMINAL)

    def test_K_validated_jobs_never_post_after_posting_is_enabled(self):
        with patch.dict(os.environ, {'APP_AUTOMATION_POSTING':'0'}): self.run_once()
        job = self.run_once()
        self.assertEqual(('validated',0), (job['status'], self.zoho.posts))

    def test_L_line_level_tax_shape_required_for_every_domestic_post(self):
        orig = self.zoho.call
        def documented_shape(endpoint, params=None, payload=None):
            r = orig(endpoint, params, payload)
            if self.zoho.bill: self.zoho.bill['line_items'][0].pop('line_item_taxes', None)
            return r
        self.zoho.call = documented_shape
        job = self.run_once()
        print(f"\n[L] created bill without line_item_taxes: {job['status']} errors={job['verification']['errors']} attachments={self.zoho.attachments}")
        self.assertEqual(('needs_review',1,0), (job['status'], self.zoho.posts, self.zoho.attachments))


class RealZohoClient(unittest.TestCase):
    def test_M_meta_alias_not_found_by_cloud_duplicate_lookup(self):
        def transport(url, headers, payload=None, timeout=30):
            return {'code':0,'bills':[{'vendor_id':'55','bill_number':'FBADS-190-12345','bill_id':'1'}],
                    'page_context':{'has_more_page':False}}
        with patch.dict(os.environ, {'ZOHO_ORGANIZATION_ID':'1'}):
            z = a.Zoho(transport); z.expires = 1e18; z.token = 'x'
            found = z.find('55', 'ADS190-12345')
        core = core_fixture()
        print(f"\n[M] cloud find -> {found}; core keys equal: {core._bill_key('FBADS-190-12345') == core._bill_key('ADS190-12345')}")
        self.assertIsNone(found)

    def test_N_budget_capacity(self):
        out = {}
        for tokens in (3000, 6000, 12000, 25000):
            per_call = (Decimal(tokens)*Decimal('1.25') + a.MAX_OUTPUT*5)/1_000_000
            out[tokens] = (float(per_call*2), int(Decimal('0.50')//(per_call*2)), int(Decimal('1.00')//(per_call*2)))
        print('\n[N] input tokens -> (USD reserved per invoice, invoices within 0.50, within hard max 1.00):', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
