# Vendor-cost → customer-invoice leakage reconciliation — design

Status: owner-approved approach (A), 19 Nov 2026. Read-only. Not deployed.

## Intent

Owner has **zero tolerance** for paying a vendor for a shipment without billing the
customer and collecting. Find every unbilled or underbilled shipment cost, late vendor
cost, unpaid customer invoice and vendor payment not yet recovered. Show actual INR
exposure with supporting Zoho records. Separate **confirmed leakage**, **potential
issues** and **accepted commercial losses**. Never report "clean" without complete
evidence; uncertain matches are never silently cleared.

## Owner decisions (interview, 19 Nov 2026)

| Topic | Decision |
|---|---|
| Where | Module inside existing `cloud_review` app (Railway), approach A |
| Match key | Sales invoices carry carrier **MBL and/or container numbers in line text / notes** (no dedicated field historically). Vendor bills carry MBL in `reference_number` (`src/zoho_bills.py` build_payload). |
| Going forward | Whoever invoices puts the MBL in the sales invoice **Reference#**. Reference# is primary key; text scan is fallback. |
| Shape | **1 shipment ↔ 1 customer** (one or more invoices to that customer). One shipment may have **several vendor bills**. |
| Leak types | All four: cost with no invoice; invoiced but unpaid; billed below cost; cost added after invoicing. Plus vendor-paid-not-recovered cash gap. |
| Scope | All shipment costs must be re-billed. Overheads excluded. |
| Timing | Visible from **day 0** ("awaiting invoice"); escalates to **LEAK at day 7** from bill date. |
| Coverage | FY26-27 (bills dated ≥ 2026-04-01), then daily monitoring. |
| Alert | **Daily email** (always sent — status line says COMPLETE / INCOMPLETE; silence must never mean clean). |

## Critical caveat: account classification

COGS account (914109000000000567) routing became reliable only **12 Nov 2026**. Earlier
bills may have shipment costs on other accounts and overheads on COGS. Therefore the
classification is **by vendor + shipment key, account is supplemental**:

- Overhead (excluded) ONLY if: vendor is a known overhead vendor (first word in
  `SOFTWARE_VENDORS ∪ ADVERTISING_VENDORS` from `automation.py`, plus Google India)
  AND the bill has no shipment key; or the owner recorded an overhead decision
  (per bill, or per vendor for key-less bills) with a reason.
- Everything else is an in-scope shipment cost by default. Unknown overhead
  (rent, phone…) shows as "potential: no shipment key" until the owner classifies it.
- Excluded items are still listed (count + INR) — nothing disappears silently.

## Matching rules

- Key normalisation: uppercase, alphanumerics only. Valid MBL key: ≥ 8 chars and
  contains a digit; otherwise "weak key" = treated as no key.
- Container numbers: ISO 6346 `[A-Z]{3}[UJZ]\d{7}` **with check-digit validation**.
- Bill keys: `reference_number`, plus MBL/containers in line descriptions/notes.
- Invoice text: reference_number, notes, line descriptions, custom field values.
  Candidates = each alnum token and joins of 2–3 adjacent tokens (handles
  "OOLU 2712 345678").
- **Exact** = candidate equals key → match. **Near** (one contains the other, len ≥ 8,
  e.g. "OOLU2712345678" vs "2712345678") → uncertain, flagged, never cleared.
- Zoho-native links (invoice line `bill_id`, bill line `invoice_id`) count as exact.
- Shipment = union of bills sharing an MBL or container key.
- Invoice matching several shipments, or shipment with invoices to >1 customer →
  uncertain (1:1 violation), flagged.
- Draft invoices are not billing (flag "draft only"). Void invoices/bills ignored
  (noted). Credit notes reduce revenue; tied via applied-invoice ids or text keys;
  untied credit notes = potential issue.
- Owner **Link** (bill → invoice) with mandatory reason is shown as "owner-linked".

## Money

All INR, pre-GST, × document `exchange_rate`.
- Cost = Σ in-scope bill line `item_total` × rate.
- Revenue = Σ non-void, non-draft invoice `sub_total` × rate − matched credit notes.
- Vendor paid (pre-GST, proportional) = cost × (total − balance)/total.
- Customer received (pre-GST, proportional) = revenue × (total − balance)/total.

## Categories

**Confirmed leakage**
- UNBILLED: shipment cost, valid key, no exact invoice, age ≥ 7 days. Exposure = cost.
- LATE COST NOT RE-BILLED: bill dated after the shipment's last invoice, age ≥ 7,
  no newer invoice, and the shipment is below cost. Exposure = min(late cost, shortfall).
- UNRECOVERED VENDOR PAYMENT: vendor paid > customer received and the customer invoice
  is past due. Exposure = gap.

**Potential issues** (need owner eye)
- Awaiting invoice (age < 7), no/weak shipment key, uncertain/near match, draft only,
  multi-customer / multi-shipment invoice, untied credit note, below cost without
  late cost (until owner marks commercial loss), late cost while margin still positive,
  overdue receivable where vendor not yet paid, incomplete source data.

**Accepted commercial losses**: below-cost shipments the owner marked with a reason.
Decision is bound to a fingerprint of the shipment's bill/invoice ids + amounts; any
change re-opens it.

## Completeness / failure handling

- Lists fully paginated with `has_more_page` verification (pattern of `Zoho.all`).
- Every needed detail fetched or served from cache with identical
  `last_modified_time`; otherwise run = **INCOMPLETE** with reasons.
- Per-run API budget (Zoho org daily limits; shared with the posting engine) and
  ~1 req/sec pacing; cache persists so the first backfill may finish over several runs,
  each honestly marked INCOMPLETE until done.
- Each run stored immutably (report JSON) for audit.

## Safety

- Separate read-only client: GET only, endpoint allowlist
  `(bills|invoices|creditnotes)(/\d+)?`, no payload. Tests prove non-GET is impossible.
- Separate read-only refresh token `ZOHO_RECON_REFRESH_TOKEN` (scopes bills/invoices/
  creditnotes/customerpayments/contacts/settings READ). Posting token untouched.
- No Zoho writes, no vendor/customer edits.

## Ongoing monitoring

- Background daily run in the app (IST hour configurable) + "Run now" on `/leakage`.
- `/leakage` page: run status + evidence counts, exposure per category, tables with
  supporting records, decision forms (link / overhead / accepted loss, reason required).
- Email via Resend HTTP API only if `RESEND_API_KEY`, `LEAKAGE_EMAIL_TO`,
  `LEAKAGE_EMAIL_FROM` set (needs owner approval); otherwise page only and the run
  records "email not configured".
- CLI `python cloud_review/leakage.py --once --out <file>` for the historical pass.

## Environment (set up 19 Nov 2026)

- Cloud environment **sattva-zoho-bills** allows `www.zohoapis.in`, `accounts.zoho.in`.
- Env vars: `ZOHO_ORGANIZATION_ID`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`,
  `ZOHO_RECON_REFRESH_TOKEN` (read-only Self Client token, same client as Railway app).

## Approvals still required

Railway: add `ZOHO_RECON_REFRESH_TOKEN`; email provider key; merge/deploy (main
auto-deploys). Client-secret rotation (was pasted in chat) — coordinated later.
