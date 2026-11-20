"""Leakage monitor for the Books app: scheduled read-only runs, owner decisions, email, and the /leakage page.

Read-only towards Zoho (see leakage.py). Writes only to the app's own SQLite database.
Email goes out only when RESEND_API_KEY, LEAKAGE_EMAIL_TO and LEAKAGE_EMAIL_FROM are all set.
"""
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import leakage as L

IST = timezone(timedelta(hours=5, minutes=30))
STATIC = Path(__file__).with_name('static')
STATIC_FILES = {
    'sattva-swirl-white.svg': 'image/svg+xml',
    'fonts/poppins-latin-400-normal.woff2': 'font/woff2',
    'fonts/poppins-latin-500-normal.woff2': 'font/woff2',
    'fonts/poppins-latin-600-normal.woff2': 'font/woff2',
    'fonts/poppins-latin-700-normal.woff2': 'font/woff2',
    'fonts/dm-mono-latin-400-normal.woff2': 'font/woff2',
    'fonts/dm-mono-latin-500-normal.woff2': 'font/woff2',
}
# Which owner decision each finding type accepts on the page.
ACTIONS = {
    'UNBILLED': ('write_off', 'link'),
    'awaiting invoice': ('write_off', 'link'),
    'uncertain near match': ('link', 'write_off'),
    'draft invoice only': ('write_off',),
    'no or weak shipment key': ('overhead', 'link', 'write_off'),
    'LATE COST NOT RE-BILLED': ('accept_loss',),
    'billed below cost': ('accept_loss',),
    'late cost while margin still positive': ('accept_loss',),
    'UNRECOVERED VENDOR PAYMENT': ('settled',),
    'overdue receivable (vendor not yet paid)': ('settled',),
}
PLAIN = {
    'UNBILLED': 'Cost not billed to any customer',
    'LATE COST NOT RE-BILLED': 'Late cost not re-billed',
    'UNRECOVERED VENDOR PAYMENT': 'Vendor paid, customer has not',
    'awaiting invoice': 'Awaiting invoice (under 7 days)',
    'no or weak shipment key': 'No MBL or container on the bill',
    'uncertain near match': 'Possible match — confirm',
    'draft invoice only': 'Invoice still in draft',
    'billed below cost': 'Priced below cost',
    'late cost while margin still positive': 'Cost booked 14+ days after invoice, still in margin',
    'overdue receivable (vendor not yet paid)': 'Customer overdue',
    'unused vendor credit (vendor owes us)': 'Unapplied credit with vendor',
    'invoice with no vendor cost recorded': 'Invoice with no vendor cost',
    'possible duplicate vendor bill': 'Possible duplicate vendor bill',
    'possible re-issued vendor bill booked twice': 'Vendor bill possibly booked twice',
    'untied credit note': 'Credit note not tied to an invoice',
    'multi-customer shipment (1:1 violation)': 'One shipment, several customers',
    'invoice matches several shipments (1:1 violation)': 'One invoice, several shipments',
    'books: receivable settled per owner, still open in Zoho': 'Settled per you, open in Zoho',
    'decision without reason': 'Decision missing a reason',
    'owner link to unknown invoice': 'Link to an unknown invoice',
    'accepted commercial loss': 'Accepted loss',
    'written off (unbilled)': 'Written off',
}
FIX_IN_ZOHO = {
    'unused vendor credit (vendor owes us)': 'Apply this credit to the bill it paid, or recover it from the vendor.',
    'invoice with no vendor cost recorded': 'Book the vendor bill, with the MBL in Reference#.',
    'possible duplicate vendor bill': 'Check both bills against the vendor invoice; void the extra one.',
    'possible re-issued vendor bill booked twice': 'Check for a vendor credit note; apply it to the cancelled bill.',
    'untied credit note': 'Apply the credit note to its invoice in Zoho.',
    'books: receivable settled per owner, still open in Zoho': 'Record the settlement against the invoice in Zoho.',
    'multi-customer shipment (1:1 violation)': 'Check which customer the shipment belongs to.',
    'invoice matches several shipments (1:1 violation)': 'Split the invoice or put one MBL in its Reference#.',
}


# Order of "needs your call" groups: what you can act on and what costs money first; noise last.
PRIORITY = ['possible re-issued vendor bill booked twice', 'possible duplicate vendor bill', 'billed below cost',
            'unused vendor credit (vendor owes us)', 'overdue receivable (vendor not yet paid)', 'uncertain near match', 'draft invoice only',
            'awaiting invoice', 'invoice with no vendor cost recorded', 'books: receivable settled per owner, still open in Zoho',
            'no or weak shipment key', 'untied credit note', 'multi-customer shipment (1:1 violation)',
            'invoice matches several shipments (1:1 violation)', 'owner link to unknown invoice', 'decision without reason',
            'late cost while margin still positive']
WHY = {
    'billed below cost': 'All costs were known when you invoiced, and the invoice is lower. Usually a deliberate rate; accept the loss or rebill.',
    'unused vendor credit (vendor owes us)': 'Money sitting with the vendor, unapplied in Zoho.',
    'overdue receivable (vendor not yet paid)': 'Customer late; you have not paid the vendor yet either.',
    'invoice with no vendor cost recorded': 'Billed, but no carrier bill booked in Zoho.',
    'no or weak shipment key': 'Mark overhead, link an invoice, or write off.',
    'late cost while margin still positive': 'Vendor bill dated over 14 days after the invoice; still profitable. Check it was re-billed.',
    'books: receivable settled per owner, still open in Zoho': 'Settled per you; Zoho still shows it open.',
    'possible re-issued vendor bill booked twice': 'Same lines, different bill numbers.',
    'possible duplicate vendor bill': 'Same vendor and bill number twice.',
    'awaiting invoice': 'Inside the 7-day window.',
}


def env_enabled(env=None):
    env = os.environ if env is None else env
    return bool(env.get('ZOHO_RECON_REFRESH_TOKEN')) and env.get('LEAKAGE_ENABLED', '1') != '0'


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.execute('PRAGMA journal_mode=WAL')
    return db


