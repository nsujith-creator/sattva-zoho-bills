"""Regressions for the independent audit; synthetic source text and fake providers only."""
import copy
import io
import json
import os
import threading
import time
import unittest
import urllib.error
from decimal import Decimal
from unittest.mock import patch
import automation as a
import test_automation as fixtures
from test_automation import DOC, PDF, core_fixture, invoice, source_fixture


class AuditRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()
    setUp = fixtures.CloudAutomationTests.setUp
    tearDown = fixtures.CloudAutomationTests.tearDown
    process = fixtures.CloudAutomationTests.process

    def test_consistent_wrong_number_and_date_never_post(self):
        self.ai.parsed.update(bill_number='SYNTHETIC-00I', bill_date='2026-10-23')
        job = self.process()
        self.assertEqual('needs_review', job['status'])
        self.assertIn('PDF text', job['message'])
        self.assertEqual(0, self.zoho.posts)

    def test_printed_header_values_cannot_be_swapped_with_other_amounts(self):
        text = source_fixture().replace('Grand Total 118.00', 'Other amount 118.00\nGrand Total 119.00')
        self.engine.source_reader = lambda pdf: text
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_due_date_elsewhere_does_not_prove_invoice_date(self):
        self.ai.parsed['bill_date'] = '2026-11-28'
        self.engine.source_reader = lambda pdf: source_fixture()+'Due Date 2026-11-28\n'
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_offsetting_gst_headers_must_match_printed_head_labels(self):
        self.engine.source_reader = lambda pdf: source_fixture().replace('CGST 9.00', 'Other amount 9.00\nCGST 9.01').replace('SGST 9.00', 'SGST 8.99')
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_attachment_lost_reply_is_verified_without_second_upload(self):
        original = self.zoho.attach
        def lost_reply(bid, pdf):
            original(bid, pdf)
            raise a.ProviderError('Attachment response lost', retryable=True)
        self.zoho.attach = lost_reply
        self.assertEqual('retry_wait', self.process()['status'])
        self.assertEqual('verified', self.process()['status'])
        self.assertEqual((2,1,1), (self.ai.calls,self.zoho.posts,self.zoho.attachments))

    def test_one_transient_source_failure_waits_for_owner_and_preserves_first_read(self):
        original = self.ai.extract
        failed = [False]
        def second_fails_once(pdf, second=False):
            if second and not failed[0]:
                failed[0] = True
                raise a.ProviderError('AI overloaded', retryable=True)
            return original(pdf, second)
        self.ai.extract = second_fails_once
        self.assertEqual('retryable', self.process()['status'])
        self.assertEqual(1, self.ai.calls)
        self.process()
        self.assertEqual(1, self.ai.calls)
        self.engine.retry(DOC)
        self.assertEqual('verified', self.process()['status'])
        self.assertEqual((2,1), (self.ai.calls,self.zoho.posts))

    def test_charge_amount_must_belong_to_its_own_printed_row(self):
        self.engine.source_reader = lambda pdf: source_fixture().replace('Documentation    100.00', 'Documentation    99.00')
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_charge_rate_must_match_printed_rate(self):
        self.engine.source_reader = lambda pdf: source_fixture().replace('18%', '5%')
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_absent_buyer_gstin_cannot_be_echoed_from_prompt(self):
        self.engine.source_reader = lambda pdf: source_fixture().replace(a.OWNER_GSTIN, '')
        self.assertNotIn(a.OWNER_GSTIN, a.PROMPT)
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_missing_printed_supplier_registration_never_posts(self):
        self.ai.parsed['vendor_gstin'] = ''
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_supplier_name_disagreement_is_a_hold(self):
        self.ai.second = {**invoice(), 'vendor_name': 'DIFFERENT SUPPLIER'}
        self.assertNotEqual(a.source_signature(invoice()), a.source_signature(self.ai.second))
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual(0, self.zoho.posts)

    def test_reused_number_with_different_financials_is_not_duplicate(self):
        self.zoho.bill = {'bill_id': '999', 'bill_number': 'SYNTHETIC-001', 'date': '2025-04-02', 'total': 5000}
        job = self.process()
        self.assertEqual('needs_review', job['status'])
        self.assertIn('Same invoice number', job['message'])
        self.assertEqual((0, 0), (self.zoho.posts, self.zoho.attachments))

    def test_documented_bill_level_tax_shape_is_sufficient(self):
        original = self.zoho.call
        def documented(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.zoho.bill:
                self.zoho.bill['line_items'][0].pop('line_item_taxes', None)
            return result
        self.zoho.call = documented
        self.assertEqual('verified', self.process()['status'])
        self.assertEqual((1, 1), (self.zoho.posts, self.zoho.attachments))

    def test_transient_readback_recovers_without_reposting_or_reparsing(self):
        original = self.zoho.call
        first = [True]
        def flaky(endpoint, params=None, payload=None):
            if endpoint.startswith('bills/') and first[0]:
                first[0] = False
                raise a.ProviderError('Temporary read failure', retryable=True)
            return original(endpoint, params, payload)
        self.zoho.call = flaky
        self.assertEqual('retry_wait', self.process()['status'])
        self.assertEqual('verified', self.process()['status'])
        self.assertEqual((2, 1, 1), (self.ai.calls, self.zoho.posts, self.zoho.attachments))

    def test_definitive_rejection_retains_zoho_reason_and_never_enters_uncertain(self):
        original = self.zoho.call
        def rejected(endpoint, params=None, payload=None):
            if payload:
                self.zoho.posts += 1
                raise a.ProviderError('Zoho 3032: IGST required', definitive=True)
            return original(endpoint, params, payload)
        self.zoho.call = rejected
        job = self.process()
        self.assertEqual('rejected', job['status'])
        self.assertIn('3032', job['message'])
        self.assertNotIn('uncertain', job['message'])
        self.process()
        self.assertEqual(1, self.zoho.posts)

    def test_interrupted_second_read_requires_explicit_retry_and_reuses_first(self):
        job = self.engine.read(DOC)
        self.engine.save(job, 'checking_source', parsed=invoice())
        self.assertEqual('retryable', self.process()['status'])
        self.assertEqual(0, self.ai.calls)
        self.engine.retry(DOC)
        self.assertEqual('verified', self.process()['status'])
        self.assertEqual((1, 1), (self.ai.calls, self.zoho.posts))

    def test_retries_of_unknown_create_only_lookup(self):
        self.zoho.lose_without_create = True
        self.assertEqual('reconcile_pending', self.process()['status'])
        for _ in range(4):
            self.process()
        self.assertEqual('retryable', self.engine.read(DOC)['status'])
        self.engine.retry(DOC)
        self.process()
        self.assertEqual((2, 1), (self.ai.calls, self.zoho.posts))

    def test_queue_does_not_wait_for_provider(self):
        entered, release = threading.Event(), threading.Event()
        def slow(pdf, second=False):
            entered.set()
            release.wait(3)
            return copy.deepcopy(invoice())
        self.ai.extract = slow
        worker = threading.Thread(target=self.engine.process, args=(DOC,))
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            start = time.monotonic()
            self.engine.enqueue('b'*32)
            self.assertLess(time.monotonic()-start, .5)
            self.assertEqual('queued', self.engine.read('b'*32)['status'])
        finally:
            release.set()
            worker.join(3)

    def test_intent_scan_failure_is_visible_and_worker_survives(self):
        broken = [True]
        def scan():
            if broken[0]:raise OSError('synthetic disk error')
        with patch.object(self.engine, 'recover_uploads', side_effect=scan):
            self.engine.start()
            deadline = time.monotonic()+3
            while not self.engine.worker_error and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.engine.worker.is_alive())
            self.assertFalse(self.engine.healthy())
            broken[0] = False
            deadline = time.monotonic()+3
            while not self.engine.healthy() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.engine.healthy())

    def test_source_reader_failure_prevents_paid_calls(self):
        self.engine.source_reader = lambda pdf: (_ for _ in ()).throw(a.Hold('No readable PDF text'))
        self.assertEqual('needs_review', self.process()['status'])
        self.assertEqual((0, 0), (self.ai.calls, self.zoho.posts))

    def test_retry_form_and_organisation_link(self):
        with patch.dict(os.environ, {'ZOHO_ORGANIZATION_ID': '12345'}):
            page = a.render_status(DOC, 's.pdf', {'status': 'retryable', 'message': 'Retry', 'bill_id': '456'}, 'csrf-test')
        self.assertIn('/api/retry/'+DOC, page)
        self.assertIn('books.zoho.in/app/12345#/bills/456', page)
        self.assertIn('name="csrf"', page)


