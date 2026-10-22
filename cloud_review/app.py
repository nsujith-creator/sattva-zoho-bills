"""Sattva Bills: isolated, review-only PDF inbox. No Zoho credentials or POST code."""
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
from review import render, validate_draft, init_db as init_reviews, DraftError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_PDF = 12 * 1024 * 1024

CSS = r'''
:root { color-scheme:light; --ink:#112033; --muted:#5a6b7f; --line:#d9e0e8; --accent:#245ddc; --paper:#fff; --wash:#f3f6fa; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:var(--paper); font-family:system-ui,-apple-system,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased; }
header { display:flex; justify-content:space-between; align-items:center; padding:17px clamp(18px,4vw,64px); border-bottom:1px solid var(--line); }
.brand { font-size:15px; letter-spacing:-.3px; font-weight:760; }.brand span { color:var(--muted); font-weight:500; margin-left:12px; border-left:1px solid #aab6c4; padding-left:12px; }
.mode { font-size:11px; letter-spacing:.12em; font-weight:730; color:#506274; text-transform:uppercase; }
main { width:min(960px,calc(100% - 36px)); margin:clamp(36px,7vw,88px) auto 100px; }
.eyebrow { font-weight:750; letter-spacing:.13em; text-transform:uppercase; color:var(--accent); font-size:11px; }
h1 { margin:12px 0 8px; font-size:clamp(36px,7vw,64px); letter-spacing:-.055em; line-height:1.03; font-weight:730; }
.lede { font-size:16px; line-height:1.55; color:var(--muted); margin:16px 0 36px; max-width:650px; }
.upload { display:flex; flex-wrap:wrap; gap:18px; align-items:center; justify-content:space-between; padding:28px; background:var(--wash); border:1px dashed #a6b6c8; border-radius:12px; }
.upload strong { display:block; font-size:16px; margin-bottom:6px; }.upload .hint { font-size:13px; color:var(--muted); }
button { background:var(--accent); color:white; border:none; border-radius:8px; font:700 14px system-ui,sans-serif; min-height:46px; padding:11px 20px; cursor:pointer; }
button:hover { background:#1949b9; }button:focus-visible,a:focus-visible,input:focus-visible { outline:3px solid #ff9b35; outline-offset:3px; }
button[disabled] { opacity:.5; cursor:not-allowed; }input[type=file] { position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); }
#message { min-height:26px; margin:12px 0 40px; color:var(--muted); font-size:13px; }#message.error { color:#a12727; }#message.ok { color:#125e48; }
section { margin-top:48px; }.section-head { display:flex; align-items:baseline; justify-content:space-between; padding-bottom:14px; border-bottom:1px solid var(--line); gap:12px; }
h2 { font-size:21px; letter-spacing:-.03em; margin:0; }.count { color:var(--muted); font-size:12px; }
.grid-head { display:grid; grid-template-columns:minmax(0,1fr) 150px 140px; color:#66758a; font-size:11px; font-weight:750; text-transform:uppercase; letter-spacing:.08em; padding:16px 0 10px; }
.invoice { display:grid; grid-template-columns:minmax(0,1fr) 150px 140px; gap:0; align-items:center; padding:19px 0; border-top:1px solid #e7edf2; font-size:13px; }
.invoice a { color:var(--ink); font-weight:650; text-decoration:underline; text-decoration-color:#bbc8d6; text-underline-offset:4px; overflow-wrap:anywhere; }
.invoice .stamp { color:var(--muted); } .status { color:#43566b; font-size:12px; }
.empty { padding:50px 0; color:var(--muted); font-size:14px; }
footer { border-top:1px solid var(--line); padding:25px 0; margin-top:60px; color:var(--muted); font-size:12px; line-height:1.5; }
@media(max-width:640px){header{padding:17px 18px}.brand span{display:block;border:0;padding:0;margin:3px 0 0;font-size:12px}.upload{align-items:stretch;padding:20px}.upload button{width:100%}.grid-head{display:none}.invoice{grid-template-columns:minmax(0,1fr) auto;row-gap:9px}.invoice .status{grid-column:1 / -1;grid-row:2}.stamp{text-align:right} .lede{font-size:15px}}
@media(prefers-reduced-motion:no-preference){button{transition:background .12s ease;}}
'''

