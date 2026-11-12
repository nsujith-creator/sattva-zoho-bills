"""Synthetic OOCL India layout (poppler -layout) only; no business PDFs or provider calls."""
import copy
import unittest
import automation as a
from test_automation import GSTIN, core_fixture, invoice


def row(*cells):
    """Place each (column, text) pair at its printed column."""
    out = ''
    for column, value in cells:
        out += ' '*max(1, column-len(out)) + value if out else ' '*column + value
    return out


def header(page):
    return '\n'.join([
        row((62, 'Ack.No:'), (77, '000000000000001')),
        row((1, 'ORIGINAL FOR'), (64, 'IRN:'), (77, 'SYNTHETICIRN')),
        row((116, 'PAGE NO.'), (143, ':'+str(page))),
        row((43, 'Orient Overseas Container Line Limited'), (116, 'INVOICE NO.'), (143, ': SYN 000001A')),
        row((65, GSTIN)),
        row((116, 'ISSUE DATE'), (143, ': 12 Nov 2026')),
        row((66, 'TAX INVOICE'), (116, 'DUE DATE'), (143, ': 12 Nov 2026')),
        ''])


CHARGES = [('SYNTHETIC HANDLING', '1.000', '1000.0000', 'INR', '1000.00', '1.00000', '1,000.00', '996711'),
           ('SYNTHETIC SEAL', '1.000', '5.0000', 'USD', '5.00', '90.00000', '450.00', '996799')]


def fixture():
    page1 = header(1) + '\n'.join([
        row((2, 'INVOICE TO'), (47, 'CUSTOMER ID'), (82, 'OFFICE OF ISSUE'), (120, 'BILL OF LADING NO.')),
        row((2, 'SYNTHETIC BUYER'), (47, '0000000001'), (82, 'Mumbai'), (120, 'SYNBL0001')),
        row((2, 'SYNTHETIC ADDRESS'), (47, a.OWNER_GSTIN)),
        row((2, 'PAN:SYNTHETIC'), (47, 'SHIP TO/BY'), (82, 'PORT OF DISCHARGE')),
        row((47, 'SYNTHETIC SHIPPER'), (82, 'Synthetic Port')),
        '',
        row((97, 'EXTENDED EXCHANGE'), (144, 'AMOUNT IN')),
        row((6, 'CHARGE DESCRIPTION'), (44, 'BASIS'), (64, 'RATE'), (84, 'CUR'), (129, 'GST%')),
        row((99, 'VALUE'), (108, 'RATE'), (147, 'INR')),
        *[row((2, d), (49, b), (64, r), (84, c), (98, e), (112, x), (133, 'YES'), (146, i)) for d, b, r, c, e, x, i, _ in CHARGES],
        row((3, 'SUB-TOTAL'), (146, '1,450.00')),
        row((3, 'CGST + SGST'), (147, '261.00')),
        '', '',
        row((2, 'AMOUNT DUE'), (120, 'INR 1,711.00')),
        row((2, 'This is a computer generated Invoice and hence no signature is required'))])
    page2 = header(2) + '\n'.join([
        row((62, 'GST Detail')),
        row((28, 'TAXABLE'), (50, 'CGST'), (75, 'SGST'), (100, 'UTGST'), (119, 'IGST'), (138, 'SAC')),
        row((4, 'CHARGE DESCRIPTION'), (27, 'VALUE IN INR')),
        row((43, 'RATE'), (52, 'AMOUNT'), (68, 'RATE'), (77, 'AMOUNT'), (93, 'RATE'), (102, 'AMOUNT'), (112, 'RATE'), (122, 'AMOUNT')),
        '',
        row((3, 'SYNTHETIC HANDLING'), (29, '1,000.00'), (43, '9%'), (57, '90.00'), (68, '9%'), (82, '90.00'), (138, '996711')),
        '',
        row((3, 'SYNTHETIC SEAL'), (31, '450.00'), (43, '9%'), (57, '40.50'), (68, '9%'), (82, '40.50'), (138, '996799')),
        '',
        row((3, 'SUB-TOTAL'), (55, '130.50'), (80, '130.50')),
        '',
        row((3, 'Place of Supply'), (28, 'Maharashtra'))])
    parsed = invoice()
    parsed.update(vendor_name='Orient Overseas Container Line Limited', bill_number='SYN 000001A',
                  bill_date='2026-11-12', due_date='2026-11-12', mbl_number='SYNBL0001',
                  subtotal='1450.00', tax_amount_cgst='130.50', tax_amount_sgst='130.50', invoice_total='1711.00')
    parsed['line_items'] = [
        dict(description='SYNTHETIC HANDLING', quantity=1, rate='1000.00', hsn_or_sac='996711',
             tax_percentage='18', cgst='90.00', sgst='90.00', igst=None),
        dict(description='SYNTHETIC SEAL', quantity=1, rate='450.00', hsn_or_sac='996799',
             tax_percentage='18', cgst='40.50', sgst='40.50', igst=None)]
    return parsed, page1+'\n\f'+page2+'\n'


