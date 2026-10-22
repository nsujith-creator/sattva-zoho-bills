"""Manually entered invoice draft. It never verifies a PDF or posts to Zoho."""
import html
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

FIELDS = ("supplier", "gstin", "invoice_number", "invoice_date", "currency",
          "subtotal", "cgst", "sgst", "igst", "discount", "total", "notes")
MONEY = ("subtotal", "cgst", "sgst", "igst", "discount", "total")
LABELS = {"supplier": "Supplier name", "gstin": "Supplier GSTIN (optional)",
          "invoice_number": "Invoice number", "invoice_date": "Invoice date",
          "currency": "Invoice currency", "subtotal": "Subtotal", "cgst": "CGST on invoice",
          "sgst": "SGST on invoice", "igst": "IGST on invoice",
          "discount": "Discount / credit", "total": "Printed amount payable",
          "notes": "Notes / unresolved questions"}


class DraftError(ValueError):
    pass


def validate_draft(value):
    """Validate typed figures only. A balanced calculation is NOT PDF verification."""
    if not isinstance(value, dict) or any(k not in FIELDS for k in value):
        raise DraftError("Unsupported invoice fields.")
    if any(not isinstance(v, str) for v in value.values()):
        raise DraftError("All fields must be entered as text.")
    data = {k: value.get(k, '').strip() for k in FIELDS}
    for field, max_len in (("supplier", 160), ("invoice_number", 100), ("notes", 1000)):
        if len(data[field]) > max_len:
            raise DraftError(f"{LABELS[field]} is too long.")
    if not data["supplier"] or not data["invoice_number"]:
        raise DraftError("Supplier and invoice number are required.")
    if len(data["gstin"]) > 15 or (data["gstin"] and not re.fullmatch(r'[0-9]{2}[A-Za-z0-9]{13}', data["gstin"])):
        raise DraftError("Supplier GSTIN must have 15 letters/numbers, or be blank.")
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', data["invoice_date"]):
        raise DraftError("Use YYYY-MM-DD for invoice date.")
    try:
        date.fromisoformat(data["invoice_date"])
    except ValueError as e:
        raise DraftError("Invoice date is not valid.") from e
    data["currency"] = data["currency"].upper()
    if not re.fullmatch(r'[A-Z]{3}', data["currency"]):
        raise DraftError("Use a three-letter invoice currency code, such as INR or USD.")
    numbers = {}
    for field in MONEY:
        raw = data[field]
        if not re.fullmatch(r'\d{1,11}(?:\.\d{1,2})?', raw):
            raise DraftError(f"{LABELS[field]} must be a non-negative amount with at most 2 decimals.")
        try:
            numbers[field] = Decimal(raw)
        except InvalidOperation as e:
            raise DraftError(f"{LABELS[field]} is not an amount.") from e
        data[field] = format(numbers[field], '.2f')
    expected = (numbers['subtotal'] + numbers['cgst'] + numbers['sgst']
                + numbers['igst'] - numbers['discount'])
    if abs(expected - numbers['total']) > Decimal('0.01'):
        raise DraftError(f"Amounts don't add up. Entered components give {expected:.2f} {data['currency']}, not {numbers['total']:.2f}.")
    return data


def init_db(db):
    db.execute('CREATE TABLE IF NOT EXISTS reviews (invoice_id TEXT PRIMARY KEY REFERENCES invoices(id), '
               'fields_json TEXT NOT NULL, saved_at TEXT NOT NULL)')


