# Klickity Klack — Design Document

*Property-management mail-intake system for Karen Lacks & Co. Last updated 2026-06-30.*

---

## 1. Purpose

Klickity Klack turns the daily flood of physical mail and email into clean, searchable,
paid-tracked records for a ~40-property real-estate portfolio held across several trusts
and LLCs. It is **AI/intake-forward**: scan or receive a document, let Claude classify it,
review/confirm in the browser, and file it under Karen's 13-year naming convention.

Design posture (see §9): **own the intake + the property data model; lean on ERPNext for
the heavy accounting/ledger lifecycle** rather than rebuild Yardi/AppFolio from scratch.

---

## 2. Architecture

```
ScanSnap ─┐                          ┌─ classifier.py (Claude: classify + extract)
Gmail ────┼─► watched folders ─► watcher.py ─► enqueue_scan ─► splitter.py (boundary split)
          │                                          │
          └─ email_poller.py (IMAP pull)             ▼
                                          Review Queue (in-memory + pending_scans table)
                                                     │  staff review/confirm in browser
                                                     ▼
                                          intake_log (SQLite)  +  filed PDF (Entity/Property/Year)
```

**Components**
| File | Role |
|---|---|
| `app.py` | Flask app: all routes, review queue, batch logic, filing, nav-context, background threads |
| `database.py` | SQLite access (`intake.db`), schema/migrations, all `intake_log` queries. WAL mode. |
| `property_model.py` | **New** relational core: entities / properties / units / vendors (§4.2) |
| `classifier.py` | Claude classification + extraction; `DOMAIN_RULES` distilled from the schema doc; regex fallback |
| `splitter.py` | Splits multi-doc PDFs via Claude boundary detection (keeps same-sender correspondence together) |
| `watcher.py` | watchdog folder watcher (fires on new files) |
| `email_poller.py` | Gmail IMAP pull → saves attachments + a `.meta.json` sidecar (received date, sender, subject, body) |
| `templates/` | Jinja2 UI (`base.html` shell + per-page templates) |
| `config.json` | Current source of truth for entities, properties, bill types, vendors, staff, API keys, folders |

**Runtime**
- Flask, `host=0.0.0.0`, `port=5050`, `debug=False`. Embedded Python at `python/python.exe`.
- Runs on **LACKSSERVER2021** (`F:\File-Cabinet\IntakeSystem`), reachable at `http://192.168.10.240:5050`.
- The workstation sees the same tree as the mapped drive `T:\IntakeSystem`.
- Background threads: folder watchers (scan + email), Gmail poller (5-min loop), startup catch-up sweep.

---

## 3. Deployment & operations

- **Service:** `install_service.bat` (+ `install_service.ps1`) registers a Scheduled Task
  `KlickityKlack` that runs `run_service.bat` as SYSTEM at boot (+1-min delay,
  restart-on-failure, no battery gating). Logs to `klickity_klack.log`.
- **Boot resilience:** also set `bcdedit /set {current} bootstatuspolicy ignoreallfailures`
  and `recoveryenabled No` so an unclean power-loss reboot doesn't halt at a recovery screen.
- **In-app restart:** the Restart button / `POST /api/restart` calls `os._exit(42)`; the bat
  loop relaunches. Code and template changes require a restart (`debug=False` caches Jinja).
- **Review-queue persistence:** the in-memory queue is mirrored to the `pending_scans` table
  (WAL + busy_timeout), restored on startup — a restart no longer loses the queue.
- **⚠ WAL-over-SMB caveat:** `intake.db` is WAL. SQLite WAL is **not** safe across hosts.
  Never run DB-writing scripts against the live DB from the workstation — run them **on the
  server**, or stop the app first, or via a server-side in-app route. (A bulk rename once
  wedged this way; the work committed but the process hung on exit.)

---

## 4. Data model

### 4.1 `intake_log` (the record of every processed document)
Key columns: `date_received, scanned_date, batch_number, mail_type, entity_code,
entity_name, property_code` (comma-separated for multi-property), `doc_type, bill_type_code,
vendor, amount, due_date, invoice_number, account_number, status, notes, pdf_path, filed_path,
ai_confidence`. Payment: `paid_date, paid_amount, paid_by, payment_method, check_number,
payment_note`. Source email: `email_from, email_subject, email_date, email_body`.
Supporting tables: `pending_scans` (queue), `vendor_hints`, `account_hints`, `activity_log`,
`filtered_docs`, `attachments`, `processed_emails`, `bug_reports`.