class OOCL(unittest.TestCase):
    def assertHolds(self, parsed, text):
        with self.assertRaises(a.Hold):
            a.corroborate_source(parsed, text, core_fixture())

    def test_spaced_number_and_unlabelled_gstins_corroborate(self):
        parsed, text = fixture()
        before = copy.deepcopy(parsed)
        proof = a.corroborate_source(parsed, text, core_fixture())
        self.assertEqual(proof['method'], 'pdftotext+oocl-columns')
        self.assertEqual(parsed, before)  # Proof only; readings are never repaired.
        parsed['line_items'].reverse()  # Readings may list charges in GST Detail order.
        a.corroborate_source(parsed, text, core_fixture())

    def test_wrong_or_missing_fields_never_corroborate(self):
        for key, value in [('buyer_gstin', '27ZZZZZ9999Z1Z9'), ('vendor_gstin', a.OWNER_GSTIN),
                           ('bill_number', 'SYN 000002A'), ('bill_number', 'SYN'), ('bill_date', '2026-11-11'),
                           ('due_date', None), ('due_date', '2026-12-12'), ('currency_code', 'USD'),
                           ('subtotal', '1451.00'), ('invoice_total', '1711.01'), ('tax_amount_cgst', '130.51'),
                           ('tax_amount_igst', '261.00'), ('discounts_or_credits', '10.00'),
                           ('mbl_number', 'OTHERBL'), ('mbl_number', ''), ('vendor_name', 'OTHER SUPPLIER')]:
            with self.subTest(key=key, value=value):
                parsed, text = fixture(); parsed[key] = value
                self.assertHolds(parsed, text)

    def test_missing_extra_wrong_and_duplicate_charges_hold(self):
        for mutate in (lambda p: p['line_items'].pop(),
                       lambda p: p['line_items'].append(copy.deepcopy(p['line_items'][0])),
                       lambda p: p['line_items'][0].update(rate='1000.01'),
                       lambda p: p['line_items'][0].update(cgst=None),
                       lambda p: p['line_items'][0].update(sgst='89.99'),
                       lambda p: p['line_items'][0].update(igst='180.00'),
                       lambda p: p['line_items'][0].update(quantity=2),
                       lambda p: p['line_items'][0].update(tax_percentage='5'),
                       lambda p: p['line_items'][0].update(hsn_or_sac='996799'),
                       lambda p: p['line_items'][0].update(description='Wrong charge')):
            parsed, text = fixture(); mutate(parsed)
            self.assertHolds(parsed, text)

    def test_altered_conflicting_or_unexpected_printed_rows_hold(self):
        parsed, text = fixture()
        page2 = lambda t, old, new: t.split('\f')[0]+'\f'+t.split('\f')[1].replace(old, new, 1)
        for name, change in [('second page invoice number', lambda t: page2(t, ': SYN 000001A', ': SYN 000009A')),
                             ('GST Detail charge name', lambda t: page2(t, 'SYNTHETIC SEAL', 'OTHER SEAL')),
                             ('second page supplier GSTIN', lambda t: page2(t, GSTIN, '27ZZZZZ9999Z1Z9'))]:
            with self.subTest(name=name):
                changed = change(text)
                self.assertNotEqual(changed, text)
                self.assertHolds(copy.deepcopy(parsed), changed)
        for old, new in [('1.00000', '1.10000'),                                   # INR at a non-1 rate
                         ('5.0000', '6.0000'),                                     # basis x rate != value
                         (' 450.00\n', ' 451.00\n'),                                # value x exchange != INR
                         ('1,450.00', '1,450.01'),                                  # charges != sub-total
                         ('INR 1,711.00', 'INR 1,712.00'),                          # sub-total + GST != due
                         ('     40.50     ', '     40.51     '),                    # printed CGST != computed
                         (' 130.50 ', ' 130.51 '),                                  # GST Detail total
                         ('SYNTHETIC SHIPPER', '27ZZZZZ9999Z1Z9  '),               # extra party GSTIN
                         ('   CGST + SGST', '   DISCOUNT                    10.00\n   CGST + SGST'),
                         ('   CGST + SGST', '   IGST                       261.00\n   CGST + SGST'),
                         ('SHIP TO/BY', 'CONSIGNEE '),                              # buyer block never closes
                         ('GST Detail', 'GST Summary')]:
            with self.subTest(old=old[:40]):
                changed = text.replace(old, new, 1)
                self.assertNotEqual(changed, text)
                self.assertHolds(copy.deepcopy(parsed), changed)

    def test_five_percent_freight_in_foreign_currency(self):
        # The USD line becomes 5% ocean freight: 450.00 x 2.5% = 11.25 per head.
        parsed, text = fixture()
        gst_rows = {row((3, 'SYNTHETIC SEAL'), (31, '450.00'), (43, '9%'), (57, '40.50'), (68, '9%'), (82, '40.50'), (138, '996799')):
                    row((3, 'OCEAN FREIGHT'), (31, '450.00'), (43, '2.5%'), (57, '11.25'), (68, '2.5%'), (82, '11.25'), (138, '996521')),
                    row((3, 'SUB-TOTAL'), (55, '130.50'), (80, '130.50')): row((3, 'SUB-TOTAL'), (55, '101.25'), (80, '101.25')),
                    row((3, 'CGST + SGST'), (147, '261.00')): row((3, 'CGST + SGST'), (147, '202.50')),
                    'INR 1,711.00': 'INR 1,652.50', 'SYNTHETIC SEAL': 'OCEAN FREIGHT'}
        for old, new in gst_rows.items():
            self.assertIn(old, text)
            text = text.replace(old, new)
        parsed['line_items'][1].update(description='OCEAN FREIGHT', tax_percentage='5', cgst='11.25', sgst='11.25',
                                       hsn_or_sac='996521')
        parsed.update(tax_amount_cgst='101.25', tax_amount_sgst='101.25', invoice_total='1652.50')
        self.assertEqual(a.corroborate_source(parsed, text, core_fixture())['method'], 'pdftotext+oocl-columns')

    def test_other_suppliers_keep_the_generic_checks(self):
        self.assertFalse(a.is_oocl_layout('PLEASE PAY "ORIENT OVERSEAS CONTAINER LINE LIMITED"\nGST Detail\n'))
        self.assertFalse(a.is_oocl_layout('Orient Overseas Container Line Limited\nNo GST table\n'))


if __name__ == '__main__':
    unittest.main()
