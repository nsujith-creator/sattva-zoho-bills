"""Synthetic column-layout evidence only; no business PDFs or provider calls."""
import copy
import unittest
import automation as a
from test_automation import core_fixture, invoice


def fixture():
    parsed = invoice()
    parsed.update(vendor_name='Total Transport Systems Limited', bill_number='SYNTHETIC-001',
                  bill_date='2026-10-28', due_date='2026-12-07', mbl_number='TESTBL123',
                  subtotal='1200.00', tax_amount_cgst='34.00', tax_amount_sgst='34.00',
                  invoice_total='1268.00')
    parsed['line_items'] = [
        dict(description='Synthetic freight', quantity=1, rate='1000.00', hsn_or_sac='996521',
             tax_percentage='5', cgst='25.00', sgst='25.00', igst=None),
        dict(description='Synthetic documentation', quantity=1, rate='200.00', hsn_or_sac='996759',
             tax_percentage='9', cgst='9.00', sgst='9.00', igst=None)]
    # This second synthetic line deliberately uses 9% TOTAL GST (4.5% per head).
    def row(values):
        positions = [0, 38, 48, 57, 66, 82, 90, 103, 112, 125, 135, 148]
        result = ''
        for start, value in zip(positions, values):
            result += ' ' * (start-len(result)) + value
        return result
    panel = ('Total Transport Systems Limited\nGST Code : '+parsed['vendor_gstin']+
             '\nBILLED TO                          Invoice No. : SYNTHETIC-001\n'
             'SYNTHETIC BUYER                     Invoice Date\n'
             '                                    : 28/Oct/2026\n'
             '                                   GST No. : '+a.OWNER_GSTIN+'\n'
             'Shipper : SYNTHETIC SHIPPER          MBL / MAWB : TESTBL123 / 27/Oct/2026\n'
             '                                   Invoice Due Date : 07/Dec/2026\n')
    header = ('             Particulars             HSN/SAC    Curr     Qty         Rate          ROE     Taxable     CGST       CGST       SGST       SGST          Total\n'
              '                                                                                            Value       %          Amt        %          Amt          (INR)\n\n')
    text = panel+header
    # The taxable amount wraps across two physical rows in one printed cell.
    text += row(['', '', '', '', '', '', '1,000.'])+'\n'
    text += row(['Synthetic freight','996521','USD','1.000','10.00','100.00','00','2.50','25.00','2.50','25.00','1050.00'])+'\n\n'
    text += row(['Synthetic documentation','996759','INR','1.000','200.00','1.00','200.00','4.50','9.00','4.50','9.00','218.00'])+'\n\n'
    text += row(['Total','','','','','','1,200.00','','34.00','','34.00','1268.00'])+'\n\nAmount In Words : SYNTHETIC\n'
    return parsed, text, panel


class TotalTransport(unittest.TestCase):
    def test_wrapped_mixed_rate_table_and_repeated_identity_page(self):
        parsed, text, panel = fixture()
        for source in (text, text+'\f'+panel+'Bank Details : SYNTHETIC\n'):
            before = copy.deepcopy(parsed)
            proof = a.corroborate_source(parsed, source, core_fixture())
            self.assertEqual(proof['method'], 'pdftotext+total-transport-columns')
            self.assertEqual(parsed, before)
        parsed['mbl_number'] = ''  # Preserve the existing optional-reference policy.
        a.corroborate_source(parsed, text, core_fixture())

    def test_wrong_or_missing_fields_never_corroborate(self):
        for key, value in [('buyer_gstin','27ZZZZZ9999Z1Z9'), ('vendor_gstin',a.OWNER_GSTIN),
                           ('bill_number','SYNTHETIC-002'), ('bill_date','2026-10-27'),
                           ('due_date',None), ('due_date','2026-12-08'), ('currency_code','USD'),
                           ('subtotal','1201.00'), ('invoice_total','1269.00'),
                           ('tax_amount_cgst','35.00'), ('mbl_number','OTHERBL'),
                           ('vendor_name','OTHER SUPPLIER')]:
            with self.subTest(key=key,value=value):
                parsed, text, _ = fixture(); parsed[key] = value
                with self.assertRaises(a.Hold): a.corroborate_source(parsed,text,core_fixture())

    def test_missing_extra_wrong_reordered_and_duplicate_charges_hold(self):
        for mutate in (lambda p:p['line_items'].pop(),
                       lambda p:p['line_items'].append(copy.deepcopy(p['line_items'][0])),
                       lambda p:p['line_items'].reverse(),
                       lambda p:p['line_items'][0].update(rate='10.00'),
                       lambda p:p['line_items'][0].update(cgst=None),
                       lambda p:p['line_items'][0].update(sgst='24.99'),
                       lambda p:p['line_items'][0].update(hsn_or_sac='996759'),
                       lambda p:p['line_items'][0].update(description='Wrong charge')):
            parsed, text, _ = fixture(); mutate(parsed)
            with self.assertRaises(a.Hold): a.corroborate_source(parsed,text,core_fixture())

    def test_conflicting_second_page_or_credit_and_corrupt_columns_hold(self):
        parsed, text, panel = fixture()
        variants = [text+'\f'+panel.replace('SYNTHETIC-001','SYNTHETIC-002'),
                    text+'\f'+panel.replace('07/Dec/2026','08/Dec/2026'),
                    text+'\nCredit Note\n', text+'\nDiscount 10.00\n',
                    text+'\nInvoice currency: USD\n',
                    text+'\nInvoice OTHER-999\n',
                    text.replace('25.00','25.00CR',1), text.replace('25.00','-25.00',1),
                    text.replace('1268.00','1269.00'), text.replace('(INR)','(USD)'),
                    text.replace('HSN/SAC','HSN/???'), text.replace('Amount In Words','Unknown Footer')]
        for source in variants:
            with self.subTest(source=source[-60:]):
                with self.assertRaises(a.Hold): a.corroborate_source(parsed,source,core_fixture())

    def test_redundant_sac_summary_must_reconcile_to_every_charge(self):
        parsed, text, _ = fixture()
        summary = ('\nHSN/SAC   Taxable Value   CGST %   CGST Amt   SGST %   SGST Amt   Total Amt With Tax (INR)\n'
                   '996521    1000.00        2.50     25.00      2.50     25.00      1050.00\n'
                   '996759    200.00         4.50     9.00       4.50     9.00       218.00\n'
                   'This is a computer generated document.\n')
        a.corroborate_source(parsed,text+summary,core_fixture())
        for changed in (summary.replace('1000.00','1001.00'), summary.replace('4.50','9.00'),
                        summary.replace('9.00','8.99'), summary.replace('996759','996521'),
                        summary.replace('996521    1000.00        2.50     25.00      2.50     25.00      1050.00\n','')):
            with self.assertRaises(a.Hold): a.corroborate_source(parsed,text+changed,core_fixture())


    def test_wrapped_cgs_sgs_headers_and_split_amounts_corroborate(self):
        parsed, text, _ = fixture()
        wrapped = (text
                   .replace('CGST       CGST       SGST       SGST', 'CGS        CGS        SGS        SGS ')
                   .replace('Value       %          Amt        %          Amt', 'Value      T%          Amt       T%          Amt')
                   .replace('Invoice Due Date : 07/Dec/2026', 'Invoice Due Date\n                                   : 07/Dec/2026'))
        proof = a.corroborate_source(parsed, wrapped, core_fixture())
        self.assertEqual(proof['method'], 'pdftotext+total-transport-columns')


if __name__ == '__main__':
    unittest.main()
