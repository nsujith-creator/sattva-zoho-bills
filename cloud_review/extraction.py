"""Isolated Claude Haiku PDF extraction, suggestions only. No Zoho access."""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation
from review import MONEY, FIELDS

MODEL = "claude-haiku-4-5"
MAX_RESPONSE_BYTES = 250000
ALLOW_FIELDS = tuple(k for k in FIELDS if k != "notes")

class ExtractionError(ValueError):
    pass

SYSTEM = """Extract figures PRINTED on exactly one supplier invoice PDF for review by Sattva Global Logistics.
Use the supplier/issuer, NOT the buyer. Capture the local Indian GSTIN of a shipping-line billing agent, not a foreign principal's identity. Never infer GSTIN from name.
Return ONLY JSON with keys:
{"supplier":string|null,"gstin":string|null,"invoice_number":string|null,"invoice_date":"YYYY-MM-DD"|null,"currency":three_letter_string|null,"subtotal":string|null,"cgst":string|null,"sgst":string|null,"igst":string|null,"discount":string|null,"total":string|null,"multi_invoice":boolean,"issues":array_of_short_strings}
Rules:
- Preserve printed figures and invoice currency, without computing FX or inventing GST. Unknown fields must be null.
- Tax fields are ONLY actual CGST, SGST and IGST printed on supplier invoice. Do not invent reverse-charge tax, ITC or tax eligibility. If a tax head is clearly absent, enter "0.00"; if uncertain, null.
- On a complete INR invoice showing CGST and SGST only, absent IGST is "0.00". If no discount or credit is shown, discount is "0.00". Include every money key; leave null only where the document is genuinely unclear.
- total is invoice GRAND TOTAL, not an outstanding balance adjusted for previous payments. Never calculate it yourself.
- discount is a positive reduction. Flag credit notes and negative totals.
- CMA CGM: use the invoice ID, NOT the Bill of Lading/MBL number. Meta: prefer Tax Invoice ID at top, not FBADS number below. Flag conflicting identifiers.
- CMA CGM EXPORT INVOICE header has TWO different values on the same line: "Bill of Lading: AMC..." and, further to the right, the invoice number such as INEMHC.... Use the second value. Never return the AMC bill-of-lading reference as invoice_number. If the invoice number cannot be identified, return null and flag it.
- Sattva buyer GSTIN 27AEFPN9399L1ZI is not a conflicting supplier GSTIN. An invoice billed to that GSTIN belongs to business-expense review even if a personal billing-contact name appears. This does not establish tax-credit eligibility or approval to post.
- If multiple invoices or credit notes appear in one PDF, set multi_invoice true. Do not select only the largest.
- Currency follows currency of invoice payable, not quoted USD references for INR invoices. Never fabricate rates.
- All money amounts decimal strings with no symbols/separators, up to 2 fractional digits. Be explicit about uncertainty.
"""