class ProviderRegressions(unittest.TestCase):
    def test_meta_alias_uses_maintained_core_key(self):
        def transport(*args, **kwargs):
            return {'code': 0, 'bills': [{'vendor_id': '55', 'bill_number': 'FBADS-190-12345', 'bill_id': '1'}], 'page_context': {'has_more_page': False}}
        with patch.dict(os.environ, {'ZOHO_ORGANIZATION_ID': '1'}):
            zoho = a.Zoho(transport)
        zoho.token, zoho.expires, zoho.core = 'synthetic', 1e18, core_fixture()
        self.assertEqual('1', zoho.find('55', 'ADS190-12345')['bill_id'])

    def test_http_error_preserves_code_and_sanitizes_secret(self):
        error = urllib.error.HTTPError('https://synthetic.invalid', 400, 'Bad Request', {}, io.BytesIO(b'{"code":3032,"message":"IGST required synthetic-secret-123"}'))
        with patch.dict(os.environ, {'ZOHO_CLIENT_SECRET': 'synthetic-secret-123'}), patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(a.ProviderError) as caught:
                a.json_request('https://synthetic.invalid', {}, {})
        self.assertTrue(caught.exception.definitive)
        self.assertIn('3032', str(caught.exception))
        self.assertIn('IGST required', str(caught.exception))
        self.assertNotIn('synthetic-secret-123', str(caught.exception))


