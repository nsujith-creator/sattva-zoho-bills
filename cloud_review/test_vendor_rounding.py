"""Owner's Total Transport rounding allowance (30 Oct 2026). Synthetic values only."""
import copy
import unittest
from decimal import Decimal
import automation as a
from test_automation import invoice, core_fixture

TOTAL_GSTIN = '27AAACT3276C2ZI'
OTHER_GSTIN = '27ABCDE1234F1Z5'


class TotalTransportRounding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def parsed(self, gstin):
        # Two 9% lines whose printed per-line taxes sum one paisa below Zoho's group rounding.
        value = invoice()
        value.update(vendor_name='SYNTHETIC FREIGHT VENDOR', vendor_gstin=gstin)
        value['line_items'] = [dict(value['line_items'][0], description='Charge '+str(i), rate='100.05')
                               for i in (1, 2)]
        value.update(subtotal='200.10', tax_amount_cgst='18.00', tax_amount_sgst='18.00', invoice_total='236.10')
        return value

    def zoho_bill(self, parsed, payload, head='18.01', line=('9.01', '9.00'), subtotal=None, total=None):
        bill = dict(copy.deepcopy(payload), bill_id='9', currency_code='INR', gst_no=parsed['vendor_gstin'],
                    sub_total=subtotal or parsed['subtotal'],
                    tax_total=str(Decimal(head)*2), total=total or str(Decimal(parsed['subtotal'])+Decimal(head)*2),
                    taxes=[{'tax_name': 'CGST', 'tax_amount': head}, {'tax_name': 'SGST', 'tax_amount': head}])
        for item, tax in zip(bill['line_items'], line):
            item['line_item_taxes'] = [{'tax_name': 'CGST', 'tax_amount': tax}, {'tax_name': 'SGST', 'tax_amount': tax}]
        return bill

    def check(self, gstin, **bill):
        parsed = self.parsed(gstin)
        vendors = {'SYNTHETIC FREIGHT VENDOR': {'id': '123', 'gst_no': gstin}}
        payload = a.validate(copy.deepcopy(parsed), self.core, vendors)
        return a.verify_detail(self.core, '9', payload, parsed, self.zoho_bill(parsed, payload, **bill))

    def test_total_zoho_rounding_within_one_rupee_is_verified_and_recorded(self):
        result = self.check(TOTAL_GSTIN)
        self.assertTrue(result['ok'], result['errors'])
        policy = result['vendor_rounding_policy']
        self.assertEqual(policy['accepted_differences']['total'], '0.02')
        self.assertEqual(len(policy['line_tax_differences']), 2)

    def test_exact_total_bill_needs_no_allowance(self):
        result = self.check(TOTAL_GSTIN, head='18.00', line=('9.00', '9.00'))
        self.assertTrue(result['ok'], result['errors'])
        self.assertNotIn('vendor_rounding_policy', result)

    def test_more_than_one_rupee_still_holds(self):
        result = self.check(TOTAL_GSTIN, head='18.51', line=('9.51', '9.00'))
        self.assertFalse(result['ok'])
        self.assertNotIn('vendor_rounding_policy', result)

    def test_other_suppliers_keep_exact_readback(self):
        result = self.check(OTHER_GSTIN)
        self.assertFalse(result['ok'])
        self.assertNotIn('vendor_rounding_policy', result)

    def test_taxable_subtotal_is_never_relaxed(self):
        result = self.check(TOTAL_GSTIN, subtotal='200.11', total='236.13')
        self.assertFalse(result['ok'])
        self.assertTrue(any(e.startswith('sub_total_mismatch') for e in result['errors']))


if __name__ == '__main__':
    unittest.main()
