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


if __name__ == '__main__':
    unittest.main()