class BudgetSettlement(unittest.TestCase):
    setUp = fixtures.BudgetAndProviderTests.setUp
    tearDown = fixtures.BudgetAndProviderTests.tearDown

    def test_settlement_releases_unused_reservation_without_erasing_prior_costs(self):
        a.atomic(self.folder/'ai-budget.json', {'reserved_usd': '.10', 'calls_reserved': 2})
        def transport(url, headers, body, timeout=30):
            if url.endswith('count_tokens'):return {'input_tokens':1000}
            self.assertEqual(Decimal('.13125'), Decimal(json.loads((self.folder/'ai-budget.json').read_text())['reserved_usd']))
            return {'stop_reason':'end_turn', 'usage':{'input_tokens':1000,'output_tokens':200},
                    'content':[{'type':'text','text':json.dumps(invoice())}]}
        a.Haiku(self.folder,transport).extract(PDF)
        budget=json.loads((self.folder/'ai-budget.json').read_text())
        self.assertEqual(Decimal('.102'),Decimal(budget['reserved_usd']))
        self.assertEqual((3,1),(budget['calls_reserved'],budget['calls_settled']))

    def test_unknown_failed_call_keeps_full_reservation(self):
        def transport(url, *args, **kwargs):
            if url.endswith('count_tokens'):return {'input_tokens':1000}
            raise a.ProviderError('Synthetic lost response',retryable=True)
        with self.assertRaises(a.ProviderError):a.Haiku(self.folder,transport).extract(PDF)
        self.assertEqual(Decimal('.03125'),Decimal(json.loads((self.folder/'ai-budget.json').read_text())['reserved_usd']))


if __name__ == '__main__':
    unittest.main()