def render(doc_id, filename, fields, csrf, suggestions=None, extraction_enabled=False):
    fields = fields or {}
    def esc(s):
        return html.escape(str(s), quote=True)
    form = []
    for key in FIELDS:
        label = LABELS[key]
        value = esc(fields.get(key, ''))
        if key == 'notes':
            form.append(f'<label class="field field-wide"><span>{label}</span><textarea name="{key}" maxlength="1000" rows="3">{value}</textarea></label>')
        elif key == 'currency':
            form.append(f'<label class="field"><span>{label}</span><input name="{key}" value="{value}" maxlength="3" required placeholder="INR" autocomplete="off"></label>')
        else:
            attrs = ' required' if key not in ('gstin',) else ''
            placeholder = '0.00' if key in MONEY else ''
            form.append(f'<label class="field"><span>{label}</span><input name="{key}" value="{value}" {attrs} placeholder="{placeholder}" autocomplete="off"></label>')
    suggestions = suggestions or {}
    proposed = suggestions.get("fields", {})
    held = suggestions.get("status") == "multi_invoice_hold"
    proposal_js = json.dumps(proposed,ensure_ascii=True).replace("<",r"\u003c").replace("> ",r"\u003e").replace("&",r"\u0026")
    issue_html = ''.join('<li>'+esc(x)+'</li>' for x in suggestions.get("issues",[]))
    suggestions_html = ''
    if suggestions:
        title = "Multiple invoices: held for manual review" if held else "AI suggestions only"
        rows = ''.join('<div class="suggestion-row"><span>'+esc(LABELS[k])+'</span><strong>'+esc(v)+'</strong></div>' for k,v in proposed.items() if k in LABELS)
        fill = '' if held or not proposed else '<button type="button" id="fill-empty">Fill empty fields only</button>'
        suggestions_html = '<section class="suggestions"><h3>'+title+'</h3><p>Unverified. Compare every value with the original PDF.</p>'+rows+('<ul>'+issue_html+'</ul>' if issue_html else '')+fill+'</section>'
    extract_control = ('<button type="button" id="extract-btn">Extract with AI (paid)</button>' if extraction_enabled and not suggestions else
                       '<p class="paid-note">Extraction off or already completed. Manual entry remains available.</p>')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{esc(csrf)}"><title>Review · Sattva Bills</title><style>
