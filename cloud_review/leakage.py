"""Vendor-cost -> customer-invoice leakage reconciliation (read-only).

Design: docs/superpowers/specs/2026-11-19-leakage-reconciliation-design.md (owner-approved, 19 Nov 2026).

Safety: this module can only issue HTTP GET requests to Zoho Books for bills, invoices and
credit notes (plus the OAuth token refresh). It has no write method, never sends a request
body to Zoho Books, and never edits vendors, customers or documents.

CLI (historical pass):
    python cloud_review/leakage.py --once --out report.json [--cache DIR] [--decisions FILE]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:  # The overhead vendor list is owned by the posting engine; never fork it.
    from automation import SOFTWARE_VENDORS, ADVERTISING_VENDORS
except Exception:  # pragma: no cover - fallback keeps the CLI usable outside the app
    SOFTWARE_VENDORS = {'ANTHROPIC', 'OPENAI', 'RAILWAY', 'CLOUDFLARE', 'SHIPSGO'}
    ADVERTISING_VENDORS = {'SENDINBLUE', 'BREVO'}

BOOKS = 'https://www.zohoapis.in/books/v3/'
TOKEN_URL = 'https://accounts.zoho.in/oauth/v2/token'
ENDPOINT = re.compile(r'(?:bills|invoices|creditnotes)(?:/[0-9]+)?')
FY_START = '2026-04-01'
LEAK_DAYS = 7
TOLERANCE = Decimal('1.00')  # INR; absorbs paise from exchange-rate products only
SINGULAR = {'bills': 'bill', 'invoices': 'invoice', 'creditnotes': 'creditnote'}
ID_FIELD = {'bills': 'bill_id', 'invoices': 'invoice_id', 'creditnotes': 'creditnote_id'}
REQUIRED = {
    'bills': ('bill_id', 'vendor_id', 'vendor_name', 'date', 'status', 'sub_total', 'total', 'balance', 'line_items'),
    'invoices': ('invoice_id', 'customer_id', 'customer_name', 'date', 'status', 'sub_total', 'total', 'balance', 'line_items'),
    'creditnotes': ('creditnote_id', 'customer_id', 'date', 'status', 'sub_total', 'total'),
}
BILLED_INVOICE = {'sent', 'viewed', 'paid', 'partially_paid', 'overdue', 'unpaid', 'approved'}


class ConfigError(Exception):
    pass


class Incomplete(Exception):
    """Evidence could not be gathered; the run must be reported INCOMPLETE."""


class BudgetExhausted(Incomplete):
    pass


# ---------------------------------------------------------------- read-only client

def load_config(env=None):
    env = os.environ if env is None else env
    names = ('ZOHO_ORGANIZATION_ID', 'ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_RECON_REFRESH_TOKEN')
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise ConfigError('Missing environment variables: '+', '.join(missing))
    values = {n: env[n] for n in names}
    for name, value in values.items():
        if value != value.strip() or value[:1] in '<"\'' or value[-1:] in '>"\'':
            raise ConfigError(name+' looks like a template placeholder or is quoted/padded; store the bare value.')
    if not values['ZOHO_ORGANIZATION_ID'].isdigit():
        raise ConfigError('ZOHO_ORGANIZATION_ID must be digits only.')
    for name in ('ZOHO_CLIENT_ID', 'ZOHO_RECON_REFRESH_TOKEN'):
        if not values[name].startswith('1000.'):
            raise ConfigError(name+' does not have the Zoho 1000.* shape; it may be truncated.')
    return values


def http_get(url, headers, timeout=30):
    """The only Zoho Books transport: GET, no body."""
    request = urllib.request.Request(url, headers=headers, method='GET')
    if request.get_method() != 'GET' or request.data is not None:
        raise RuntimeError('Read-only client refused a non-GET request.')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(5_000_001)
    except urllib.error.HTTPError as exc:
        raise Incomplete('Zoho HTTP '+str(exc.code)+' for '+url.split('?')[0].replace(BOOKS, '')) from None
    except (OSError, ValueError) as exc:
        raise Incomplete('Zoho connection failed: '+type(exc).__name__) from None
    if len(raw) > 5_000_000:
        raise Incomplete('Zoho response too large.')
    return json.loads(raw)


def refresh_token(config, timeout=20):
    """OAuth refresh (accounts.zoho.in). Not a Zoho Books request; changes no Books data."""
    body = urllib.parse.urlencode({'client_id': config['ZOHO_CLIENT_ID'], 'client_secret': config['ZOHO_CLIENT_SECRET'],
                                   'refresh_token': config['ZOHO_RECON_REFRESH_TOKEN'], 'grant_type': 'refresh_token'}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body, method='POST'), timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise Incomplete('Zoho sign-in HTTP '+str(exc.code)) from None
    except (OSError, ValueError) as exc:
        raise Incomplete('Zoho sign-in connection failed: '+type(exc).__name__) from None
    if not result.get('access_token'):
        raise Incomplete('Zoho sign-in refused: '+str(result.get('error', 'no access token'))[:60])
    if result.get('api_domain', 'https://www.zohoapis.in') != 'https://www.zohoapis.in':
        raise Incomplete('Unexpected Zoho data centre.')
    return result['access_token']


class ReadOnlyZoho:
    """GET-only Zoho Books client. There is deliberately no method that accepts a payload."""

    def __init__(self, config, transport=http_get, token_source=refresh_token, budget=1500, pace=1.0,
                 sleep=time.sleep, clock=time.monotonic):
        self.org = config['ZOHO_ORGANIZATION_ID']
        self.config, self.transport, self.token_source = config, transport, token_source
        self.budget, self.pace, self.sleep, self.clock = budget, pace, sleep, clock
        self.calls, self.token, self.expires, self.last = 0, '', 0.0, None

    def headers(self):
        if not self.token or self.clock() >= self.expires:
            self.token, self.expires = self.token_source(self.config), self.clock()+2700
        return {'Authorization': 'Zoho-oauthtoken '+self.token}

    def get(self, endpoint, params=None):
        if not isinstance(endpoint, str) or not ENDPOINT.fullmatch(endpoint):
            raise PermissionError('Endpoint not on the read-only allowlist: '+repr(endpoint))
        if self.calls >= self.budget:
            raise BudgetExhausted('Per-run Zoho API budget of '+str(self.budget)+' requests used.')
        if self.last is not None and self.pace:
            wait = self.pace-(self.clock()-self.last)
            if wait > 0:
                self.sleep(wait)
        query = urllib.parse.urlencode({'organization_id': self.org, **(params or {})})
        self.calls += 1
        self.last = self.clock()
        result = self.transport(BOOKS+endpoint+'?'+query, self.headers())
        if not isinstance(result, dict) or result.get('code') != 0:
            raise Incomplete('Zoho rejected GET '+endpoint+': '+str((result or {}).get('message', 'no code'))[:80])
        return result

    def all(self, collection, params=None):
        rows = []
        for page in range(1, 501):
            data = self.get(collection, {**(params or {}), 'page': page, 'per_page': 200})
            if not isinstance(data.get(collection), list) or not isinstance(data.get('page_context'), dict):
                raise Incomplete('Zoho returned incomplete list evidence for '+collection+'.')
            rows.extend(data[collection])
            more = data['page_context'].get('has_more_page')
            if more is False:
                return rows
            if more is not True:
                raise Incomplete('Zoho pagination could not be verified for '+collection+'.')
        raise Incomplete('Zoho list '+collection+' exceeded the supported page count.')


# ---------------------------------------------------------------- evidence collection

class Cache:
    def __init__(self, directory):
        self.dir = Path(directory) if directory else None
        self.data = {k: {} for k in SINGULAR}
        if self.dir and self.dir.exists():
            for kind in SINGULAR:
                path = self.dir/(kind+'.json')
                if path.exists():
                    self.data[kind] = json.loads(path.read_text())

    def lookup(self, kind, doc_id, modified):
        hit = self.data[kind].get(doc_id)
        if hit and modified and hit.get('last_modified_time') == modified:
            return hit['doc']
        return None

    def store(self, kind, doc_id, modified, doc):
        self.data[kind][doc_id] = {'last_modified_time': modified, 'doc': doc}

    def save(self):
        if not self.dir:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        for kind, rows in self.data.items():
            tmp = self.dir/(kind+'.json.tmp')
            tmp.write_text(json.dumps(rows, sort_keys=True))
            tmp.replace(self.dir/(kind+'.json'))


def collect(client, cache, bills_since=FY_START, invoices_since=None):
    """Fetch every list (fully paginated) and every needed detail. Returns (evidence, reasons)."""
    invoices_since = invoices_since or str(date.fromisoformat(bills_since)-timedelta(days=90))
    since = {'bills': bills_since, 'invoices': invoices_since, 'creditnotes': invoices_since}
    evidence = {k: {} for k in SINGULAR}
    reasons, counts = [], {}
    try:
        for kind in ('bills', 'invoices', 'creditnotes'):
            rows = client.all(kind, {'filter_by': 'Status.All', 'date_start': since[kind]})
            rows = [r for r in rows if str(r.get('date', '')) >= since[kind]]
            counts[kind] = len(rows)
            for row in rows:
                doc_id = str(row.get(ID_FIELD[kind], ''))
                if not doc_id.isdigit():
                    reasons.append(kind+' list row without a valid id')
                    continue
                if str(row.get('status', '')).lower() == 'void':
                    evidence[kind][doc_id] = dict(row, _void=True)
                    continue
                modified = row.get('last_modified_time')
                doc = cache.lookup(kind, doc_id, modified)
                if doc is None:
                    doc = client.get(kind+'/'+doc_id).get(SINGULAR[kind])
                    if not isinstance(doc, dict):
                        reasons.append(kind+' '+doc_id+': detail missing')
                        continue
                    if modified:
                        cache.store(kind, doc_id, modified, doc)
                evidence[kind][doc_id] = doc
    except Incomplete as exc:
        reasons.append(str(exc))
    finally:
        cache.save()
    for kind in SINGULAR:
        if kind not in counts:
            reasons.append(kind+' list not fully read')
        elif len(evidence[kind]) < counts[kind]:
            reasons.append(kind+': '+str(counts[kind]-len(evidence[kind]))+' of '+str(counts[kind])+' details not fetched')
    for kind, docs in evidence.items():
        for doc_id, doc in docs.items():
            if doc.get('_void'):
                continue
            absent = [f for f in REQUIRED[kind] if doc.get(f) in (None, '')]
            if absent:
                reasons.append(kind+' '+doc_id+' missing '+', '.join(absent))
                doc['_invalid'] = absent
    return evidence, reasons, client.calls


# ---------------------------------------------------------------- keys

def norm(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def valid_key(key):
    return len(key) >= 8 and any(c.isdigit() for c in key)


_ISO = {c: v for c, v in zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                             [10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 34, 35, 36, 37, 38])}


def is_container(token):
    if not re.fullmatch(r'[A-Z]{3}[UJZ][0-9]{7}', token):
        return False
    total = sum((_ISO[c] if c.isalpha() else int(c))*(2**i) for i, c in enumerate(token[:10]))
    return total % 11 % 10 == int(token[10])


def candidates(text):
    """Alnum tokens and joins of 2-3 adjacent tokens, within one text field."""
    tokens = re.findall(r'[A-Z0-9]+', str(text or '').upper())
    out = set()
    for i in range(len(tokens)):
        for n in (1, 2, 3):
            if i+n <= len(tokens):
                out.add(''.join(tokens[i:i+n]))
    return out


# Master and house bill-of-lading labels. Customer invoices sometimes carry the HBL in the
# "MBL #" slot (live data, 19 Nov 2026), so labelled house numbers on bills are keys too.
_MBL_LABEL = re.compile(r'(?:\b[MH]\.?\s?B\.?\s?/?\s?L\b|\bB\s?/\s?L\b|\bBL\b|BILL OF LADING|\b(?:MASTER|HOUSE)(?:\s+B\s?/\s?L)?)\.?\s*(?:NO\b\.?|NUMBER|#)?\s*[:.\-]?\s*((?:[A-Z0-9][A-Z0-9\-/]*\s*){1,3})')


def labelled_mbls(text):
    keys = set()
    for match in _MBL_LABEL.finditer(str(text or '').upper()):
        tokens, joined = re.findall(r'[A-Z0-9]+', match.group(1)), ''
        for token in tokens:
            joined += token
            if valid_key(joined):
                keys.add(joined)
                break
    return keys


def doc_texts(doc):
    texts = [doc.get('reference_number'), doc.get('notes'), doc.get('terms')]
    for line in doc.get('line_items') or []:
        texts += [line.get('description'), line.get('name')]
    for field in doc.get('custom_fields') or []:
        texts.append(field.get('value'))
    return [t for t in texts if t]


_REF_LABEL = re.compile(r'^\s*(?:M\.?\s?B\.?\s?/?\s?L|B\s?/\s?L|BL|BILL OF LADING)\.?\s*(?:NO\b\.?|NUMBER|#)?\s*[:.\-]?\s*')


def ref_key(value):
    """Reference# as a key, without a leading 'MBL No:' style label."""
    return norm(_REF_LABEL.sub('', str(value or '').upper()))


