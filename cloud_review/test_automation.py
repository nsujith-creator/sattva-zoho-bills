"""Cloud upload-to-Zoho checks with synthetic PDFs, fake providers and isolated state."""
import copy
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import automation as a

PDF = b'%PDF-1.4\nsynthetic invoice\n%%EOF'
DOC = 'a'*32
GSTIN = '27ABCDE1234F1Z5'


def source_fixture():
    return ('SYNTHETIC FREIGHT VENDOR\nSupplier GSTIN '+GSTIN+'\nBuyer GSTIN '+a.OWNER_GSTIN+
            '\nInvoice SYNTHETIC-001\nInvoice Date 2026-10-28\nCurrency INR\n'
            'Documentation    100.00    18%\nSAC 996713\nSubtotal 100.00\nCGST 9.00\nSGST 9.00\nGrand Total 118.00\n')


def invoice():
    return {'vendor_name':'SYNTHETIC FREIGHT VENDOR', 'vendor_gstin':GSTIN,
            'buyer_gstin':a.OWNER_GSTIN, 'bill_number':'SYNTHETIC-001',
            'bill_date':'2026-10-28', 'due_date':None, 'currency_code':'INR',
            'is_overseas':False, 'line_items':[{'description':'Documentation', 'quantity':1,
                'rate':'100.00', 'tax_percentage':'18', 'hsn_or_sac':'996713'}],
            'subtotal':'100.00', 'discounts_or_credits':'0', 'tax_amount_cgst':'9',
            'tax_amount_sgst':'9', 'tax_amount_igst':'0', 'invoice_total':'118.00',
            'mbl_number':'', 'notes':'', 'confidence':'high', 'issues':[], 'multi_invoice_count':1}


def core_fixture():
    real_open = open
    def isolated_open(path, *args, **kwargs):
        if str(path) == r'C:\temp\zoho\config.json':
            return io.StringIO(json.dumps({'anthropic_api_key':'offline-fixture', 'client_id':'offline',
                 'client_secret':'offline', 'refresh_token':'offline', 'access_token':''}))
        if str(path).endswith('.env'):
            return io.StringIO('')
        return real_open(path, *args, **kwargs)
    with patch('builtins.open', side_effect=isolated_open), patch.dict(os.environ,{'APP_AUTOMATION_POLICY':''}):
        return a.load_core()


class FakeAI:
    def __init__(self, parsed):
        self.parsed, self.calls, self.second = parsed, 0, None
    def extract(self, pdf, second=False):
        self.calls += 1
        return copy.deepcopy(self.second if second and self.second is not None else self.parsed)


class FakeZoho:
    def __init__(self, folder):
        self.folder, self.bill, self.posts, self.attachments = Path(folder), None, 0, 0
        self.lose_response, self.lose_without_create = False, False
    def all(self, endpoint, collection, params=None):
        return [{'contact_name':'SYNTHETIC FREIGHT VENDOR','contact_id':'123','gst_no':GSTIN}]
    def find(self, vendor_id, number):
        return self.bill
    def call(self, endpoint, params=None, payload=None):
        if payload:
            journal = json.loads((self.folder/'automation'/(DOC+'.json')).read_text())
            assert journal['status'] == 'posting' and journal['payload'] == payload
            assert journal['authorized_by'] == a.POLICY and journal['pdf_sha256'] == hashlib.sha256(PDF).hexdigest()
            self.posts += 1
            if not self.lose_without_create:
                self.bill = {**copy.deepcopy(payload), 'bill_id':'456', 'sub_total':100, 'tax_total':18,
                             'gst_no':GSTIN,
                             'total':118, 'taxes':[{'tax_name':'CGST9','tax_amount':9},
                             {'tax_name':'SGST9','tax_amount':9}], 'documents':[]}
                self.bill['line_items'][0]['line_item_taxes']=[{'tax_name':'CGST9','tax_amount':9},{'tax_name':'SGST9','tax_amount':9}]
            if self.lose_response or self.lose_without_create:
                raise a.Hold('Synthetic lost response')
        return {'code':0, 'bill':self.bill}
    def attach(self, bid, pdf):
        self.attachments += 1
        self.pdf = pdf
        self.bill['documents'] = [{'document_id':'789'}]
    def verify_attachment(self, bid, pdf):
        assert self.pdf == pdf


class CloudAutomationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db = self.folder/'review.sqlite3'
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT,pdf BLOB)')
            db.execute('INSERT INTO invoices VALUES(?,?,?,?)',(DOC,'synthetic.pdf',hashlib.sha256(PDF).hexdigest(),PDF))
        self.ai = FakeAI(invoice())
        self.zoho = FakeZoho(self.folder)
        self.engine = a.Engine(self.db, self.core, self.ai, self.zoho, source_reader=lambda pdf: source_fixture())
        self.engine.enqueue(DOC)
        self.env = patch.dict(os.environ, {'APP_AUTOMATION_POSTING':'1'})
        self.env.start()
        self.no_network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden in offline tests'))
        self.no_network.start()
    def tearDown(self):
        self.engine.close()
        self.no_network.stop()
        self.env.stop()
        self.tmp.cleanup()
    def process(self):
        deadline = (self.engine.read(DOC) or {}).get('retry_at', 0)
        with patch.object(a.time, 'time', return_value=max(time.time(), deadline)+.01):
            self.engine.process(DOC)
        return self.engine.read(DOC)
    def test_clean_upload_posts_once_verifies_and_attaches_source(self):
        job = self.process()
        self.assertEqual('verified', job['status'])
        self.assertTrue(job['verification']['ok'])
        self.assertEqual((2,1,1), (self.ai.calls,self.zoho.posts,self.zoho.attachments))
        self.process()
        self.assertEqual((2,1,1), (self.ai.calls,self.zoho.posts,self.zoho.attachments))
    def test_posting_stays_off_until_explicit_release_setting(self):
        with patch.dict(os.environ, {'APP_AUTOMATION_POSTING':'0'}):
            self.assertEqual('validated',self.process()['status'])
        self.assertEqual(0,self.zoho.posts)
    def test_existing_invoice_never_creates_or_changes_bill(self):
        payload = a.validate(invoice(), self.core, {'SYNTHETIC FREIGHT VENDOR':{'id':'123','gst_no':GSTIN}})
        self.zoho.bill = {**payload, 'bill_id':'999', 'sub_total':100, 'tax_total':18, 'total':118,
                          'gst_no':GSTIN, 'taxes':[{'tax_name':'CGST9','tax_amount':9}, {'tax_name':'SGST9','tax_amount':9}]}
        self.assertEqual('duplicate',self.process()['status'])
        self.assertEqual((0,0),(self.zoho.posts,self.zoho.attachments))
    def test_disagreement_between_source_reads_stops_posting(self):
        self.ai.second = invoice(); self.ai.second['bill_number']='DIFFERENT'
        self.assertEqual('needs_review',self.process()['status'])
        self.assertEqual(0,self.zoho.posts)
    def test_low_confidence_missing_amount_wrong_buyer_and_multi_are_held(self):
        changes = ({'confidence':'low'}, {'subtotal':None}, {'buyer_gstin':GSTIN},
                   {'multi_invoice_count':2}, {'issues':['Unclear digit']}, {'bill_date':'2026-02-30'},
                   {'vendor_gstin':'27ZZZZZ9999Z1Z9'}, {'discounts_or_credits':'1'},
                   {'tax_amount_cgst':'10'}, {'invoice_total':'118.01'}, {'currency_code':'USD'})
        for change in changes:
            with self.subTest(change=change):
                parsed = invoice(); parsed.update(change)
                with self.assertRaises(a.Hold):
                    a.validate(parsed,self.core,{'SYNTHETIC FREIGHT VENDOR':{'id':'123','gst_no':GSTIN}})
        self.assertEqual(0,self.zoho.posts)
    def test_broken_original_hash_blocks_all_provider_calls(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:db.execute('UPDATE invoices SET sha256=?',('0'*64,))
        self.assertEqual('needs_review',self.process()['status'])
        self.assertEqual((0,0),(self.ai.calls,self.zoho.posts))
    def test_lost_create_response_reconciles_without_second_post(self):
        self.zoho.lose_response = True
        self.assertEqual('reconcile_pending',self.process()['status'])
        self.assertEqual('verified',self.process()['status'])
        self.assertEqual((1,1),(self.zoho.posts,self.zoho.attachments))
    def test_uncertain_create_without_record_is_not_reposted(self):
        self.zoho.lose_without_create = True
        self.assertEqual('reconcile_pending',self.process()['status'])
        for _ in range(4):self.process()
        self.assertEqual('retryable',self.process()['status'])
        self.assertEqual(1,self.zoho.posts)
    def test_recovery_does_not_modify_another_matching_record(self):
        self.zoho.lose_response = True
        self.process()
        self.zoho.bill['notes']='Another source'
        self.assertEqual('needs_review',self.process()['status'])
        self.assertEqual((1,0),(self.zoho.posts,self.zoho.attachments))
    def test_interrupted_paid_extraction_is_not_repeated(self):
        job=self.engine.read(DOC);self.engine.save(job,'parsing')
        self.assertEqual('retryable',self.process()['status'])
        self.assertEqual(0,self.ai.calls)
    def test_corrupt_or_misdirected_journal_never_restarts_processing(self):
        path=self.engine.job_path(DOC)
        for content in ('broken',json.dumps({'id':'b'*32,'status':'queued','policy':a.POLICY})):
            path.write_text(content)
            self.assertEqual('needs_review',self.process()['status'])
            self.assertEqual(content,path.read_text())
        self.assertEqual((0,0),(self.ai.calls,self.zoho.posts))
    def test_failed_live_readback_is_not_success_or_reposted(self):
        original_call=self.zoho.call
        def bad_readback(endpoint,params=None,payload=None):
            result=original_call(endpoint,params,payload)
            if self.zoho.bill:self.zoho.bill['total']=118.01
            return result
        self.zoho.call=bad_readback
        self.assertEqual('needs_review',self.process()['status'])
        self.assertEqual((1,0),(self.zoho.posts,self.zoho.attachments))
        self.process();self.assertEqual(1,self.zoho.posts)
    def test_wrong_saved_account_is_not_reported_as_verified(self):
        original_call=self.zoho.call
        def wrong_account(endpoint,params=None,payload=None):
            result=original_call(endpoint,params,payload)
            if self.zoho.bill:self.zoho.bill['line_items'][0]['account_id']='999'
            return result
        self.zoho.call=wrong_account
        job=self.process()
        self.assertEqual('needs_review',job['status'])
        self.assertIn('line_account_id_mismatch',job['verification']['errors'])
        self.assertEqual((1,0),(self.zoho.posts,self.zoho.attachments))
    def test_equal_line_total_does_not_hide_changed_quantity_and_rate(self):
        original_call=self.zoho.call
        def changed_units(endpoint,params=None,payload=None):
            result=original_call(endpoint,params,payload)
            if self.zoho.bill:self.zoho.bill['line_items'][0].update(quantity=2,rate=50)
            return result
        self.zoho.call=changed_units
        job=self.process()
        self.assertEqual('needs_review',job['status'])
        self.assertIn('line_quantity_mismatch',job['verification']['errors'])
        self.assertIn('line_rate_mismatch',job['verification']['errors'])
    def test_offsetting_line_gst_errors_are_not_hidden_by_equal_header_totals(self):
        original_call=self.zoho.call
        def wrong_head(endpoint,params=None,payload=None):
            result=original_call(endpoint,params,payload)
            if self.zoho.bill:
                self.zoho.bill['line_items'][0]['line_item_taxes']=[{'tax_name':'CGST9','tax_amount':'9.01'},{'tax_name':'SGST9','tax_amount':'8.99'}]
            return result
        self.zoho.call=wrong_head
        job=self.process()
        self.assertEqual('needs_review',job['status'])
        self.assertIn('line_cgst_mismatch',job['verification']['errors'])
        self.assertIn('line_sgst_mismatch',job['verification']['errors'])
    def test_pre_create_tax_group_rounding_disagreement_is_held(self):
        p=invoice()
        p['line_items']=[{'description':'Documentation','quantity':1,'rate':'0.06','tax_percentage':18}]*2
        p.update(subtotal='0.12',tax_amount_cgst='0.02',tax_amount_sgst='0.02',invoice_total='0.16')
        with self.assertRaisesRegex(a.Hold,'tax-group rounding'):
            a.validate(p,self.core,{'SYNTHETIC FREIGHT VENDOR':{'id':'123','gst_no':GSTIN}})
    def test_second_worker_cannot_share_the_posting_state(self):
        with self.assertRaises(OSError):a.Engine(self.db,self.core,self.ai,self.zoho)
    def test_status_screen_escapes_untrusted_invoice_and_has_no_entry_form(self):
        text=a.render_status(DOC,'<script>alert(1)</script>.pdf',{'status':'needs_review','message':'<img onerror=x>','parsed':{}})
        self.assertNotIn('<script>alert',text)
        self.assertNotIn('<form',text)
        self.assertIn('&lt;img',text)


class BudgetAndProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        a.atomic(self.folder/'ai-budget.json',{'reserved_usd':'0','calls_reserved':0})
        self.env=patch.dict(os.environ,{'ANTHROPIC_API_KEY':'offline-test','APP_AI_BUDGET_USD':'1.00'})
        self.env.start();self.calls=[]
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def transport(self,url,headers,body,timeout=30):
        self.calls.append(url)
        if url.endswith('count_tokens'):return {'input_tokens':1000}
        budget=json.loads((self.folder/'ai-budget.json').read_text())
        self.assertGreater(Decimal(budget['reserved_usd']),0)
        return {'stop_reason':'end_turn','content':[{'type':'text','text':json.dumps(invoice())}]}
    def test_reservation_is_persisted_before_paid_request(self):
        self.assertEqual('SYNTHETIC-001',a.Haiku(self.folder,self.transport).extract(PDF)['bill_number'])
        self.assertEqual(1,json.loads((self.folder/'ai-budget.json').read_text())['calls_reserved'])
    def test_exhausted_budget_never_sends_paid_request(self):
        a.atomic(self.folder/'ai-budget.json',{'reserved_usd':'1','calls_reserved':20})
        with self.assertRaises(a.Hold):a.Haiku(self.folder,self.transport).extract(PDF)
        self.assertEqual(1,len(self.calls))
    def test_failed_directory_sync_prevents_paid_request(self):
        with patch.object(a,'sync_directory',side_effect=OSError('simulated unsynced directory')):
            with self.assertRaises(OSError):a.Haiku(self.folder,self.transport).extract(PDF)
        self.assertEqual(1,len(self.calls))
    def test_oversized_input_never_sends_paid_request(self):
        calls=[]
        def large(url,*args,**kwargs):calls.append(url);return {'input_tokens':40001}
        with self.assertRaises(a.Hold):a.Haiku(self.folder,large).extract(PDF)
        self.assertEqual(1,len(calls))
    def test_incomplete_response_is_not_accepted(self):
        def truncated(url,*args,**kwargs):
            if url.endswith('count_tokens'):return {'input_tokens':100}
            return {'stop_reason':'max_tokens','content':[{'type':'text','text':json.dumps(invoice())}]}
        with self.assertRaises(a.Hold):a.Haiku(self.folder,truncated).extract(PDF)
    def test_missing_budget_is_not_reset(self):
        (self.folder/'ai-budget.json').unlink()
        with self.assertRaises(FileNotFoundError):a.Haiku(self.folder,self.transport).extract(PDF)


if __name__=='__main__':unittest.main()
