"""Zoho group-level GST rounding against per-line printed GST. Synthetic data only."""
import copy
import unittest
from decimal import Decimal as D
import automation as a
from test_automation import GSTIN, core_fixture, invoice

VENDORS = {'SYNTHETIC FREIGHT VENDOR': {'id': '123', 'gst_no': GSTIN}}


def two_line_invoice():
    # Printed per line: 100.05 x 9% = 9.0045 -> 9.00 per head, twice = 18.00.
    # Zoho on the group: 200.10 x 9% = 18.009 -> 18.01 per head.
    parsed = invoice()
    line = dict(parsed['line_items'][0], rate='100.05', cgst='9.00', sgst='9.00', igst=None)
    parsed['line_items'] = [dict(line, description='Documentation'), dict(line, description='Handling')]
    parsed.update(subtotal='200.10', tax_amount_cgst='18.00', tax_amount_sgst='18.00', invoice_total='236.10')
    return parsed


def readback(payload, head, lines, sub='200.10'):
    detail = {**copy.deepcopy(payload), 'bill_id': '456', 'gst_no': GSTIN, 'documents': [],
              'sub_total': sub, 'tax_total': str(D(head)*2), 'total': str(D(sub)+D(head)*2),
              'taxes': [{'tax_name': 'CGST9', 'tax_amount': head}, {'tax_name': 'SGST9', 'tax_amount': head}]}
    for item, value in zip(detail['line_items'], lines):
        item['line_item_taxes'] = [{'tax_name': 'CGST9', 'tax_amount': value}, {'tax_name': 'SGST9', 'tax_amount': value}]
    return detail


class ZohoGroupRounding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()

    def setUp(self):
        self.parsed = two_line_invoice()
        self.payload = a.validate(copy.deepcopy(self.parsed), self.core, VENDORS)

    def verify(self, *args, **kwargs):
        return a.verify_detail(self.core, '456', self.payload, self.parsed, readback(self.payload, *args, **kwargs))

    def test_exact_group_rounding_with_complete_line_evidence_is_accepted_and_recorded(self):
        check = self.verify('18.01', ['9.00', '9.01'])
        self.assertTrue(check['ok'], check['errors'])
        policy = check['zoho_rounding_policy']
        self.assertEqual(('18.00', '18.01'), (policy['printed_head'], policy['zoho_group_head']))
        self.assertEqual('0.02', policy['accepted_differences']['total'])
        self.assertEqual([[1, 'cgst', '9.00', '9.01'], [1, 'sgst', '9.00', '9.01']], policy['line_tax_differences'])

    def test_printed_line_taxes_are_required_evidence(self):
        for line in self.parsed['line_items']:
            line['cgst'] = line['sgst'] = None
        check = self.verify('18.01', ['9.00', '9.01'])
        self.assertFalse(check['ok'])
        self.assertNotIn('zoho_rounding_policy', check)

    def test_tax_on_an_uncharged_head_is_never_accepted(self):
        detail = readback(self.payload, '18.01', ['9.00', '9.01'])
        detail['line_items'][0]['line_item_taxes'].append({'tax_name': 'IGST18', 'tax_amount': '0.01'})
        check = a.verify_detail(self.core, '456', self.payload, self.parsed, detail)
        self.assertFalse(check['ok'])
        self.assertIn('line_igst_mismatch', check['errors'])
        self.assertNotIn('zoho_rounding_policy', check)

    def test_complete_line_taxes_from_zoho_are_required(self):
        detail = readback(self.payload, '18.01', ['9.00', '9.01'])
        del detail['line_items'][0]['line_item_taxes']
        check = a.verify_detail(self.core, '456', self.payload, self.parsed, detail)
        self.assertFalse(check['ok'])

    def test_printed_figures_still_verify_without_the_policy(self):
        check = self.verify('18.00', ['9.00', '9.00'])
        self.assertTrue(check['ok'], check['errors'])
        self.assertNotIn('zoho_rounding_policy', check)

    def test_anything_beyond_proven_group_rounding_holds(self):
        for name, args, kwargs in (('head beyond group rounding', ('18.02', ['9.01', '9.01']), {}),
                                   ('lines not summing to the group', ('18.01', ['9.00', '9.00']), {}),
                                   ('line two paise out', ('18.01', ['8.99', '9.02']), {}),
                                   ('subtotal differs', ('18.01', ['9.00', '9.01']), {'sub': '200.11'})):
            with self.subTest(name):
                self.assertFalse(self.verify(*args, **kwargs)['ok'])

    def test_other_errors_keep_the_rounding_errors_too(self):
        detail = readback(self.payload, '18.01', ['9.00', '9.01'])
        detail['line_items'][0]['description'] = 'Changed'
        check = a.verify_detail(self.core, '456', self.payload, self.parsed, detail)
        self.assertFalse(check['ok'])
        self.assertTrue(any(e.startswith('total_mismatch') for e in check['errors']))

    def test_duplicates_keep_their_own_rules(self):
        check = a.verify_detail(self.core, '456', self.payload, self.parsed,
                                readback(self.payload, '18.01', ['9.00', '9.01']), owned=False)
        self.assertNotIn('zoho_rounding_policy', check)


class ReadbackRetry(unittest.TestCase):
    def job(self, **changes):
        value = {'status': 'needs_review', 'bill_id': '456', 'parsed': {}, 'source_check': {},
                 'message': 'Zoho read-back differs: total_mismatch'}
        value.update(changes)
        return value

    def test_only_a_created_bill_held_by_its_read_back_can_retry(self):
        self.assertTrue(a.readback_recheck_allowed(self.job()))
        for changes in ({'bill_id': ''}, {'status': 'verified'}, {'message': 'Same invoice number, different record'},
                        {'parsed': None}):
            with self.subTest(changes):
                self.assertFalse(a.readback_recheck_allowed(self.job(**changes)))


if __name__ == '__main__':
    unittest.main()
