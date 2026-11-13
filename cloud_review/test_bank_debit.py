"""Overseas foreign-currency invoices booked in INR from the owner's bank debits. Synthetic data only."""
import contextlib
import copy
import hashlib
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import automation as a
from test_automation import DOC, PDF, FakeAI, core_fixture

OIDAR = '9925USA12345OSI'   # Synthetic overseas registration.
VENDOR = 'SYNTHETIC CLOUD TOOLS'


def overseas_text(amount='10.00'):
    return '\n'.join([
        'Invoice', 'Invoice number        SYNCLOUD-0001', 'Date of issue         November 3, 2026',
        'Date due              November 3, 2026', 'VAT Registration      India GST:', '                      '+OIDAR, '',
        'Synthetic Cloud Tools, Inc                        Bill to',
        '1 Example Street                                  Synthetic Owner',
        'United States                                     India',
        '                                                  IN GST '+a.OWNER_GSTIN, '',
        '$'+amount+' USD due November 3, 2026', '',
        'Description                                    Qty   Unit price    Tax            Amount',
        'Prepaid usage, Synthetic plan                    1    $'+amount+'      0%             $'+amount, '',
        '                                     Subtotal                                        $'+amount,
        '                                     Total                                           $'+amount,
        '                                     Amount due                                  $'+amount+' USD', '',
        'HSN Code: 998315', 'Tax Invoice', '[1] Tax to be paid on reverse charge basis', ''])


def overseas_invoice(amount='10.00'):
    return {'vendor_name': 'Synthetic Cloud Tools, Inc', 'vendor_gstin': OIDAR, 'buyer_gstin': a.OWNER_GSTIN,
            'bill_number': 'SYNCLOUD-0001', 'bill_date': '2026-11-03', 'due_date': '2026-11-03',
            'currency_code': 'USD', 'is_overseas': True,
            'line_items': [{'description': 'Prepaid usage, Synthetic plan', 'quantity': 1, 'rate': amount,
                            'hsn_or_sac': '998315', 'tax_percentage': '0', 'cgst': None, 'sgst': None, 'igst': None}],
            'subtotal': amount, 'discounts_or_credits': '0.00', 'tax_amount_cgst': '0.00', 'tax_amount_sgst': '0.00',
            'tax_amount_igst': '0.00', 'invoice_total': amount, 'mbl_number': '', 'notes': '',
            'confidence': 'high', 'issues': [], 'multi_invoice_count': 1}


class RcmZoho:
    """Saves a bill the way Zoho reports a reverse-charge bill: no supplier tax, RCM per line."""
    def __init__(self, tax_bank_line=False):
        self.bill, self.posts, self.attachments, self.tax_bank_line = None, 0, 0, tax_bank_line
    def all(self, endpoint, collection, params=None):
        return [{'contact_name': VENDOR, 'contact_id': '321', 'gst_no': ''}]
    def find(self, vendor_id, number):
        return self.bill
    def call(self, endpoint, params=None, payload=None):
        if payload:
            self.posts += 1
            lines = copy.deepcopy(payload['line_items'])
            for line in lines:
                if line.get('reverse_charge_tax_id') or self.tax_bank_line:
                    line['reverse_charge_tax_id'] = line.get('reverse_charge_tax_id') or '914109000000014234'
                    line['reverse_charge_tax_amount'] = float((Decimal(str(line['rate']))*Decimal('0.18')).quantize(Decimal('.01')))
            total = sum(Decimal(str(line['rate'])) for line in lines)
            self.bill = {**copy.deepcopy(payload), 'line_items': lines, 'bill_id': '456', 'gst_no': '',
                         'sub_total': float(total), 'total': float(total), 'tax_total': 0, 'taxes': [],
                         'is_reverse_charge_applied': True, 'documents': []}
        return {'code': 0, 'bill': self.bill}
    def attach(self, bid, pdf):
        self.attachments += 1; self.pdf = pdf; self.bill['documents'] = [{'document_id': '789'}]
    def verify_attachment(self, bid, pdf):
        assert self.pdf == pdf


class SourceChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def test_overseas_dollar_layout_is_corroborated(self):
        self.assertTrue(a.corroborate_source(overseas_invoice(), overseas_text(), self.core))

    def test_oidar_registration_must_be_the_printed_one(self):
        other = overseas_invoice(); other['vendor_gstin'] = '9925USA99999OSI'
        with self.assertRaises(a.Hold):
            a.corroborate_source(other, overseas_text(), self.core)
        text = overseas_text().replace('VAT Registration      India GST:', 'VAT Registration')
        with self.assertRaisesRegex(a.Hold, 'overseas GST registration'):
            a.corroborate_source(overseas_invoice(), text, self.core)

    def test_printed_inr_buyer_must_still_be_sattva(self):
        text = overseas_text().replace('IN GST '+a.OWNER_GSTIN, 'IN GST 27ZZZZZ9999Z1Z9')
        with self.assertRaises(a.Hold):
            a.corroborate_source(overseas_invoice(), text, self.core)


class BankBooking(unittest.TestCase):
    def test_lines_sum_exactly_to_the_payment_debit(self):
        parsed = overseas_invoice('30.00')
        parsed['line_items'] = [dict(parsed['line_items'][0], rate='10.00'), dict(parsed['line_items'][0], rate='20.00', description='Second')]
        booking = a.bank_booking(parsed, {'transaction_inr': '2851.36', 'charges_inr': '50.00'})
        self.assertEqual(['950.45', '1900.91'], [x['rate'] for x in booking['line_items']])
        self.assertEqual(('INR', '2851.36', '2851.36'), (booking['currency_code'], booking['subtotal'], booking['invoice_total']))
        self.assertIn('USD 30.00', booking['bank_note'])
        self.assertEqual('USD', parsed['currency_code'])  # The printed reading is never rewritten.

    def test_implausible_rate_or_charges_ask_again(self):
        for bank in ({'transaction_inr': '9504.50', 'charges_inr': '0'}, {'transaction_inr': '95.04', 'charges_inr': '0'},
                     {'transaction_inr': '950.45', 'charges_inr': '96.00'}):
            with self.subTest(bank=bank), self.assertRaises(a.NeedsBankAmounts):
                a.bank_booking(overseas_invoice(), bank)


