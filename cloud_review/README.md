# Cloud review inbox — phase 1, isolated prototype

**Status: draft code only, NOT deployed, NOT production-ready.** Deliberately no Zoho API credentials, GET or POST calls; no supplier extraction, financial logic, PDF attachment in Zoho, review decisions or approval workflow yet. The feature is a password-gated PDF inbox with content-hash exact duplicate rejection and private server persistence.

## Owner journey

1. Visit private URL and authenticate with configured credentials.
2. Choose a supplier invoice PDF (12 MB maximum), upload it, and see it in the review inbox.
3. Open the exact original PDF in a new browser tab for review; see that it is **awaiting review**, not posted.
4. Retrying an identical file is rejected as a duplicate. Invalid file, lost login, missing server storage and invalid credentials have explicit failure states. No auto-post capability exists.

## Run locally (Python 3.12 standard library only)

```shell
APP_USERNAME=owner APP_PASSWORD=<long-random-password> DATA_DIR=/absolute/private/path python app.py
```

On Windows, supply environment variables using the platform shell. Use `PORT` for Railway and `BIND=0.0.0.0` only behind HTTPS with secured external access.

## Production boundaries

- DO NOT DEPLOY TO A PUBLIC DOMAIN UNTIL you have chosen an authentication method suited to real personal financial data (HTTP Basic is **prototype-only**), created a **persistent private Railway volume** mounted at `DATA_DIR`, set secrets through Railway environment variables and completed a backup/restore drill. Railway app ephemeral disk is not safe for real invoices.
- Basic auth transmits user/password on each request (TLS must be enforced by fronting proxy). A mobile-friendly login and short-lived sessions are required before use with real supplier invoices.
- Review-only. No vendor extraction, tax calculations, currency conversion, historical migration, Zoho writes, or changes to the live Windows watcher.
- Never point this app at Sattva CRM storage, databases or domain. Create a separate Railway project, with approval for any paid spend and deployment.
- SQLite BLOB is used to keep PDF and record metadata together atomically; physical backup/restore remains to be verified before real use.

## Local verification

`python -m unittest discover -s cloud_review -p 'test_*.py' -v` from the repository root. Tests use only synthetic PDFs, an ephemeral port and a temporary SQLite database.