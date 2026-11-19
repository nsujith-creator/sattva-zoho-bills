"""Synthetic leakage-reconciliation tests. No credentials, no network, no Zoho calls."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import leakage as L

TODAY = date(2026, 11, 19)
CONFIG = {'ZOHO_ORGANIZATION_ID': '123456', 'ZOHO_CLIENT_ID': '1000.SYNTHETIC',
          'ZOHO_CLIENT_SECRET': 'synthetic', 'ZOHO_RECON_REFRESH_TOKEN': '1000.aa.bb'}


def bill(bid, ref='OOLU2712345678', amount='10000', d='2026-10-01', vendor='SYNTHETIC LINER', number=None,
         balance='0', total=None, lines=None, **extra):
    total = total or str(round(float(amount)*1.18, 2))
    return dict({'bill_id': bid, 'bill_number': number or 'B-'+bid, 'vendor_id': 'v-'+vendor, 'vendor_name': vendor,
                 'date': d, 'status': 'paid' if balance == '0' else 'open', 'reference_number': ref, 'sub_total': amount,
                 'total': total, 'balance': balance, 'exchange_rate': 1, 'currency_code': 'INR',
                 'line_items': lines or [{'description': 'Ocean freight', 'item_total': amount}],
                 'last_modified_time': '2026-10-01T10:00:00+0530'}, **extra)


def invoice(iid, text='MBL OOLU2712345678', amount='12000', d='2026-10-03', customer='C1', status='paid',
            balance='0', due='2026-10-18', ref='', **extra):
    return dict({'invoice_id': iid, 'invoice_number': 'INV-'+iid, 'customer_id': customer, 'customer_name': 'Customer '+customer,
                 'date': d, 'due_date': due, 'status': status, 'reference_number': ref, 'sub_total': amount,
                 'total': str(round(float(amount)*1.18, 2)), 'balance': balance, 'exchange_rate': 1,
                 'line_items': [{'description': text, 'item_total': amount}], 'notes': '',
                 'last_modified_time': '2026-10-03T10:00:00+0530'}, **extra)


def evidence(bills=(), invoices=(), credits=()):
    return {'bills': {b['bill_id']: b for b in bills}, 'invoices': {i['invoice_id']: i for i in invoices},
            'creditnotes': {c['creditnote_id']: c for c in credits}}


def types(report, section):
    return [i['type'] for i in report[section]]


class FakeZoho:
    """Transport double: serves lists and details, records every request."""
    def __init__(self, docs, fail_after=None):
        self.docs, self.requests, self.fail_after = docs, [], fail_after

    def __call__(self, url, headers):
        self.requests.append(url)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            raise L.Incomplete('Zoho connection failed: synthetic timeout')
        path = url.split(L.BOOKS)[1].split('?')[0]
        kind, _, doc_id = path.partition('/')
        if doc_id:
            return {'code': 0, L.SINGULAR[kind]: self.docs[kind][doc_id]}
        rows = [{k: v for k, v in d.items() if k not in ('line_items', 'notes')} for d in self.docs[kind].values()]
        return {'code': 0, kind: rows, 'page_context': {'has_more_page': False}}


def client(transport, **kw):
    return L.ReadOnlyZoho(CONFIG, transport=transport, token_source=lambda c: 'tok', pace=0, **kw)


class Safety(unittest.TestCase):
    def test_client_has_no_write_surface(self):
        c = client(lambda u, h: {'code': 0})
        for name in ('post', 'put', 'delete', 'patch', 'call', 'attach', 'create'):
            self.assertFalse(hasattr(c, name), name)
        with self.assertRaises(TypeError):
            c.get('bills', {}, {'payload': 1})

    def test_endpoint_allowlist(self):
        c = client(lambda u, h: {'code': 0})
        for bad in ('contacts', 'bills/1/attachment', 'invoices/1/status/void', 'bills?x', '../bills', 'settings/taxes', 'creditnotes/abc'):
            with self.assertRaises(PermissionError, msg=bad):
                c.get(bad)

    def test_transport_only_issues_get_without_body(self):
        seen = []

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, n): return b'{"code":0}'

        def fake_urlopen(request, timeout):
            seen.append((request.get_method(), request.data))
            return Resp()
        with patch.object(L.urllib.request, 'urlopen', fake_urlopen):
            L.http_get(L.BOOKS+'bills?organization_id=1', {})
        self.assertEqual(seen, [('GET', None)])

    def test_placeholder_and_truncated_config_refused(self):
        with self.assertRaises(L.ConfigError):
            L.load_config(dict(CONFIG, ZOHO_CLIENT_SECRET='<secret>'))
        with self.assertRaises(L.ConfigError):
            L.load_config(dict(CONFIG, ZOHO_RECON_REFRESH_TOKEN='100.aa.bb'))
        with self.assertRaises(L.ConfigError):
            L.load_config({k: v for k, v in CONFIG.items() if k != 'ZOHO_ORGANIZATION_ID'})
        self.assertEqual(L.load_config(CONFIG)['ZOHO_ORGANIZATION_ID'], '123456')

    def test_budget_stops_requests(self):
        c = client(lambda u, h: {'code': 0}, budget=2)
        c.get('bills'); c.get('bills')
        with self.assertRaises(L.BudgetExhausted):
            c.get('bills')

    def test_report_files_are_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'r.json'
            out.write_text('{}')
            with self.assertRaises(FileExistsError):
                L.run_once(out, client=client(FakeZoho(evidence())))


class Keys(unittest.TestCase):
    def test_container_check_digit(self):
        self.assertTrue(L.is_container('CSQU3054383'))
        self.assertFalse(L.is_container('CSQU3054384'))

    def test_split_tokens_join_and_weak_keys(self):
        self.assertIn('OOLU2712345678', L.candidates('B/L: OOLU 2712 345678'))
        self.assertFalse(L.valid_key('NA'))
        self.assertFalse(L.valid_key('ABCDEFGHIJ'))
        self.assertEqual(L.bill_keys(bill('1', ref='MBL - 1234567')), set())

    def test_bill_text_keys(self):
        b = bill('1', ref='', lines=[{'description': 'THC for CSQU 305438 3, B/L No: HLCU MUM 2410 9988', 'item_total': '5'}])
        self.assertEqual(L.bill_keys(b), {'CSQU3054383', 'HLCUMUM2410'})


class Cases(unittest.TestCase):
    # 1. Clean invoice
    def test_clean_shipment_has_no_findings(self):
        r = L.reconcile(evidence([bill('1')], [invoice('9')]), TODAY)
        self.assertEqual(r['status'], 'COMPLETE')
        self.assertEqual((r['confirmed_leakage'], r['potential_issues']), ([], []))
        self.assertEqual(r['shipments'], 1)

    def test_gst_is_excluded_from_margin(self):
        # Bill total with GST (11,800) exceeds invoice pre-GST (11,000) but pre-GST margin is positive.
        r = L.reconcile(evidence([bill('1', amount='10000')], [invoice('9', amount='11000')]), TODAY)
        self.assertEqual(r['confirmed_leakage'] + r['potential_issues'], [])

    def test_unbilled_after_seven_days_is_confirmed(self):
        r = L.reconcile(evidence([bill('1', d='2026-11-10')]), TODAY)
        self.assertEqual(types(r, 'confirmed_leakage'), ['UNBILLED'])
        self.assertEqual(r['totals_inr']['confirmed_deduplicated'], '10000.00')

    def test_awaiting_invoice_before_day_seven(self):
        r = L.reconcile(evidence([bill('1', d='2026-11-14')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['awaiting invoice'])
        self.assertEqual(r['confirmed_leakage'], [])

    def test_foreign_currency_uses_exchange_rate(self):
        b = bill('1', amount='100', exchange_rate=84.5, currency_code='USD', d='2026-10-01')
        r = L.reconcile(evidence([b]), TODAY)
        self.assertEqual(r['confirmed_leakage'][0]['exposure_inr'], '8450.00')

    # 2. Poor-quality or ambiguous
    def test_near_match_is_flagged_never_cleared(self):
        r = L.reconcile(evidence([bill('1', ref='2712345678')], [invoice('9', text='Freight OOLU2712345678X')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['uncertain near match'])
        self.assertEqual(r['confirmed_leakage'], [])

    def test_weak_key_is_potential_not_dropped(self):
        r = L.reconcile(evidence([bill('1', ref='TBA')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['no or weak shipment key'])
        self.assertEqual(r['potential_issues'][0]['exposure_inr'], '10000.00')

    def test_overhead_vendor_excluded_but_listed(self):
        r = L.reconcile(evidence([bill('1', ref='', vendor='Anthropic, PBC')]), TODAY)
        self.assertEqual(len(r['excluded_overhead']), 1)
        self.assertEqual(r['totals_inr']['excluded_overhead'], '10000.00')

    def test_overhead_vendor_with_shipment_key_stays_in_scope(self):
        r = L.reconcile(evidence([bill('1', vendor='ShipsGo')]), TODAY)
        self.assertEqual(types(r, 'confirmed_leakage'), ['UNBILLED'])

    def test_draft_invoice_is_not_billing(self):
        r = L.reconcile(evidence([bill('1')], [invoice('9', status='draft')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['draft invoice only'])

    # 3. Duplicate
    def test_duplicate_vendor_bill_flagged(self):
        r = L.reconcile(evidence([bill('1', number='X/1'), bill('2', number='X-1')], [invoice('9', amount='30000')]), TODAY)
        self.assertIn('possible duplicate vendor bill', types(r, 'potential_issues'))

    def test_reissued_vendor_bill_same_lines_flagged(self):
        r = L.reconcile(evidence([bill('1', number='A1', amount='1000'), bill('2', number='A2', amount='990', d='2026-10-04')],
                                 [invoice('9', amount='1200')]), TODAY)
        self.assertIn('possible re-issued vendor bill booked twice', types(r, 'potential_issues'))

    def test_house_number_in_bill_notes_matches_invoice_mbl_slot(self):
        b = bill('1', ref='MEDUAIX20528', notes='Master Number MEDUAIX20528, House Number NAV26MU3036, Vessel X')
        self.assertIn('NAV26MU3036', L.bill_keys(b))
        r = L.reconcile(evidence([b], [invoice('9', text='Freight', notes='MBL #: NAV26MU3036 / NAV26MU3037', amount='12000')]), TODAY)
        self.assertEqual(r['confirmed_leakage'] + r['potential_issues'], [])

    def test_invoice_without_vendor_cost_is_listed_without_exposure(self):
        r = L.reconcile(evidence([], [invoice('9')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['invoice with no vendor cost recorded'])
        self.assertEqual(r['totals_inr']['potential'], '0.00')

    def test_vendor_credit_cancelling_wrong_bill_is_not_double_cost(self):
        # Wrong bill A paid in cash; vendor credit note fully applied to corrected bill B.
        a = bill('1', number='A1', amount='10000', total='11800', payment_made=11800)
        b = bill('2', number='A2', amount='9900', total='11682', d='2026-10-04', vendor_credits_applied=11682, payment_made=0)
        r = L.reconcile(evidence([a, b], [invoice('9', amount='10500')]), TODAY)
        self.assertEqual(r['confirmed_leakage'], [])
        self.assertNotIn('possible re-issued vendor bill booked twice', types(r, 'potential_issues'))

    def test_credit_settled_bill_is_not_vendor_cash(self):
        b = bill('1', vendor_credits_applied=11800, payment_made=0)
        r = L.reconcile(evidence([b], [invoice('9', status='overdue', balance='14160')]), TODAY)
        self.assertNotIn('UNRECOVERED VENDOR PAYMENT', types(r, 'confirmed_leakage'))

    def test_unused_vendor_credit_reported_once_per_vendor(self):
        r = L.reconcile(evidence([bill('1', unused_credits_payable_amount=2345.52), bill('2', ref='MSCU1111111111', unused_credits_payable_amount=2345.52)],
                                 [invoice('9'), invoice('8', text='MSCU1111111111')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['unused vendor credit (vendor owes us)'])
        self.assertEqual(r['potential_issues'][0]['exposure_inr'], '2345.52')

    def test_invoice_matching_two_shipments_is_uncertain(self):
        r = L.reconcile(evidence([bill('1', ref='OOLU2712345678'), bill('2', ref='MSCU1111111111')],
                                 [invoice('9', text='OOLU2712345678 and MSCU1111111111')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['invoice matches several shipments (1:1 violation)']*2)

    def test_multi_customer_is_uncertain(self):
        r = L.reconcile(evidence([bill('1')], [invoice('9'), invoice('8', customer='C2')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['multi-customer shipment (1:1 violation)'])

    def test_bills_sharing_container_form_one_shipment(self):
        b1 = bill('1', ref='OOLU2712345678', lines=[{'description': 'Freight CSQU3054383', 'item_total': '6000'}], amount='6000')
        b2 = bill('2', ref='', vendor='CFS', lines=[{'description': 'CFS charges CSQU3054383', 'item_total': '4000'}], amount='4000')
        r = L.reconcile(evidence([b1, b2], [invoice('9', amount='12000')]), TODAY)
        self.assertEqual(r['shipments'], 1)
        self.assertEqual(r['confirmed_leakage'] + r['potential_issues'], [])

    # 4. Tax / amount mismatch
    def test_late_cost_not_rebilled_confirmed(self):
        r = L.reconcile(evidence([bill('1'), bill('2', vendor='CFS', d='2026-10-20', amount='5000',
                                                  lines=[{'description': 'Detention', 'item_total': '5000'}], ref='MBL: OOLU2712345678')],
                                 [invoice('9', amount='12000')]), TODAY)
        self.assertEqual(types(r, 'confirmed_leakage'), ['LATE COST NOT RE-BILLED'])
        self.assertEqual(r['confirmed_leakage'][0]['exposure_inr'], '3000.00')  # min(5000 late, 3000 shortfall)

    def test_below_cost_without_late_cost_needs_owner(self):
        r = L.reconcile(evidence([bill('1', d='2026-10-05')], [invoice('9', amount='9000', d='2026-10-06')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['billed below cost'])

    def test_accepted_loss_bound_to_fingerprint(self):
        ev = evidence([bill('1', d='2026-10-05')], [invoice('9', amount='9000', d='2026-10-06')])
        fp = L.reconcile(ev, TODAY)['potential_issues'][0]['fingerprint']
        decisions = {'links': [], 'overhead': [], 'accepted_losses': [{'fingerprint': fp, 'reason': 'Rate war', 'shipment_keys': ['OOLU2712345678']}]}
        self.assertEqual(types(L.reconcile(ev, TODAY, decisions), 'accepted_losses'), ['accepted commercial loss'])
        ev['invoices']['9']['sub_total'] = '8000'  # record changed -> decision re-opens
        r = L.reconcile(ev, TODAY, decisions)
        self.assertEqual((types(r, 'accepted_losses'), types(r, 'potential_issues')), ([], ['billed below cost']))
        self.assertIn('re-opened', r['potential_issues'][0]['detail'])

    def test_credit_note_reduces_revenue(self):
        cn = {'creditnote_id': '5', 'creditnote_number': 'CN-5', 'customer_id': 'C1', 'customer_name': 'Customer C1', 'date': '2026-10-10',
              'status': 'closed', 'sub_total': '4000', 'total': '4720', 'exchange_rate': 1, 'invoices_credited': [{'invoice_id': '9'}]}
        r = L.reconcile(evidence([bill('1')], [invoice('9')], [cn]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['billed below cost'])

    def test_credit_application_is_not_customer_cash(self):
        # Invoice 9 fully credited (balance 0) and re-issued as 8, which is overdue; vendor paid in full.
        cn = {'creditnote_id': '5', 'creditnote_number': 'CN-5', 'customer_id': 'C1', 'customer_name': 'Customer C1', 'date': '2026-10-04',
              'status': 'closed', 'sub_total': '12000', 'total': '14160', 'exchange_rate': 1, 'invoices_credited': [{'invoice_id': '9'}]}
        r = L.reconcile(evidence([bill('1')], [invoice('9'), invoice('8', d='2026-10-05', status='overdue', balance='14160')], [cn]), TODAY)
        self.assertEqual(types(r, 'confirmed_leakage'), ['UNRECOVERED VENDOR PAYMENT'])
        self.assertEqual(r['confirmed_leakage'][0]['exposure_inr'], '10000.00')

    def test_untied_credit_note_flagged(self):
        cn = {'creditnote_id': '5', 'creditnote_number': 'CN-5', 'customer_id': 'C1', 'customer_name': 'X', 'date': '2026-10-10',
              'status': 'open', 'sub_total': '100', 'total': '118', 'exchange_rate': 1}
        r = L.reconcile(evidence([], [], [cn]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['untied credit note'])

    def test_unrecovered_vendor_payment(self):
        r = L.reconcile(evidence([bill('1', balance='0')], [invoice('9', status='overdue', balance='14160', due='2026-10-18')]), TODAY)
        self.assertEqual(types(r, 'confirmed_leakage'), ['UNRECOVERED VENDOR PAYMENT'])
        self.assertEqual(r['confirmed_leakage'][0]['exposure_inr'], '10000.00')

    def test_overdue_receivable_vendor_unpaid_is_potential(self):
        r = L.reconcile(evidence([bill('1', balance='11800')], [invoice('9', status='overdue', balance='14160')]), TODAY)
        self.assertEqual(types(r, 'potential_issues'), ['overdue receivable (vendor not yet paid)'])

    def test_owner_link_requires_reason(self):
        ev = evidence([bill('1', ref='')], [invoice('9', text='no key here')])
        r = L.reconcile(ev, TODAY, {'links': [{'bill_id': '1', 'invoice_id': '9'}], 'overhead': [], 'accepted_losses': []})
        self.assertIn('decision without reason', types(r, 'potential_issues'))
        r = L.reconcile(ev, TODAY, {'links': [{'bill_id': '1', 'invoice_id': '9', 'reason': 'Same job'}], 'overhead': [], 'accepted_losses': []})
        self.assertEqual(r['confirmed_leakage'] + r['potential_issues'], [])

    # 5. Missing mandatory field
    def test_missing_field_makes_run_incomplete(self):
        docs = evidence([bill('1')], [invoice('9')])
        del docs['invoices']['9']['customer_id']
        with tempfile.TemporaryDirectory() as d:
            r = L.run_once(Path(d)/'r.json', cache_dir=Path(d)/'c', client=client(FakeZoho(docs)), today=TODAY)
        self.assertEqual(r['status'], 'INCOMPLETE')
        self.assertTrue(any('missing customer_id' in x for x in r['incomplete_reasons']))
        # The bill is not silently cleared: with its invoice unusable it is reported unbilled.
        self.assertEqual(types(r, 'confirmed_leakage'), ['UNBILLED'])

    def test_unverifiable_pagination_is_incomplete(self):
        def transport(url, headers):
            return {'code': 0, 'bills': [], 'invoices': [], 'creditnotes': [], 'page_context': {}}
        with tempfile.TemporaryDirectory() as d:
            r = L.run_once(Path(d)/'r.json', client=client(transport), today=TODAY)
        self.assertEqual(r['status'], 'INCOMPLETE')

    # 6. Retry after partial failure
    def test_retry_after_partial_failure_uses_cache_and_completes(self):
        docs = evidence([bill(str(n), ref='OOLU27123456'+str(n).zfill(2)) for n in range(1, 6)],
                        [invoice('9'+str(n), text='OOLU27123456'+str(n).zfill(2)) for n in range(1, 6)])
        with tempfile.TemporaryDirectory() as d:
            first = L.run_once(Path(d)/'r1.json', cache_dir=Path(d)/'c', client=client(FakeZoho(docs, fail_after=4)), today=TODAY)
            self.assertEqual(first['status'], 'INCOMPLETE')
            second_transport = FakeZoho(docs)
            second = L.run_once(Path(d)/'r2.json', cache_dir=Path(d)/'c', client=client(second_transport), today=TODAY)
            self.assertEqual(second['status'], 'COMPLETE')
            self.assertEqual(second['confirmed_leakage'] + second['potential_issues'], [])
            detail_gets = [u for u in second_transport.requests if '/bills/' in u]
            self.assertEqual(len(detail_gets), 2)  # three bill details came from the first run's cache
            # A changed last_modified_time forces a refetch rather than trusting the cache.
            docs['bills']['1']['last_modified_time'] = '2026-11-01T00:00:00+0530'
            third_transport = FakeZoho(docs)
            L.run_once(Path(d)/'r3.json', cache_dir=Path(d)/'c', client=client(third_transport), today=TODAY)
            self.assertEqual([u.split('?')[0].rsplit('/', 1)[1] for u in third_transport.requests if '/bills/' in u], ['1'])

    def test_budget_exhaustion_is_incomplete_not_clean(self):
        docs = evidence([bill('1')], [invoice('9')])
        with tempfile.TemporaryDirectory() as d:
            r = L.run_once(Path(d)/'r.json', client=client(FakeZoho(docs), budget=2), today=TODAY)
        self.assertEqual(r['status'], 'INCOMPLETE')
        self.assertTrue(any('budget' in x for x in r['incomplete_reasons']))

    def test_every_request_was_get_on_allowlist(self):
        docs = evidence([bill('1')], [invoice('9')])
        t = FakeZoho(docs)
        with tempfile.TemporaryDirectory() as d:
            L.run_once(Path(d)/'r.json', client=client(t), today=TODAY)
        for url in t.requests:
            self.assertRegex(url.split(L.BOOKS)[1].split('?')[0], r'^(bills|invoices|creditnotes)(/\d+)?$')


if __name__ == '__main__':
    unittest.main()