def bill_keys(bill):
    keys = set()
    ref = ref_key(bill.get('reference_number'))
    if valid_key(ref):
        keys.add(ref)
    for text in doc_texts(bill):
        keys |= {c for c in candidates(text) if is_container(c)}
        keys |= labelled_mbls(text)
    return keys


def invoice_candidates(doc):
    out = set()
    for text in doc_texts(doc):
        out |= candidates(text)
    return out


def near(key, cands):
    return sorted(c for c in cands if c != key and len(c) >= 8 and (key in c or c in key))


# ---------------------------------------------------------------- money

def dec(value):
    try:
        return Decimal(str(value if value not in (None, '') else 0))
    except InvalidOperation:
        return Decimal(0)


def rate(doc):
    r = dec(doc.get('exchange_rate') or 1)
    return r if r > 0 else Decimal(1)


def bill_gross_cost(bill):
    return sum((dec(l.get('item_total')) for l in bill.get('line_items') or []), Decimal(0))*rate(bill)


def bill_cost(bill):
    """Pre-GST cost net of vendor credits applied to the bill (credit split pro rata to pre-GST).

    Live data, 20 Nov 2026: a vendor's credit note cancelling a wrong invoice was applied in
    Zoho to the re-issued bill; ignoring credits double-counted the shipment cost."""
    gross, total = bill_gross_cost(bill), dec(bill.get('total'))
    credit = dec(bill.get('vendor_credits_applied'))
    if credit > 0 and total > 0:
        gross -= min(credit, total)*gross/total
    return gross