JS = r'''
const fileInput = document.getElementById('pdf');
const button = document.getElementById('upload-btn');
const message = document.getElementById('message');
button.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) { show('Choose a PDF invoice.', true); return; }
  if (file.size > 12 * 1024 * 1024) { show('Maximum file size is 12 MB.', true); return; }
  button.disabled = true; button.textContent = 'Uploading…'; show('Uploading ' + file.name + '…');
  try {
    const response = await fetch('/api/invoices', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type':'application/pdf', 'X-Filename':encodeURIComponent(file.name), 'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
      body: file
    });
    const data = await response.json();
    if (!response.ok) { show(data.error || 'Upload failed. Try again.', true); return; }
    show('Saved. This invoice has not been sent to Zoho.');
    window.location.reload();
  } catch (_) { show('Connection interrupted. Check the inbox before retrying.', true); }
  finally { button.disabled = false; button.textContent = 'Choose PDF'; fileInput.value=''; }
});
function show(t, error=false) { message.textContent=t; message.className = error ? 'error' : 'ok'; message.setAttribute('role', error ? 'alert' : 'status'); }
'''


def require_settings():
    user = os.environ.get('APP_USERNAME', '')
    password = os.environ.get('APP_PASSWORD', '')
    data_dir = os.environ.get('DATA_DIR', '')
    if not user or len(password) < 16 or not data_dir or not Path(data_dir).is_absolute():
        raise RuntimeError('Set APP_USERNAME, APP_PASSWORD (16+ chars), absolute DATA_DIR. Refusing insecure startup.')
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


def template(rows, csrf):
    parts = []
    for doc_id, filename, when, review_saved in rows:
        safe_id = html.escape(doc_id, quote=True)
        display = html.escape(filename)
        ts = html.escape(when[:16].replace('T', ' ') + ' UTC')
        parts.append(f'<div class="invoice"><a href="/review/{safe_id}">{display}</a><span class="stamp">{ts}</span><span class="status">{"Manual draft saved · Not verified" if review_saved else "Awaiting manual entry"}</span></div>')
    content = ''.join(parts) if parts else '<div class="empty">No invoices yet. Upload the first PDF to start the review inbox.</div>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{html.escape(csrf, quote=True)}"><title>Sattva · Bills Review</title><style>{CSS}</style></head><body>
