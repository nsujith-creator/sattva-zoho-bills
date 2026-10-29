"""Synthetic rounding/source regressions. No invoices, credentials or provider calls."""
import copy
import unittest
from decimal import Decimal
from unittest.mock import patch
import automation as a
from test_automation import invoice, core_fixture, source_fixture, GSTIN


class RoundingRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = core_fixture()
        cls.vendors = {'SYNTHETIC FREIGHT VENDOR': {'id':'123','gst_no':GSTIN}}

    def parsed(self, bases=('100.00','100.00'), rates=('18','18'), cgst='18.00', sgst=None):
        value=invoice()
        value['line_items']=[dict(value['line_items'][0],description='Synthetic charge '+str(i),
                                 rate=base,tax_percentage=rate)
                             for i,(base,rate) in enumerate(zip(bases,rates))]
        subtotal=sum(Decimal(base) for base in bases)
        sgst=cgst if sgst is None else sgst
        value.update(subtotal=str(subtotal),tax_amount_cgst=cgst,tax_amount_sgst=sgst,
                     invoice_total=str(subtotal+Decimal(cgst)+Decimal(sgst)))
        return value

    def bill(self, parsed, payload, heads):
        result=dict(copy.deepcopy(payload),bill_id='999',currency_code='INR',gst_no=GSTIN,
                    sub_total=parsed['subtotal'],tax_total=str(Decimal(parsed['tax_amount_cgst'])+Decimal(parsed['tax_amount_sgst'])),
                    total=parsed['invoice_total'],taxes=[
                        {'tax_name':'CGST','tax_amount':parsed['tax_amount_cgst']},
                        {'tax_name':'SGST','tax_amount':parsed['tax_amount_sgst']}])
        for line,(cgst,sgst) in zip(result['line_items'],heads):
            line['line_item_taxes']=[{'tax_name':'CGST','tax_amount':cgst},{'tax_name':'SGST','tax_amount':sgst}]
        return result

    def verify(self, parsed, heads, owned=False):
        payload=a.validate(copy.deepcopy(parsed),self.core,self.vendors,check_zoho_rounding=False)
        bill=self.bill(parsed,payload,heads)
        return payload,bill,a.verify_detail(self.core,'999',payload,parsed,bill,owned=owned)

    def test_duplicate_same_head_rate_offsetting_pennies_pass_without_mutation(self):
        parsed=self.parsed()
        before=copy.deepcopy(parsed)
        payload,bill,result=self.verify(parsed,[('9.01','9.01'),('8.99','8.99')])
        self.assertTrue(result['ok'],result['errors'])
        self.assertIn('duplicate_allocation_policy',result)
        saved=copy.deepcopy(bill)
        result=a.verify_detail(self.core,'999',payload,parsed,bill,owned=False)
        self.assertTrue(result['ok'])
        self.assertEqual(before,parsed);self.assertEqual(saved,bill)

    def test_new_bill_never_inherits_the_duplicate_exception(self):
        _,_,result=self.verify(self.parsed(),[('9.01','9.01'),('8.99','8.99')],owned=True)
        self.assertFalse(result['ok'])
        self.assertIn('line_cgst_mismatch',result['errors'])

    def test_cross_head_offset_is_not_net_zero_within_a_head(self):
        _,_,result=self.verify(self.parsed(),[('9.01','8.99'),('9.00','9.00')])
        self.assertFalse(result['ok'])

    def test_cross_rate_offset_is_never_allowed(self):
        parsed=self.parsed(rates=('18','5'),cgst='11.50')
        _,_,result=self.verify(parsed,[('9.01','9.01'),('2.49','2.49')])
        self.assertFalse(result['ok'])

    def test_more_than_the_approved_one_penny_still_holds(self):
        _,_,result=self.verify(self.parsed(),[('9.02','9.02'),('8.98','8.98')])
        self.assertFalse(result['ok'])

    def test_missing_line_evidence_cannot_prove_an_offset(self):
        parsed=self.parsed()
        payload,bill,_=self.verify(parsed,[('9.01','9.01'),('8.99','8.99')])
        bill['line_items'][1].pop('line_item_taxes')
        self.assertFalse(a.verify_detail(self.core,'999',payload,parsed,bill,owned=False)['ok'])

    def test_empty_or_partial_head_arrays_cannot_prove_penny_offsets(self):
        parsed=self.parsed(bases=('0.06','0.06'),cgst='0.02')
        payload,bill,_=self.verify(parsed,[('0.00','0.00'),('0.02','0.02')])
        for taxes in ([],[{'tax_name':'CGST','tax_amount':'0.00'}],
                      [{'tax_name':'SGST','tax_amount':'0.00'}]):
            changed=copy.deepcopy(bill);changed['line_items'][0]['line_item_taxes']=taxes
            result=a.verify_detail(self.core,'999',payload,parsed,changed,owned=False)
            self.assertFalse(result['ok'])
            self.assertNotIn('duplicate_allocation_policy',result)
        self.assertTrue(a.verify_detail(self.core,'999',payload,parsed,bill,owned=False)['ok'])

    def test_equal_header_totals_never_hide_changed_quantity(self):
        parsed=self.parsed()
        payload,bill,_=self.verify(parsed,[('9.01','9.01'),('8.99','8.99')])
        bill['line_items'][0].update(quantity=2,rate=50)
        result=a.verify_detail(self.core,'999',payload,parsed,bill,owned=False)
        self.assertFalse(result['ok'])
        self.assertIn('line_quantity_mismatch',result['errors'])

    def test_each_exact_mode_is_selected_without_total_tolerance(self):
        group=self.parsed(bases=('100.04','100.04'),cgst='18.01')
        payload=a.validate(copy.deepcopy(group),self.core,self.vendors,check_zoho_rounding=False)
        self.assertIs(payload['is_item_level_tax_calc'],False)
        item=self.parsed(bases=('100.06','100.06'),cgst='18.02')
        payload=a.validate(copy.deepcopy(item),self.core,self.vendors)
        self.assertIs(payload['is_item_level_tax_calc'],True)
        for invalid in (self.parsed(cgst='18.01'),self.parsed(cgst='18.01',sgst='17.99')):
            with self.assertRaises(a.Hold):a.validate(invalid,self.core,self.vendors,check_zoho_rounding=False)

    def test_new_group_allocation_needs_its_own_creation_proof(self):
        parsed=self.parsed(bases=('100.04','100.04'),cgst='18.01')
        with self.assertRaisesRegex(a.Hold,'before creation'):
            a.validate(parsed,self.core,self.vendors)

    def test_calculation_mode_cannot_bypass_an_incorrect_tax_id(self):
        parsed=self.parsed(bases=('100.04','100.04'),cgst='18.01')
        original=self.core.build_payload
        def bad(*args,**kwargs):
            payload=original(*args,**kwargs);payload['line_items'][0]['tax_id']='INVALID';return payload
        with patch.object(self.core,'build_payload',side_effect=bad):
            with self.assertRaises(a.Hold):a.validate(parsed,self.core,self.vendors,check_zoho_rounding=False)

    def cma(self):
        parsed=invoice()
        parsed.update(vendor_name='CMA CGM TEST VENDOR',bill_number='INTEST000001',mbl_number='AMC111111')
        text=source_fixture().replace('SYNTHETIC FREIGHT VENDOR','CMA CGM TEST VENDOR, C/O. CCAI')
        text=text.replace('Invoice SYNTHETIC-001','Bill of Lading: AMC111111   INTEST000001')
        text=text.replace('Documentation    100.00    18%\nSAC 996713',
            'Documentation    100.00\nTax summary\nCGST @ 9% DOC 996713 GH 100.00 9.00\nSGST @ 9% DOC 996713 GH 100.00 9.00')
        return parsed,text

    def test_labelled_pdf_rows_resolve_both_agreeing_and_disagreeing_misreads(self):
        first,text=self.cma();second=copy.deepcopy(first)
        first['line_items'][0].update(cgst='8.99',sgst='8.99')
        second['line_items'][0].update(cgst='8.99',sgst='9.00')
        second['vendor_name']='CMA CGM TEST VENDOR, C/O. CCAI'
        original=copy.deepcopy([first,second])
        canonical,evidence=a.reconcile_readings(first,second,text,self.core)
        self.assertEqual(canonical['line_items'][0]['cgst'],'9.00')
        self.assertEqual(canonical['line_items'][0]['sgst'],'9.00')
        self.assertEqual(len(evidence['corrections']),3)
        self.assertEqual(evidence['raw_reading_sha256'],[a.reading_digest(x) for x in original])
        self.assertEqual([first,second],original)

    def test_repeated_or_wrong_sac_rows_never_supply_a_correction(self):
        parsed,text=self.cma();other=copy.deepcopy(parsed)
        for changed in (text+'\nCGST @ 9% DOC 996713 GH 100.00 9.00',
                        text.replace('996713','999999')):
            with self.assertRaises(a.Hold):a.reconcile_readings(parsed,other,changed,self.core)

    def test_signed_or_credit_tax_values_never_become_positive_corrections(self):
        parsed,text=self.cma();other=copy.deepcopy(parsed)
        for signed in ('-9.00','−9.00','- 9.00','-INR 9.00','(9.00)','(INR 9.00)','9.00-', '+9.00'):
            with self.subTest(signed=signed):
                changed=text.replace('100.00 9.00','100.00 '+signed)
                with self.assertRaisesRegex(a.Hold,'Signed or credit'):
                    a.reconcile_readings(parsed,other,changed,self.core)

    def test_unrelated_disagreement_or_bad_confidence_still_holds(self):
        first,text=self.cma()
        for values in ({'confidence':'low'},{'bill_date':'2026-10-27'},{'vendor_gstin':'27ZZZZZ9999Z1Z9'}):
            second=copy.deepcopy(first);second.update(values)
            with self.assertRaises(a.Hold):a.reconcile_readings(first,second,text,self.core)

    def test_other_supplier_qualifier_is_not_silently_discarded(self):
        first,text=self.cma();second=copy.deepcopy(first)
        second['vendor_name']='CMA CGM TEST VENDOR, C/O. OTHER'
        text=text.replace('C/O. CCAI','C/O. OTHER')
        with self.assertRaises(a.Hold):a.reconcile_readings(first,second,text,self.core)


if __name__=='__main__':unittest.main()
