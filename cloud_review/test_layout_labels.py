"""Zoho-invoice-template layouts ("GSTN:", "Total ₹", "CGST9 (9%)") and free source re-checks."""
import contextlib
import copy
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
import automation as a
from test_automation import GSTIN, PDF, DOC, FakeAI, FakeZoho, core_fixture, invoice


def layout_text(cgst_rows=('                 CGST9 (9%)        9.00',), total='Total               ₹118.00'):
    return '\n'.join([
        '        SYNTHETIC FREIGHT VENDOR', '        GSTN: '+GSTIN, '                      TAX INVOICE',
        'Invoice no        : SYNTHETIC-001', 'Invoice Date      : 28/10/2026', '', 'Bill To', 'SATTVA GLOBAL LOGISTICS',
        'GSTIN '+a.OWNER_GSTIN, '',
        'No  Item & Description      Qty   Rate       %      Amt     %      Amt     Amount',
        ' 1  Documentation          1.00  100.00     9%     9.00    9%     9.00    100.00', '    SAC: 996713',
        '                                              Sub Total          100.00',
        *cgst_rows, '                 SGST9 (9%)        9.00',
        'Notes                                         '+total, ''])


class LayoutLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def test_zoho_template_layout_is_corroborated(self):
        self.assertTrue(a.corroborate_source(invoice(), layout_text(), self.core))

    def test_split_rate_summary_rows_cannot_prove_one_head_total(self):
        rows = ('                 CGST9 (9%)        4.50', '                 CGST2.5 (2.5%)    4.50')
        with self.assertRaisesRegex(a.Hold, 'tax amount cgst'):
            a.corroborate_source(invoice(), layout_text(cgst_rows=rows), self.core)

    def test_total_without_currency_sign_or_with_other_currency_still_holds(self):
        for total in ('Total               118.00', 'Total               ₹118.00   USD'):
            with self.subTest(total=total), self.assertRaises(a.Hold):
                a.corroborate_source(invoice(), layout_text(total=total), self.core)

    def wrapped(self, description, row, after='    2026'):
        text = layout_text().replace(
            ' 1  Documentation          1.00  100.00     9%     9.00    9%     9.00    100.00',
            row+'\n'+after)
        reading = invoice(); reading['line_items'][0]['description'] = description
        return reading, text

    def test_description_joined_from_a_month_column_and_wrapped_year_is_corroborated(self):
        reading, text = self.wrapped('Documentation charges Oct 2026',
            ' 1  Documentation charges   Oct   1.00  100.00   9%   9.00   9%   9.00   100.00')
        self.assertTrue(a.corroborate_source(reading, text, self.core))

    def test_loosely_scattered_or_unprinted_description_words_still_hold(self):
        row = ' 1  Documentation charges   Oct   1.00  100.00   9%   9.00   9%   9.00   100.00'
        cases = (('Documentation charges Oct 2026', row, '\n\n\n    2026'),    # too far below the row
                 ('Documentation charges Nov 2026', row, '    2026'),          # word not printed
                 ('Documentation fee for Oct 2026', row, '    2026'))         # under half contiguous
        for description, printed, after in cases:
            with self.subTest(description=description, after=after), self.assertRaises(a.Hold):
                reading, text = self.wrapped(description, printed, after)
                a.corroborate_source(reading, text, self.core)

    def test_supplier_gstn_must_still_match_the_reading(self):
        other = copy.deepcopy(invoice()); other['vendor_gstin'] = '27ZZZZZ9999Z1Z9'
        with self.assertRaises(a.Hold):
            a.corroborate_source(other, layout_text(), self.core)


class SourceRecheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); folder = Path(self.tmp.name)
        db = folder/'review.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT,pdf BLOB)')
            conn.execute('INSERT INTO invoices VALUES(?,?,?,?)', (DOC, 's.pdf', hashlib.sha256(PDF).hexdigest(), PDF))
        self.engine = a.Engine(db, core_fixture(), FakeAI(invoice()), FakeZoho(folder), source_reader=lambda pdf: layout_text())
        self.engine.enqueue(DOC)

    def tearDown(self):
        self.engine.close(); self.tmp.cleanup()

    def hold(self, **values):
        job = self.engine.read(DOC)
        self.engine.save(job, 'needs_review', parsed=invoice(), source_check=invoice(), message='held', **values)

    def test_pre_create_source_hold_can_be_rechecked_without_new_reading(self):
        self.hold()
        self.engine.retry(DOC)
        job = self.engine.read(DOC)
        self.assertEqual('queued', job['status'])
        self.assertEqual(invoice(), job['parsed'])

    def test_hold_after_a_create_attempt_is_never_rechecked(self):
        for values in ({'create_attempted': True}, {'bill_id': '456'}, {'payload_sha256': 'x'}):
            with self.subTest(values=values):
                self.hold(**values)
                with self.assertRaises(a.Hold):
                    self.engine.retry(DOC)

    def test_reread_takes_two_fresh_readings_and_keeps_the_old_ones(self):
        flagged = dict(invoice(), confidence='medium', issues=['dash unit price'])
        job = self.engine.read(DOC)
        self.engine.save(job, 'needs_review', parsed=flagged, source_check=flagged, message='The two source readings disagree')
        self.engine.reread(DOC)
        job = self.engine.read(DOC)
        self.assertEqual('queued', job['status']); self.assertNotIn('parsed', job)
        self.assertEqual(flagged, job['previous_readings'][0]['parsed'])
        calls = self.engine.ai.calls
        self.engine.process(DOC)
        self.assertEqual(calls+2, self.engine.ai.calls)
        self.assertEqual(invoice(), self.engine.read(DOC)['parsed'])

    def test_reread_is_refused_after_any_create_attempt_or_while_running(self):
        with self.assertRaises(a.Hold):
            self.engine.reread(DOC)   # queued, not a hold
        for values in ({'create_attempted': True}, {'bill_id': '456'}, {'payload_sha256': 'x'}):
            with self.subTest(values=values):
                self.hold(**values)
                with self.assertRaises(a.Hold):
                    self.engine.reread(DOC)

    def test_reread_button_only_on_pre_create_holds(self):
        held = {'status': 'needs_review', 'message': 'held', 'parsed': invoice(), 'source_check': invoice()}
        self.assertIn('/api/reread/'+DOC, a.render_status(DOC, 's.pdf', held, csrf='tok'))
        self.assertNotIn('/api/reread/', a.render_status(DOC, 's.pdf', dict(held, bill_id='456'), csrf='tok'))

    def test_prompt_treats_reverse_charge_and_dash_unit_price_as_normal(self):
        self.assertIn('reverse charge', a.PROMPT); self.assertIn('dash', a.PROMPT)


if __name__ == '__main__':
    unittest.main()


class LimitReminders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); folder = Path(self.tmp.name)
        db = folder/'review.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE invoices(id TEXT,filename TEXT,sha256 TEXT,pdf BLOB)')
        self.engine = a.Engine(db, core_fixture(), FakeAI(invoice()), FakeZoho(folder))

    def tearDown(self):
        self.engine.close(); self.tmp.cleanup()

    def warnings(self, reserved, days_left):
        a.atomic(self.engine.folder/'ai-budget.json', {'reserved_usd': reserved, 'calls_reserved': 1})
        from unittest.mock import patch
        import os
        with patch.dict(os.environ, {'APP_AI_BUDGET_USD': '2.00', 'OWNER_INBOX_STOP_AT_EPOCH': str(1000+int(days_left*86400))}):
            return self.engine.limit_warnings(now=1000)

    def test_no_reminder_while_both_limits_are_comfortable(self):
        self.assertEqual([], self.warnings('0.50', 30))

    def test_reminders_ten_days_before_window_and_near_budget_end(self):
        result = self.warnings('1.90', 9)
        self.assertEqual(2, len(result))
        self.assertIn('AI budget nearly used', result[0]); self.assertIn('Service window ends', result[1])


def summary_text(igst='18.00', total='118.00', hsn='998319'):
    return '\n'.join([
        '                                              SYNTHETIC CLOUD SUITE PRIVATE LIMITED', 'Invoice',
        'Invoice number: SYNSUITE-001', '                                              GSTIN: 06ZZZZZ0000Z1Z5',
        'Bill to', 'SATTVA GLOBAL LOGISTICS', 'GSTIN: '+a.OWNER_GSTIN,
        'Details                                       HSN: '+hsn,
        'Invoice  date                             31 Oct 2026', '', 'Page 1 of 2',
        'Subscription                    Description    Interval        Quantity    Amount(₹)',
        'Synthetic Suite Standard        Usage          1 Oct - 31 Oct         1       100.00',
        '                          Subtotal in INR                          ₹100.00',
        '                          Integrated GST (18%)                      ₹'+igst,
        '                          Total in INR                             ₹'+total, ''])


def summary_invoice():
    reading = invoice()
    reading.update(vendor_name='SYNTHETIC CLOUD SUITE PRIVATE LIMITED', vendor_gstin='06ZZZZZ0000Z1Z5',
                   bill_number='SYNSUITE-001', bill_date='2026-10-31', tax_amount_cgst='0', tax_amount_sgst='0',
                   tax_amount_igst='18.00')
    reading['line_items'][0].update(description='Synthetic Suite Standard', hsn_or_sac='998319')
    return reading


class SubscriptionSummaryLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def test_total_in_inr_integrated_gst_and_invoice_level_hsn_are_corroborated(self):
        self.assertTrue(a.corroborate_source(summary_invoice(), summary_text(), self.core))

    def test_wrong_printed_tax_total_or_code_still_holds(self):
        for text in (summary_text(igst='18.50'), summary_text(total='119.00'), summary_text(hsn='998314')):
            with self.subTest(text=text[-80:]), self.assertRaises(a.Hold):
                a.corroborate_source(summary_invoice(), text, self.core)

    def test_two_different_invoice_level_codes_do_not_cover_a_line(self):
        text = summary_text().replace('Invoice  date', 'SAC: 996713\nInvoice  date')
        with self.assertRaises(a.Hold):
            a.corroborate_source(summary_invoice(), text, self.core)


class ReadingFlagsShown(unittest.TestCase):
    def test_status_page_shows_what_a_reading_flagged(self):
        flagged = dict(invoice(), confidence='medium', issues=['Note says <GST> applies but none charged'])
        page = a.render_status(DOC, 's.pdf', {'status': 'needs_review', 'message': 'held', 'parsed': invoice(), 'source_check': flagged})
        self.assertIn('Second reading flagged', page); self.assertIn('&lt;GST&gt; applies', page)
        self.assertNotIn('First reading flagged', page)


def untaxed_overseas_text(extra=''):
    return '\n'.join([
        '                                                   INVOICE', '                                                   Invoice # SYNMAIL-0001',
        '                                                   Invoice Date Nov 09, 2026', 'Synthetic Mail SAS',
        '1 rue Exemple', 'France', 'GSTIN : 9923FRA12345OS5',
        'BILLED TO                                          SUBSCRIPTION', 'Sattva Global Logistics',
        'India', 'GSTIN : '+a.OWNER_GSTIN,
        'DESCRIPTION                                   UNITS        UNIT PRICE   AMOUNT (INR)',
        'Starter plan monthly                              1           ₹25.00         ₹25.00',
        'Starter recurring emails                      40000                -         ₹75.00',
        '                                                             Total       ₹100.00',
        '                                                Amount Due (INR) ₹100.00', extra, ''])


def untaxed_overseas_invoice():
    reading = invoice()
    line = lambda d, r: {'description': d, 'quantity': 1, 'rate': r, 'hsn_or_sac': '', 'tax_percentage': '0',
                         'cgst': None, 'sgst': None, 'igst': None}
    reading.update(vendor_name='Synthetic Mail SAS', vendor_gstin='9923FRA12345OS5', bill_number='SYNMAIL-0001',
                   bill_date='2026-11-09', is_overseas=True, subtotal='100.00', tax_amount_cgst='0',
                   tax_amount_sgst='0', tax_amount_igst='0', invoice_total='100.00',
                   line_items=[line('Starter plan monthly', '25.00'), line('Starter recurring emails', '75.00')])
    return reading


class UntaxedOverseasInrLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def test_inr_overseas_invoice_without_subtotal_or_rates_is_corroborated(self):
        self.assertTrue(a.corroborate_source(untaxed_overseas_invoice(), untaxed_overseas_text(), self.core))

    def test_a_printed_gst_rate_contradicting_zero_still_holds(self):
        with self.assertRaises(a.Hold):
            a.corroborate_source(untaxed_overseas_invoice(), untaxed_overseas_text('IGST @ 18%   ₹0.00'), self.core)

    def test_missing_subtotal_never_excuses_a_taxed_invoice(self):
        reading = untaxed_overseas_invoice(); reading.update(subtotal='100.00', tax_amount_igst='18.00', invoice_total='118.00')
        with self.assertRaises(a.Hold):
            a.corroborate_source(reading, untaxed_overseas_text(), self.core)


class UnitsAndOverseasNormalisation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def readings(self, quantity=40000, overseas=False, rate='75.00'):
        reading = untaxed_overseas_invoice()
        reading['is_overseas'] = overseas
        reading['line_items'][1].update(quantity=quantity, rate=rate)
        return reading, copy.deepcopy(reading)

    def test_units_in_quantity_and_missed_overseas_flag_are_normalised_after_printed_proof(self):
        parsed, evidence = a.reconcile_readings(*self.readings(), untaxed_overseas_text(), self.core)
        self.assertEqual([1, 1], [x['quantity'] for x in parsed['line_items']])
        self.assertIs(True, parsed['is_overseas'])
        self.assertEqual({'quantity', 'is_overseas'}, {c['field'] for c in evidence['corrections']})

    def test_unit_price_read_as_line_amount_still_holds(self):
        with self.assertRaises(a.Hold):
            a.reconcile_readings(*self.readings(rate='0.01'), untaxed_overseas_text(), self.core)