def paid_fraction(doc):
    total = dec(doc.get('total'))
    return (total-dec(doc.get('balance')))/total if total > 0 else Decimal(0)


def cash_paid_fraction(bill):
    """Share of a vendor bill settled in cash; a vendor credit clears the balance but is not a payment."""
    total = dec(bill.get('total'))
    if total <= 0:
        return Decimal(0)
    if bill.get('payment_made') not in (None, ''):
        return min(dec(bill['payment_made']), total)/total
    return max(Decimal(0), total-dec(bill.get('balance'))-dec(bill.get('vendor_credits_applied')))/total


def q(value):
    return str(Decimal(value).quantize(Decimal('0.01')))


def days(today, value):
    try:
        return (today-date.fromisoformat(str(value)[:10])).days
    except ValueError:
        return None


# ---------------------------------------------------------------- reconciliation

def is_overhead_vendor(name):
    words = re.sub(r'[^A-Z0-9 ]', ' ', str(name or '').upper()).split()
    return bool(words) and (words[0] in SOFTWARE_VENDORS or words[0] in ADVERTISING_VENDORS or words[:2] == ['GOOGLE', 'INDIA'])


def load_decisions(path):
    if not path or not Path(path).exists():
        return {'links': [], 'overhead': [], 'accepted_losses': []}
    data = json.loads(Path(path).read_text())
    return {k: list(data.get(k) or []) for k in ('links', 'overhead', 'accepted_losses')}