:root {{color-scheme:light;--ink:#112033;--muted:#536379;--line:#d5dfe9;--accent:#245ddc;--bg:#f4f7fa}}
* {{box-sizing:border-box}}body{{margin:0;color:var(--ink);font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:white}}
header{{border-bottom:1px solid var(--line);padding:20px clamp(18px,4vw,54px);display:flex;gap:14px;justify-content:space-between;flex-wrap:wrap}}
header strong{{letter-spacing:-.02em}}a{{color:var(--accent);text-underline-offset:4px}}
main{{width:min(1140px,calc(100% - 36px));margin:45px auto 80px}}
h1{{font-size:clamp(30px,5vw,48px);letter-spacing:-.045em;margin:8px 0 10px}}
.eyebrow{{text-transform:uppercase;font-size:11px;font-weight:750;letter-spacing:.12em;color:var(--accent)}}
.lede{{color:var(--muted);line-height:1.55;max-width:760px}}
.review-layout{{display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1.2fr);gap:28px;margin-top:26px;align-items:start}}
.source{{border:1px solid var(--line);border-radius:10px;padding:22px;background:var(--bg);overflow-wrap:anywhere}}
.source h2{{font-size:17px;margin:0 0 14px}}.source p{{font-size:13px;line-height:1.5;color:var(--muted)}}
.form{{border:1px solid var(--line);border-radius:10px;padding:22px;min-width:0}}
.form h2{{font-size:19px;letter-spacing:-.03em;margin:0 0 8px}}.form>p{{color:var(--muted);line-height:1.45;font-size:13px;margin:0 0 22px}}
.fields{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}
.field{{display:grid;gap:6px;min-width:0}}.field-wide{{grid-column:1/-1}}.field span{{font-size:12px;font-weight:680}}
input,textarea{{font:14px system-ui,sans-serif;border:1px solid #aebdcc;border-radius:6px;padding:12px;width:100%;color:var(--ink);background:#fff;min-width:0}}
input:focus-visible,textarea:focus-visible,button:focus-visible,a:focus-visible{{outline:3px solid #ff9b35;outline-offset:2px}}
.actions{{display:flex;align-items:center;flex-wrap:wrap;gap:13px;border-top:1px solid var(--line);padding-top:20px;margin-top:22px}}
button{{border:0;border-radius:7px;padding:12px 20px;min-height:45px;background:var(--accent);color:white;font-weight:750;cursor:pointer}}button:disabled{{opacity:.5}}
#message{{font-size:13px;color:var(--muted);overflow-wrap:anywhere;line-height:1.4}}#message.error{{color:#a32323}}#message.ok{{color:#126349}}
.warning{{padding:15px 18px;border-left:3px solid #657689;background:var(--bg);font-size:13px;line-height:1.5;margin-top:25px}}
.suggestions{{border-top:1px solid var(--line);margin-top:20px;padding-top:18px}}.suggestions h3{{font-size:16px}}.suggestions p,.paid-note{{font-size:12px;line-height:1.5;color:var(--muted)}}.suggestion-row{{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--line);font-size:12px}}.suggestion-row span{{color:var(--muted)}}.suggestion-row strong{{overflow-wrap:anywhere;text-align:right}}
@media(max-width:760px){{main{{margin-top:28px}}.review-layout{{grid-template-columns:1fr;gap:18px}}.source,.form{{padding:18px}}}}
@media(max-width:400px){{.fields{{grid-template-columns:1fr}}}}
</style></head><body>
<header><strong>SATTVA <span style="font-weight:400;color:#59697d">/ Bills review</span></strong><a href="/">Back to inbox</a></header>
<main><div class="eyebrow">PRIVATE REVIEW / NO POSTING</div><h1>Enter the invoice details</h1>
<p class="lede">This is a <strong>manually entered draft</strong>, not AI extraction or verified accounting. Compare every entry with the original PDF before using it elsewhere.</p>
<div class="review-layout"><aside class="source"><h2>Original supplier PDF</h2><strong>{esc(filename)}</strong><p>Open the saved original in a separate tab to check dates, vendor details, tax components and payable.</p><a href="/invoice/{esc(doc_id)}" target="_blank" rel="noopener">Open original PDF ↗</a>{extract_control}{suggestions_html}<p id="extract-message" role="status"></p></aside>
<section class="form"><h2>Draft fields</h2><p>All amounts here use the <strong>currency printed on the invoice</strong>. Enter 0 for absent tax/discount fields. Any imported-services reverse charge, currency conversion or GST eligibility must be reviewed separately.</p>
<form id="draft-form" autocomplete="off"><div class="fields">{''.join(form)}</div><div class="actions"><button type="submit">Save draft</button><span id="message" role="status" aria-live="polite">{'Draft saved previously. Review before editing.' if fields else 'Nothing saved yet.'}</span></div></form>
<div class="warning">Saving checks only the numbers you typed add up. It does <strong>not</strong> verify supplier identity, GST, source PDF or Zoho records. No approval or posting action exists here.</div></section></div></main>
<script>const proposal={proposal_js};
const fill=document.getElementById('fill-empty');if(fill)fill.addEventListener('click',()=>{{let n=0;for(const [k,v] of Object.entries(proposal)){{const input=document.querySelector('[name="'+k+'"]');if(input&&!input.value.trim()){{input.value=v;n++;}}}}document.getElementById('message').textContent=n+' blank fields filled. Compare PDF before saving.';}});
const extract=document.getElementById('extract-btn');if(extract)extract.addEventListener('click',async()=>{{extract.disabled=true;const msg=document.getElementById('extract-message');msg.textContent='Extracting invoice…';try{{const r=await fetch('/api/extract/{esc(doc_id)}',{{method:'POST',credentials:'same-origin',headers:{{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}}}});const data=await r.json();if(r.ok)window.location.reload();else{{msg.textContent=data.error||'Extraction failed.';extract.disabled=false;}}}}catch(e){{msg.textContent='Connection failed. Manual draft unchanged.';extract.disabled=false;}}}});
const form=document.getElementById('draft-form'),msg=document.getElementById('message'),button=form.querySelector('button');form.addEventListener('submit',async e=>{{e.preventDefault();const data=Object.fromEntries(new FormData(form));button.disabled=true;msg.textContent='Saving draft…';msg.className='';try{{const r=await fetch('/api/reviews/{esc(doc_id)}',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}},body:JSON.stringify(data)}});const result=await r.json();msg.textContent=r.ok?'Draft saved. Not verified or sent to Zoho.':(result.error||'Could not save draft.');msg.className=r.ok?'ok':'error';}}catch(_e){{msg.textContent='Connection interrupted. Check saved draft before retrying.';msg.className='error';}}finally{{button.disabled=false;}}}});</script>
</body></html>'''