def request_anthropic(pdf_bytes, filename, key, *, transport=None):
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ExtractionError("Invalid PDF header.")
    transport = transport or urllib.request.urlopen
    payload = {"model": MODEL, "max_tokens": 1900, "system": SYSTEM,
               "messages":[{"role":"user","content":[
                   {"type":"document","source":{"type":"base64","media_type":"application/pdf",
                    "data":base64.standard_b64encode(pdf_bytes).decode("ascii")}},
                   {"type":"text","text":"Extract supplier invoice for human review. Filename (not evidence): "+filename[:180]}
               ]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        method="POST", data=json.dumps(payload, separators=(",",":")).encode("utf-8"),
        headers={"x-api-key":key,"anthropic-version":"2023-06-01",
                 "content-type":"application/json"})
    try:
        with transport(req,timeout=65) as resp:
            data=resp.read(MAX_RESPONSE_BYTES+1)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ExtractionError("The extraction provider could not finish. Draft unchanged.") from e
    if len(data)>MAX_RESPONSE_BYTES:
        raise ExtractionError("Provider response too large.")
    try:
        obj=json.loads(data)
        raw="\n".join(b.get("text","") for b in obj["content"]
                      if isinstance(b,dict) and b.get("type")=="text").strip()
        marker=chr(96)*3
        if raw.startswith(marker):
            raw=re.sub("^"+re.escape(marker)+r"(?:json)?\s*","",raw,flags=re.I)
            raw=raw.removesuffix(marker).strip()
        parsed=json.loads(raw)
    except (KeyError,TypeError,ValueError) as e:
        raise ExtractionError("Provider returned unreadable suggestions.") from e
    return normalize_suggestions(parsed)

def normalize_suggestions(parsed):
    if not isinstance(parsed,dict):
        raise ExtractionError("Not an invoice object.")
    issues=parsed.get("issues") or []
    if not isinstance(issues,list):
        issues=["Provider returned unstructured warnings."]
    issues=[str(i).strip()[:180] for i in issues[:8]
            if isinstance(i,(str,int,float)) and str(i).strip()]
    multi=parsed.get("multi_invoice")
    if multi is not True and multi is not False:
        issues.append("Number of invoices uncertain; check source.")
    if multi is True:
        return {"status":"multi_invoice_hold","fields":{},
                "issues":issues or ["Multiple invoices detected."],
                "source_verified":False,"posted_to_zoho":False,"provider":MODEL}
    fields={}
    for k in ALLOW_FIELDS:
        raw=parsed.get(k)
        if raw is None or isinstance(raw,bool):
            continue
        value=str(raw).strip()
        if not value or len(value)>180:
            issues.append(f"{k}: missing/too long.")
            continue
        if k in MONEY:
            value=value.replace(",","")
            if not re.fullmatch(r"\d{1,11}(?:\.\d{1,2})?",value):
                issues.append(f"{k}: not a non-negative two-decimal amount.")
                continue
            try: value=format(Decimal(value),".2f")
            except InvalidOperation:
                issues.append(f"{k}: invalid decimal.")
                continue
        elif k=="invoice_date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
                issues.append("invoice_date: ambiguous.")
                continue
            try: date.fromisoformat(value)
            except ValueError:
                issues.append("invoice_date: invalid date.")
                continue
        elif k=="currency":
            value=value.upper()
            if not re.fullmatch(r"[A-Z]{3}",value):
                issues.append("currency: ambiguous.")
                continue
        elif k=="gstin":
            value=value.upper()
            if not re.fullmatch(r"\d{2}[A-Z0-9]{13}",value):
                issues.append("gstin: invalid format, omitted.")
                continue
        fields[k]=value
    supplier = fields.get("supplier", "").upper()
    invoice_number = fields.get("invoice_number", "")
    if ("CMA CGM" in supplier or "CCAI" in supplier) and re.fullmatch(r"AMC\d{7,}", invoice_number, re.I):
        fields.pop("invoice_number")
        issues.append("CMA CGM bill-of-lading reference " + invoice_number +
                      " withheld as invoice number. Check the EXPORT INVOICE header.")
    if all(k in fields for k in (*MONEY,"currency")):
        sub,cgst,sgst,igst,disc,total=(Decimal(fields[k]) for k in MONEY)
        if abs(sub+cgst+sgst+igst-disc-total)>Decimal("0.01"):
            issues.append("Extracted components do not match printed total. Check PDF, credits and tax.")
    missing=[k for k in ("supplier","invoice_number","invoice_date","currency",*MONEY) if k not in fields]
    if missing: issues.append("Missing suggestions: "+", ".join(missing))
    return {"status":"suggestions_only","fields":fields,"issues":issues[:12],
            "source_verified":False,"posted_to_zoho":False,"provider":MODEL}

def configured():
    return os.environ.get("APP_ENABLE_AI_EXTRACTION")=="1" and bool(os.environ.get("ANTHROPIC_API_KEY","").strip())