class Monitor:
    def __init__(self, db_path, data_dir, env=None, client_factory=None, sender=None, now=None):
        self.db_path, self.env = db_path, (os.environ if env is None else env)
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir/'leakage_cache'
        self.client_factory = client_factory or (lambda: L.ReadOnlyZoho(L.load_config(self.env), budget=self.budget()))
        self.sender = sender or resend_send
        self.now = now or (lambda: datetime.now(IST))
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.thread = None
        with connect(db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS leakage_runs (id INTEGER PRIMARY KEY, trigger TEXT NOT NULL, started_at TEXT NOT NULL, '
                       'finished_at TEXT, status TEXT NOT NULL, report_json TEXT, email_status TEXT, error TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS leakage_decisions (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL, '
                       'reason TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)')
            # A run interrupted by a restart must never look finished.
            db.execute("UPDATE leakage_runs SET status='INCOMPLETE', finished_at=?, error='Interrupted by a restart' WHERE status='RUNNING'",
                       (self.now().isoformat(),))

    def budget(self):
        try:
            return max(50, min(5000, int(self.env.get('LEAKAGE_BUDGET', '1500'))))
        except ValueError:
            return 1500

    def daily_hour(self):
        try:
            return max(0, min(23, int(self.env.get('LEAKAGE_DAILY_HOUR_IST', '8'))))
        except ValueError:
            return 8

    # ---------------------------------------------------------- scheduling
    def start(self):
        self.thread = threading.Thread(target=self._loop, name='leakage-monitor', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()

    def _loop(self):
        while not self.stop.wait(30):
            try:
                if self.due():
                    self.run('daily')
            except Exception as exc:  # never kill the web app
                print('leakage monitor error:', type(exc).__name__, flush=True)

    def due(self):
        now = self.now()
        if now.hour < self.daily_hour():
            return False
        day = now.date().isoformat()
        with connect(self.db_path) as db:
            row = db.execute("SELECT 1 FROM leakage_runs WHERE trigger='daily' AND substr(started_at,1,10)=? LIMIT 1", (day,)).fetchone()
        return row is None

    def running(self):
        return self.lock.locked()

    def run_now(self):
        if self.running():
            return False
        threading.Thread(target=self.run, args=('manual',), daemon=True).start()
        return True

    # ---------------------------------------------------------- runs
    def run(self, trigger):
        if not self.lock.acquire(blocking=False):
            return None
        try:
            started = self.now()
            with connect(self.db_path) as db:
                run_id = db.execute('INSERT INTO leakage_runs(trigger, started_at, status) VALUES (?,?,?)',
                                    (trigger, started.isoformat(), 'RUNNING')).lastrowid
            error = None
            try:
                client = self.client_factory()
                evidence, reasons, calls = L.collect(client, L.Cache(self.cache_dir), L.FY_START)
                report = L.reconcile(evidence, started.date(), self.decisions(), reasons)
                report['api_calls'] = calls
                self._save_evidence(evidence, reasons, calls, started)
            except L.ConfigError as exc:
                error = str(exc)
                report = self._failed_report(started, 'Configuration: '+error)
            except Exception as exc:
                error = type(exc).__name__+': '+str(exc)[:200]
                report = self._failed_report(started, error)
            report['generated_at'] = self.now().isoformat(timespec='seconds')
            email_status = self.email(report)
            with connect(self.db_path) as db:
                db.execute('UPDATE leakage_runs SET finished_at=?, status=?, report_json=?, email_status=?, error=? WHERE id=?',
                           (self.now().isoformat(), report['status'], json.dumps(report), email_status, error, run_id))
            return run_id
        finally:
            self.lock.release()

    def _save_evidence(self, evidence, reasons, calls, started):
        """Keep the last Zoho read so a decision can be applied at once, without reading Zoho again."""
        path = Path(self.data_dir)/'leakage-evidence.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'evidence': evidence, 'reasons': list(reasons), 'calls': calls,
                                   'read_at': started.isoformat(timespec='seconds')}, default=str))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def reapply(self):
        """Re-run the reconciliation on the last Zoho read with the current decisions. No Zoho calls, no email."""
        path = Path(self.data_dir)/'leakage-evidence.json'
        if not path.exists() or not self.lock.acquire(blocking=False):
            return False
        try:
            saved = json.loads(path.read_text())
            read_at = datetime.fromisoformat(saved['read_at'])
            report = L.reconcile(saved['evidence'], read_at.date(), self.decisions(), saved['reasons'])
            report.update(api_calls=saved['calls'], read_at=saved['read_at'], generated_at=self.now().isoformat(timespec='seconds'))
            stamp = self.now().isoformat()
            with connect(self.db_path) as db:
                db.execute('INSERT INTO leakage_runs(trigger, started_at, finished_at, status, report_json, email_status) VALUES (?,?,?,?,?,?)',
                           ('decision', stamp, stamp, report['status'], json.dumps(report), 'not sent (decision applied)'))
            return True
        finally:
            self.lock.release()

    @staticmethod
    def _failed_report(started, reason):
        empty = {k: [] for k in ('confirmed_leakage', 'potential_issues', 'accepted_losses', 'excluded_overhead', 'notes')}
        return dict(empty, status='INCOMPLETE', incomplete_reasons=[reason], as_of=str(started.date()), shipments=0,
                    evidence_counts={'bills': 0, 'invoices': 0, 'creditnotes': 0},
                    totals_inr={'confirmed_deduplicated': '0.00', 'confirmed_gross': '0.00', 'potential': '0.00',
                                'accepted_losses': '0.00', 'excluded_overhead': '0.00'})

    def runs(self, limit=8):
        with connect(self.db_path) as db:
            return db.execute('SELECT id, trigger, started_at, finished_at, status, email_status, error FROM leakage_runs '
                              'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()

    def latest_report(self):
        with connect(self.db_path) as db:
            row = db.execute("SELECT report_json, started_at, email_status FROM leakage_runs WHERE report_json IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1").fetchone()
        return (json.loads(row[0]), row[1], row[2]) if row else (None, None, None)

    def previous_report(self):
        """The last complete report before the latest one, for "new since last check"."""
        with connect(self.db_path) as db:
            rows = db.execute("SELECT report_json FROM leakage_runs WHERE report_json IS NOT NULL AND trigger != 'decision' ORDER BY id DESC LIMIT 20").fetchall()
        for (raw,) in rows[1:]:
            report = json.loads(raw)
            if report.get('status') == 'COMPLETE':
                return report
        return None

    # ---------------------------------------------------------- decisions
    def decisions(self):
        out = {k: [] for k in L.DECISION_KEYS}
        with connect(self.db_path) as db:
            for kind, payload, reason in db.execute('SELECT kind, payload_json, reason FROM leakage_decisions WHERE revoked_at IS NULL ORDER BY id'):
                if kind in out:
                    out[kind].append(dict(json.loads(payload), reason=reason))
        return out

    def decision_rows(self):
        with connect(self.db_path) as db:
            return db.execute('SELECT id, kind, payload_json, reason, created_at FROM leakage_decisions WHERE revoked_at IS NULL ORDER BY id DESC').fetchall()

    def decide(self, action, form):
        """Validate an owner decision from the page and store it. Returns the number of rows stored."""
        reason = ' '.join(str(form.get('reason', '')).split())
        if not 3 <= len(reason) <= 500:
            raise ValueError('Give a reason (3–500 characters). It is kept with the decision.')
        ids = [x for x in re.split(r'[\s,]+', str(form.get('ids', ''))) if x]
        if any(not re.fullmatch(r'[0-9]{1,24}', x) for x in ids) or len(ids) > 50:
            raise ValueError('The records on this decision are not valid. Reload the page.')
        rows = []
        if action == 'write_off':
            rows = [('write_offs', {'bill_id': i}) for i in ids]
        elif action == 'settled':
            rows = [('receivable_settled', {'invoice_id': i}) for i in ids]
        elif action == 'overhead':
            if form.get('scope') == 'vendor':
                vendor = str(form.get('vendor_id', ''))
                if not re.fullmatch(r'[0-9]{1,24}', vendor):
                    raise ValueError('Vendor not recognised. Reload the page.')
                rows = [('overhead', {'vendor_id': vendor})]
            else:
                rows = [('overhead', {'bill_id': i}) for i in ids]
        elif action == 'link':
            number = str(form.get('invoice_number', '')).strip()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/\-_. ]{1,40}', number):
                raise ValueError('Enter the Zoho invoice number to link, for example TISGLSE2026042.')
            rows = [('links', {'bill_id': i, 'invoice_number': number}) for i in ids]
        elif action == 'accept_loss':
            fingerprint = str(form.get('fingerprint', ''))
            if not re.fullmatch(r'[0-9a-f]{16}', fingerprint):
                raise ValueError('This shipment changed since the page loaded. Reload and decide again.')
            keys = [k for k in str(form.get('keys', '')).split(',') if re.fullmatch(r'[A-Z0-9]{1,40}', k)]
            rows = [('accepted_losses', {'fingerprint': fingerprint, 'shipment_keys': keys})]
        else:
            raise ValueError('Unknown decision.')
        if not rows:
            raise ValueError('Nothing to decide on. Reload the page.')
        stamp = self.now().isoformat(timespec='seconds')
        with connect(self.db_path) as db:
            for kind, payload in rows:
                db.execute('INSERT INTO leakage_decisions(kind, payload_json, reason, created_at) VALUES (?,?,?,?)',
                           (kind, json.dumps(payload, sort_keys=True), reason, stamp))
        return len(rows)

    def revoke(self, decision_id):
        with connect(self.db_path) as db:
            return db.execute('UPDATE leakage_decisions SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                              (self.now().isoformat(timespec='seconds'), int(decision_id))).rowcount

    # ---------------------------------------------------------- email
    def email(self, report):
        key, to, sender = (self.env.get(k, '') for k in ('RESEND_API_KEY', 'LEAKAGE_EMAIL_TO', 'LEAKAGE_EMAIL_FROM'))
        if not (key and to and sender):
            return 'not configured'
        t = report['totals_inr']
        subject = ('Leakage '+report['as_of']+' · '+report['status']+' · confirmed ₹'+inr(t['confirmed_deduplicated']))
        lines = [L.summary(report), '']
        for item in report['confirmed_leakage'][:15]:
            lines.append('- ₹'+inr(item['exposure_inr'])+'  '+PLAIN.get(item['type'], item['type'])+'  '+', '.join(item['shipment_keys'][:3]))
        url = self.env.get('LEAKAGE_PUBLIC_URL', 'https://books.sattvaglobal.in/leakage')
        lines += ['', 'Open the ledger: '+url]
        try:
            self.sender(key, sender, [x.strip() for x in to.split(',') if x.strip()], subject, '\n'.join(lines))
            return 'sent'
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(300).decode('utf8', 'replace')
            except Exception:
                detail = ''
            return ('failed: HTTP '+str(exc.code)+' '+' '.join(detail.split()))[:200]
        except Exception as exc:
            return ('failed: '+type(exc).__name__+' '+str(exc))[:200]


def resend_send(key, sender, to, subject, text):
    body = json.dumps({'from': sender, 'to': to, 'subject': subject, 'text': text}).encode()
    req = urllib.request.Request('https://api.resend.com/emails', data=body, method='POST',
                                 headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json', 'User-Agent': 'sattva-books/1.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status >= 300:
            raise RuntimeError('Resend HTTP '+str(response.status))


# ============================================================== rendering

def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def inr(value, paise=False):
    d = Decimal(str(value or 0))
    neg, d = d < 0, abs(d)
    whole, frac = divmod(d.quantize(Decimal('0.01')), 1)
    s = str(int(whole))
    if len(s) > 3:
        s = _group(s[:-3])+','+s[-3:]
    out = s+('.'+str(frac)[2:].ljust(2, '0') if paise else '')
    return ('-' if neg else '')+out


def _group(head):
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ','.join(parts)


def ist(value):
    try:
        return datetime.fromisoformat(str(value)).astimezone(IST).strftime('%d %b %Y, %H:%M')
    except ValueError:
        return str(value or '')


BASE_CSS = r'''
@font-face{font-family:Poppins;font-weight:400;font-display:swap;src:url(/static/fonts/poppins-latin-400-normal.woff2) format('woff2')}
@font-face{font-family:Poppins;font-weight:500;font-display:swap;src:url(/static/fonts/poppins-latin-500-normal.woff2) format('woff2')}
@font-face{font-family:Poppins;font-weight:600;font-display:swap;src:url(/static/fonts/poppins-latin-600-normal.woff2) format('woff2')}
@font-face{font-family:Poppins;font-weight:700;font-display:swap;src:url(/static/fonts/poppins-latin-700-normal.woff2) format('woff2')}
@font-face{font-family:'DM Mono';font-weight:400;font-display:swap;src:url(/static/fonts/dm-mono-latin-400-normal.woff2) format('woff2')}
@font-face{font-family:'DM Mono';font-weight:500;font-display:swap;src:url(/static/fonts/dm-mono-latin-500-normal.woff2) format('woff2')}
:root{--navy:#030722;--navy-2:#070d36;--blue:#024aab;--electric:#1f7cff;--sky:#5cb6f9;--pale:#e8f5ff;--ink:#111a33;--slate:#4a5875;
--line:#ccd9e8;--paper:#fff;--wash:#f4f9ff;--loss:#c62828;--ok:#0f7a4a;--pad:max(24px,calc((100vw - 1320px)/2));
--accent:#cfe8ff;--sans:Poppins,system-ui,-apple-system,'Segoe UI',sans-serif;--mono:'DM Mono',ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:400 15px/1.65 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--blue)}:focus-visible{outline:3px solid var(--sky);outline-offset:3px}
.bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:24px;min-height:88px;padding:0 var(--pad);background:var(--navy);border-bottom:1px solid rgba(255,255,255,.1)}
.brand{display:flex;align-items:center;gap:14px;color:#fff;text-decoration:none;font-weight:700;font-size:19px;letter-spacing:-.02em;white-space:nowrap}
.mark{width:56px;height:56px;overflow:hidden;display:block;flex:0 0 56px}.mark img{height:56px;width:auto;max-width:none;display:block}
.brand small{font:500 11px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--sky);border-left:1px solid rgba(255,255,255,.25);padding-left:14px}
.nav{display:flex;align-items:center;gap:28px}.nav a{color:rgba(255,255,255,.82);text-decoration:none;font-size:14px;padding:10px 0;border-bottom:2px solid transparent}
.nav a[aria-current]{color:#fff;border-color:var(--electric)}.nav form{margin:0}
.bar>form button,.nav button{background:transparent;color:rgba(255,255,255,.82);border:1px solid rgba(255,255,255,.25);border-radius:2px;font:500 13px var(--sans);min-height:40px;padding:0 14px;cursor:pointer}
@media (max-width:640px){.bar{flex-wrap:wrap;min-height:0;padding-top:12px;padding-bottom:0;gap:4px 12px}.mark{width:44px;height:44px;flex-basis:44px}.mark img{height:44px}
.brand{font-size:15px;gap:10px}.brand small{display:none}.nav{order:3;width:100%;gap:22px;border-top:1px solid rgba(255,255,255,.1)}.nav a{font-size:14px;padding:12px 0}
.bar>form button{min-height:36px;padding:0 10px;font-size:12px}
}
'''

CSS = BASE_CSS + r'''
.hero{background:var(--navy);color:#fff;padding:clamp(26px,3.4vw,40px) var(--pad) clamp(22px,2.8vw,32px);display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px 48px;align-items:end}
.state{display:flex;flex-direction:column;align-items:flex-end;gap:10px;text-align:right}
.state .line{font:500 12px/1.5 var(--mono);letter-spacing:.06em;color:rgba(255,255,255,.75)}
.state .line b{font-weight:500;color:#7fd3a8}.state .line b.bad{color:#ff8a80}
.state form{display:flex;align-items:center;gap:12px;margin:0}
.evidence{grid-column:1/-1;border-top:1px solid rgba(92,182,249,.25);padding-top:10px}
.evidence>summary{list-style:none;cursor:pointer;font:500 11px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--sky);display:inline-block;padding:6px 0}
.evidence>summary::-webkit-details-marker{display:none}.evidence>summary::after{content:' +'}.evidence[open]>summary::after{content:' −'}
.evidence dl{display:flex;flex-wrap:wrap;gap:8px 32px;margin:10px 0 4px}.evidence dt{font-size:12px;color:rgba(255,255,255,.6)}.evidence dd{margin:0;font-weight:600}
.evidence p{margin:6px 0 4px;font-size:13px;color:rgba(255,255,255,.8);max-width:760px}
.reasons{grid-column:1/-1;margin:0;padding:12px 14px;border-left:3px solid #ff8a80;background:rgba(255,138,128,.08);font-size:13px;color:#ffd6d2}
.eyebrow{font:500 11px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--electric)}
.hero .eyebrow{color:var(--sky)}
h1{margin:10px 0 0;font-weight:700;font-size:clamp(1.9rem,3.6vw,3.25rem);line-height:1.04;letter-spacing:-.04em}
h1 .money{color:#ff6b5e}h1 .calm{color:#7fd3a8}
.lede{margin:22px 0 0;max-width:620px;font-size:clamp(16px,1.6vw,19px);line-height:1.6;color:rgba(255,255,255,.82)}
.register{border:1px solid rgba(92,182,249,.45);background:rgba(7,13,54,.6)}
.register header{display:flex;justify-content:space-between;gap:12px;padding:14px 16px;border-bottom:1px solid rgba(92,182,249,.3);font:500 11px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--sky)}
.register dl{display:grid;grid-template-columns:1fr 1fr;margin:0}
.register dl div{padding:14px 16px;border-bottom:1px solid rgba(92,182,249,.2)}.register dl div:nth-child(odd){border-right:1px solid rgba(92,182,249,.2)}
.register dt{font-size:12px;color:rgba(255,255,255,.6)}.register dd{margin:2px 0 0;font-weight:600;font-size:15px}
.register .verdict{padding:14px 16px;border-left:3px solid var(--sky);font-size:13px;line-height:1.55;color:rgba(255,255,255,.85)}
.register .verdict.bad{border-left-color:#ff6b5e}
.register form{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px;margin:0;border-top:1px solid rgba(92,182,249,.3)}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:46px;padding:0 20px;border-radius:2px;border:1px solid transparent;font:600 14px var(--sans);cursor:pointer;text-decoration:none}
.btn-light{background:#fff;color:var(--navy)}.btn-light:hover{background:var(--pale)}
.btn-primary{background:var(--electric);color:#fff}.btn-primary:hover{background:#0f63d8}
.btn-quiet{background:#fff;color:var(--blue);border-color:var(--line)}.btn-quiet:hover{border-color:var(--blue)}
.btn[disabled]{opacity:.55;cursor:progress}
.hint{font-size:12px;color:rgba(255,255,255,.6)}
.strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:var(--blue);color:#fff;padding:0 var(--pad)}
.strip div{padding:16px 24px;border-left:1px solid rgba(255,255,255,.22)}.strip div:first-child{border-left:0;padding-left:0}
.strip b{display:block;font-size:clamp(20px,2vw,26px);font-weight:700;letter-spacing:-.03em;line-height:1.1}
.strip span{font-size:13px;color:rgba(255,255,255,.8)}
.filters{z-index:4;background:#fff;border-bottom:1px solid var(--line);padding:12px var(--pad) 8px;box-shadow:0 6px 18px -14px rgba(3,7,34,.35)}
@media (min-width:1100px){.filters{position:sticky;top:88px}}
.filters *{min-width:0}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;max-width:100%;text-align:left;border:1px solid var(--line);border-radius:2px;background:#fff;color:var(--ink);padding:6px 10px;font:500 13px/1.3 var(--sans);cursor:pointer}
.chip .l{flex:0 1 auto;overflow-wrap:anywhere}.filters .chip .c,.filters .chip .s{flex:0 0 auto;min-width:auto}.chip .c{font:500 12px var(--mono);color:var(--slate)}.chip .s{font:500 12px var(--mono);color:var(--ink);white-space:nowrap}
.chip.red{border-left:3px solid var(--loss)}.chip.red .s{color:#b71c1c}.chip.new{border-color:var(--electric);color:var(--blue)}
.chip:hover{border-color:var(--electric)}.chip[aria-pressed=true]{background:var(--navy);border-color:var(--navy);color:#fff}.chip[aria-pressed=true] .c,.chip[aria-pressed=true] .s{color:var(--sky)}
.frow{display:flex;flex-wrap:wrap;align-items:flex-end;gap:8px 12px;margin-top:10px}
.frow label{display:flex;flex-direction:column;gap:3px;font:500 11px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--slate)}
.frow .grow{flex:1 1 180px}.dates{display:flex;gap:8px}
.frow input,.frow select{width:100%;font:400 14px var(--sans);color:var(--ink);border:1px solid var(--line);border-radius:2px;padding:6px 8px;background:#fff;min-height:36px;text-transform:none;letter-spacing:0}
.frow input[type=date]{width:9.5em}
.frow input:focus,.frow select:focus{outline:2px solid var(--electric);outline-offset:-1px}
.seg{display:flex;border:1px solid var(--line);border-radius:2px;overflow:hidden}
.seg button{border:0;border-left:1px solid var(--line);background:#fff;color:var(--ink);padding:0 10px;min-height:36px;font:500 12px var(--sans);cursor:pointer;white-space:nowrap}
.seg button:first-child{border-left:0}.seg button[aria-pressed=true]{background:var(--blue);color:#fff}
.fline{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin-top:8px;position:relative}
.fsum{margin:0;font:500 12px var(--mono);color:var(--slate)}
.pactive button{border:1px solid var(--navy);background:var(--navy);color:#fff;border-radius:2px;padding:3px 8px;font:500 12px var(--sans);cursor:pointer;max-width:100%;overflow-wrap:anywhere;text-align:left}
.parties{margin-left:auto}.parties>summary{list-style:none;cursor:pointer;font:600 12px var(--sans);color:var(--blue);padding:4px 0}.parties>summary::-webkit-details-marker{display:none}
.parties>summary::after{content:' ▾'}.parties[open]>summary::after{content:' ▴'}
.plist{position:absolute;right:0;top:100%;z-index:6;list-style:none;margin:4px 0 0;padding:4px 0;width:min(380px,100%);max-height:min(420px,60vh);overflow-y:auto;background:#fff;border:1px solid var(--line);box-shadow:0 12px 30px -12px rgba(3,7,34,.35)}
.plist button{display:flex;justify-content:space-between;gap:12px;width:100%;border:0;background:none;padding:8px 12px;text-align:left;font:400 13px var(--sans);color:var(--ink);cursor:pointer}
.plist button:hover,.plist button[aria-pressed=true]{background:var(--pale)}.plist .pn{flex:1 1 auto;overflow-wrap:anywhere}.filters .plist .pv{flex:0 0 auto;min-width:auto}.plist .pv{font:500 12px var(--mono);white-space:nowrap;color:var(--slate)}
.ledger .n{display:flex;flex-direction:column;align-items:flex-start;gap:6px}
.pick{width:18px;height:18px;margin:2px 0 0;accent-color:var(--blue);cursor:pointer}
.newtag{display:inline-block;margin-left:8px;padding:1px 6px;border-radius:2px;background:var(--electric);color:#fff;font:600 10px/1.6 var(--mono);letter-spacing:.06em;text-transform:uppercase;vertical-align:2px}
.bulk{position:fixed;left:0;right:0;bottom:0;z-index:8;display:flex;flex-wrap:wrap;align-items:flex-end;gap:8px 14px;padding:12px var(--pad);background:var(--navy);color:#fff;box-shadow:0 -10px 30px -12px rgba(3,7,34,.5)}
.state .btn{white-space:nowrap}
.bulk *{min-width:0}.bsum{margin:0 0 8px;font:600 14px var(--sans);white-space:nowrap}
.breason{flex:1 1 260px;display:flex;flex-direction:column;gap:3px;font:500 11px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--sky)}
.breason input{width:100%;font:400 14px var(--sans);border:0;border-radius:2px;padding:8px 10px;min-height:38px;color:var(--ink);text-transform:none;letter-spacing:0}
.bacts{display:flex;flex-wrap:wrap;gap:8px}.bulk .btn{min-height:38px}.bulk .btn:not(.btn-quiet){background:#fff;color:var(--navy)}.bulk .btn:not(.btn-quiet):hover{background:var(--pale)}.bulk .btn-quiet{background:transparent;color:#fff;border-color:rgba(255,255,255,.4)}
.bnote{flex-basis:100%;margin:0;font-size:12px;color:var(--sky)}.bnote:empty{display:none}
body.picking{padding-bottom:180px}
[hidden]{display:none!important}
@media (max-width:640px){.frow>*{flex:1 1 100%}.perl,.sortl{flex:1 1 40%!important}.dates label{flex:1 1 0}.frow input[type=date]{width:100%}.seg button{flex:1 1 0;padding:0 6px}
.parties{margin-left:0}.plist{left:0;right:auto}.bulk{padding:10px 16px}.bsum{flex-basis:100%;margin:0}}
.section{display:grid;grid-template-columns:minmax(0,.62fr) minmax(0,1.38fr);gap:clamp(28px,5vw,72px);padding:clamp(34px,4vw,56px) var(--pad);border-top:1px solid var(--line)}
.section.wash{background:var(--wash)}
.section h2{margin:10px 0 0;font-size:clamp(1.35rem,2.1vw,1.9rem);line-height:1.08;letter-spacing:-.035em;font-weight:700}
.section .aside p{color:var(--slate);margin:14px 0 0;max-width:380px}
.ledger{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.ledger>li{border-bottom:1px solid var(--line)}
.ledger details>summary{list-style:none;display:grid;grid-template-columns:40px minmax(0,1fr) auto;gap:16px;padding:16px 0 16px 14px;cursor:pointer}
.ledger>li{border-left:3px solid var(--edge,transparent)}.ledger.confirmed{--edge:var(--loss)}.bucket .ledger{--edge:#efa21b}.ledger.accepted{--edge:#a5b1c6}
.ledger>li:hover{background:rgba(232,245,255,.5)}
.ledger details>summary::-webkit-details-marker{display:none}
.ledger .n{font:500 14px/1.6 var(--mono);color:var(--electric)}
.ledger .what{font-weight:600;font-size:16px;line-height:1.4}
.ledger .keys{margin-top:4px;font:400 13px/1.5 var(--mono);color:var(--ink);overflow-wrap:anywhere}
.ledger .who{margin-top:2px;font-size:14px;color:var(--slate)}
.ledger .amt{text-align:right;font-weight:700;font-size:20px;letter-spacing:-.02em;white-space:nowrap}
.ledger .amt small{display:block;font:400 12px var(--sans);letter-spacing:0;color:var(--slate)}
.confirmed .amt{color:var(--loss)}
.gap{margin-top:8px;max-width:520px}.gap .track{position:relative;height:6px;background:var(--pale)}
.gap .billed{position:absolute;inset:0 auto 0 0;background:var(--blue)}.gap .short{position:absolute;top:0;bottom:0;background:var(--loss)}
.gap p{margin:6px 0 0;font:400 12px/1.4 var(--mono);color:var(--slate)}
.ledger details[open]>summary .what{color:var(--blue)}
.body{padding:0 0 22px 70px}
.records{width:100%;border-collapse:collapse;font-size:13px}.records th{font:500 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--slate);text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid var(--line)}
.records td{padding:9px 10px 9px 0;border-bottom:1px solid #e6edf5;vertical-align:top}.records td:nth-child(4){white-space:nowrap}.records td.num{text-align:right;font-family:var(--mono);white-space:nowrap}
.records .mono{font-family:var(--mono)}.scroll{overflow-x:auto}
.detail{margin:14px 0 0;color:var(--slate);font-size:14px}
.fix{margin:16px 0 0;padding:12px 14px;border-left:3px solid var(--electric);background:var(--pale);font-size:14px}
.fixlinks{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px}
a.zoho{color:var(--blue);font-weight:600;white-space:nowrap;text-decoration:none;border-bottom:1px solid var(--line)}a.zoho:hover{border-bottom-color:var(--blue)}
.decide{margin-top:18px;display:grid;gap:12px}
.decide fieldset{border:1px solid var(--line);background:#fff;padding:16px;margin:0}
.decide legend{font:500 11px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--slate);padding:0 6px}
.decide label{display:block;font-size:13px;font-weight:600;margin:0 0 6px}
.decide input[type=text],.decide textarea{width:100%;font:400 15px var(--sans);color:var(--ink);border:1px solid #a9bdd6;border-radius:2px;padding:10px 12px;background:#fff}
.decide textarea{min-height:64px;resize:vertical}
.decide .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}
.decide .opts{display:flex;flex-wrap:wrap;gap:18px;font-size:14px;margin-bottom:10px}
.bucket{border-top:1px solid var(--line)}.bucket:last-child{border-bottom:1px solid var(--line)}
.bucket>summary{list-style:none;display:grid;grid-template-columns:minmax(0,1fr) 56px 120px;gap:16px;align-items:baseline;padding:20px 0;cursor:pointer}
.bucket>summary::-webkit-details-marker{display:none}
.bucket>summary::before{content:'+';position:absolute;margin-left:-28px;font:500 16px var(--mono);color:var(--electric)}
.bucket[open]>summary::before{content:'−'}.bucket>summary{position:relative}
.bname{font-weight:600;font-size:16px}.bname small{display:block;font-weight:400;font-size:13px;color:var(--slate);margin-top:2px}
.bcount{font:500 14px var(--mono);color:var(--electric);text-align:right}.btotal{font-weight:700;text-align:right;white-space:nowrap}
.bucket .ledger{border-top:1px solid var(--line);margin-bottom:18px}
.group{margin:34px 0 0}.group:first-child{margin-top:0}
.group h3{margin:0 0 10px;display:flex;justify-content:space-between;gap:12px;font:500 11px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--slate)}
.empty{padding:22px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);color:var(--slate)}
.flash{margin:0;padding:14px var(--pad);font-size:14px;background:var(--pale);border-bottom:1px solid var(--line)}.flash.bad{background:#fdecea;color:#8e1c1c}
.log{width:100%;border-collapse:collapse;font-size:13px}.log td,.log th{padding:10px 10px 10px 0;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.log th{font:500 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--slate)}
.log form{margin:0}.log button{background:none;border:0;color:var(--blue);font:500 13px var(--sans);cursor:pointer;padding:0;min-height:32px;text-decoration:underline}
.foot{background:var(--navy);color:rgba(255,255,255,.6);padding:28px var(--pad);font-size:12px;display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px}
.foot b{color:#fff;font-weight:600}
@media (max-width:980px){.section{grid-template-columns:minmax(0,1fr)}.strip{grid-template-columns:1fr 1fr}.strip div:nth-child(3){border-left:0;padding-left:0}.strip div{border-top:1px solid rgba(255,255,255,.18)}}
@media (max-width:640px){.bucket>summary{grid-template-columns:minmax(0,1fr) auto;padding-left:22px}.bucket>summary::before{margin-left:-22px}.bcount{display:none}
.ledger details>summary{grid-template-columns:18px minmax(0,1fr) auto;gap:10px;padding-left:10px}.ledger .nn{display:none}.body{padding-left:12px}
.hero{grid-template-columns:minmax(0,1fr)}.state{align-items:flex-start;text-align:left}.ledger .amt{font-size:17px}
.strip div{padding:12px 14px}.strip b{font-size:20px}.section .aside p{display:none}.section{gap:16px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
'''


def books_header(active=None):
    """Shared Books navy bar. active: 'inbox', 'leakage', or None (signed out: no navigation)."""
    links = (('inbox', '/', 'Bills inbox'), ('leakage', '/leakage', 'Leakage'))
    nav = '' if active is None else ('<nav class="nav" aria-label="Books">'+''.join(
        '<a href="'+href+'"'+(' aria-current="page"' if key == active else '')+'>'+label+'</a>' for key, href, label in links)+'</nav>')
    return ('<header class="bar"><a class="brand" href="/" aria-label="Sattva Books home"><span class="mark" aria-hidden="true">'
            '<img src="/static/sattva-swirl-white.svg" alt=""></span>Sattva Global Logistics<small>Books</small></a>'+nav+'</header>')


def gap_bar(item, confirmed):
    cost, rev = item.get('cost_inr'), item.get('revenue_inr')
    if cost is None:
        if not confirmed or item['type'] != 'UNBILLED':
            return ''
        cost, rev = sum(Decimal(b['cost_inr']) for b in item['bills']), Decimal(0)
    cost, rev = Decimal(str(cost)), Decimal(str(rev))
    if cost <= 0:
        return ''
    billed = max(Decimal(0), min(rev, cost))
    pct_billed = float(billed/cost*100)
    short = cost-billed
    bar = '<div class="track"><span class="billed" style="width:%.1f%%"></span>' % pct_billed
    if short > 0:
        bar += '<span class="short" style="left:%.1f%%;width:%.1f%%"></span>' % (pct_billed, 100-pct_billed)
    bar += '</div>'
    text = 'Cost ₹'+inr(cost)+' · billed ₹'+inr(rev)+(' · short ₹'+inr(short) if short > 0 else ' · margin ₹'+inr(rev-cost))
    return '<div class="gap" aria-label="'+esc(text)+'">'+bar+'<p>'+esc(text)+'</p></div>'


ZOHO_WEB = 'https://books.zoho.in/app/'


def zoho_link(org, kind, record_id, text):
    """A link that opens the record in Zoho Books (India). Plain text when the organisation or id is unknown."""
    rid = str(record_id or '')
    if not (str(org or '').isdigit() and rid.isdigit()):
        return esc(text)
    return '<a class="zoho" href="'+esc(ZOHO_WEB+str(org)+'#/'+kind+'/'+rid)+'" target="_blank" rel="noopener noreferrer">'+esc(text)+' ↗</a>'


def records_table(item, org=''):
    rows = []
    for b in item.get('bills', []):
        rows.append('<tr><td>Vendor bill</td><td class="mono">'+zoho_link(org, 'bills', b.get('bill_id'), b.get('bill_number') or 'open')+'</td><td>'+esc(b.get('vendor'))+'</td><td>'+esc(b.get('date'))+
                    '</td><td class="num">₹'+esc(inr(b.get('cost_inr'), True))+'</td><td class="num">'+esc(inr(b.get('balance') or 0, True))+'</td><td>'+esc(b.get('status'))+'</td></tr>')
    for i in item.get('invoices', []):
        rows.append('<tr><td>Sales invoice</td><td class="mono">'+zoho_link(org, 'invoices', i.get('invoice_id'), i.get('invoice_number') or 'open')+'</td><td>'+esc(i.get('customer'))+'</td><td>'+esc(i.get('date'))+
                    '</td><td class="num">₹'+esc(inr(i.get('revenue_inr'), True))+'</td><td class="num">'+esc(inr(i.get('balance') or 0, True))+'</td><td>'+esc(i.get('status'))+'</td></tr>')
    if not rows:
        return ''
    return ('<div class="scroll"><table class="records"><thead><tr><th>Record</th><th>Number</th><th>Party</th><th>Date</th>'
            '<th style="text-align:right">Pre-GST INR</th><th style="text-align:right">Balance</th><th>Status</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>')


def fix_links(item, org):
    links = []
    if item['type'] == 'unused vendor credit (vendor owes us)':
        vid = (item.get('bills') or [{}])[0].get('vendor_id')
        links.append(zoho_link(org, 'contacts', vid, 'Open vendor in Zoho'))
    links += [zoho_link(org, 'bills', b.get('bill_id'), 'Bill '+str(b.get('bill_number') or '')) for b in item.get('bills', [])[:3]]
    links += [zoho_link(org, 'invoices', i.get('invoice_id'), 'Invoice '+str(i.get('invoice_number') or '')) for i in item.get('invoices', [])[:3]]
    links = [l for l in links if l.startswith('<a')]
    return ('<span class="fixlinks">'+' '.join(links)+'</span>') if links else ''


def decision_forms(item, csrf, org=''):
    actions = ACTIONS.get(item['type'], ())
    if not actions:
        return ('<p class="fix">'+esc(FIX_IN_ZOHO[item['type']])+fix_links(item, org)+'</p>') if item['type'] in FIX_IN_ZOHO else ''
    bill_ids = ','.join(b['bill_id'] for b in item.get('bills', []))
    open_inv = ','.join(i['invoice_id'] for i in item.get('invoices', []) if Decimal(str(i.get('balance') or 0)) > 0)
    keys = ','.join(item.get('shipment_keys', []))
    common = '<input type="hidden" name="csrf" value="'+esc(csrf)+'">'
    reason = lambda ph: ('<label>Reason <span style="font-weight:400;color:var(--slate)">— kept with the decision</span>'
                         '<textarea name="reason" required minlength="3" maxlength="500" placeholder="'+esc(ph)+'"></textarea></label>')
    out = []
    if 'accept_loss' in actions and item.get('fingerprint'):
        out.append('<form method="post" action="/leakage/decide/accept_loss"><fieldset><legend>Accept as a commercial loss</legend>'+common+
                   '<input type="hidden" name="fingerprint" value="'+esc(item['fingerprint'])+'"><input type="hidden" name="keys" value="'+esc(keys)+'">'+
                   reason('e.g. Rate held for the customer; carrier FX moved after booking')+
                   '<div class="row"><button class="btn btn-quiet" type="submit">Accept loss</button><span class="hint" style="color:var(--slate)">Reopens if any bill or invoice on this shipment changes.</span></div></fieldset></form>')
    if 'write_off' in actions and bill_ids:
        out.append('<form method="post" action="/leakage/decide/write_off"><fieldset><legend>Write off — not billing anyone</legend>'+common+
                   '<input type="hidden" name="ids" value="'+esc(bill_ids)+'">'+reason('e.g. Spot booking no-show fee, absorbed')+
                   '<div class="row"><button class="btn btn-quiet" type="submit">Write off</button></div></fieldset></form>')
    if 'link' in actions and bill_ids:
        out.append('<form method="post" action="/leakage/decide/link"><fieldset><legend>It was billed — link the invoice</legend>'+common+
                   '<input type="hidden" name="ids" value="'+esc(bill_ids)+'"><label>Zoho invoice number<input type="text" name="invoice_number" required maxlength="40" '
                   'autocomplete="off" spellcheck="false" placeholder="TISGLSE2026…"></label>'+reason('e.g. Billed on the house B/L number')+
                   '<div class="row"><button class="btn btn-quiet" type="submit">Link invoice</button></div></fieldset></form>')
    if 'overhead' in actions and bill_ids:
        vendor = (item.get('bills') or [{}])[0].get('vendor_id', '')
        vname = (item.get('bills') or [{}])[0].get('vendor', 'this vendor')
        out.append('<form method="post" action="/leakage/decide/overhead"><fieldset><legend>Overhead — not a shipment cost</legend>'+common+
                   '<input type="hidden" name="ids" value="'+esc(bill_ids)+'"><input type="hidden" name="vendor_id" value="'+esc(vendor)+'">'
                   '<div class="opts"><label style="font-weight:400"><input type="radio" name="scope" value="bill" checked> This bill only</label>'
                   '<label style="font-weight:400"><input type="radio" name="scope" value="vendor"> All key-less bills from '+esc(vname)+'</label></div>'+
                   reason('e.g. Monthly accounting retainer')+'<div class="row"><button class="btn btn-quiet" type="submit">Mark overhead</button></div></fieldset></form>')
    if 'settled' in actions and open_inv:
        out.append('<form method="post" action="/leakage/decide/settled"><fieldset><legend>Customer has settled this</legend>'+common+
                   '<input type="hidden" name="ids" value="'+esc(open_inv)+'">'+reason('e.g. Adjusted against the customer advance')+
                   '<div class="row"><button class="btn btn-quiet" type="submit">Mark settled</button><span class="hint" style="color:var(--slate)">Stays listed as a books fix until Zoho shows it paid.</span></div></fieldset></form>')
    return '<div class="decide">'+''.join(out)+'</div>' if out else ''


def day(iso):
    try:
        return datetime.strptime(iso[:10], '%Y-%m-%d').strftime('%-d %b %Y')
    except ValueError:
        return iso


BULK_ACTIONS = ('write_off', 'settled', 'overhead')


def item_key(item):
    """Stable identity of a finding across runs: its type and the Zoho records behind it."""
    return json.dumps([item.get('type'), sorted(str(b.get('bill_id')) for b in item.get('bills', [])),
                       sorted(str(i.get('invoice_id')) for i in item.get('invoices', [])),
                       '' if item.get('bills') or item.get('invoices') else str(item.get('detail', ''))])


def report_keys(report):
    if not report:
        return None
    return {item_key(i) for i in report.get('confirmed_leakage', [])+report.get('potential_issues', [])}


def item_party(item):
    """Who to talk to about it: the customer when there is an invoice, otherwise the vendor."""
    for i in item.get('invoices', []):
        if i.get('customer'):
            return str(i['customer'])
    for b in item.get('bills', []):
        if b.get('vendor'):
            return str(b['vendor'])
    return ''


def filter_bar(confirmed, potential, has_previous):
    """Chips per finding type with count and amount, view, dates, search, sort, party rollup. Filtering runs in the browser."""
    kinds, seen = [], set()
    for it in confirmed+potential:
        if it['type'] not in seen:
            seen.add(it['type']); kinds.append(it['type'])
    conf_types = {it['type'] for it in confirmed}
    chip = lambda attr, label, cls: ('<button type="button" class="chip'+cls+'" '+attr+' aria-pressed="false">'
                                     '<span class="l">'+esc(label)+'</span><span class="c"></span><span class="s"></span></button>')
    chips = chip('data-f=""', 'All open', '')
    if has_previous:
        chips += chip('data-new="1"', 'New since last check', ' new')
    chips += ''.join(chip('data-f="'+esc(k)+'"', PLAIN.get(k, k), ' red' if k in conf_types else '') for k in kinds)
    view = ('<div class="seg" role="group" aria-label="Show"><button type="button" data-v="" aria-pressed="true">Everything</button>'
            '<button type="button" data-v="decide" aria-pressed="false">Decide here</button><button type="button" data-v="fix" aria-pressed="false">Fix in Zoho</button></div>')
    return ('<section class="filters" id="filters" aria-label="Filter findings"><div class="chips" role="group" aria-label="Finding type">'+chips+'</div>'
            '<div class="frow">'+view+
            '<label class="perl">Period<select name="per"><option value="">All dates</option><option value="m">This month</option>'
            '<option value="lm">Last month</option><option value="90">Last 90 days</option><option value="fy">This financial year</option><option value="c">Custom range…</option></select></label>'
            '<span class="dates" hidden><label>From<input type="date" name="from"></label><label>To<input type="date" name="to"></label></span>'
            '<label class="grow">Search<input type="search" name="q" placeholder="MBL, bill or invoice no., vendor, customer" autocomplete="off" spellcheck="false"></label>'
            '<label class="sortl">Sort<select name="sort"><option value="amt">Biggest first</option><option value="new">Newest first</option><option value="old">Oldest first</option></select></label></div>'
            '<div class="fline"><p class="fsum" aria-live="polite"></p><span class="pactive" hidden></span>'
            '<details class="parties"><summary>By party</summary><ol class="plist"></ol></details></div></section>')


def bulk_bar(csrf):
    return ('<form class="bulk" id="bulk" method="post" hidden><input type="hidden" name="csrf" value="'+esc(csrf)+'"><input type="hidden" name="ids">'
            '<p class="bsum" aria-live="polite"></p><label class="breason">Reason<input type="text" name="reason" required minlength="3" maxlength="500" '
            'placeholder="One reason for all selected, e.g. Spot booking no-show fees, absorbed"></label>'
            '<span class="bacts"><button class="btn" type="submit" data-a="write_off">Write off</button><button class="btn" type="submit" data-a="settled">Mark settled</button>'
            '<button class="btn" type="submit" data-a="overhead">Overhead</button><button class="btn btn-quiet" type="button" data-clear="1">Clear</button></span>'
            '<p class="bnote"></p></form>')


FILTER_JS = '''<script>(()=>{const bar=document.getElementById('filters');if(!bar)return;
const items=[...document.querySelectorAll('#confirmed li[data-type],#decide li[data-type]')];
const st={t:'',n:'',v:'',p:'',per:'',from:'',to:'',q:'',sort:'amt'};
try{const h=new URLSearchParams(location.hash.slice(1));for(const k in st){if(h.has(k))st[k]=h.get(k)}}catch(e){}
const inp=n=>bar.querySelector('[name='+n+']');
const fmt=v=>v?'\\u20b9'+Math.round(v).toLocaleString('en-IN'):'\\u2014';
const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
const pass=(li,skip)=>{const q=st.q.trim().toLowerCase(),d=li.dataset.date,x=li.dataset;
 return (skip==='t'||!st.t||x.type===st.t)&&(skip==='n'||!st.n||x.new==='1')&&(!st.v||x.kind===st.v)&&(skip==='p'||!st.p||x.party===st.p)
  &&(!st.from||(d&&d>=st.from))&&(!st.to||(d&&d<=st.to))&&(!q||x.q.includes(q))};
const cmp=(a,b)=>st.sort==='amt'?(+b.dataset.amt)-(+a.dataset.amt):
 (st.sort==='new'?(b.dataset.date||'').localeCompare(a.dataset.date||''):(a.dataset.date||'~').localeCompare(b.dataset.date||'~'));
const bulk=document.getElementById('bulk');
function picks(){return items.filter(li=>{const c=li.querySelector('.pick');return c&&c.checked&&!li.hidden})}
function syncBulk(){if(!bulk)return;const sel=picks();bulk.hidden=!sel.length;document.body.classList.toggle('picking',sel.length>0);if(!sel.length)return;
 const acts=['write_off','settled','overhead'].filter(a=>sel.every(li=>(li.dataset.acts||'').split(' ').includes(a)));
 bulk.querySelectorAll('[data-a]').forEach(b=>b.hidden=!acts.includes(b.dataset.a));
 bulk.querySelector('.bsum').textContent=sel.length+' selected \\u00b7 '+fmt(sel.reduce((a,li)=>a+(+li.dataset.amt),0));
 bulk.querySelector('.bnote').textContent=acts.length?'':'These items have no decision in common. Select items of one kind.'}
function apply(){let n=0,sum=0;
 items.forEach(li=>{const ok=pass(li);li.hidden=!ok;if(ok){n++;sum+=+li.dataset.amt}});
 bar.querySelectorAll('.chip').forEach(b=>{const isNew='new' in b.dataset,t=b.dataset.f;let c=0,s=0;
  items.forEach(li=>{if(isNew?(li.dataset.new==='1'&&pass(li,'n')):((!t||li.dataset.type===t)&&pass(li,'t'))){c++;s+=+li.dataset.amt}});
  b.querySelector('.c').textContent=c;b.querySelector('.s').textContent=fmt(s);
  const on=isNew?st.n==='1':(st.t===t&&!(t===''&&st.n));b.setAttribute('aria-pressed',String(on));b.hidden=!isNew&&!!t&&!c&&!on});
 bar.querySelectorAll('[data-v]').forEach(b=>b.setAttribute('aria-pressed',String(st.v===b.dataset.v)));
 const narrowed=!!(st.t||st.n||st.v||st.p||st.q||st.from||st.to);
 document.querySelectorAll('#confirmed ol.ledger,#decide ol.ledger').forEach(ol=>{[...ol.children].sort(cmp).forEach(li=>ol.appendChild(li))});
 document.querySelectorAll('#decide details.bucket').forEach(d=>{const vis=[...d.querySelectorAll('li[data-type]')].filter(li=>!li.hidden);
  d.hidden=!vis.length;d.querySelector('.bcount').textContent=vis.length;d.querySelector('.btotal').textContent=fmt(vis.reduce((a,li)=>a+(+li.dataset.amt),0));
  if(narrowed&&vis.length)d.open=true});
 ['confirmed','decide'].forEach(id=>{const sec=document.getElementById(id);if(!sec)return;let i=0;
  sec.querySelectorAll('li[data-type]').forEach(li=>{if(!li.hidden){const el=li.querySelector('.nn');if(el)el.textContent=String(++i).padStart(2,'0')}});
  let e=sec.querySelector('.fempty');if(!e){e=document.createElement('p');e.className='empty fempty';e.textContent='Nothing here matches these filters.';(sec.querySelector('ol.ledger,.bucket,.empty')||sec.lastElementChild).parentNode.appendChild(e)}
  e.hidden=!narrowed||i>0});
 bar.querySelector('.fsum').textContent=(narrowed?'Showing '+n+' of '+items.length:items.length)+' open items \\u00b7 '+fmt(sum)+' exposure';
 const pa=bar.querySelector('.pactive');pa.hidden=!st.p;pa.innerHTML='';if(st.p){const b=document.createElement('button');b.type='button';b.dataset.party='';b.textContent=st.p+'  \\u00d7';b.setAttribute('aria-label','Clear party filter '+st.p);pa.appendChild(b)}
 const tot={};items.forEach(li=>{const p=li.dataset.party;if(p&&pass(li,'p')){(tot[p]=tot[p]||[0,0])[0]++;tot[p][1]+=+li.dataset.amt}});
 bar.querySelector('.plist').innerHTML='';Object.entries(tot).sort((a,b)=>b[1][1]-a[1][1]).slice(0,15).forEach(([p,[c,s]])=>{
  const li=document.createElement('li'),b=document.createElement('button');b.type='button';b.dataset.party=p;b.setAttribute('aria-pressed',String(st.p===p));
  const l=document.createElement('span');l.className='pn';l.textContent=p;const r=document.createElement('span');r.className='pv';r.textContent=c+' \\u00b7 '+fmt(s);
  b.append(l,r);li.appendChild(b);bar.querySelector('.plist').appendChild(li)});
 items.forEach(li=>{if(li.hidden){const c=li.querySelector('.pick');if(c)c.checked=false}});syncBulk();
 const h=new URLSearchParams();for(const k in st){if(st[k]&&!(k==='sort'&&st[k]==='amt'))h.set(k,st[k])}
 history.replaceState(null,'',h.toString()?'#'+h:location.pathname+location.search)}
bar.addEventListener('click',e=>{const c=e.target.closest('.chip');
 if(c){if('new' in c.dataset){st.n=st.n?'':'1'}else if(c.dataset.f===''){st.t='';st.n=''}else{st.t=st.t===c.dataset.f?'':c.dataset.f}apply();return}
 const v=e.target.closest('[data-v]');if(v){st.v=v.dataset.v;apply();return}
 const pb=e.target.closest('[data-party]');if(pb){st.p=st.p===pb.dataset.party?'':pb.dataset.party;bar.querySelector('.parties').open=false;apply();return}
});
function period(){const now=new Date(),y=now.getFullYear(),m=now.getMonth();let f='',t='';
 if(st.per==='m')f=iso(new Date(y,m,1));
 else if(st.per==='lm'){f=iso(new Date(y,m-1,1));t=iso(new Date(y,m,0))}
 else if(st.per==='90')f=iso(new Date(now-90*864e5));
 else if(st.per==='fy')f=iso(new Date(m>=3?y:y-1,3,1));
 if(st.per!=='c'){st.from=f;st.to=t}inp('from').value=st.from;inp('to').value=st.to;bar.querySelector('.dates').hidden=st.per!=='c'}
if((st.from||st.to)&&!st.per)st.per='c';
['per','from','to','q','sort'].forEach(k=>{const el=inp(k);el.value=st[k];el.addEventListener(k==='q'?'input':'change',()=>{st[k]=el.value;if(k==='per')period();apply()})});
period();
document.addEventListener('change',e=>{if(e.target.classList&&e.target.classList.contains('pick'))syncBulk()});
document.addEventListener('click',e=>{if(e.target.classList&&e.target.classList.contains('pick'))e.stopPropagation()},true);
if(bulk){let act='';bulk.addEventListener('click',e=>{const b=e.target.closest('[data-a]');if(b)act=b.dataset.a;
  if(e.target.closest('[data-clear]')){items.forEach(li=>{const c=li.querySelector('.pick');if(c)c.checked=false});syncBulk()}});
 bulk.addEventListener('submit',e=>{const sel=picks();const key=act==='settled'?'invs':'bills';
  const ids=[...new Set(sel.flatMap(li=>(li.dataset[key]||'').split(',').filter(Boolean)))];
  if(!act||!ids.length||ids.length>50){e.preventDefault();bulk.querySelector('.bnote').textContent=ids.length>50?'Up to 50 records at a time.':'Nothing to decide on.';return}
  bulk.action='/leakage/decide/'+act;bulk.querySelector('[name=ids]').value=ids.join(',')})}
apply()})()</script>'''


def day(iso):
    try:
        return datetime.strptime(iso[:10], '%Y-%m-%d').strftime('%-d %b %Y')
    except ValueError:
        return iso


def age_days(as_of, iso):
    try:
        return (datetime.strptime(str(as_of)[:10], '%Y-%m-%d')-datetime.strptime(iso[:10], '%Y-%m-%d')).days
    except ValueError:
        return None


def ledger_row(n, item, csrf, confirmed=False, org='', seen=None, as_of=''):
    keys = ' · '.join(item.get('shipment_keys', [])) or 'No shipment key'
    who = ' → '.join([', '.join(sorted({b.get('vendor') for b in item.get('bills', []) if b.get('vendor')})),
                      ', '.join(sorted({i.get('customer') for i in item.get('invoices', []) if i.get('customer')}))]).strip(' →')
    amt = Decimal(item.get('exposure_inr') or 0)
    amount = ('₹'+inr(amt)) if amt else '—'
    dated = sorted(str(b.get('date')) for b in item.get('bills', []) if b.get('date')) or \
        sorted(str(i.get('date')) for i in item.get('invoices', []) if i.get('date'))
    when = dated[-1][:10] if dated else ''
    age = age_days(as_of, when) if confirmed and when and as_of else None
    label = ('at risk'+(' · '+str(age)+' day'+('s' if age != 1 else '') if age is not None else '')) if confirmed else ('exposure' if amt else '')
    search = ' '.join([PLAIN.get(item['type'], item['type'])] + list(item.get('shipment_keys', [])) +
                      [str(b.get(k) or '') for b in item.get('bills', []) for k in ('bill_number', 'vendor')] +
                      [str(i.get(k) or '') for i in item.get('invoices', []) for k in ('invoice_number', 'customer')]).lower()
    is_new = seen is not None and item_key(item) not in seen
    acts = ACTIONS.get(item['type'], ())
    kind = 'decide' if acts else ('fix' if item['type'] in FIX_IN_ZOHO else 'other')
    bills = ','.join(str(b['bill_id']) for b in item.get('bills', []) if str(b.get('bill_id', '')).isdigit())
    invs = ','.join(str(i['invoice_id']) for i in item.get('invoices', [])
                    if str(i.get('invoice_id', '')).isdigit() and Decimal(str(i.get('balance') or 0)) > 0)
    bulk = [a for a in BULK_ACTIONS if a in acts and (invs if a == 'settled' else bills)]
    pick = ('<input type="checkbox" class="pick" aria-label="Select for a bulk decision">' if bulk else '')
    body = ('<div class="body">'+records_table(item, org)+'<p class="detail">'+esc(item.get('detail'))+'</p>'+
            ''.join('<p class="detail">⚑ '+esc(f)+'</p>' for f in item.get('flags', []))+decision_forms(item, csrf, org)+'</div>')
    return ('<li data-type="'+esc(item['type'])+'" data-amt="'+esc(str(amt))+'" data-date="'+esc(when)+'" data-q="'+esc(search)+'" data-kind="'+kind+
            '" data-party="'+esc(item_party(item))+'" data-new="'+('1' if is_new else '')+'" data-acts="'+' '.join(bulk)+'" data-bills="'+esc(bills)+'" data-invs="'+esc(invs)+'">'
            '<details><summary><span class="n">'+pick+'<span class="nn">'+'%02d' % n+'</span></span><span><span class="what">'+esc(PLAIN.get(item['type'], item['type']))+
            ('<b class="newtag">New</b>' if is_new else '')+'</span>'
            '<span class="keys" style="display:block">'+esc(keys)+(' · '+esc(day(when)) if when else '')+'</span>'+('<span class="who" style="display:block">'+esc(who)+'</span>' if who else '')+
            gap_bar(item, confirmed)+'</span><span class="amt">'+esc(amount)+('<small>'+esc(label)+'</small>' if label else '')+'</span></summary>'+body+'</details></li>')


def page(report, started_at, email_status, runs, decisions, csrf, running=False, flash='', flash_bad=False, enabled=True, daily_hour=8, zoho_org='', previous=None):
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Leakage · Sattva Books</title><style>'+CSS+'</style></head><body>'+books_header('leakage'))
    flash_html = ('<p class="flash'+(' bad' if flash_bad else '')+'" role="status">'+esc(flash)+'</p>') if flash else ''
    run_form = ('<form method="post" action="/leakage/run"><input type="hidden" name="csrf" value="'+esc(csrf)+'">'
                '<button class="btn btn-light" type="submit"'+(' disabled' if running or not enabled else '')+'>'+
                ('Checking Zoho…' if running else 'Check Zoho now')+'</button><span class="hint">'+
                ('A full check takes a few minutes. This page refreshes itself.' if running else
                 'Daily check runs at %02d:00 IST.' % daily_hour if enabled else
                 'Zoho read access is not configured on this server.')+'</span></form>')
    refresh = '<script>setTimeout(()=>location.reload(),8000)</script>' if running else ''
    if not report:
        hero = ('<section class="hero"><div><div class="eyebrow">Leakage check · vendor cost → customer invoice</div>'
                '<h1>No check has run yet.</h1><p class="lede">The first check reads every FY26-27 vendor bill, sales invoice and credit note from Zoho, read-only, '
                'and matches them by MBL and container number.</p></div><div class="state">'+run_form+'</div></section>')
        return head+flash_html+hero+'<footer class="foot"><span><b>Sattva Books</b> · read-only against Zoho</span></footer>'+refresh+'</body></html>'

    t = report['totals_inr']
    confirmed, potential, accepted = report['confirmed_leakage'], report['potential_issues'], report['accepted_losses']
    complete = report['status'] == 'COMPLETE'
    conf_amt = Decimal(t['confirmed_deduplicated'])
    n_conf = len({','.join(sorted(b['bill_id'] for b in i['bills'])) for i in confirmed})
    if not complete:
        title = 'This check is incomplete. <span class="money">Silence is not clean.</span>'
        lede = 'Some Zoho records could not be read, so nothing below is a full picture. Reasons are listed on the right.'
    elif conf_amt > 0:
        title = '<span class="money">₹'+esc(inr(conf_amt))+'</span> is leaking across '+str(n_conf)+' shipment'+('s' if n_conf != 1 else '')+'.'
        lede = 'Vendor cost that has not reached a customer invoice, or that a customer has not paid back. Pre-GST, in INR, from Zoho as of this check.'
    else:
        title = '<span class="calm">Nothing is leaking.</span> Every shipment cost is billed.'
        lede = 'Every in-scope vendor cost matched a customer invoice. '+str(len(potential))+' item'+('s' if len(potential) != 1 else '')+' still need a look below.'
    ev = report.get('evidence_counts', {})
    reasons = report.get('incomplete_reasons') or []
    status_line = ('<div class="line"><b'+('' if complete else ' class="bad"')+'>● '+('Complete' if complete else 'Incomplete')+'</b> · checked '+
                   esc(ist(report.get('read_at') or started_at))+' IST'+(' · decisions applied '+esc(ist(started_at))+' IST' if report.get('read_at') else ' · email '+esc(email_status or '—'))+'</div>')
    evidence = ('<details class="evidence"><summary>What this check read</summary><dl>'
                '<div><dt>Vendor bills</dt><dd>'+esc(ev.get('bills', 0))+'</dd></div><div><dt>Sales invoices</dt><dd>'+esc(ev.get('invoices', 0))+'</dd></div>'
                '<div><dt>Credit notes</dt><dd>'+esc(ev.get('creditnotes', 0))+'</dd></div><div><dt>Shipments matched</dt><dd>'+esc(report.get('shipments', 0))+'</dd></div>'
                '<div><dt>Zoho requests</dt><dd>'+esc(report.get('api_calls', '—'))+'</dd></div></dl>'
                '<p>'+esc(lede)+' Every list and record was read; uncertain matches are shown, never cleared.</p></details>')
    reasons_html = ('<p class="reasons" role="alert"><b>Why incomplete:</b> '+esc('; '.join(reasons[:6]))+'</p>') if reasons else ''
    hero = ('<section class="hero"><div><div class="eyebrow">Leakage check · FY26-27 · pre-GST INR</div><h1>'+title+'</h1></div>'
            '<div class="state">'+status_line+run_form+'</div>'+(reasons_html or evidence)+'</section>')
    strip = ('<section class="strip" aria-label="Totals">'
             '<div><b>'+str(n_conf)+'</b><span>shipment'+('s' if n_conf != 1 else '')+' leaking</span></div>'
             '<div><b>'+str(len(potential))+'</b><span>need your call</span></div>'
             '<div><b>₹'+esc(inr(t['accepted_losses']))+'</b><span>accepted or written off</span></div>'
             '<div><b>₹'+esc(inr(t['excluded_overhead']))+'</b><span>overhead, excluded</span></div></section>')

    seen, as_of = report_keys(previous), report.get('as_of', '')
    conf_list = ''.join(ledger_row(n, it, csrf, True, zoho_org, seen, as_of) for n, it in enumerate(confirmed, 1)) or ''
    conf_html = ('<ol class="ledger confirmed">'+conf_list+'</ol>') if conf_list else \
        '<p class="empty">'+('No confirmed leakage in this check.' if complete else 'Nothing confirmed — but this check is incomplete.')+'</p>'
    sec_conf = ('<section class="section" id="confirmed"><div class="aside"><div class="eyebrow">Confirmed leakage</div><h2>Money that left and has not come back.</h2>'
                '<p>Open a shipment to see the bills and invoices behind it, then accept the loss or fix the invoice in Zoho.</p></div><div>'+conf_html+'</div></section>')

    groups, order = {}, []
    for it in potential:
        if it['type'] not in groups:
            order.append(it['type'])
        groups.setdefault(it['type'], []).append(it)
    order.sort(key=lambda k: (PRIORITY.index(k) if k in PRIORITY else len(PRIORITY), -sum(Decimal(i['exposure_inr']) for i in groups[k])))
    pot_html, n = '', 0
    for kind in order:
        items = groups[kind]
        rows = ''
        for it in items:
            n += 1
            rows += ledger_row(n, it, csrf, org=zoho_org, seen=seen, as_of=as_of)
        total = sum(Decimal(i['exposure_inr']) for i in items)
        why = WHY.get(kind, '')
        pot_html += ('<details class="bucket"><summary><span class="bname">'+esc(PLAIN.get(kind, kind))+'<small>'+esc(why)+'</small></span>'
                     '<span class="bcount">'+str(len(items))+'</span><span class="btotal">'+('₹'+esc(inr(total)) if total else '—')+'</span></summary>'
                     '<ol class="ledger">'+rows+'</ol></details>')
    sec_pot = ('<section class="section wash" id="decide"><div class="aside"><div class="eyebrow">Needs your call</div><h2>Not proven leakage. Not cleared either.</h2>'
               '<p>Each group says what is uncertain. Decide here with a reason, or fix the record in Zoho and the next check picks it up.</p></div><div>'+
               (pot_html or '<p class="empty">Nothing waiting on you.</p>')+'</div></section>')

    acc_rows = ''.join(ledger_row(i, it, csrf, org=zoho_org) for i, it in enumerate(accepted, 1))
    dec_rows = ''
    for did, kind, payload, reason, created in decisions:
        p = json.loads(payload)
        target = p.get('bill_id') and 'Bill '+p['bill_id'] or p.get('invoice_id') and 'Invoice '+p['invoice_id'] or \
            p.get('vendor_id') and 'Vendor '+p['vendor_id'] or p.get('fingerprint') and 'Shipment '+', '.join(p.get('shipment_keys') or [p['fingerprint']]) or '—'
        if p.get('invoice_number'):
            target += ' → '+p['invoice_number']
        dec_rows += ('<tr><td>'+esc(kind.replace('_', ' '))+'</td><td class="mono" style="font-family:var(--mono)">'+esc(target)+'</td><td>'+esc(reason)+'</td><td>'+
                     esc(ist(created))+'</td><td><form method="post" action="/leakage/revoke/'+str(int(did))+'"><input type="hidden" name="csrf" value="'+esc(csrf)+
                     '"><button type="submit">Undo</button></form></td></tr>')
    sec_acc = ('<section class="section" id="accepted"><div class="aside"><div class="eyebrow">Your decisions</div><h2>Accepted, written off, explained.</h2>'
               '<p>Every decision carries its reason. Undo puts the item back in the next check.</p></div><div>'+
               ('<ol class="ledger accepted">'+acc_rows+'</ol>' if acc_rows else '<p class="empty">No accepted losses or write-offs in this check.</p>')+
               ('<div class="group" style="margin-top:40px"><h3><span>Decisions on record · '+str(len(decisions))+'</span></h3><div class="scroll"><table class="log"><thead><tr><th>Decision</th><th>On</th><th>Reason</th><th>When (IST)</th><th></th></tr></thead><tbody>'+
                dec_rows+'</tbody></table></div></div>' if dec_rows else '')+'</div></section>')

    over = report.get('excluded_overhead', [])
    over_rows = ''.join('<tr><td class="mono" style="font-family:var(--mono)">'+esc(o.get('bill_number'))+'</td><td>'+esc(o.get('vendor'))+'</td><td>'+esc(o.get('date'))+
                        '</td><td class="num" style="text-align:right;font-family:var(--mono)">₹'+esc(inr(o.get('cost_inr'), True))+'</td><td>'+esc(o.get('why'))+'</td></tr>' for o in over)
    run_rows = ''.join('<tr><td>'+esc(ist(r[2]))+'</td><td>'+esc(r[1])+'</td><td>'+esc(r[4].capitalize())+'</td><td>'+esc(r[5] or '—')+'</td><td>'+esc(r[6] or '')+'</td></tr>' for r in runs)
    sec_more = ('<section class="section wash" id="evidence"><div class="aside"><div class="eyebrow">Evidence</div><h2>Excluded overhead and run history.</h2>'
                '<p>Nothing disappears quietly: excluded bills are listed with the rule that excluded them.</p></div><div>'
                '<details class="group"><summary class="btn btn-quiet">Overhead excluded · '+str(len(over))+' bills · ₹'+esc(inr(t['excluded_overhead']))+'</summary>'
                '<div class="scroll" style="margin-top:14px"><table class="log"><thead><tr><th>Bill</th><th>Vendor</th><th>Date</th><th style="text-align:right">Pre-GST</th><th>Why</th></tr></thead><tbody>'+
                over_rows+'</tbody></table></div></details>'
                '<div class="group" style="margin-top:34px"><h3><span>Recent checks</span></h3><div class="scroll"><table class="log"><thead><tr><th>Started (IST)</th><th>Trigger</th><th>Status</th><th>Email</th><th>Note</th></tr></thead><tbody>'+
                run_rows+'</tbody></table></div></div></div></section>')
    foot = ('<footer class="foot"><span><b>Sattva Books</b> · reads Zoho, never writes to it</span><span>Amounts are pre-GST INR at each document’s exchange rate.</span></footer>')
    return head+flash_html+hero+strip+(filter_bar(confirmed, potential, seen is not None) if confirmed or potential else '')+sec_conf+sec_pot+sec_acc+sec_more+foot+refresh+(bulk_bar(csrf)+FILTER_JS if confirmed or potential else '')+'</body></html>'