<header><div class="brand">SATTVA <span>Bills review</span></div><div class="mode">Private preview · No posting</div></header>
<main><div class="eyebrow">INVOICE CONTROL</div><h1>Every bill,<br>accounted for.</h1>
<p class="lede">A private place for incoming supplier invoices. Upload a PDF, open the original and review it before any accounting action.</p>
<div class="upload"><div><strong>Add a supplier invoice</strong><div class="hint">PDF only · Maximum 12 MB · Stored privately on the server</div></div><div><input id="pdf" type="file" accept=".pdf,application/pdf" aria-label="Select PDF invoice"><button id="upload-btn" type="button">Choose PDF</button></div></div><div id="message" role="status" aria-live="polite"></div>
<section><div class="section-head"><h2>Review inbox</h2><span class="count">{len(rows)} invoice{'s' if len(rows) != 1 else ''}</span></div><div class="grid-head"><span>Original PDF</span><span>Received (UTC)</span><span>Status</span></div>{content}</section>
<footer>Review-only prototype. No AI extraction, payment functionality, Zoho authentication or bill-posting endpoints are included. Uploads must remain on a persistent private cloud volume before real invoices are used.</footer></main><script>{JS}</script></body></html>'''


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, db, user, password):
        super().__init__(addr, handler)
        self.db_path = db
        self.username = user
        self.password = password
        self.csrf = hmac.new(password.encode('utf8'), b'sattva-review-upload-v1', hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SattvaReview/0.1'
    sys_version = ''

    def log_message(self, fmt, *args):
        # Do not log filenames, PDF contents, credentials or request URLs.
        print('request completed', flush=True)

    def reply(self, code, payload=b'', content_type='text/plain; charset=utf-8', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def err(self, code, message):
        self.reply(code, json.dumps({'error': message}).encode(), 'application/json; charset=utf-8')

    def authorized(self):
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode('utf8')
            name, password = decoded.split(':', 1)
        except (ValueError, UnicodeError):
            return False
        return (secrets.compare_digest(name, self.server.username)
                and secrets.compare_digest(password, self.server.password))

    def guard(self):
        if not self.authorized():
            self.reply(401, b'Authentication required', headers={'WWW-Authenticate':'Basic realm="Sattva Bills", charset="UTF-8"'})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            self.reply(200, b'ok')
            return
        if not self.guard():
            return
        if path == '/':
            with connect(self.server.db_path) as db:
                rows = db.execute('SELECT i.id, i.filename, i.created_at, r.invoice_id IS NOT NULL FROM invoices i LEFT JOIN reviews r ON r.invoice_id=i.id ORDER BY i.created_at DESC, i.id DESC LIMIT 500').fetchall()
            self.reply(200, template(rows, self.server.csrf).encode(), 'text/html; charset=utf-8')
        elif path.startswith('/review/'):
            doc_id = path.removeprefix('/review/')
            if len(doc_id) != 32 or any(ch not in '0123456789abcdef' for ch in doc_id):
                self.err(404, 'Invoice not found')
                return
            with connect(self.server.db_path) as db:
                row = db.execute('SELECT i.filename, r.fields_json FROM invoices i LEFT JOIN reviews r ON r.invoice_id=i.id WHERE i.id=?', (doc_id,)).fetchone()
            if not row:
                self.err(404, 'Invoice not found')
                return
            self.reply(200, render(doc_id, row[0], json.loads(row[1]) if row[1] else None, self.server.csrf).encode(), 'text/html; charset=utf-8')
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

    def save_review(self, doc_id):
        if len(doc_id) != 32 or any(ch not in '0123456789abcdef' for ch in doc_id):
            self.err(404, 'Invoice not found')
            return
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.csrf):
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
        with connect(self.server.db_path) as db:
            if not db.execute('SELECT 1 FROM invoices WHERE id=?', (doc_id,)).fetchone():
                self.err(404, 'Invoice not found')
                return
            db.execute('INSERT INTO reviews(invoice_id,fields_json,saved_at) VALUES (?,?,?) '
                       'ON CONFLICT(invoice_id) DO UPDATE SET fields_json=excluded.fields_json,saved_at=excluded.saved_at',
                       (doc_id, json.dumps(fields, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        self.reply(200, b'{"status":"manual_draft_saved","pdf_verified":false,"posted_to_zoho":false}', 'application/json; charset=utf-8')

    def do_POST(self):
        if not self.guard():
            return
        path = urlsplit(self.path).path
        if path.startswith('/api/reviews/'):
            self.save_review(path.removeprefix('/api/reviews/'))
            return
        if path != '/api/invoices':
            self.err(404, 'Endpoint not found')
            return
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''), self.server.csrf):
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
        if size < 8 or size > MAX_PDF:
            self.err(413, 'PDF must be under 12 MB and nonempty.')
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
        except sqlite3.IntegrityError:
            self.err(409, 'This exact PDF was already uploaded. Check the inbox.')
            return
        self.reply(201, json.dumps({'id':doc_id, 'status':'awaiting_review'}).encode(), 'application/json; charset=utf-8')


def main():
    user, password, folder = require_settings()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = folder / 'review.sqlite3'
    init_db(db)
    port = int(os.environ.get('PORT', '8080'))
    bind = os.environ.get('BIND', '127.0.0.1')
    server = Server((bind, port), Handler, db, user, password)
    print(f'Sattva review-only inbox ready on port {server.server_port}; Zoho integration disabled', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()