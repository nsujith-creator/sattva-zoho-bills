"""Actual PDF text extraction on a generated synthetic PDF; no provider calls."""
import shutil
import unittest
from unittest.mock import patch
import automation as a
from test_automation import core_fixture, invoice, source_fixture


def text_pdf(text):
    rows = ['BT /F1 9 Tf 35 760 Td']
    for line in text.splitlines():
        value = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        rows.extend(['('+value+') Tj', '0 -14 Td'])
    rows.append('ET')
    stream = '\n'.join(rows).encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    pdf = b'%PDF-1.4\n'; offsets = [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf += str(i).encode()+b' 0 obj\n'+value+b'\nendobj\n'
    xref = len(pdf)
    pdf += b'xref\n0 6\n0000000000 65535 f \n'
    pdf += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    return pdf+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'


class SourceText(unittest.TestCase):
    def test_unrelated_branch_registration_cannot_replace_supplier_registration(self):
        text=source_fixture().replace('Supplier GSTIN 27ABCDE1234F1Z5',
             'Supplier GSTIN 27ZZZZZ9999Z1Z9\nBranch office GSTIN 27ABCDE1234F1Z5')
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())
        text=source_fixture().replace('Supplier GSTIN 27ABCDE1234F1Z5',
             'Supplier: SYNTHETIC FREIGHT VENDOR\nBranch office GSTIN 27ABCDE1234F1Z5')
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())

    def test_printed_discount_cannot_be_hidden_by_two_zero_readings(self):
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),source_fixture()+'Discount (INR): 10.00\n',core_fixture())
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),source_fixture()+'Discount\n10.00\n',core_fixture())

    def test_printed_sac_cannot_be_discarded(self):
        parsed=invoice();parsed['line_items'][0]['hsn_or_sac']=''
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,source_fixture(),core_fixture())
        text=source_fixture().replace('Documentation    100.00    18%\nSAC 996713',
             'Description   SAC/HSN   Amount  GST\nDocumentation   996713   100.00  18%')
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,text,core_fixture())
        parsed['line_items'][0]['hsn_or_sac']='996713'
        a.corroborate_source(parsed,text,core_fixture())

    def test_printed_charge_taxes_require_extracted_amounts(self):
        text=source_fixture().replace('Documentation    100.00    18%\nSAC 996713',
             'Documentation    CGST 9%    9.00    100.00\nSAC 996713  SGST 9%  9.00')
        parsed=invoice()
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,text,core_fixture())
        parsed['line_items'][0].update(cgst='9.00',sgst='9.00')
        a.corroborate_source(parsed,text,core_fixture())
        inline=source_fixture().replace('Documentation    100.00    18%\nSAC 996713',
             'Documentation CGST 9% 9.00 SGST 9% 9.00 100.00\nSAC 996713')
        parsed['line_items'][0]['sgst']=None
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,inline,core_fixture())
        parsed['line_items'][0]['sgst']='9.00'
        a.corroborate_source(parsed,inline,core_fixture())

    def test_distinct_labelled_invoices_never_share_one_result(self):
        text=source_fixture()+source_fixture().replace('Invoice SYNTHETIC-001','Invoice ACTUAL-999')
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())

    def test_purchase_order_cannot_replace_the_labelled_invoice_number(self):
        text=source_fixture().replace('Invoice SYNTHETIC-001','Invoice ACTUAL-999\nPurchase Order SYNTHETIC-001')
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())

    def test_shipper_registration_cannot_replace_the_labelled_buyer(self):
        text=source_fixture().replace('Buyer GSTIN '+a.OWNER_GSTIN,'Buyer GSTIN 27ZZZZZ9999Z1Z9\nShipper GSTIN '+a.OWNER_GSTIN)
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())

    def test_reference_currency_cannot_replace_invoice_currency(self):
        text=source_fixture().replace('Currency INR','Invoice currency USD\nReference currency INR')
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())
        text=source_fixture().replace('Currency INR','Currency INR                         Invoice currency USD')
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())

    def test_staggered_shipper_column_cannot_supply_missing_buyer_registration(self):
        text=source_fixture().replace('Buyer GSTIN '+a.OWNER_GSTIN,
             'Buyer: SOME OTHER PARTY\n                              Shipper: SATTVA\n'
             '                              GSTIN: '+a.OWNER_GSTIN)
        with self.assertRaises(a.Hold):a.corroborate_source(invoice(),text,core_fixture())

    def test_each_charge_consumes_a_distinct_printed_row(self):
        parsed=invoice();parsed['line_items']*=2
        parsed.update(subtotal='200.00',tax_amount_cgst='18.00',tax_amount_sgst='18.00',invoice_total='236.00')
        text=source_fixture().replace('SAC 996713\nSubtotal','SAC 996713\nFreight    100.00    18%\nSAC 996713\nSubtotal')
        text=text.replace('Subtotal 100.00','Subtotal 200.00').replace('CGST 9.00','CGST 18.00').replace('SGST 9.00','SGST 18.00').replace('Grand Total 118.00','Grand Total 236.00')
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,text,core_fixture())
        # Actual repeated charges remain admissible when there are two source rows.
        a.corroborate_source(parsed,text.replace('Freight    100.00','Documentation    100.00'),core_fixture())

    def test_due_date_requires_its_own_printed_deadline(self):
        parsed=invoice();parsed['due_date']='2030-01-01'
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,source_fixture()+'Due Date 2026-11-28\n',core_fixture())
        parsed['due_date']='2026-11-28'
        a.corroborate_source(parsed,source_fixture()+'Due Date 2026-11-28\n',core_fixture())
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,source_fixture(),core_fixture())

    def test_customer_tax_id_and_two_column_invoice_to_preserve_buyer_role(self):
        for block in ('Customer\nSYNTHETIC BUYER\nTax id.no. '+a.OWNER_GSTIN,
                      'Payable to: SYNTHETIC SUPPLIER         Invoice To: SYNTHETIC BUYER\n'
                      +' '*len('Payable to: SYNTHETIC SUPPLIER         ')+'GSTIN: '+a.OWNER_GSTIN):
            a.corroborate_source(invoice(),source_fixture().replace('Buyer GSTIN '+a.OWNER_GSTIN,block),core_fixture())

    def test_split_gst_rows_with_container_line_between(self):
        text = source_fixture().replace('Documentation    100.00    18%\nSAC 996713',
            'Documentation    CGST 9%    9.00    100.00\nContainer SYNTHETIC\nSAC 996713  SGST 9%  9.00')
        parsed=invoice();parsed['line_items'][0].update(cgst='9.00',sgst='9.00')
        a.corroborate_source(parsed, text, core_fixture())

    def test_cma_abbreviated_tax_rows_are_bound_by_sac_and_taxable_base(self):
        parsed = invoice();parsed.update(vendor_name='CMA CGM TEST VENDOR',bill_number='INTEST000001',mbl_number='AMC111111')
        text = source_fixture().replace('SYNTHETIC FREIGHT VENDOR','CMA CGM TEST VENDOR').replace('Invoice SYNTHETIC-001','Bill of Lading: AMC111111   INTEST000001')
        text = text.replace('Documentation    100.00    18%\nSAC 996713',
            'Documentation    100.00\nTax summary\nCGST @ 9% DOC 996713 GH 100.00 9.00\nSGST @ 9% DOC 996713 GH 100.00 9.00')
        parsed['line_items'][0].update(cgst='9.00',sgst='9.00')
        a.corroborate_source(parsed,text,core_fixture())
        parsed['line_items'][0]['hsn_or_sac']='999999'
        with self.assertRaises(a.Hold):a.corroborate_source(parsed,text,core_fixture())

    @unittest.skipUnless(shutil.which('pdftotext') and shutil.which('pdfinfo'), 'Approved system reader not installed')
    def test_real_text_extraction_and_corroboration(self):
        text = a.source_text(text_pdf(source_fixture()))
        evidence = a.corroborate_source(invoice(), text, core_fixture())
        self.assertEqual('pdftotext+printed-fields', evidence['method'])
        self.assertEqual(64, len(evidence['text_sha256']))
        wrong = invoice();wrong['bill_number']='SYNTHETIC-00I'
        with self.assertRaises(a.Hold):a.corroborate_source(wrong,text,core_fixture())

    def test_reader_missing_and_corrupt_pdf_never_return_invented_text(self):
        with patch('subprocess.run',side_effect=FileNotFoundError):
            with self.assertRaises(a.Hold):a.source_text(b'%PDF-1.4 bad')


if __name__ == '__main__':unittest.main()
