# Scan Straight Into Klick Klack — Design (reports #23, #18; #13 context)

**Date:** 2026-07-23 · **Requested by:** Joshua (HIGH priority) · **Status:** Approach A chosen, built this session

## The ask

Load documents in the ScanSnap, press one button, and the scan lands in Klick Klack
immediately — with a suggested filename in Karen's naming convention and a suggested
filing location in her folder system (`F:\File-Cabinet\Files\<YEAR>\<CATEGORY>\…`),
both overridable before saving. Plus (Joshua, mid-design): every machine needs a
*connector*, and if someone clicks Scan and their scanner isn't connected to this
flow, the app should walk them through connecting it.

## What the workstations have (investigated 2026-07-23)

- **ScanSnap Home 3.7.0.9** installed (`C:\Program Files (x86)\PFU\ScanSnap`), with a
  **ScanSnap iX1600** attached and healthy on Karen's workstation (touchscreen model —
  supports one-touch profile tiles on the device itself). An older iX500 registration
  is also present.
- Profiles live per-user in `%APPDATA%\PFU\ScanSnap Home\Managed\Profiles\PfuSsHome.xml`.
  Today only the seven stock profiles exist — **nothing points at the intake share**, so
  scans land in ScanSnap Home's private library and get hand-moved (the "Unfiled Scans"
  desktop shortcut is the workaround). That is exactly the double-handling #23/#18 complain about.
- The server already watches `F:\File-Cabinet\IntakeSystem\Unfiled Scans` (watcher.py,
  ~1 s detection, stability-checked) and every workstation reaches that folder as
  `\\LACKSSERVER2021\File-Cabinet\IntakeSystem\Unfiled Scans` (UNC always — drive
  letters like T:/Z: differ per desk; rule of 2026-07-24).
- **No programmatic scan trigger exists for consumer ScanSnaps**: no TWAIN/WIA driver,
  no public CLI/SDK for ScanSnap Home. Only ScanSnap software can drive the scanner.

## Approaches considered

### A — ScanSnap profile → watched share + per-machine connector + in-app Scan page  ← CHOSEN

Scanner side: a **"Klick Klack" profile** in ScanSnap Home (type *Scan to folder*,
destination `\\LACKSSERVER2021\File-Cabinet\IntakeSystem\Unfiled Scans`). On the iX1600 the profile is a tile on
the scanner's own touchscreen: load pages → tap tile → press Scan. The server watcher
ingests seconds later and the AI classifies as it already does for every other scan.

App side: a new **/scan page**. It knows whether *this* computer is ready via a tiny
**connector** (stdlib Python, run from the share with the bundled `pythonw.exe`) that
heartbeats scanner status to the server every 20 s; the server matches heartbeat to
browser by client IP. Three states:
1. **Ready** → "Load pages and press Scan on the ScanSnap." The page live-polls and
   jumps straight to the new document's review screen the moment it arrives.
2. **Connector alive, scanner not ready** → targeted fix steps (plug in / power on,
   start ScanSnap Home).
3. **No connector** → the walk-through Joshua asked for: connect this machine
   (double-click `Connect My Scanner.bat` once) and create the Klick Klack profile
   (exact clicks, documented in SCANNER_SETUP.md). Page flips live when the heartbeat appears.

Review side: the review form gains a **"File as" panel** — suggested filename (Karen's
convention via `_make_filename`) and suggested folder (learned from where the same
vendor/entity was filed before, with year/month segments bumped to the received date;
bills default to `ACCOUNTS PAYABLE - INCOMING BILLS\<MM. MONTH>`), both editable. If
left untouched, they re-derive from the final form values at confirm, so correcting
the vendor never yields a stale filename.

- **Pros:** one physical button per scan; reuses the proven watcher→classifier→review
  path (no new write path — same ruling as the in-tray stamp); no undocumented APIs;
  degrades gracefully (a scan still lands in the queue even if the connector is down —
  the connector only powers guidance and the live jump); works per-machine, USB or Wi-Fi.
- **Cons:** the "one button" is the scanner's button, not an in-app button (see B for
  why that's forced); first-time per-machine setup is a two-step manual (run the .bat,
  create the profile) — mitigated by the /scan walkthrough.

### B — In-app button actually triggers the scanner

The browser can't reach a USB scanner; a local connector would have to drive it.
Consumer ScanSnaps expose **no supported automation surface** (no TWAIN/WIA/CLI), so this
means UI-automating ScanSnap Home or reverse-engineering its internal WCF/COM services —
brittle across updates, per-machine fragility, unsupported when it breaks. And it saves
zero steps: you still walk to the scanner to load paper, where the Scan button already is.
**Rejected: highest effort, least reliable, no real UX win.**

### C — Keep scanning into ScanSnap Home's library; connector exports to the share

No profile setup, but the connector must mine ScanSnap Home's undocumented content
database (`Managed\Content\<uuid>` + `ScanSnapHome.db`), doubling storage and creating
sync/retention ambiguity (delete in library ≠ delete in Klick Klack), and every scan —
including personal/non-intake ones — gets swept in. **Rejected: fragile internals,
junk-ingest risk, and the intent signal ("this is intake") is lost.**

## Decision

**A.** It's the only approach with no unsupported dependencies, it preserves the
single proven ingest path, and the connector concept gives Joshua's per-machine
"walk them through it" requirement a natural home (and covers #13 — Nancy's machine
gets the same guided setup).

## Key implementation points

- New files: `scanflow.py` (blueprint: /scan page, heartbeat + status + arrivals APIs,
  connector registry), `filing.py` (filename/folder suggestion engine + destination
  validation), `scan_connector.py` (workstation agent), `Connect My Scanner.bat`
  (one-click per-machine install: Startup shortcut + immediate start), `templates/scan.html`,
  `SCANNER_SETUP.md` (human guide incl. iX1600 touchscreen + Nancy's first-time setup).
- `app.py` edits kept minimal (concurrent sessions): blueprint registration, File-as
  panel wiring in `review_item`/`confirm_item`/`_finalize_entry`, CSRF/login exemption
  for the token-authed heartbeat.
- Config additions (ASCII-only): `archive_folder` (`F:\File-Cabinet\Files`),
  `connector_token`, `app_url` (connector reads the same shared config.json off T:).
- Filing safety (June-30 lesson): destination must resolve under `archive_folder` or
  `filed_folder`; same collision suffix `(entry_id)`; `file_hash` provenance rides along,
  and the hash self-heal already searches relative to each record's own recorded path,
  so archive-tree filings stay repairable. Nothing ever overwrites; unparseable/invalid
  destinations fall back to the default Filed tree with an activity-log note.
- Arrival watch filters to files under `scan_folder` only (an email attachment arriving
  mid-wait must not hijack the jump).
- Bulk-confirm / auto-confirm / harvest paths unchanged — they never see the panel.