class BankDebitJourney(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def setUp(self, zoho=None):
        self.tmp = tempfile.TemporaryDirectory(); folder = Path(self.tmp.name)
        db = folder/'review.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT,pdf BLOB)')
            conn.execute('INSERT INTO invoices VALUES(?,?,?,?)', (DOC, 's.pdf', hashlib.sha256(PDF).hexdigest(), PDF))
        self.ai, self.zoho = FakeAI(overseas_invoice()), zoho or RcmZoho()
        self.engine = a.Engine(db, self.core, self.ai, self.zoho, source_reader=lambda pdf: overseas_text())
        self.engine.enqueue(DOC)
        for p in (patch.dict(os.environ, {'APP_AUTOMATION_POSTING': '1'}),
                  patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))):
            p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.engine.close(); self.tmp.cleanup()

    def test_asks_for_bank_amounts_then_books_inr_with_separate_untaxed_charges(self):
        self.engine.process(DOC)
        job = self.engine.read(DOC)
        self.assertEqual('needs_bank_amounts', job['status'], job['message'])
        self.assertEqual(0, self.zoho.posts)
        page = a.render_status(DOC, 's.pdf', job, csrf='tok')
        self.assertIn('/api/bank-debit/'+DOC, page); self.assertIn('name="charges"', page)
        self.engine.set_bank_debit(DOC, '950.45', '39.24')
        self.engine.process(DOC)
        job = self.engine.read(DOC)
        self.assertEqual('verified', job['status'], job['message'])
        self.assertEqual((2, 1, 1), (self.ai.calls, self.zoho.posts, self.zoho.attachments))
        service, bank = job['payload']['line_items']
        self.assertEqual((950.45, '914109000000014234'), (service['rate'], service['reverse_charge_tax_id']))
        self.assertEqual((39.24, a.BANK_CHARGE_LINE), (bank['rate'], bank['description']))
        self.assertNotIn('reverse_charge_tax_id', bank); self.assertNotIn('tax_id', bank)
        self.assertEqual((a.EXPENSE_ACCOUNT, a.BANK_FEES_ACCOUNT), (service['account_id'], bank['account_id']))
        self.assertEqual('INR', job['payload']['currency_code'])
        self.assertTrue(job['payload']['notes'].startswith('Original USD 10.00 paid by bank debit INR 950.45 (effective 95.0450 INR/USD)'))
        self.assertEqual('USD', job['parsed']['currency_code'])
        self.assertEqual('39.24', job['verification']['bank_debit']['charges_inr'])

    def test_wrong_amounts_keep_asking_and_nothing_posts(self):
        self.engine.process(DOC)
        with self.assertRaises(a.Hold):
            self.engine.set_bank_debit(DOC, '950,45abc', '1')
        self.engine.set_bank_debit(DOC, '9504.50', '39.24')
        self.engine.process(DOC)
        job = self.engine.read(DOC)
        self.assertEqual('needs_bank_amounts', job['status']); self.assertIn('INR per USD', job['message'])
        self.assertEqual(0, self.zoho.posts)
        self.assertIn('value="9504.50"', a.render_status(DOC, 's.pdf', job, csrf='tok'))

    def test_bank_amounts_only_accepted_while_waiting(self):
        with self.assertRaisesRegex(a.Hold, 'not waiting'):
            self.engine.set_bank_debit(DOC, '950.45', '39.24')

    def test_zoho_taxing_the_bank_line_is_held_after_read_back(self):
        self.tearDown(); self.setUp(RcmZoho(tax_bank_line=True))
        self.engine.process(DOC); self.engine.set_bank_debit(DOC, '950.45', '39.24'); self.engine.process(DOC)
        job = self.engine.read(DOC)
        self.assertEqual('needs_review', job['status']); self.assertIn('bank_charge_line_taxed', job['message'])
        self.assertEqual(1, self.zoho.posts)

    def test_rerun_after_creation_finds_the_same_bill_and_never_posts_twice(self):
        self.engine.process(DOC); self.engine.set_bank_debit(DOC, '950.45', '39.24'); self.engine.process(DOC)
        job = self.engine.read(DOC); self.engine.save(job, 'retryable')
        self.engine.retry(DOC); self.engine.process(DOC)
        self.assertEqual('verified', self.engine.read(DOC)['status'])
        self.assertEqual(1, self.zoho.posts)


if __name__ == '__main__':
    unittest.main()


class ReverseChargeNotes(unittest.TestCase):
    NOTE = ('Overseas supplier with Indian GSTIN for reverse charge mechanism; no GST charged despite note referencing '
            'GST applicability to non-registered customers—this is normal for reverse charge on digital services')

    def reading(self, **values):
        base = overseas_invoice(); base['currency_code'] = 'INR'; base.update(values)
        return base

    def test_high_confidence_reverse_charge_note_on_untaxed_oidar_invoice_is_accepted(self):
        self.assertTrue(a.reverse_charge_note_only(self.reading(issues=[self.NOTE])))

    def test_doubt_tax_low_confidence_or_domestic_supplier_still_hold(self):
        for values in ({'issues': [self.NOTE+'; amount inferred from total']},
                       {'issues': ['Unit price shown as dash']},
                       {'issues': [self.NOTE], 'confidence': 'medium'},
                       {'issues': [self.NOTE], 'tax_amount_igst': '18.00'},
                       {'issues': [self.NOTE], 'vendor_gstin': '27ABCDE1234F1Z5'},
                       {'issues': self.NOTE}):
            with self.subTest(values=values):
                self.assertFalse(a.reverse_charge_note_only(self.reading(**values)))