def bill_rec(bill, today):
    return {'bill_id': str(bill['bill_id']), 'bill_number': bill.get('bill_number'), 'vendor': bill.get('vendor_name'),
            'date': bill.get('date'), 'reference_number': bill.get('reference_number'), 'status': bill.get('status'),
            'cost_inr': q(bill_cost(bill)), 'vendor_credits_applied': str(bill.get('vendor_credits_applied') or 0),
            'payment_made': str(bill.get('payment_made', '')), 'currency': bill.get('currency_code', 'INR'), 'total': str(bill.get('total')),
            'balance': str(bill.get('balance')), 'age_days': days(today, bill.get('date'))}


def inv_rec(inv):
    return {'invoice_id': str(inv['invoice_id']), 'invoice_number': inv.get('invoice_number'), 'customer': inv.get('customer_name'),
            'date': inv.get('date'), 'due_date': inv.get('due_date'), 'status': inv.get('status'),
            'revenue_inr': q(dec(inv.get('sub_total'))*rate(inv)), 'total': str(inv.get('total')), 'balance': str(inv.get('balance')),
            'reference_number': inv.get('reference_number')}


def reconcile(evidence, today, decisions=None, incomplete_reasons=(), since=FY_START):
    decisions = decisions or {'links': [], 'overhead': [], 'accepted_losses': []}
    confirmed, potential, accepted, excluded, notes = [], [], [], [], []

    def issue(bucket, kind, exposure, detail, bills=(), invoices=(), keys=(), **extra):
        bucket.append(dict({'type': kind, 'exposure_inr': q(exposure), 'detail': detail, 'shipment_keys': sorted(keys),
                            'bills': [bill_rec(b, today) for b in bills], 'invoices': [inv_rec(i) for i in invoices]}, **extra))

    bills = {k: v for k, v in evidence['bills'].items() if not v.get('_void') and not v.get('_invalid')}
    invoices = {k: v for k, v in evidence['invoices'].items() if not v.get('_void') and not v.get('_invalid')}
    credits = {k: v for k, v in evidence['creditnotes'].items() if not v.get('_void') and not v.get('_invalid')}
    for kind in SINGULAR:
        voided = sum(1 for v in evidence[kind].values() if v.get('_void'))
        if voided:
            notes.append(str(voided)+' void '+kind+' ignored')

    # Owner decisions (a reason is mandatory; reason-less decisions are surfaced, not applied).
    def reasoned(rows, label):
        good = []
        for row in rows:
            if str(row.get('reason', '')).strip():
                good.append(row)
            else:
                issue(potential, 'decision without reason', 0, 'Owner '+label+' decision ignored: reason is mandatory.', decision=row)
        return good
    links = reasoned(decisions['links'], 'link')
    overhead = reasoned(decisions['overhead'], 'overhead')
    losses = reasoned(decisions['accepted_losses'], 'accepted-loss')
    over_bills = {str(o['bill_id']): o['reason'] for o in overhead if o.get('bill_id')}
    over_vendors = {str(o['vendor_id']): o['reason'] for o in overhead if o.get('vendor_id')}
    owner_links = {}
    for link in links:
        owner_links.setdefault(str(link.get('bill_id')), set()).add(str(link.get('invoice_id')))

    # Possible duplicate vendor bills double-count cost.
    seen = {}
    for b in bills.values():
        seen.setdefault((str(b.get('vendor_id')), norm(b.get('bill_number'))), []).append(b)
    for (vendor, number), group in seen.items():
        if number and len(group) > 1:
            issue(potential, 'possible duplicate vendor bill', bill_cost(group[0])*(len(group)-1),
                  'Same vendor and bill number recorded '+str(len(group))+' times.', bills=group)

    # Re-issued vendor bill: same vendor, same shipment key, identical line descriptions.
    by_vendor_key = {}
    for b in bills.values():
        for key in bill_keys(b):
            by_vendor_key.setdefault((str(b.get('vendor_id')), key), {})[str(b['bill_id'])] = b
    flagged = set()
    for (vendor, key), group in by_vendor_key.items():
        group = sorted(group.values(), key=lambda b: str(b.get('date')))
        for x in range(len(group)):
            for y in range(x+1, len(group)):
                a, c = group[x], group[y]
                pair = (str(a['bill_id']), str(c['bill_id']))
                lines = lambda d: sorted(norm(l.get('description') or l.get('name')) for l in d.get('line_items') or [])
                credited = dec(a.get('vendor_credits_applied'))+dec(c.get('vendor_credits_applied'))
                cancelled = credited >= min(dec(a.get('total')), dec(c.get('total')))-TOLERANCE
                if pair not in flagged and not cancelled and lines(a) == lines(c) and norm(a.get('bill_number')) != norm(c.get('bill_number')):
                    flagged.add(pair)
                    issue(potential, 'possible re-issued vendor bill booked twice', min(bill_cost(a), bill_cost(c)),
                          'Same vendor and shipment key '+key+' with identical line items under different bill numbers: '
                          'confirm whether this is a second container/charge or a duplicate booking/payment.', bills=[a, c], keys=[key])

    # Classify bills and build shipments (union of bills sharing any key).
    keys_of, parent = {}, {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    key_owner = {}
    for bid, b in bills.items():
        keys = bill_keys(b)
        keys_of[bid] = keys
        if not keys:
            if bid in over_bills or str(b.get('vendor_id')) in over_vendors:
                reason = over_bills.get(bid) or over_vendors.get(str(b.get('vendor_id')))
                excluded.append(dict(bill_rec(b, today), why='owner overhead decision: '+reason))
                continue
            if is_overhead_vendor(b.get('vendor_name')):
                excluded.append(dict(bill_rec(b, today), why='known overhead vendor, no shipment key'))
                continue
            if bid not in owner_links:
                issue(potential, 'no or weak shipment key', bill_cost(b),
                      'In-scope cost by default; no valid MBL/container key. Classify as overhead or link to an invoice.', bills=[b])
                continue
        parent[bid] = bid
        for key in keys:
            if key in key_owner:
                parent[find(bid)] = find(key_owner[key])
            else:
                key_owner[key] = bid
    groups = {}
    for bid in parent:
        groups.setdefault(find(bid), []).append(bid)

    inv_cands = {iid: invoice_candidates(i) for iid, i in invoices.items()}
    cand_index = {}
    for iid, cands in inv_cands.items():
        for c in cands:
            cand_index.setdefault(c, set()).add(iid)
    native = {}  # invoice line bill_id -> invoice
    for iid, inv in invoices.items():
        for line in inv.get('line_items') or []:
            if line.get('bill_id'):
                native.setdefault(str(line['bill_id']), set()).add(iid)

    shipments, near_invoices = [], set()
    for members in groups.values():
        sbills = [bills[m] for m in sorted(members)]
        keys = set().union(*(keys_of[m] for m in members))
        matched, how = set(), {}
        for key in keys:
            for iid in cand_index.get(key, ()):
                matched.add(iid)
                how.setdefault(iid, 'key '+key)
        for b in sbills:
            bid = str(b['bill_id'])
            for iid in native.get(bid, ()):
                matched.add(iid); how.setdefault(iid, 'Zoho invoice line bill_id')
            for line in b.get('line_items') or []:
                if str(line.get('invoice_id') or '') in invoices:
                    matched.add(str(line['invoice_id'])); how.setdefault(str(line['invoice_id']), 'Zoho bill line invoice_id')
            for iid in owner_links.get(bid, ()):
                if iid in invoices:
                    matched.add(iid); how[iid] = 'owner-linked'
        shipments.append({'bills': sbills, 'keys': keys, 'matched': matched, 'how': how})

    inv_shipments = {}
    for n, s in enumerate(shipments):
        for iid in s['matched']:
            inv_shipments.setdefault(iid, set()).add(n)

    # Credit notes: tie to invoices by applied ids, else by text keys.
    credit_by_inv, untied = {}, []
    for cid, cn in credits.items():
        if str(cn.get('status', '')).lower() == 'draft':
            continue
        ids = {str(cn.get('invoice_id') or '')} | {str(x.get('invoice_id')) for x in cn.get('invoices_credited') or [] if x.get('invoice_id')}
        ids = {i for i in ids if i in invoices}
        if not ids:
            cands = invoice_candidates(cn)
            hits = {n for n, s in enumerate(shipments) if s['keys'] & cands}
            if len(hits) == 1:
                shipments[hits.pop()].setdefault('credits', []).append(cn)
                continue
            untied.append(cn)
            continue
        credit_by_inv.setdefault(sorted(ids)[0], []).append(cn)
    for cn in untied:
        issue(potential, 'untied credit note', dec(cn.get('sub_total'))*rate(cn),
              'Credit note '+str(cn.get('creditnote_number'))+' ('+str(cn.get('customer_name'))+') not tied to any invoice or shipment.')

    for n, s in enumerate(shipments):
        sbills, keys = s['bills'], s['keys']
        cost = sum((bill_cost(b) for b in sbills), Decimal(0))
        all_inv = [invoices[i] for i in sorted(s['matched'])]
        billed = [i for i in all_inv if str(i.get('status', '')).lower() in BILLED_INVOICE]
        drafts = [i for i in all_inv if str(i.get('status', '')).lower() == 'draft']
        other = [i for i in all_inv if i not in billed and i not in drafts]
        ages = [days(today, b.get('date')) for b in sbills]
        age = max(a for a in ages if a is not None) if any(a is not None for a in ages) else 0
        flags = []
        if any(b.get('is_inclusive_tax') for b in sbills):
            flags.append('tax-inclusive bill: cost may include GST')
        if any(str(b.get('status', '')).lower() == 'draft' for b in sbills):
            flags.append('includes draft vendor bill')
        if other:
            flags.append('invoice status not recognised: '+', '.join(sorted({str(i.get('status')) for i in other})))
        common = {'bills': sbills, 'invoices': all_inv, 'keys': keys, 'flags': flags,
                  'matched_by': {iid: s['how'][iid] for iid in sorted(s['matched'])}}

        customers = {str(i.get('customer_id')) for i in billed+drafts}
        if len(customers) > 1:
            issue(potential, 'multi-customer shipment (1:1 violation)', cost, 'Invoices to '+str(len(customers))+' customers match this shipment.', **common)
            continue
        multi = [i for i in all_inv if len(inv_shipments.get(str(i['invoice_id']), ())) > 1]
        if multi:
            issue(potential, 'invoice matches several shipments (1:1 violation)', cost,
                  'Invoice(s) '+', '.join(str(i.get('invoice_number')) for i in multi)+' match more than one shipment.', **common)
            continue

        if not billed:
            close = {}
            for key in keys:
                for iid, cands in inv_cands.items():
                    hits = near(key, cands)
                    if hits and str(invoices[iid].get('status', '')).lower() in BILLED_INVOICE | {'draft'}:
                        close.setdefault(iid, set()).update(hits)
            if drafts:
                issue(potential, 'draft invoice only', cost, 'Matching invoice exists only as draft; not billing.', **common)
            elif close:
                near_invoices.update(close)
                common['invoices'] = [invoices[i] for i in sorted(close)]
                issue(potential, 'uncertain near match', cost, 'No exact invoice; near key match(es) need owner confirmation.',
                      near_matches={i: sorted(v) for i, v in close.items()}, **common)
            elif age >= LEAK_DAYS:
                issue(confirmed, 'UNBILLED', cost, 'Shipment cost with valid key and no customer invoice after '+str(age)+' days.', **common)
            else:
                issue(potential, 'awaiting invoice', cost, 'Day '+str(age)+' of '+str(LEAK_DAYS)+'; escalates to UNBILLED at day '+str(LEAK_DAYS)+'.', **common)
            continue

        revenue = sum((dec(i.get('sub_total'))*rate(i) for i in billed), Decimal(0))
        tied = [cn for i in billed for cn in credit_by_inv.get(str(i['invoice_id']), [])]+s.get('credits', [])
        revenue -= sum((dec(cn.get('sub_total'))*rate(cn) for cn in tied), Decimal(0))
        shortfall = cost-revenue
        last_invoice = max(str(i.get('date')) for i in billed)
        late = [b for b in sbills if str(b.get('date')) > last_invoice]
        late_aged = [b for b in late if (days(today, b.get('date')) or 0) >= LEAK_DAYS]
        late_cost = sum((bill_cost(b) for b in late_aged), Decimal(0))
        fingerprint = hashlib.sha256(json.dumps(sorted(
            [['b', str(b['bill_id']), q(bill_cost(b))] for b in sbills] +
            [['i', str(i['invoice_id']), str(i.get('sub_total'))] for i in billed] +
            [['c', str(c['creditnote_id']), str(c.get('sub_total'))] for c in tied])).encode()).hexdigest()[:16]
        common.update(cost_inr=q(cost), revenue_inr=q(revenue), fingerprint=fingerprint)

        if shortfall > TOLERANCE:
            loss = next((l for l in losses if l.get('fingerprint') == fingerprint), None)
            if late_aged:
                issue(confirmed, 'LATE COST NOT RE-BILLED', min(late_cost, shortfall),
                      'Vendor cost dated after the last invoice ('+last_invoice+'); shipment is below cost by '+q(shortfall)+'.', **common)
            elif late:
                issue(potential, 'awaiting invoice', min(sum((bill_cost(b) for b in late), Decimal(0)), shortfall),
                      'Late vendor cost under '+str(LEAK_DAYS)+' days old; shipment below cost.', **common)
            elif loss:
                issue(accepted, 'accepted commercial loss', shortfall, 'Owner: '+loss['reason'], **common)
            else:
                stale = [l for l in losses if l.get('shipment_keys') and set(l['shipment_keys']) & keys]
                issue(potential, 'billed below cost', shortfall,
                      'Revenue '+q(revenue)+' < cost '+q(cost)+'.'+(' Earlier accepted-loss decision re-opened: records changed.' if stale else ''), **common)
        elif late_aged:
            issue(potential, 'late cost while margin still positive', late_cost, 'Vendor cost dated after the last invoice; margin remains positive.', **common)

        vendor_paid = sum((bill_gross_cost(b)*cash_paid_fraction(b) for b in sbills), Decimal(0))
        # A credit note applied to an invoice clears its balance but is not cash from the customer.
        received = sum((dec(i.get('sub_total'))*rate(i)*paid_fraction(i) for i in billed), Decimal(0))
        received = max(Decimal(0), received-sum((dec(cn.get('sub_total'))*rate(cn) for cn in tied), Decimal(0)))
        past_due = [i for i in billed if dec(i.get('balance')) > 0 and (str(i.get('status')).lower() == 'overdue' or
                    (i.get('due_date') and str(i['due_date']) < str(today)))]
        if past_due and vendor_paid-received > TOLERANCE:
            issue(confirmed, 'UNRECOVERED VENDOR PAYMENT', vendor_paid-received,
                  'Vendor paid '+q(vendor_paid)+' vs customer received '+q(received)+'; invoice past due.', **common)
        elif past_due:
            issue(potential, 'overdue receivable (vendor not yet paid)', sum((dec(i.get('balance'))*rate(i) for i in past_due), Decimal(0)),
                  'Customer invoice past due.', **common)

    # Unused vendor credit: money the vendor owes back (e.g. an overpayment against a cancelled bill).
    unused = {}
    for b in bills.values():
        amt = dec(b.get('unused_credits_payable_amount'))
        vid, seen_b = str(b.get('vendor_id')), unused.get(str(b.get('vendor_id')), (None, None))[1]
        # Vendor-level figure: trust the most recently modified bill (cached bills can be older).
        if seen_b is None or str(b.get('last_modified_time', '')) > str(seen_b.get('last_modified_time', '')):
            unused[vid] = (amt, b)
    unused = {v: x for v, x in unused.items() if x[0] > TOLERANCE}
    for vendor, (amt, b) in sorted(unused.items()):
        issue(potential, 'unused vendor credit (vendor owes us)', amt,
              str(b.get('vendor_name'))+' holds an unapplied credit (as of bill '+str(b.get('bill_number'))+', modified '+str(b.get('last_modified_time', ''))[:10]+'). '
              'It may be an advance or overpayment not yet applied to open bills, or money owed back: verify in Zoho.', bills=[b])

    # Billing with no recorded vendor cost: not leakage, but the shipment's cost evidence is missing.
    for iid, inv in sorted(invoices.items()):
        if iid not in inv_shipments and iid not in near_invoices and str(inv.get('status', '')).lower() in BILLED_INVOICE and str(inv.get('date')) >= since:
            issue(potential, 'invoice with no vendor cost recorded', 0,
                  'Customer billed but no vendor bill carries this shipment key; cost may be unbooked or keyless.', invoices=[inv])

    # Deduplicated confirmed exposure: one shipment can carry several confirmed findings.
    per_shipment = {}
    for item in confirmed:
        sid = ','.join(sorted(b['bill_id'] for b in item['bills']))
        per_shipment[sid] = max(per_shipment.get(sid, Decimal(0)), Decimal(item['exposure_inr']))
    status = 'INCOMPLETE' if incomplete_reasons else 'COMPLETE'
    total = lambda rows: q(sum((Decimal(r['exposure_inr']) for r in rows), Decimal(0)))
    return {
        'status': status, 'incomplete_reasons': list(incomplete_reasons), 'as_of': str(today),
        'evidence_counts': {k: len(v) for k, v in evidence.items()},
        'shipments': len(shipments),
        'totals_inr': {'confirmed_deduplicated': q(sum(per_shipment.values(), Decimal(0))), 'confirmed_gross': total(confirmed),
                       'potential': total(potential), 'accepted_losses': total(accepted),
                       'excluded_overhead': q(sum((Decimal(e['cost_inr']) for e in excluded), Decimal(0)))},
        'confirmed_leakage': sorted(confirmed, key=lambda r: -Decimal(r['exposure_inr'])),
        'potential_issues': sorted(potential, key=lambda r: -Decimal(r['exposure_inr'])),
        'accepted_losses': accepted, 'excluded_overhead': excluded, 'notes': notes,
    }


def summary(report):
    t = report['totals_inr']
    lines = ['Leakage reconciliation '+report['as_of']+' — '+report['status'],
             'Evidence: '+', '.join(k+' '+str(v) for k, v in report['evidence_counts'].items())+'; shipments '+str(report['shipments'])]
    if report['status'] != 'COMPLETE':
        lines.append('NOT CLEAN-CERTIFIABLE. Reasons: '+'; '.join(report['incomplete_reasons'][:10]))
    lines += ['Confirmed leakage INR '+t['confirmed_deduplicated']+' (gross '+t['confirmed_gross']+', '+str(len(report['confirmed_leakage']))+' items)',
              'Potential issues INR '+t['potential']+' ('+str(len(report['potential_issues']))+' items)',
              'Accepted losses INR '+t['accepted_losses']+' ('+str(len(report['accepted_losses']))+')',
              'Excluded overhead INR '+t['excluded_overhead']+' ('+str(len(report['excluded_overhead']))+' bills)']
    by_type = {}
    for section in ('confirmed_leakage', 'potential_issues'):
        for item in report[section]:
            key = (section, item['type'])
            n, amt = by_type.get(key, (0, Decimal(0)))
            by_type[key] = (n+1, amt+Decimal(item['exposure_inr']))
    for (section, kind), (n, amt) in sorted(by_type.items()):
        lines.append('  '+('CONFIRMED ' if section == 'confirmed_leakage' else 'potential ')+kind+': '+str(n)+' / INR '+q(amt))
    return '\n'.join(lines)


def run_once(out, cache_dir=None, decisions_path=None, budget=1500, today=None, since=FY_START, env=None, client=None):
    out = Path(out)
    if out.exists():
        raise FileExistsError('Report files are immutable; choose a new --out path.')
    today = today or datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    client = client or ReadOnlyZoho(load_config(env), budget=budget)
    evidence, reasons, calls = collect(client, Cache(cache_dir), since)
    report = reconcile(evidence, today, load_decisions(decisions_path), reasons, since)
    report.update(api_calls=calls, bills_since=since, generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, sort_keys=True))
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description='Read-only vendor-cost -> customer-invoice leakage reconciliation.')
    p.add_argument('--once', action='store_true', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--cache')
    p.add_argument('--decisions')
    p.add_argument('--budget', type=int, default=1500)
    p.add_argument('--since', default=FY_START)
    p.add_argument('--today')
    a = p.parse_args(argv)
    try:
        report = run_once(a.out, a.cache, a.decisions, a.budget, date.fromisoformat(a.today) if a.today else None, a.since)
    except (ConfigError, FileExistsError) as exc:
        print('Refused: '+str(exc), file=sys.stderr)
        return 2
    print(summary(report))
    return 0 if report['status'] == 'COMPLETE' else 1


if __name__ == '__main__':
    sys.exit(main())
