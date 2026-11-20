"""Sattva Bills: isolated, review-only PDF inbox. No Zoho credentials or POST code."""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
from extraction import request_anthropic, configured
from review import render, validate_draft, init_db as init_reviews, DraftError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.parse import parse_qs, quote
from auth import OwnerSessions, cookie_value
from automation import Engine, Hold, MAX_PDF as AUTOMATIC_MAX_PDF, enabled as automation_enabled, render_status
import leakage_app

MAX_PDF = 12 * 1024 * 1024
PAGE_SIZE = 100

CSS = leakage_app.BASE_CSS + r'''
main { width:min(1040px,calc(100% - 48px)); margin:clamp(32px,5vw,64px) auto 80px; }
.eyebrow { font:500 11px/1.4 var(--mono); letter-spacing:.14em; text-transform:uppercase; color:var(--electric); }
h1 { margin:10px 0 6px; font-size:clamp(2rem,4.2vw,3.4rem); letter-spacing:-.04em; line-height:1.04; font-weight:700; }
.lede { font-size:16px; line-height:1.6; color:var(--slate); margin:12px 0 28px; max-width:680px; }
.upload { display:flex; flex-wrap:wrap; gap:18px; align-items:center; justify-content:space-between; padding:24px 28px; background:var(--wash); border:1px dashed #a9bdd6; border-radius:2px; }
.upload strong { display:block; font-size:16px; font-weight:600; margin-bottom:4px; }.upload .hint { font-size:13px; color:var(--slate); }
button { background:var(--electric); color:#fff; border:none; border-radius:2px; font:600 14px var(--sans); min-height:46px; padding:0 22px; cursor:pointer; }
button:hover { background:#0f63d8; }
button[disabled] { opacity:.5; cursor:not-allowed; }input[type=file] { position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); }
#message { min-height:26px; margin:12px 0 28px; color:var(--slate); font-size:13px; }#message.error { color:#a12727; }#message.ok { color:#0f7a4a; }
section { margin-top:36px; }.section-head { display:flex; align-items:baseline; justify-content:space-between; padding-bottom:12px; border-bottom:1px solid var(--line); gap:12px; }
h2 { font-size:22px; letter-spacing:-.03em; margin:0; font-weight:700; }.count { font:500 12px var(--mono); color:var(--slate); }
.grid-head { display:grid; grid-template-columns:minmax(0,1fr) 170px 160px; font:500 11px var(--mono); letter-spacing:.1em; text-transform:uppercase; color:var(--slate); padding:14px 0 10px; }
.invoice { display:grid; grid-template-columns:minmax(0,1fr) 170px 160px; align-items:center; padding:16px 0; border-top:1px solid var(--line); font-size:14px; }
.invoice a { color:var(--ink); font-weight:600; text-decoration:none; overflow-wrap:anywhere; }.invoice a:hover { color:var(--blue); text-decoration:underline; text-underline-offset:4px; }
.invoice .stamp { color:var(--slate); font:400 12px var(--mono); } .status { color:var(--ink); font-size:13px; }
.empty { padding:40px 0; color:var(--slate); font-size:14px; border-top:1px solid var(--line); }
.pagination { display:flex; flex-wrap:wrap; justify-content:space-between; gap:16px; align-items:center; margin-top:20px; font-size:13px; }.pagination a { color:var(--blue); padding:9px 0; }
main [role=alert]:empty { display:none; }
main [role=alert] { padding:12px 14px; border-left:3px solid #c62828; background:#fdecea; color:#8e1c1c; font-size:14px; }
footer { border-top:1px solid var(--line); padding:22px 0; margin-top:48px; color:var(--slate); font-size:12px; line-height:1.6; }
@media(max-width:640px){main{width:calc(100% - 32px)}.upload{align-items:stretch;padding:20px}.upload button{width:100%}.grid-head{display:none}.invoice{grid-template-columns:minmax(0,1fr) auto;row-gap:6px}.invoice .status{grid-column:1 / -1;grid-row:2}.stamp{text-align:right}.lede{font-size:15px}}
@media(prefers-reduced-motion:no-preference){button{transition:background .12s ease;}}
'''