### 4.2 Relational core (`property_model.py`) — the new backbone
Additive tables that sit alongside config.json; readers migrate over gradually. Every table
carries a nullable `erpnext_*` column so records can be linked to ERPNext later without a
schema change.

- **entities** — the owning/accounting vehicles. `code` (TRUST1, KLR, LRC, PP, PERS-KR…),
  `name`, `kind` (trust|llc|personal|office), `erpnext_company`.
- **properties** — `code` (T27, TL222, PP16…), `address`, `entity_id`→entities, `cluster`,
  **`unit_count`** (the #2 split weight), `property_type`, `status` (active|inactive),
  `erpnext_asset`.
- **units** — `unit_code` (T027-12), `property_id`→properties, `label`, `beds/baths/sqft`,
  `status`. *Empty for now* (populated when real unit data is entered); `unit_count` carries
  the weight in the meantime.
- **vendors** — canonical `name`, `category`, `default_bill_type`, `erpnext_supplier`;
  **vendor_aliases** maps the 3,493 archive spellings → one vendor.

**Seeding** (idempotent, `INSERT OR IGNORE`): entities/properties from config.json;
`unit_count` from `PROXY_UNIT_COUNTS` (filename-derived best guess, Karen corrects); vendors
from distinct payees already in `intake_log`. `T44` seeds `inactive` from its config note.

### 4.3 Ownership & split logic
`entity_unit_weights(property_codes)` sums each entity's unit counts across a bill's
properties → the proportional weight for the **#2 split** (a bill on a 42-unit and a 2-unit
property splits ~95/5, not 50/50). Split methods planned: **Even / By units / Manual**, all
editable, must reconcile to the total (penny-rounding handled).

---

## 5. Filename convention (Karen's 13-year standard)

