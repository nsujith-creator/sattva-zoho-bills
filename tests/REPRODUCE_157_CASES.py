import importlib.util, json, sys, traceback, tempfile, os
from unittest.mock import patch
spec=importlib.util.spec_from_file_location("live_zoho_audit",r"C:\temp\zoho\zoho_bills.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._OVERSEAS_VENDOR_SIGNALS={"ANTHROPIC","ADOBE","MICROSOFT","OPENAI","AWS"}
m._TAX_ID_MAP={"igst_18":"ID18","igst_5":"ID5","cgst_sgst_18":"IDC18","cgst_sgst_5":"IDC5"}
m.IGST_VENDORS=[]
m.VENDOR_ALIAS_MAP={}
results=[]
def check(name,actual,expected,category):
    passed=actual==expected
    results.append(dict(id="X%03d"%(len(results)+1),name=name,category=category,expected=repr(expected),actual=repr(actual),passed=passed))
def base(**kwargs):
    p={"vendor_name":"Local Services","bill_number":"INV123","bill_date":"2026-05-01","due_date":"","mbl_number":"","currency_code":"INR","is_overseas":False,"line_items":[{"description":"consulting","rate":100.0,"quantity":1,"hsn_or_sac":"998313"}],"subtotal":100,"discounts_or_credits":0,"tax_amount_igst":0,"tax_amount_cgst":0,"tax_amount_sgst":0,"invoice_total":100,"confidence":"high","notes":""}
    p.update(kwargs);return p
# Twenty-five validation checks: safe/unsafe amounts, dates, numbers and reconciliation.
cases=[
("valid",{},None),("empty_vendor",{}, "vendor_id missing",""),
("empty_bill",{"bill_number":""},"bill_number empty"),
("short_bill",{"bill_number":"A"},"bill_number too short"),
("missing_date",{"bill_date":""},"bill_date empty"),
("bad_date_format",{"bill_date":"05/01/2026"},"bill_date invalid format: 05/01/2026"),
("equal_mbl",{"mbl_number":"INV123"},"bill_number equals mbl_number"),
("empty_lines",{"line_items":[]},"no line items with rate > 0"),
("zero_amount",{"line_items":[{"rate":0}]},"no line items with rate > 0"),
("missing_total",{"invoice_total":0},"invoice_total_missing"),
("negative_total",{"invoice_total":-1},"invoice_total_missing"),
("short_due_date",{"due_date":"2026/05/01"},"due_date invalid format: 2026/05/01"),
("inconsistent_total",{"invoice_total":200},"total_reconciliation_failed:200.00_vs_100.00"),
("small_rounding",{"invoice_total":100.9},None),
("tax_integrates",{"tax_amount_igst":18,"invoice_total":118},None),
("credit_integrates",{"discounts_or_credits":-10,"invoice_total":90},None),
("quantity_integrates",{"line_items":[{"rate":50,"quantity":2}],"invoice_total":100},None),
("USD_single",{"currency_code":"USD","line_items":[{"rate":100,"currency_code":"USD"}]},None),
("INR_line_on_USD",{"currency_code":"USD","line_items":[{"rate":100,"currency_code":"INR"}]},None),
("EUR_line_on_INR",{"line_items":[{"rate":100,"currency_code":"EUR"}]},"mixed line item currencies: {'EUR'}"),
("blanks_tax",{"tax_amount_igst":"","invoice_total":100},None),
("multiple",{"line_items":[{"rate":40},{"rate":60}]},None),
("discount_positive",{"discounts_or_credits":10,"invoice_total":110},None),
("missing_currency",{"currency_code":"INR"},None),
("different_case_mbl",{"mbl_number":"inv123"},"bill_number equals mbl_number")
]
for case in cases:
    name,kw,exp=case[0],case[1],case[2]
    actual=m.validate_before_post(base(**kw),None if name=="empty_vendor" else "V1")
    check("validate_"+name,actual,exp,"validation")
# Twenty overseas classification signals, literal observed output expectation.
names=["ANTHROPIC","ADOBE IRELAND","MICROSOFT CORPORATION","OPENAI LLC","AWS EUROPE",
"ANTHROPIC LABS","ADOBE INC","MICROSOFT 365","OPENAI SERVICES","AWS ASIA"]
for n in names:
    for flag in [True,False]:
        check("overseas_"+n.replace(" ","_")+"_"+str(flag),m.determine_overseas_status(base(is_overseas=flag,currency_code="INR"),n),True,"overseas")
# Twenty filename fallback variants, expected exact token / block.
filenames=[
("BOMFALI260400035InvoicePrint.pdf","BOMFALI260400035"),
("BOMFALI260400035.pdf","BOMFALI260400035"),
("INEMHC27075341.pdf","INEMHC27075341"),
("PTGFWD112.pdf","PTGFWD112"),
("VSLNS2323.pdf","VSLNS2323"),
("AMC2486294.pdf",""),
("random_abc.pdf",""),
("12345.pdf",""),
("INEMHC27075341_duplicate.pdf","INEMHC27075341"),
("BOMFALI1_copy.pdf","BOMFALI1"),
("BOMFALI2_dup.pdf","BOMFALI2"),
("BOMFALI3_dupLicate.pdf","BOMFALI3"),
("INEMHC999.PDF","INEMHC999"),
("VSLNS-99.pdf","VSLNS-99"),
("PTGFWD001 InvoicePrint.pdf","PTGFWD001"),
("AMC000InvoicePrint.pdf",""),
("FRT_INV_INEMHC270.pdf",""),
("BOMFALI77.pdf","BOMFALI77"),
("UNKNOWN.pdf",""),
("INEMHC21.pdf","INEMHC21")]
for n,expected in filenames:check("filename_"+n,m._derive_bill_number_from_filename(n,"MBL555"),expected,"invoice_number")
# Twenty vendor-matching scenarios, each uses isolated synthetic vendor records.
vendors={"SAMPLE LOGISTICS":{"id":"V1","gst_no":"27ABCDE1234F1Z5"},"ALPHA SERVICES":{"id":"V2","gst_no":"27ABCDE1234F1Z6"}}
for i in range(20):
    name="SAMPLE LOGISTICS" if i%2==0 else "ALPHA SERVICES"
    expected_id="V1" if i%2==0 else "V2"
    entered=[name,name.lower(),name.title(),name+" ",name.replace(" ","  ")][i%5]
    got=m.match_vendor_tiered(entered,vendors)
    check("vendor_match_%02d"%i,got[0],expected_id,"vendor")
# Twenty payload asserts on currency, HSN, tax and FX, mocked route configuration.
dom={"is_overseas":False,"tax_mode":"igst","tax_id_key":"igst_18"}
over={"is_overseas":True,"tax_mode":"rcm","tax_id_key":None}
for i in range(20):
    source=base()
    code="998313" if i%2==0 else "1234567"
    source["line_items"][0]["hsn_or_sac"]=code
    payload=m.build_payload(source,"V1",dom)
    check("payload_tax_%02d"%i,payload["line_items"][0]["tax_id"],"ID18","payload")
    check("payload_hsn_%02d"%i,payload["line_items"][0].get("hsn_or_sac"),code if len(code)==6 else None,"payload")
# Review RCM and FX guard without API calls.
for i in range(10):
    p=base(currency_code="INR" if i%2 else "USD")
    pay=m.build_payload(p,"V1",over,80)
    expected=100 if i%2 else 8000
    check("overseas_fx_%02d"%i,pay["line_items"][0]["rate"],expected,"fx")
    check("rcm_id_%02d"%i,pay["line_items"][0].get("reverse_charge_tax_id"),"ID18","rcm")
# Hidden financial mismatch: expected subtotal=100.0, Zoho reports +0.5 and +2.0.
base_pdf=base()
payload=m.build_payload(base_pdf,"V1",dom)
def bill(sub,total):
    return {"bill_number":"INV123","vendor_id":"V1","date":"2026-05-01","sub_total":sub,"total":total,"line_items":[{"rate":sub,"quantity":1}]}
for i in range(10):
    delta=0.5 if i%2==0 else 2
    with patch.object(m,"zoho_get",return_value={"bill":bill(100+delta,118+delta)}):
        outcome=m.verify_created_bill("BILL",payload,base_pdf)
    check("verification_subtotal_%02d"%i,outcome["ok"],delta<=1,"verification")
# Explicit documented failures expected by an accounting-audit correctness oracle.
with patch.object(m,"zoho_get",return_value={"bill":bill(100,998)}):
    outcome=m.verify_created_bill("BILL",payload,base_pdf)
check("DEFECT_payable_ignored",outcome["ok"],False,"regression_expected_fail")
with patch.object(m,"zoho_get",return_value={"bill":bill(100.5,100.5)}):
    outcome=m.verify_created_bill("BILL",payload,base_pdf)
check("DEFECT_subtotal_nonzero_difference",outcome["ok"],False,"regression_expected_fail")
with open(r"C:\Users\sujit\AppData\Local\Temp\zoho_audit_20261021_results.json","w",encoding="utf8") as f:json.dump(results,f,indent=2)
counts={}
for row in results:counts[row["category"]]=counts.get(row["category"],0)+1
print("ISOLATED_CASES",len(results),"PASS",sum(x["passed"] for x in results),"FAIL",sum(not x["passed"] for x in results),"CATEGORIES",counts)
for row in results:
    if not row["passed"]:print("FAILED",row["id"],row["name"],"expected",row["expected"],"actual",row["actual"])
