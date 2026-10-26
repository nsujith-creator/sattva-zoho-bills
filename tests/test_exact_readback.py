"""A real read-back must never accept small differences from printed totals."""
import contextlib
import io
import unittest
from unittest.mock import patch
from test_approval_boundary import readback
from test_vendor_matching import GSTIN, MAERSK_ID, approvable_invoice, zb


class ExactReadbackTests(unittest.TestCase):
    def check_bill(self,bill):
        parsed=approvable_invoice()
        payload=zb.build_payload(parsed,MAERSK_ID,zb.determine_tax_route(parsed,'MAERSK A/S',GSTIN))
        with patch.object(zb,'zoho_get',return_value={'bill':bill}),contextlib.redirect_stdout(io.StringIO()):
            return zb.verify_created_bill('900000000001',payload,parsed)

    def test_matching_six_line_invoice_passes(self):
        result=self.check_bill(readback(approvable_invoice()))
        self.assertTrue(result['ok'],result['errors'])

    def test_two_paisa_tax_shortfall_on_six_lines_fails(self):
        bill=readback(approvable_invoice())
        bill.update(tax_total=round(bill['tax_total']-.02,2),total=round(bill['total']-.02,2))
        result=self.check_bill(bill)
        self.assertFalse(result['ok'])
        for name in ('total','tax_total'):
            self.assertFalse(result['checks'][name]['ok'])
            self.assertEqual(0,result['checks'][name]['tolerance'])

    def test_one_paisa_payable_difference_fails_even_with_matching_tax(self):
        bill=readback(approvable_invoice())
        bill['total']=round(bill['total']+.01,2)
        result=self.check_bill(bill)
        self.assertFalse(result['ok'])
        self.assertTrue(result['checks']['tax_total']['ok'])
        self.assertFalse(result['checks']['total']['ok'])

    def test_offsetting_one_paisa_head_differences_fail_with_same_total(self):
        bill=readback(approvable_invoice())
        bill['taxes'][0]['tax_amount']+=.01
        bill['taxes'][1]['tax_amount']-=.01
        result=self.check_bill(bill)
        self.assertFalse(result['ok'])
        self.assertTrue(result['checks']['tax_total']['ok'])
        self.assertTrue(result['checks']['total']['ok'])
        self.assertFalse(result['checks']['tax_amount_cgst']['ok'])
        self.assertFalse(result['checks']['tax_amount_sgst']['ok'])

    def test_wrong_tax_head_fails_with_same_total(self):
        bill=readback(approvable_invoice())
        bill['taxes']=[{'tax_name':'IGST18 (18%)','tax_amount':bill['tax_total']}]
        result=self.check_bill(bill)
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks']['tax_amount_igst']['ok'])

    def test_missing_tax_details_fail(self):
        bill=readback(approvable_invoice());del bill['taxes']
        self.assertFalse(self.check_bill(bill)['ok'])

    def test_multiple_rates_sum_within_each_printed_head(self):
        bill=readback(approvable_invoice())
        bill['taxes']=[{'tax_name':head+rate,'tax_amount':amount}
                       for head in ('CGST','SGST')
                       for rate,amount in (('2.5 (2.5%)',7951.30),('9 (9%)',2719.39))]
        self.assertTrue(self.check_bill(bill)['ok'])

    def test_unknown_or_nonfinite_tax_details_fail(self):
        for taxes in ([{'tax_name':'Unspecified','tax_amount':21341.38}],
                      [{'tax_name':'CGST9','tax_amount':'NaN'}]):
            bill=readback(approvable_invoice());bill['taxes']=taxes
            self.assertFalse(self.check_bill(bill)['ok'])