`_make_filename()` emits, and the archive follows:
```
MM-DD-YY  <ENTITY>  <TYPE|METHOD>  -  <VENDOR>  -  $<AMOUNT>  -  <PROPERTY[ PROPERTY…]>.pdf
```
- **Intake (unpaid):** the type slot is the **bill type** — `06-26-26 TRUST2 ELEC - Frontier - $147.48 - TL222.pdf`
- **Paid:** the type slot becomes the **payment method + ref** — `06-14-26 TRUST2 CK#13386 - Bartlett Tree Experts - $1,890.00 - TL222.pdf` (CK# for checks, else ACH/CC/PHONE)
Null fields (and their ` - `) are omitted; illegal Windows chars stripped; `#`/`$`/`-` kept.
Full grammar (parse + generate): `T:\_OPS INDEX\KAREN_FILENAME_SPEC.md`; system schema:
`KAREN_SYSTEM_SCHEMA.md`. On payment, `_refile_to_convention()` re-names the filed document
so it gains the CK#.

---

## 6. Intake pipeline

1. **Arrival** — a PDF/image/Word file lands in the scan folder (ScanSnap) or the email folder
   (Gmail poller). `watcher.py` fires on creation; a **startup sweep** catches files that
   arrived while the app was down (fixed the "missed Malaga statement" gap).
2. **Split** — `splitter.py` uses Claude to find document boundaries; same-sender
   correspondence/inserts stay with their bill; blank pages are dropped.
3. **Classify** — `classifier.py` (Claude, vision fallback for image-only PDFs) extracts
   entity/property/type/vendor/amount/dates/etc., guided by `DOMAIN_RULES` (property-prefix→
   trust, payee→account routing, transaction-type disambiguation, Bill vs Receipt).
4. **Batch** — physical mail inherits a batch from its cover sheet (received date drives
   pay-order); batch state is thread-safe, collision-free, and survives restarts.
5. **Review** — staff confirm in the queue: fast-lane bulk confirm, filters
   (batch/received/type/entity/bill-type/confidence/search), inline bill-type add, print
   selected (merged PDF).
6. **File** — on confirm, the record is inserted and the PDF moved to
   `Filed/<Entity>/<Property>/<Year>/<convention-name>`; vendor/account hints are learned.

---

## 7. Feature map (built)

- **Tier 0:** schema→classifier prompt; filename convention; batch integrity.
- **Tier 1:** email received-date (from headers); quick Paid button; universal in-app PDF
  viewer; clickable records everywhere; edit-save navigation (Back/Prev/Next context).
- **Tier 2:** bill+correspondence kept together; Receipt doc type; stamped-received-date OCR;
  startup sweep (Malaga root cause).
- **Tier 3:** richer review-queue filters; inline bill-type add; multiselect → print merged PDF.
- **Tier 4:** payment-method fields (+ CK# in filename); source email stored & shown.
- **Data model:** relational core (this doc §4.2).
- **Ops:** boot-resilient service; queue persistence; WAL.

---

## 8. Security & access

- Session login (`require_login` before_request); hashed passwords; server-side audit
  stamping. Admin-gated Settings/config; everyday workflow open to all staff.
- Email poller is read-only IMAP (never marks/moves/deletes mail; INBOX only).
- **Accepted risk — plain HTTP on the LAN** (decided by Joshua, 2026-07-29, closing
  the 2026-07-27 review's GH issue #3): the app serves HTTP on the wired office LAN
  only — no WAN exposure, no guest Wi-Fi on the segment. Session cookies and login
  posts therefore cross the LAN unencrypted; this is accepted for the trusted-office
  threat model rather than fronting the app with a TLS reverse proxy. Revisit if the
  network ever gains untrusted devices/Wi-Fi, or if the app is ever reachable from
  outside the office.
- Daily backups (`Backups/auto`) carry a **redacted** config copy: the Claude API
  key, connector token, mailbox passwords, and staff password hashes are replaced
  with REDACTED placeholders (GH issue #12, same decision round). On restore,
  secrets are re-entered by hand; only intake.db and the non-secret config
  structure restore from backup.
- **Night-shift dev API** (2026-08-04, reports #66–#70; spec in
  `VALIDATION_WORKFLOW.md`) — a deliberate, tightly contained trust-surface
  expansion so unattended dev sessions write issue-report state **through the
  running app** instead of ever touching intake.db over the share (WAL/SMB
  corruption rule), and without a staff login (policy: no passwords in
  automation). Three endpoints only, in `routes_reports.py`:
  `POST /api/dev/report_new` (files a new report, always Open — added
  2026-08-04 on Joshua's ruling that chat-requested issues get tracked like
  widget reports), `POST /api/dev/report_note`, and
  `POST /api/dev/report_status`. Token-authed
  by `night_shift_token` in config.json (its own secret — deliberately distinct
  from `connector_token`, so either can be rotated alone), compared with
  `hmac.compare_digest`; no token configured = the API is off. Both paths are
  login/CSRF-exempt in app.py, same pattern as `/api/connector/heartbeat`.
  Scope: add a dev note, set a report to In progress or Dev-resolved (a
  resolution note is required for Dev-resolved) — it can NEVER set Verified or
  any closed state, and touches nothing but `bug_reports`/`report_notes`.
  Every call is activity-logged under its actor label (default "Night Shift").
  The redacted backup copy must treat `night_shift_token` like the other
  secrets.

---

## 9. ERPNext boundary & roadmap

**Two layers.**
- **Own in-app (backbone):** properties, units, entities, vendors, tenants, landlords — the
  reference data intake/classification/splitting depend on, and what you integrate ERPNext
  *against*. Each table already carries `erpnext_*` link columns.
- **Lean on ERPNext (lifecycle/ledger):** leases, move-in/out, rent ledgers, charges/payments,
  owner draws, reconciliation. Model + sync rather than hand-roll.

**Standards borrowed:** ERPNext community property apps (Project→Building→Unit; Tenant/Owner=
Customer; Vendor=Supplier; Lease=Contract; rent=Sales Invoice; Property/Unit≈Asset); MITS
(NMHC/RETTC) multifamily data dictionary as canonical vocabulary and future exchange target.

**Migration path (incremental):**
1. ✅ Stand up entities/properties/units/vendors (seeded). ← *you are here*
2. Properties & Units admin page → Karen reviews/corrects seeded unit counts (option C).
3. Build the **#2 entity split** on the paid form using `entity_unit_weights`.
4. Migrate readers (dashboards, dropdowns) from config.json → tables, table by table;
   config becomes a thin bootstrap, then retires.
5. Add tenants / leases / tenancies (move-in/out, history).
6. Define + wire the ERPNext sync boundary for the ledger/accounting layer.

**Open-source angle:** the intake + property-model layer is generic enough to open-source;
implementation/onboarding for other portfolios is a plausible services business.
