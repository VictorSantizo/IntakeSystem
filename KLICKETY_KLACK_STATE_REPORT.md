# Klickety Klack — Project State Report
*Evidence-based state extraction for a completion estimate. Generated 2026-07-02 on the build machine (`T:\IntakeSystem`). No code was changed to produce this.*

> **Reading key:** every claim is tagged **[FACT]** (measured/observed on this machine) or **[INFERENCE]** (judgment). Status icons: ✅ works end-to-end against real data · 🧪 built but not validated against real data · 🚧 partial · 📝 stub/planned.

---

## A. Project identity

- **Product name:** **"Klickity Klack"** (the app's own branding, in `base.html`, `login.html`, `start.bat`). The task calls it "Klickety Klack" — same project, spelling variant. **[FACT]**
- **Folder / path used:** `T:\IntakeSystem` (the current directory; = the live production share `\\LACKSSERVER2021\File-Cabinet\IntakeSystem`). This is the one and only candidate — the CWD is the project. **[FACT]**
- **Client:** Karen Lacks & Co. (property management), under Entrusted Services / Lot De Leon. **[FACT — from project docs]**
- **Session evidence:** 6 Claude Code session files in `~/.claude/projects/T--IntakeSystem/` dated 2026-06-24 → 2026-07-02; 23 memory notes; 6 distinct build sessions referenced. **[FACT]**

**Plain-English (for Lot):** Klickity Klack is a small web app that Karen's office opens in a browser. It watches a scanner folder and a Gmail inbox, and every time a piece of mail or an email bill comes in, an AI reads it, figures out the vendor / property / amount / due date, and drops it in a review list. A staffer glances at each one, fixes anything wrong, and clicks confirm — which files the PDF under Karen's 13-year naming system and logs it so bills can be tracked and marked paid. It replaces a manual "open the mail, name the file, type it into a list" routine.

**Mismatch with "Mail Intake + new PC":** **[FACT/INFERENCE]** The ticket "Karen Lacks — Mail Intake + new PC" has two halves. This repository **is** the *Mail Intake* half (the automation). The *new PC* half is hardware/desktop setup — **there is no evidence of it in this codebase**; that is separate deployment work. Also note the build has **grown well beyond "mail intake"**: it now includes payment tracking, multi-entity bill splitting, and the beginnings of a relational property database (properties/units/entities/vendors) with a stated long-term goal of an "open-source Yardi/AppFolio" integrated with ERPNext. That expansion is real scope creep relative to the original ticket and should be priced separately from "finish the mail-intake deliverable" (see §B, §F).

---

## B. Scope / definition of done

**[FACT]** There is **no written contract, SOW, or requirements doc in the repo.** The only scope artifacts are: a prioritized fix list ("KLACKS — Prioritized Fix List," Tiers 0–4) that was worked through, plus `DESIGN.md` (written 2026-06-30, by the build itself) and memory notes.

**[INFERENCE] Reconstructed implied scope** — a mail-intake system that Karen's office uses daily to turn incoming mail + email into filed, searchable, paid-tracked records, matching her existing filename convention, running reliably on office hardware.

**"Complete" (for the mail-intake deliverable) means:**
1. Runs unattended on the office machine(s), auto-starting after reboots. *(built; see §C)*
2. Ingests physical scans **and** Gmail; AI-classifies; staff review/confirm. *(core works)*
3. Files PDFs under Karen's convention; tracks paid/unpaid + payment details.
4. **Validated against real mail by the actual users** with an acceptable error rate.
5. Deployed to the new PC(s), Karen/staff trained, basic monitoring/backup in place.

Items 1–3 are largely built; **items 4–5 are the unfinished last mile** (validation, deployment, training, hardening).

---

## C. Built and working

**Codebase size [FACT]:** ~4,000 lines of hand-written Python across 8 modules (`app.py` 2,030; `database.py` 721; `classifier.py` 369; `install.py` 314; `property_model.py` 194; `email_poller.py` 181; `splitter.py` 158; `watcher.py` 42) + 24 Jinja templates. Excludes the bundled Python runtime + site-packages.

**Real-usage evidence [FACT]** (live `intake.db`, read-only): 221 confirmed records; 9 marked Paid; 2,456 emails processed; 264 activity-log events; processors = **Karen** and **Joshua**. **Last real activity: 2026-06-29 18:18** (last confirm 2026-06-27). The queue holds 413 unreviewed items; 219 filtered.

| Component | Purpose | Status | Evidence |
|---|---|---|---|
| Scan/email intake pipeline (watcher→split→classify→queue) | Turn incoming files into review items | ✅ | 221 real records + 2,456 emails processed; `watcher.py`, `splitter.py`, `classifier.py` |
| AI classification (Claude + vision fallback) | Extract entity/property/vendor/amount/dates | ✅ | Real extracted fields on 221 records; `classifier.py` |
| Review & confirm → log + file PDF | Staff QC, then persist + file under convention | ✅ | 221 filed records, `filed_path` populated; Karen/Joshua as processors |
| Gmail IMAP poller | Pull email bill attachments | ✅ (🚧 quality) | 2,456 processed; **but junk filter too loose → over-ingest** (memory + 413 queue) |
| Intake Log / search / status | Find + update records | ✅ | Used; `log.html`, `database.py` |
| Dashboard / bills / catch-up views | AP overview | ✅ built / 🧪 real-use light | Templates present; light real traffic |
| Auth (login, hashed pw, sessions, audit) | Access control | ✅ | Karen + Joshua logged-in usage |
| Filename convention + bulk rename of 221 files | Match 13-yr archive naming | ✅ | This session: 221 files renamed, DB paths consistent, reversible undo map |
| Config editors (entities/properties/bill types) | Admin edits reference data | ✅ built | `settings.html` + routes; admin-gated |
| Batch cover sheet + received-date capture | Group a scan session, pay-in-order | 🧪 / 🚧 | Built; **data shows cover sheets not actually being used** (128 docs in one "assumed" batch) |
| Payment method fields (Check/ACH/CC/Phone + CK# in filename) | Capture how a bill was paid | 🧪 | Built 6/30; 9 paid records predate/underuse it; not exercised by Karen |
| In-app PDF viewer · clickable records · edit-save nav | UX fixes | 🧪 | Parse-verified only; no real-user mileage since 6/29 |
| Review-queue filters · inline bill-type add · print-merge PDF | Throughput | 🧪 | Built 6/30; unit/parse-tested, not real-use |
| Receipt type · stamped-date OCR · bill+correspondence keep-together | Data-integrity classifier tweaks | 🧪 | Prompt changes unverified on real documents |
| Email source stored & shown (sender/subject/body) | Traceability | 🧪 | Only *new* email intake captures it; none since |
| Startup catch-up sweep (missed-mail fix) | Ingest files that arrived while down | 🧪 | Will act on next restart; ~38+ stranded files pending |
| Boot-resilient service (`install_service.bat/.ps1`) | Auto-start after power loss | 🧪 | Built after a real outage; **unknown whether run on the server** |
| Relational core (entities/properties/units/vendors) | Scalable data backbone | 🧪 (not activated) | `property_model.py`; **tables do NOT exist in live DB yet** [FACT] |
| Multi-entity bill split (By units/Even/Manual) | Allocate shared bills | 🧪 (not activated) | `payment_splits` table absent in prod; math unit-tested only |
| Properties & Units review page | Correct seeded unit counts | 🧪 (not activated) | Route + template built; depends on unseeded tables |
| DESIGN.md + KAREN_CHEATSHEET.md | Docs | ✅ | Present in repo (2026-06-30) |

**Bottom line [INFERENCE]:** the **core intake→classify→review→confirm→file→pay loop is genuinely ✅** with real mileage (221 records, 2 real users). Nearly **everything added on 2026-06-30 and 2026-07-02 is 🧪** — code is written and self-tested, but the real user hasn't touched the app since 2026-06-29, and the newest layer (relational core, #2 split, properties page) **isn't even live** (its DB tables don't exist yet; a restart is required).

---

## D. Remaining to complete (to a production mail-intake deliverable)

| # | Item | Why required for "done" | Size | Confidence | Dependencies |
|---|---|---|---|---|---|
| 1 | Restart the server service; smoke-test the on-disk batch (relational core, #2, sweep, migrations) | A large chunk of built work is inert until a restart; must confirm it activates cleanly | **S** | High | Server access (Joshua) |
| 2 | Validate the whole 6/30 feature wave against **real mail** with Karen; fix what breaks | The 🧪 → ✅ gap; features unproven with real users/data | **L** | Med | Karen's real mail flow + time |
| 3 | Tighten email over-ingest / junk filter | 2,456 emails pulled, queue bloated to 413; adoption blocker | **M** | Med | none (dev) |
| 4 | Triage / clear the 413-item pending queue | Backlog blocks daily adoption | **M** | Med | Karen (mostly her time) |
| 5 | Karen reviews & corrects seeded unit counts (Properties & Units) | #2 split accuracy depends on it | **S** | High | Karen |
| 6 | Deploy to the **new PC(s)**: point to server or install, credentials, scheduled task, firewall, onsite check | The "new PC" half of the ticket; go-live target | **L** | Med | **New PCs, onsite/remote access, credentials** |
| 7 | Reliability: `intake.db` backups, log rotation, confirm boot-service installed, basic monitoring | "Walk away" requires it won't silently die | **M** | Med | Server access |
| 8 | Train Karen / Nancy; fold the cheat sheet into their routine | Users must operate it unaided | **M** | Med | Karen/Nancy time |
| 9 | *(Recommended, not strictly required)* Minimal automated tests / regression checks | No test suite exists; guards future edits | **L** | Low | none |
| 10 | *(Polish/tech-debt)* #2 split on the edit-form modal; migrate readers config→DB | Consistency; not required for MVP | **M** | Low | none |

**In scope for the near-term "done": #1–8.** #9–10 are recommended/optional.
**Explicitly NOT in this estimate:** the long-term platform vision (tenants/leases/move-in-out history, full ERPNext integration, open-source productization). That is open-ended and should be scoped/priced on its own.

---

## E. Risks and unknowns

- **[INFERENCE] Large unvalidated surface.** ~15 features were built in a 2-day burst and self-tested (parse/unit), but the real user hasn't run the app since 2026-06-29. Real mail is messy; expect breakage on contact. This is the single biggest estimate risk.
- **[FACT] No test suite, no git history.** Regressions are invisible until a user hits them; no bisect/rollback safety net beyond file copies.
- **[FACT] Newest layer not live.** Relational core + #2 + properties page require a restart to even exist; unknown side effects on first activation.
- **[FACT] Email over-ingest.** 2,456 processed, only ~219 filtered, queue at 413 — the AI junk filter is too loose; adoption suffers until fixed.
- **[FACT/INFERENCE] Cover-sheet workflow not adopted.** Data shows received dates defaulting to "today" (128 docs in one assumed batch) — the pay-in-order feature only works if staff actually scan cover sheets.
- **[FACT] WAL-over-SMB fragility.** `intake.db` is WAL on a network share; DB-writing tools must run on the server, not the workstation (a bulk operation already wedged once). Constrains how maintenance is done.
- **[INFERENCE] External dependencies.** Claude API (cost + availability) and Gmail app-password both currently work (real data proves it) but are single points of failure with no monitoring.
- **[INFERENCE] Deployment unknowns.** "New PC(s)" not present on this machine; onsite specifics (network, scanner, drive mappings, who logs in) unknown.

---

## F. Calibration

**Completed work, re-scored [INFERENCE, unit weights S≈0.5h, M≈2h, L≈5h, XL≈10h]:**
- Core intake pipeline (watcher/split/classify/poller/review/confirm/file): **XL**
- DB layer + config + reference data: **L**
- Dashboard/log/bills/catch-up/auth/settings: **L**
- Tier 0–2 features (schema, filename, batch, integrity fixes): **L**
- Tier 3–4 features (filters, paid methods, print, email source, etc.): **L**
- Relational core + #2 split + properties page: **M–L**
- Docs: **S**
→ **Completed ≈ XL + 4×L + M ≈ ~33 units (~33h of build-equivalent).**

**Remaining (§D #1–8): 2×S + 4×M + 2×L ≈ ~19 units (~19h).** (#9–10 add ~7 more if taken.)

**Percent complete [INFERENCE]:** 33 / (33 + 19) ≈ **63%** of the *mail-intake deliverable*.
Reasoning: the hard core is done and proven, but "done" is gated on the last-mile trio — real-mail validation, office deployment, and training/hardening — which is where hopeful estimates historically break. **If you weight by "build typed" it looks ~80% done; weighted by "deployed, validated, users trained," it's ~60%.** I use the latter. **Call it ~60–65%.**

**Observed pace [FACT for the h figure, INFERENCE for attribution]:** June KL tickets = 43.75h (Joshua's data). Build activity here spans ~2026-06-19 → 2026-07-02 across 6 Claude Code sessions. That 43.75h + earlier produced the ~33 completed units — i.e. **build-equivalent ≈ real hours** roughly 1:1, which supports the ~19h remaining figure.

**FACT vs INFERENCE summary:**
- **FACT:** LOC, module list, 221 records / 9 paid / 2,456 emails / 413 queue, last-activity 6/29, no tests, no git, new tables absent, session count/dates, 43.75h June (given).
- **INFERENCE:** status of 🧪 items, unit sizing, % complete, remaining-hours, pace attribution.

---

## G. Deployment & handoff

| Task | Detail | Size | Dependency |
|---|---|---|---|
| Activate current build | Restart the `KlickityKlack` service; verify migrations/sweep | **S** | Server access |
| Confirm boot service installed | Run `install_service.bat` on server as admin; set `bcdedit` boot-through | **S** | Server admin |
| New PC setup | The "new PC" half — browser shortcut to the server app, or a second instance; scanner/ScanSnap folder wiring | **M–L** | **New PCs + onsite** |
| Credentials | Confirm Gmail app-password + Claude API key present on the target (both currently work) | **S** | Credential owner |
| Real-mail acceptance test | Run a week of real mail; measure classification accuracy; fix top failures | **L** | Karen's mail + time |
| Backups / monitoring | Scheduled `intake.db` backup, log rotation, a "is it up?" check | **M** | Server access |
| Training | Walk Karen/Nancy through queue→confirm→pay→find; hand over cheat sheet | **M** | Karen/Nancy |
| Documentation | `DESIGN.md` + `KAREN_CHEATSHEET.md` exist; may need a 1-page ops runbook | **S** | none |

---

## H. Time-tracking data on this machine

**NOT ON THIS MACHINE.** **[FACT]** Checked `C:\Users\LacksUser\OneDrive\Documents\_Career\_Lot De Leon\` (absent) and searched OneDrive for `time_tracking.db`, `Time Tracking UPDATED v5.xlsm`, and any `_Career` / `Lot De Leon` folder — none found. This machine syncs a personal OneDrive but not Joshua's `_Career` tree. Time/billing totals must come from Joshua's other computer.

---

## I. Questions only Joshua can answer

1. Is there a **written scope/quote** with Lot for this project, and does it cover the payment-tracking / property-model expansion, or only "mail intake"?
2. What exactly is the **"new PC"** deliverable — replace Karen's workstation, add stations for staff, or a dedicated server box? Are the PCs on hand?
3. Was **`install_service.bat` actually run on the server** (is the app auto-starting after reboots now)?
4. Has anyone **restarted the service since 2026-06-30** to activate the relational core / #2 split? (Live DB says the new tables don't exist.)
5. What's the **agreed acceptance bar** — classification accuracy %, who signs off, how many users?
6. Is the **long-term vision (tenants/leases/ERPNext/open-source)** billable scope now, or aspirational/future?
7. Who owns the **Claude API cost** and is there a budget ceiling?

---

## J. Machine-readable summary

```yaml
project_name: "Klickity Klack (Karen Lacks Mail Intake automation)"
pct_complete: 63            # of the mail-intake deliverable; ~80% "typed" vs ~60% "deployed+validated"
est_hours_p50: 20.0         # remaining, realistic, §D items 1-8
est_hours_p80: 32.0         # with risk buffer (real-mail validation + onsite deploy unknowns)
remaining_items: {S: 2, M: 4, L: 2, XL: 0}   # +optional {L: 1, M: 1} for tests/polish
hard_blockers:
  - "New PC(s) not present on this machine; office deployment needs hardware + onsite/remote access"
  - "Newest build (relational core, #2 split) inert until the server service is restarted"
needs_from_client:
  - "The new PC(s) and onsite/remote access to Karen's office"
  - "Karen's time: real-mail validation, unit-count review, 413-item queue triage, training"
  - "Confirmation the boot service was installed on the server"
  - "Written acceptance criteria + scope confirmation (mail-intake only vs expanded platform)"
```