class AccountRouting(unittest.TestCase):
    def test_owner_routing_by_matched_vendor_and_google_service_code(self):
        cases = {('ANTHROPIC', '998315'): a.SOFTWARE_ACCOUNT, ('OpenAI, LLC', ''): a.SOFTWARE_ACCOUNT,
                 ('Railway Corporation', ''): a.SOFTWARE_ACCOUNT, ('Cloudflare, Inc.', ''): a.SOFTWARE_ACCOUNT,
                 ('ShipsGo', ''): a.SOFTWARE_ACCOUNT, ('Sendinblue SAS', ''): a.ADVERTISING_ACCOUNT,
                 ('GOOGLE INDIA PRIVATE LIMITED', '998319'): a.SOFTWARE_ACCOUNT,
                 ('GOOGLE INDIA PRIVATE LIMITED', '998365'): a.ADVERTISING_ACCOUNT,
                 ('GOOGLE INDIA PRIVATE LIMITED', '996713'): a.EXPENSE_ACCOUNT,
                 ('SYNTHETIC FREIGHT VENDOR', '996713'): a.EXPENSE_ACCOUNT,
                 ('TOTAL TRANSPORT SYSTEMS LIMITED', ''): a.EXPENSE_ACCOUNT}
        for (vendor, sac), account in cases.items():
            with self.subTest(vendor=vendor, sac=sac):
                self.assertEqual(account, a.line_account(vendor, sac))


class PrintedCredit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def credit_invoice(self, credit='5.00', total='15.00', currency='USD'):
        reading = overseas_invoice('20.00')
        reading.update(currency_code=currency, discounts_or_credits=credit, invoice_total=total)
        return reading

    def credit_text(self, line='Included usage ($5.00 off)                                 -$5.00'):
        return overseas_text('20.00').replace(
            '                                     Total                                           $20.00',
            '                                     '+line+'\n                                     Total                                           $15.00').replace(
            'Amount due                                  $20.00 USD', 'Amount due                                  $15.00 USD').replace(
            '$20.00 USD due', '$15.00 USD due')

    def test_printed_credit_is_netted_into_the_bank_payment(self):
        reading = self.credit_invoice()
        self.assertTrue(a.corroborate_source(reading, self.credit_text(), self.core))
        booking = a.bank_booking(reading, {'transaction_inr': '1450.00', 'charges_inr': '50.00'})
        self.assertEqual(['1450.00'], [x['rate'] for x in booking['line_items']])
        self.assertIn('after printed credit USD 5.00 on charges of USD 20.00', booking['bank_note'])

    def test_credit_not_printed_as_a_deduction_holds(self):
        with self.assertRaisesRegex(a.Hold, 'credit is not printed'):
            a.corroborate_source(self.credit_invoice(), self.credit_text('Included usage                                             5.00'), self.core)

    def test_credit_on_inr_invoice_or_with_wrong_arithmetic_still_holds(self):
        for reading in (self.credit_invoice(currency='INR'), self.credit_invoice(total='14.00')):
            with self.subTest(reading=reading['currency_code']+reading['invoice_total']), self.assertRaises(a.Hold):
                a.validate(reading, self.core, {VENDOR: {'id': '321', 'gst_no': ''}})


