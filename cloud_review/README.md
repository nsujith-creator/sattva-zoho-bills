# Cloud review inbox — phase 1, isolated prototype

**Status: draft code only, NOT deployed, NOT production-ready.** Deliberately no Zoho API credentials, GET or POST calls; no supplier extraction, financial logic, PDF attachment in Zoho, review decisions or approval workflow yet. The feature is a password-gated PDF inbox with content-hash exact duplicate rejection and private server persistence.

## Owner journey

1. Visit private URL and authenticate with configured credentials.
2. Choose a supplier invoice PDF (12 MB maximum), upload it, and see it in the review inbox.
3. Open the exact original PDF in a new browser tab for review; see that it is **awaiting review**, not posted.
4. Retrying an identical file is rejected as a duplicate. Invalid file, lost login, missing server storage and invalid credentials have explicit failure states. No auto-post capability exists.

## Run locally (Python 3.12 standard library only)

```shell
APP_USERNAME=owner APP_PASSWORD=<synthetic-local-test-password> APP_ALLOW_LOCAL_HTTP=1 BIND=127.0.0.1 DATA_DIR=/absolute/private/path python app.py
```

On Windows, supply environment variables using the platform shell. This loopback page is for development, not the owner credential setup. Production requires `APP_PASSWORD_HASH`, an absolute private `DATA_DIR`, and HTTPS through the trusted Railway proxy. Raw `APP_PASSWORD` is refused outside explicit loopback mode.

## Production boundaries

- DO NOT DEPLOY TO A PUBLIC DOMAIN UNTIL owner credentials have been securely set and live HTTPS verified, a **persistent private Railway volume** has been created mounted at `DATA_DIR`, set secrets through Railway environment variables and completed a backup/restore drill. Railway app ephemeral disk is not safe for real invoices.
- Owner sign-in now uses a salted PBKDF2-SHA256 verifier (600,000 iterations), revocable random sessions, secure HTTP-only same-site cookies, CSRF protection, logout and login throttling. Sessions expire after 30 minutes idle or 12 hours total. HTTP Basic is refused.
- Review-only. No vendor extraction, tax calculations, currency conversion, historical migration, Zoho writes, or changes to the live Windows watcher.
- Never point this app at Sattva CRM storage, databases or domain. Create a separate Railway project, with approval for any paid spend and deployment.
- SQLite BLOB is used to keep PDF and record metadata together atomically; physical backup/restore remains to be verified before real use.

## Local verification

`python -m unittest discover -s cloud_review -p 'test_*.py' -v` from the repository root. Tests use only synthetic PDFs, an ephemeral port and a temporary SQLite database.
## Manual invoice draft review (PR #13 additional slice)

After upload, select an invoice in the inbox. A draft form captures supplier, GSTIN, invoice number/date, invoice currency, subtotal, CGST, SGST, IGST, discount/credit, printed payable and notes. Save preserves exact decimal amounts in a separate SQLite table. It checks only the typed arithmetic, rejects mismatches and unsafe input, and preserves the earlier valid draft when an edit fails. Status explicitly says **manual draft saved, NOT source verified**. The original PDF is available separately.

No extraction, source-PDF parity, purchase bill approval, tax eligibility judgment, Zoho GET/POST, or financial posting is present. This is strictly an internal review prototype; do not deploy for real invoices until production authentication, backups, private volumes and access policies have been verified.

## Draft AI extraction (opt-in, unverified)

A saved invoice PDF can be submitted to Anthropic Claude Haiku `claude-haiku-4-5` **only after the authenticated user clicks Extract with AI**. This feature is **OFF by default**. Set `APP_ENABLE_AI_EXTRACTION=1` and a private server-side `ANTHROPIC_API_KEY` to expose the action. Each first click can incur an API charge. Provider availability and PDF extraction accuracy have **not** been live-tested in this PR.

Extraction returns suggestions only. They are stored separately from the manually saved draft; the user may fill *empty fields* but existing edits are never overwritten. Ambiguous/multi-invoice PDFs are flagged or held; GST, reverse charge, ITC, vendor identity, FX, source correctness and final payable are **not certified**. No Zoho request is possible from this module. Failed API calls do not change saved drafts. Retrying a successfully cached response does not call the provider again. All actual financial posting remains disabled.

Offline tests use mocked provider responses and synthetic values. Local implementation verification performed on 23 Oct 2026: 23 tests passed; the extraction comparison screen rendered and its fill-empty interaction passed at 320/360/390/768/1440px. Tests validate protection, not real invoice accuracy. Do not provision or enable production credentials, attach real invoices or deploy until the service's authentication, persistent private storage, restore drill, provider permission/cost and actual invoice-matching gates pass.

## Owner session deployment requirements

Set the password through a private setup interface; never paste it into chat, command history, logs or a repository. `auth.password_hash` produces the server verifier; supply it privately as `APP_PASSWORD_HASH`. Do not store the raw production password. No real owner credential has been created by these tests.

Use one process and one replica: session and throttle state is in memory. Restarting invalidates sessions but preserves invoices and drafts. Only expose the app behind the trusted HTTPS proxy; the app requires `X-Forwarded-Proto: https` in production. Do not expose its origin directly. Five failed logins from one peer in 15 minutes, or 20 globally in one minute, stop further attempts; the proxy may make peers share that limit. Login nonce state and sessions are bounded. A proxy/server restart clears throttling, so edge protection remains a production verification item.

Local tests use synthetic passwords and PDFs only. Production provisioning, owner credential setup, persistent cloud restore, live HTTPS and remote-device verification remain outstanding. Authentication tests do not certify invoice extraction or accounting correctness, and posting remains unavailable.
