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
    def test_split_gst_rows_with_container_line_between(self):
        text = source_fixture().replace('Documentation    100.00    18%\nSAC 996713',
            'Documentation    CGST 9%    9.00    100.00\nContainer SYNTHETIC\nSAC 996713  SGST 9%  9.00')
        a.corroborate_source(invoice(), text, core_fixture())

    def test_cma_abbreviated_tax_rows_are_bound_by_sac_and_taxable_base(self):
        parsed = invoice();parsed.update(vendor_name='CMA CGM TEST VENDOR',bill_number='INTEST000001',mbl_number='AMC111111')
        text = source_fixture().replace('SYNTHETIC FREIGHT VENDOR','CMA CGM TEST VENDOR').replace('Invoice SYNTHETIC-001','Bill of Lading: AMC111111   INTEST000001')
        text = text.replace('Documentation    100.00    18%\nSAC 996713',
            'Documentation    100.00\nTax summary\nCGST @ 9% DOC 996713 GH 100.00 9.00\nSGST @ 9% DOC 996713 GH 100.00 9.00')
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