class PrintedCreditReviewFindings(PrintedCredit):
    def test_reading_notes_that_only_describe_the_proven_credit_are_accepted(self):
        reading = self.credit_invoice(); reading['issues'] = ['Included usage credit of $5.00 applied to subtotal']
        self.assertTrue(a.reverse_charge_note_only(reading))
        for note in ('Credit amount unclear', 'Discount inferred from total'):
            with self.subTest(note=note):
                self.assertFalse(a.reverse_charge_note_only(dict(reading, issues=[note])))
        self.assertFalse(a.reverse_charge_note_only(dict(self.credit_invoice(total='14.00'), issues=reading['issues'])))

    def test_explicit_credit_label_row_with_the_exact_credit_is_accepted(self):
        self.assertTrue(a.corroborate_source(self.credit_invoice(), self.credit_text(
            'Credit                                                     -$5.00'), self.core))
        with self.assertRaises(a.Hold):
            a.corroborate_source(self.credit_invoice(), self.credit_text(
                'Credit                                                     -$5.00\n   Adjustment   -$1.00'), self.core)

    def test_credit_evidence_must_be_in_the_invoice_currency(self):
        with self.assertRaisesRegex(a.Hold, 'credit is not printed'):
            a.corroborate_source(self.credit_invoice(), self.credit_text(
                'Included usage                                             -€5.00'), self.core)

    def test_zero_line_kept_by_one_reading_and_omitted_by_the_other_still_agree(self):
        first = self.credit_invoice()
        zero = dict(first['line_items'][0], description='Prepaid usage, Synthetic plan', rate='0.00')
        second = copy.deepcopy(first)
        text = self.credit_text().replace('Prepaid usage, Synthetic plan                    1    $20.00      0%             $20.00',
            'Prepaid usage, Synthetic plan                    1    $20.00      0%             $20.00\nUnused storage                                   0    $0.00       0%             $0.00')
        second['line_items'].append(dict(zero, description='Unused storage'))
        parsed, evidence = a.reconcile_readings(first, second, text, self.core)
        self.assertEqual(1, len(parsed['line_items']))


class OmittedInvoiceCount(unittest.TestCase):
    """Live Haiku readings sometimes leave multi_invoice_count out entirely."""
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def reading(self, **changes):
        value = overseas_invoice(); value.pop('multi_invoice_count'); value.update(changes)
        return value

    def test_clean_reading_without_the_count_is_one_invoice(self):
        parsed, evidence = a.reconcile_readings(self.reading(), self.reading(), overseas_text(), self.core)
        self.assertEqual(1, parsed['multi_invoice_count'])
        self.assertIn('multi_invoice_count', {c['field'] for c in evidence['corrections']})
        with self.assertRaises(a.NeedsBankAmounts):
            a.validate(parsed, self.core, {VENDOR: {'id': '7', 'gst_no': OIDAR}})

    def test_explicit_or_doubtful_counts_still_hold(self):
        for first in (self.reading(multi_invoice_count=None), self.reading(multi_invoice_count=2),
                      self.reading(multi_invoice_count=0), self.reading(confidence='medium'),
                      self.reading(issues=['Unclear digit in total'])):
            with self.subTest(first=first.get('multi_invoice_count'), confidence=first['confidence']):
                with self.assertRaises(a.Hold):
                    a.reconcile_readings(first, self.reading(), overseas_text(), self.core)

    def test_omitted_count_needs_the_pdf_to_show_one_invoice(self):
        second_invoice = overseas_text() + '\n'.join([
            '', 'Invoice', 'Invoice number        SYNCLOUD-0002',
            '                                     Total                                           $10.00',
            '                                     Amount due                                  $10.00 USD', ''])
        unlabelled_second = overseas_text() + '\n'.join([
            '', 'Statement page', '                                     Total                                           $3.00', ''])
        credit_note = overseas_text().replace('Tax Invoice', 'Credit Note')
        for name, text in (('second invoice', second_invoice), ('unlabelled second total', unlabelled_second),
                           ('credit note', credit_note)):
            with self.subTest(name), self.assertRaises(a.Hold):
                a.reconcile_readings(self.reading(), self.reading(), text, self.core)
        # An explicit count of one is still taken from the reading, as before.
        a.reconcile_readings(overseas_invoice(), overseas_invoice(), overseas_text(), self.core)