JS = r'''
const fileInput = document.getElementById('pdf');
const button = document.getElementById('upload-btn');
const message = document.getElementById('message');
button.addEventListener('click', () => fileInput.click());
async function uploadInvoice(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) { show('Choose a PDF invoice.', true); return; }
    if (file.size > __MAX_PDF__) { show('Maximum file size is __MAX_MB__ MB.', true); return; }
  button.disabled = true; button.textContent = 'Uploading…'; show('Uploading ' + file.name + '…');
  try {
    const response = await fetch('/api/invoices', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type':'application/pdf', 'X-Filename':encodeURIComponent(file.name), 'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
      body: file
    });
    const data = await response.json();
    if (!response.ok) { show(data.error || 'Upload failed. Try again.', true); return; }
    if (data.status === 'queued' || data.status === 'existing') { window.location.assign('/review/'+data.id); }
    else { show('Saved. This invoice has not been sent to Zoho.'); window.location.reload(); }
  } catch (_) { show('Connection interrupted. Check the inbox before retrying.', true); }
  finally { button.disabled = false; button.textContent = 'Choose PDF'; fileInput.value=''; }
}
fileInput.addEventListener('change', () => uploadInvoice(fileInput.files && fileInput.files[0]));
const dropZone = document.querySelector('.upload');
dropZone.addEventListener('dragover', e => { e.preventDefault(); });
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  if (button.disabled) return;
  if (e.dataTransfer.files.length !== 1) { show('Drop one invoice PDF at a time.', true); return; }
  uploadInvoice(e.dataTransfer.files[0]);
});
function show(t, error=false) { message.textContent=t; message.className = error ? 'error' : 'ok'; message.setAttribute('role', error ? 'alert' : 'status'); }
'''


def require_settings():
    user = os.environ.get('APP_USERNAME', '')
    password = os.environ.get('APP_PASSWORD_HASH', '') or os.environ.get('APP_PASSWORD', '')
    data_dir = os.environ.get('DATA_DIR', '')
    if not user or len(password) < 16 or not data_dir or not Path(data_dir).is_absolute():
        raise RuntimeError('Set APP_USERNAME, APP_PASSWORD_HASH and absolute DATA_DIR. Local test passwords also require APP_ALLOW_LOCAL_HTTP=1.')
    local_http = os.environ.get('APP_ALLOW_LOCAL_HTTP') == '1'
    if local_http and os.environ.get('BIND', '127.0.0.1') != '127.0.0.1':
        raise RuntimeError('Local HTTP is allowed only on 127.0.0.1.')
    if not local_http and not os.environ.get('APP_PASSWORD_HASH', '').startswith('pbkdf2_sha256$'):
        raise RuntimeError('Production requires APP_PASSWORD_HASH and HTTPS.')
    return user, password, Path(data_dir)


def connect(db_path):
    db = sqlite3.connect(db_path, timeout=15)
    db.execute('PRAGMA busy_timeout=15000')
    return db


def init_db(db_path):
    with connect(db_path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS invoices (id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, pdf BLOB NOT NULL)')
        init_reviews(db)
        db.execute('CREATE TABLE IF NOT EXISTS suggestions (invoice_id TEXT PRIMARY KEY REFERENCES invoices(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL)')


def draft_revision(fields_json=None, saved_at=None):
    if fields_json is None:
        return '"new"'
    return '"'+hashlib.sha256((fields_json+'\0'+saved_at).encode('utf-8')).hexdigest()+'"'


def template(rows, csrf, total=None, page=1, pages=1, automation=None):
    total = len(rows) if total is None else total
    parts = []
    for doc_id, filename, when, review_saved in rows:
        safe_id = html.escape(doc_id, quote=True)
        display = html.escape(filename)
        ts = html.escape(when[:16].replace('T', ' ') + ' UTC')
        job = automation.read(doc_id) if automation else None
        status = html.escape(job['status'].replace('_', ' ').title()) if job else ('Ready to process' if automation else ('Manual draft saved · Not verified' if review_saved else 'Awaiting manual entry'))
        parts.append(f'<div class="invoice"><a href="/review/{safe_id}">{display}</a><span class="stamp">{ts}</span><span class="status">{status}</span></div>')
    content = ''.join(parts) if parts else '<div class="empty">No invoices yet. Upload the first PDF to start the review inbox.</div>'
    previous = f'<a href="/?page={page-1}" rel="prev">Previous page</a>' if page > 1 else '<span></span>'
    following = f'<a href="/?page={page+1}" rel="next">Next page</a>' if page < pages else '<span></span>'
    pagination = (f'<nav class="pagination" aria-label="Invoice pages">{previous}<span>Page {page} of {pages}</span>{following}</nav>' if pages > 1 else '')
    mode = 'Haiku → checked Zoho bills' if automation else 'Private preview · No posting'
    intro = 'Drop a vendor bill here. Haiku reads it, checks run automatically, and matching bills go to Zoho. Open an invoice below to see its result.' if automation else 'A private place for incoming supplier invoices. Upload a PDF, open the original and review it before any accounting action.'
    warning = ''
    if automation and os.environ.get('APP_AUTOMATION_POSTING') != '1':
        mode = 'Invoice checks · Posting pending acceptance'
        intro = 'Upload a supplier PDF to run its source checks. Creating bills in Zoho is waiting for release acceptance.'
    if automation and not automation.healthy():
        warning = '<p role="alert">Processing is temporarily unavailable. Saved originals are retained; processing resumes when the service recovers.</p>'
    if automation and hasattr(automation, 'limit_warnings'):
        warning += ''.join('<p role="alert" class="limit">'+html.escape(text)+'</p>' for text in automation.limit_warnings())
    footer = 'Original PDFs stay private. Duplicate bills are blocked. Unclear invoices show the reason here instead of being posted.' if automation else 'Review-only prototype. No AI extraction, payment functionality, Zoho authentication or bill-posting endpoints are included. Uploads must remain on a persistent private cloud volume before real invoices are used.'
    maximum = AUTOMATIC_MAX_PDF if automation else MAX_PDF
    max_mb = '5' if automation else '12'
    upload_js = JS.replace('__MAX_PDF__', str(maximum)).replace('__MAX_MB__', max_mb)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{html.escape(csrf, quote=True)}"><title>Bills inbox · Sattva Books</title><style>{CSS}</style></head><body>
{leakage_app.books_header('inbox')}
<main><div class="eyebrow">Bills inbox · {html.escape(mode)}</div><h1>Every bill, accounted for.</h1>
<p class="lede">{intro}</p>{warning}
<div class="upload"><div><strong>Drop a supplier invoice here</strong><div class="hint">PDF only · Maximum {max_mb} MB · Stored privately on the server</div></div><div><input id="pdf" type="file" accept=".pdf,application/pdf" aria-label="Select PDF invoice"><button id="upload-btn" type="button">Choose PDF</button></div></div><div id="message" role="status" aria-live="polite"></div>
<section><div class="section-head"><h2>Review inbox</h2><span class="count">{total} invoice{'s' if total != 1 else ''}</span></div><div class="grid-head"><span>Original PDF</span><span>Received (UTC)</span><span>Status</span></div>{content}{pagination}</section>
<footer>{footer}</footer></main><script>{upload_js}</script></body></html>'''


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, db, user, password, local_http=False):
        super().__init__(addr, handler)
        self.db_path = db
        self.extract_lock = threading.Lock()
        self.automation = Engine(db) if automation_enabled() else None
        if self.automation:
            self.automation.start()
        self.leakage = leakage_app.Monitor(db, Path(db).parent) if leakage_app.env_enabled() else None
        if self.leakage:
            self.leakage.start()
        if local_http and addr[0] != '127.0.0.1':
            raise ValueError('Insecure cookies require loopback binding.')
        self.auth = OwnerSessions(password)
        self.local_http = local_http
        self.session_cookie = 'sattva_session' if local_http else '__Host-sattva_session'
        self.login_cookie = 'sattva_login' if local_http else '__Host-sattva_login'

    def server_close(self):
        if self.automation:
            self.automation.close()
        if self.leakage:
            self.leakage.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SattvaReview/0.1'
    sys_version = ''

    def log_message(self, fmt, *args):
        # Do not log filenames, PDF contents, credentials or request URLs.
        print('request completed', flush=True)

    def reply(self, code, payload=b'', content_type='text/plain; charset=utf-8', headers=None):
        if content_type.startswith('text/html') and getattr(self, 'session', None):
            logout = ('<form method="post" action="/logout" style="display:inline;margin:0">'
                      '<input type="hidden" name="csrf" value="'+html.escape(self.session['csrf'], quote=True)+'">'
                      '<button type="submit" style="background:transparent;color:var(--accent);border:1px solid var(--line);padding:8px 12px;font-size:13px">Sign out</button></form>')
            payload = payload.replace(b'</header>', (logout+'</header>').encode(), 1)
            payload = payload.replace(b'</body>', b'<script>addEventListener("pageshow",e=>{if(e.persisted)location.reload();});</script></body>')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if not self.server.local_http:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def err(self, code, message):
        self.reply(code, json.dumps({'error': message}).encode(), 'application/json; charset=utf-8')

    def authorized(self):
        token = cookie_value(self.headers.get('Cookie', ''), self.server.session_cookie)
        self.session = self.server.auth.lookup(token)
        return self.session is not None

    def guard(self):
        if not self.authorized():
            path = urlsplit(self.path).path
            if self.command == 'GET' and not path.startswith('/api/'):
                self.reply(303, headers={'Location':'/login?next='+quote(self.safe_next(self.path),safe='')})
            elif self.command == 'POST' and (path == '/logout' or path.startswith(('/api/retry/', '/api/bank-debit/', '/api/reread/', '/leakage/'))):
                # Browser form buttons: return to sign-in, then to the same invoice page.
                back = '/leakage' if path.startswith('/leakage/') else '/review/'+path.rsplit('/', 1)[1] if path.startswith('/api/') else '/'
                self.reply(303, headers={'Location':'/login?next='+quote(self.safe_next(back),safe='')})
            else:
                self.err(401, 'Your session has ended. Sign in again; saved invoices and drafts are unchanged.')
            return False
        return True

    @staticmethod
    def safe_next(value):
        import re
        path = urlsplit(value or '/').path
        return path if path in ('/', '/leakage') or re.fullmatch(r'/(?:review|invoice)/[0-9a-f]{32}', path) else '/'

    def auth_cookie(self, name, value, max_age):
        return name+'='+value+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+str(max_age)+('' if self.server.local_http else '; Secure')

    def auth_peer(self):
        if self.server.local_http:
            return self.client_address[0]
        # Production is served only through Railway's HTTPS edge. Its socket
        # peers rotate, so bind nonces/throttles to its documented client IP.
        values = self.headers.get_all('X-Real-IP', [])
        if len(values) != 1:
            raise ValueError('Missing or ambiguous proxy client address')
        value = values[0].strip()
        if '%' in value:
            raise ValueError('Scoped proxy client addresses are unsupported')
        return str(ipaddress.ip_address(value))

    def login_page(self, message='', code=200, destination='/'):
        try: peer = self.auth_peer()
        except ValueError:
            self.err(400, 'Secure proxy client address unavailable. Please try again.');return
        token = self.server.auth.form(peer)
        esc = lambda s:html.escape(s,quote=True)
        page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Sattva Books</title><style>{CSS}
main{{max-width:460px;margin:clamp(30px,9vh,96px) auto;padding:0 18px;width:100%}}h1{{font-size:clamp(2rem,6vw,2.75rem);line-height:1.06}}label{{display:block;font-size:14px;font-weight:650;margin:28px 0 8px}}input[type=password]{{width:100%;font:400 17px var(--sans);padding:12px 13px;border:1px solid #a9bdd6;border-radius:2px}}.sign-in{{width:100%;margin-top:18px}}.login-message{{font-size:14px;line-height:1.5;color:#a12727;min-height:25px}}.privacy{{color:var(--slate);font-size:13px;line-height:1.6;margin-top:26px}}</style></head><body>
{leakage_app.books_header()}<main><div class="eyebrow">Sattva Books · owner access</div><h1>Sign in to Books.</h1><p class="lede">Vendor bills, Zoho checks and the leakage ledger.</p><p class="login-message" role="alert">{esc(message)}</p>
<form method="post" action="/login"><input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="next" value="{esc(self.safe_next(destination))}"><label for="password">Owner password</label><input id="password" name="password" type="password" autocomplete="current-password" maxlength="256" required><button class="sign-in" type="submit">Sign in</button></form><p class="privacy">For Sattva's owner only. Signing in opens the private review inbox; it does not approve or post a bill. You'll be asked to sign in again after 30 minutes away.</p></main><script>document.querySelector('form').addEventListener('submit',()=>{{const b=document.querySelector('button');b.disabled=true;b.textContent='Signing in…';}});</script></body></html>'''
        self.reply(code,page.encode(),'text/html; charset=utf-8',{'Set-Cookie':self.auth_cookie(self.server.login_cookie,token,600)})

    def auth_form(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0] != 'application/x-www-form-urlencoded':
            self.err(400,'Refresh the sign-in page and try again.');return None
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:length=0
        if not 0 < length <= 4096:
            self.err(400,'Refresh the sign-in page and try again.');return None
        try:return {k:v[0] for k,v in parse_qs(self.rfile.read(length).decode('utf8'),keep_blank_values=True).items()}
        except (UnicodeError,ValueError):self.err(400,'Refresh the sign-in page and try again.');return None

    def login_submit(self):
        form=self.auth_form()
        if form is None:return
        nonce=cookie_value(self.headers.get('Cookie',''),self.server.login_cookie)
        try: peer = self.auth_peer()
        except ValueError:
            self.err(400, 'Secure proxy client address unavailable. Please try again.');return
        token,error=self.server.auth.login(nonce,form.get('csrf',''),form.get('password',''),peer)
        if error:
            message={'invalid':'That password did not match. Try again.','throttled':'Too many sign-in attempts. Please wait 15 minutes before trying again.','form_expired':'This sign-in page expired. Please try again.'}[error]
            self.login_page(message,429 if error=='throttled' else 400 if error=='form_expired' else 401,form.get('next','/'));return
        old=cookie_value(self.headers.get('Cookie',''),self.server.session_cookie)
        self.server.auth.logout(old)
        self.reply(303,headers={'Location':self.safe_next(form.get('next')),'Set-Cookie':self.auth_cookie(self.server.session_cookie,token,43200)})

    def logout_submit(self):
        form=self.auth_form()
        if form is None:return
        if not secrets.compare_digest(form.get('csrf',''),self.session['csrf']):
            self.err(403,'Sign-out token missing. Refresh and try again.');return
        self.server.auth.logout(cookie_value(self.headers.get('Cookie',''),self.server.session_cookie))
        self.reply(303,headers={'Location':'/login','Set-Cookie':self.auth_cookie(self.server.session_cookie,'',0)})

    LEAKAGE_FLASH = {'started': ('Checking Zoho now. This takes a few minutes.', False), 'busy': ('A check is already running.', False),
                     'saved': ('Decision saved. It applies from the next check.', False), 'undone': ('Decision undone. It applies from the next check.', False),
                     'applied': ('Decision saved and applied. The list below already reflects it.', False),
                     'undone_applied': ('Decision undone. The item is back in the list.', False),
                     'off': ('Zoho read access is not configured on this server.', True)}

    def leakage_page(self, message='', bad=False, code=200):
        monitor = self.server.leakage
        query = parse_qs(urlsplit(self.path).query)
        if not message and query.get('m', [''])[0] in self.LEAKAGE_FLASH:
            message, bad = self.LEAKAGE_FLASH[query['m'][0]]
        if not monitor:
            body = leakage_app.page(None, None, None, [], [], self.session['csrf'], flash=message or self.LEAKAGE_FLASH['off'][0], flash_bad=True, enabled=False)
        else:
            report, started, email_status = monitor.latest_report()
            body = leakage_app.page(report, started, email_status, monitor.runs(), monitor.decision_rows(), self.session['csrf'],
                                    running=monitor.running(), flash=message, flash_bad=bad, daily_hour=monitor.daily_hour(),
                                    zoho_org=monitor.env.get('ZOHO_ORGANIZATION_ID', ''), previous=monitor.previous_report())
        self.reply(code, body.encode(), 'text/html; charset=utf-8')

    def leakage_post(self, path):
        form = self.auth_form()
        if form is None:
            return
        if not secrets.compare_digest(form.get('csrf', ''), self.session['csrf']):
            self.err(403, 'Form token missing. Refresh and try again.'); return
        monitor = self.server.leakage
        if not monitor:
            self.reply(303, headers={'Location': '/leakage?m=off'}); return
        if path == '/leakage/run':
            self.reply(303, headers={'Location': '/leakage?m='+('started' if monitor.run_now() else 'busy')}); return
        match = re.fullmatch(r'/leakage/decide/(write_off|settled|overhead|link|accept_loss)', path)
        if match:
            try:
                monitor.decide(match.group(1), form)
            except ValueError as exc:
                self.leakage_page(str(exc), True, 400); return
            self.reply(303, headers={'Location': '/leakage?m='+('applied' if monitor.reapply() else 'saved')}); return
        match = re.fullmatch(r'/leakage/revoke/([0-9]{1,12})', path)
        if match:
            monitor.revoke(match.group(1))
            self.reply(303, headers={'Location': '/leakage?m='+('undone_applied' if monitor.reapply() else 'undone')}); return
        self.err(404, 'Endpoint not found')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            healthy = not self.server.automation or self.server.automation.healthy()
            self.reply(200 if healthy else 503, b'ok' if healthy else b'processing unavailable')
            return
        if path == '/favicon.ico':
            self.reply(204)
            return
        if path.startswith('/static/'):
            name = path.removeprefix('/static/')
            kind = leakage_app.STATIC_FILES.get(name)
            if not kind:
                self.err(404, 'Not found'); return
            data = (leakage_app.STATIC/name).read_bytes()
            self.send_response(200)
            for k, v in (('Content-Type', kind), ('Content-Length', str(len(data))), ('Cache-Control', 'public, max-age=604800, immutable'),
                         ('X-Content-Type-Options', 'nosniff'), ('Cross-Origin-Resource-Policy', 'same-origin')):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
            return
        if not self.server.local_http and self.headers.get('X-Forwarded-Proto','').lower() != 'https':
            self.err(403,'Use the secure HTTPS address to sign in.');return
        if path == '/login':
            destination=parse_qs(urlsplit(self.path).query).get('next',['/'])[0]
            self.login_page(destination=destination)
            return
        if not self.guard():
            return
        if path == '/leakage':
            self.leakage_page()
            return
        if path == '/':
            try:
                values = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=8).get('page', ['1'])
                if len(values) != 1 or not re.fullmatch(r'[1-9][0-9]{0,6}', values[0]):
                    raise ValueError
                page = int(values[0])
            except ValueError:
                self.err(400, 'Choose a valid invoice page.')
                return
            with connect(self.server.db_path) as db:
                total = db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]
                pages = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
                page = min(page, pages)
                rows = db.execute('SELECT i.id, i.filename, i.created_at, r.invoice_id IS NOT NULL FROM invoices i LEFT JOIN reviews r ON r.invoice_id=i.id ORDER BY i.created_at DESC, i.id DESC LIMIT ? OFFSET ?', (PAGE_SIZE, (page-1)*PAGE_SIZE)).fetchall()
            self.reply(200, template(rows, self.session['csrf'], total, page, pages, self.server.automation).encode(), 'text/html; charset=utf-8')
        elif path.startswith('/review/'):
            doc_id = path.removeprefix('/review/')
            if len(doc_id) != 32 or any(ch not in '0123456789abcdef' for ch in doc_id):
                self.err(404, 'Invoice not found')
                return
            with connect(self.server.db_path) as db:
                row = db.execute('SELECT i.filename, r.fields_json, s.payload_json, r.saved_at FROM invoices i LEFT JOIN reviews r ON r.invoice_id=i.id LEFT JOIN suggestions s ON s.invoice_id=i.id WHERE i.id=?', (doc_id,)).fetchone()
            if not row:
                self.err(404, 'Invoice not found')
                return
            if self.server.automation:
                self.reply(200, render_status(doc_id, row[0], self.server.automation.read(doc_id), self.session['csrf']).encode(), 'text/html; charset=utf-8')
                return
            self.reply(200, render(doc_id, row[0], json.loads(row[1]) if row[1] else None, self.session['csrf'], json.loads(row[2]) if row[2] else None, configured(), draft_revision(row[1], row[3])).encode(), 'text/html; charset=utf-8')
        elif path.startswith('/invoice/'):
            doc_id = path.removeprefix('/invoice/')
            if len(doc_id) != 32 or any(ch not in '0123456789abcdef' for ch in doc_id):
                self.err(404, 'Invoice not found')
                return
            with connect(self.server.db_path) as db:
                row = db.execute('SELECT pdf FROM invoices WHERE id=?', (doc_id,)).fetchone()
            if not row:
                self.err(404, 'Invoice not found')
                return
            self.reply(200, row[0], 'application/pdf', {'Content-Disposition':'inline; filename="invoice.pdf"'})
        else:
            self.err(404, 'Page not found')

    def extract_pdf(self, doc_id):
        if len(doc_id) != 32 or any(c not in '0123456789abcdef' for c in doc_id):
            self.err(404, 'Invoice not found')
            return
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''), self.session['csrf']):
            self.err(403, 'Extraction token missing.')
            return
        if self.headers.get('Content-Length') not in (None, '0'):
            self.err(400, 'Extraction request body must be empty.')
            return
        if not configured():
            self.err(503, 'AI extraction is disabled.')
            return
        if not self.server.extract_lock.acquire(blocking=False):
            self.err(409, 'Another extraction is in progress.')
            return
        try:
            with connect(self.server.db_path) as db:
                row = db.execute('SELECT i.filename, i.pdf, s.payload_json FROM invoices i LEFT JOIN suggestions s ON s.invoice_id=i.id WHERE i.id=?', (doc_id,)).fetchone()
            if not row:
                self.err(404, 'Invoice not found')
                return
            if row[2]:
                result = json.loads(row[2])
                result['cached'] = True
            else:
                try:
                    result = request_anthropic(row[1],row[0],os.environ['ANTHROPIC_API_KEY'])
                except Exception:
                    self.err(502, 'AI extraction failed; draft unchanged.')
                    return
                with connect(self.server.db_path) as db:
                    db.execute('INSERT OR IGNORE INTO suggestions(invoice_id,payload_json,created_at) VALUES (?,?,?)',(doc_id,json.dumps(result,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
            self.reply(200,json.dumps(result,ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8')
        finally:
            self.server.extract_lock.release()

    def save_review(self, doc_id):
        if len(doc_id) != 32 or any(ch not in '0123456789abcdef' for ch in doc_id):
            self.err(404, 'Invoice not found')
            return
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.session['csrf']):
            self.err(403, 'Review token missing. Refresh and retry.')
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self.err(415, 'Expected JSON fields.')
            return
        if self.headers.get('Transfer-Encoding'):
            self.err(400, 'Chunked requests unsupported.')
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.err(411, 'Request length required.')
            return
        if length <= 0 or length > 16384:
            self.err(413, 'Review data too large or empty.')
            return
        try:
            fields = validate_draft(json.loads(self.rfile.read(length)))
        except (ValueError, UnicodeError) as e:
            self.err(400, str(e) if isinstance(e, DraftError) else 'Invalid review fields.')
            return
        revisions = self.headers.get_all('If-Match', [])
        if not revisions:
            self.err(428, 'Open the saved draft before saving changes.')
            return
        if len(revisions) != 1 or not re.fullmatch(r'"(?:new|[0-9a-f]{64})"', revisions[0]):
            self.err(400, 'Invalid draft version. Open the saved draft and compare your entries.')
            return
        fields_json = json.dumps(fields, ensure_ascii=False)
        saved_at = datetime.now(timezone.utc).isoformat()
        conflict = 'This draft changed in another tab or device. Your entries are still on this page. Open the saved draft in a new tab and compare before saving again.'
        with connect(self.server.db_path) as db:
            if not db.execute('SELECT 1 FROM invoices WHERE id=?', (doc_id,)).fetchone():
                self.err(404, 'Invoice not found')
                return
            current = db.execute('SELECT fields_json,saved_at FROM reviews WHERE invoice_id=?', (doc_id,)).fetchone()
            if not hmac.compare_digest(revisions[0], draft_revision(*current) if current else draft_revision()):
                self.err(409, conflict)
                return
            # The saved values are the compare-and-swap condition, so a concurrent
            # save between the SELECT and this write cannot overwrite newer work.
            if current:
                changed = db.execute('UPDATE reviews SET fields_json=?,saved_at=? WHERE invoice_id=? AND fields_json=? AND saved_at=?',
                    (fields_json, saved_at, doc_id, *current)).rowcount
            else:
                changed = db.execute('INSERT OR IGNORE INTO reviews(invoice_id,fields_json,saved_at) VALUES (?,?,?)',
                    (doc_id, fields_json, saved_at)).rowcount
            if changed != 1:
                self.err(409, conflict)
                return
        self.reply(200, json.dumps({'status':'manual_draft_saved', 'pdf_verified':False,
                   'posted_to_zoho':False, 'revision':draft_revision(fields_json, saved_at)}).encode(), 'application/json; charset=utf-8')

    def do_POST(self):
        if not self.server.local_http and self.headers.get('X-Forwarded-Proto','').lower() != 'https':
            self.err(403,'Use the secure HTTPS address to sign in.');return
        path = urlsplit(self.path).path
        if path == '/login':
            self.login_submit()
            return
        if not self.guard():
            return
        if path == '/logout':
            self.logout_submit()
            return
        if path.startswith('/leakage/'):
            self.leakage_post(path)
            return
        if path.startswith('/api/bank-debit/'):
            doc_id = path.removeprefix('/api/bank-debit/')
            if not self.server.automation or not re.fullmatch(r'[a-f0-9]{32}', doc_id):
                self.err(404, 'Invoice not found'); return
            form = self.auth_form()
            if form is None:
                return
            if not secrets.compare_digest(form.get('csrf', ''), self.session['csrf']):
                self.err(403, 'Form token missing. Refresh and try again.'); return
            try:
                self.server.automation.set_bank_debit(doc_id, form.get('transaction'), form.get('charges'))
            except Hold as exc:
                self.err(409, str(exc)); return
            self.reply(303, headers={'Location':'/review/'+doc_id})
            return
        if path.startswith('/api/reread/'):
            doc_id = path.removeprefix('/api/reread/')
            if not self.server.automation or not re.fullmatch(r'[a-f0-9]{32}', doc_id):
                self.err(404, 'Invoice not found'); return
            form = self.auth_form()
            if form is None:
                return
            if not secrets.compare_digest(form.get('csrf', ''), self.session['csrf']):
                self.err(403, 'Form token missing. Refresh and try again.'); return
            try:
                self.server.automation.reread(doc_id)
            except Hold as exc:
                self.err(409, str(exc)); return
            self.reply(303, headers={'Location':'/review/'+doc_id})
            return
        if path.startswith('/api/retry/'):
            doc_id = path.removeprefix('/api/retry/')
            if not self.server.automation or not re.fullmatch(r'[a-f0-9]{32}', doc_id):
                self.err(404, 'Invoice not found'); return
            form = self.auth_form()
            if form is None:
                return
            if not secrets.compare_digest(form.get('csrf', ''), self.session['csrf']):
                self.err(403, 'Retry token missing. Refresh and try again.'); return
            try:
                self.server.automation.retry(doc_id)
            except Hold as exc:
                self.err(409, str(exc)); return
            self.reply(303, headers={'Location':'/review/'+doc_id})
            return
        if path.startswith('/api/extract/'):
            if self.server.automation:
                self.err(409, 'Extraction is managed by automatic processing. Open the invoice status.'); return
            self.extract_pdf(path.removeprefix('/api/extract/'))
            return
        if path.startswith('/api/reviews/'):
            if self.server.automation:
                self.err(409, 'This invoice uses automatic processing; original extraction is preserved.'); return
            self.save_review(path.removeprefix('/api/reviews/'))
            return
        if path != '/api/invoices':
            self.err(404, 'Endpoint not found')
            return
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''), self.session['csrf']):
            self.err(403, 'Upload token missing or invalid. Refresh and retry.')
            return
        if self.headers.get('Transfer-Encoding'):
            self.err(400, 'Chunked uploads are unsupported.')
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/pdf':
            self.err(415, 'Choose a PDF invoice.')
            return
        try:
            size = int(self.headers.get('Content-Length',''))
        except ValueError:
            self.err(411, 'File size required.')
            return
        maximum = AUTOMATIC_MAX_PDF if self.server.automation else MAX_PDF
        if size < 8 or size > maximum:
            self.err(413, 'PDF must be nonempty and within the '+('5' if self.server.automation else '12')+' MB limit.')
            return
        filename = unquote(self.headers.get('X-Filename', 'invoice.pdf'))
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1].strip()
        if not filename.lower().endswith('.pdf') or len(filename) > 180 or not filename or any(ord(c) < 32 for c in filename):
            self.err(400, 'Invalid PDF filename.')
            return
        data = self.rfile.read(size)
        if len(data) != size or not data.startswith(b'%PDF-'):
            self.err(400, 'Not a readable PDF header.')
            return
        digest = hashlib.sha256(data).hexdigest()
        doc_id = secrets.token_hex(16)
        try:
            with connect(self.server.db_path) as db:
                db.execute('INSERT INTO invoices VALUES (?, ?, ?, ?, ?)', (doc_id, filename, digest, datetime.now(timezone.utc).isoformat(), data))
                if self.server.automation:
                    from automation import POLICY
                    intent = {'kind':'automatic_upload_intent','policy':POLICY,'id':doc_id,'pdf_sha256':digest}
                    db.execute('INSERT INTO suggestions(invoice_id,payload_json,created_at) VALUES(?,?,?)',
                        (doc_id,json.dumps(intent),datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError:
            if self.server.automation:
                with connect(self.server.db_path) as db:
                    existing = db.execute('SELECT id FROM invoices WHERE sha256=?', (digest,)).fetchone()
                if existing:
                    self.reply(200, json.dumps({'id':existing[0], 'status':'existing'}).encode(), 'application/json; charset=utf-8')
                    return
            self.err(409, 'This exact PDF was already uploaded. Check the inbox.')
            return
        if self.server.automation:
            try:
                self.server.automation.enqueue(doc_id)
            except Exception:
                self.err(503, 'Original saved, but processing could not start. Check the inbox before retrying.'); return
        self.reply(201, json.dumps({'id':doc_id, 'status':'queued' if self.server.automation else 'awaiting_review'}).encode(), 'application/json; charset=utf-8')


def main():
    user, password, folder = require_settings()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = folder / 'review.sqlite3'
    init_db(db)
    port = int(os.environ.get('PORT', '8080'))
    bind = os.environ.get('BIND', '127.0.0.1')
    server = Server((bind, port), Handler, db, user, password, local_http=os.environ.get('APP_ALLOW_LOCAL_HTTP')=='1')
    print(f'Sattva inbox ready on port {server.server_port}; automation={bool(server.automation)}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
