# Klick Klack — Full Codebase Review, 2026-07-25

Requested by Joshua, run overnight while Karen and Joshua were unavailable to approve anything.
Nine parallel read-only review passes covered every module in `T:\IntakeSystem` plus the two
standalone ops scripts that share its database (`T:\_OPS INDEX\harvest_daily_filing.py`,
`T:\_OPS INDEX\reconcile_payments.py`). Five findings were verified safe enough to fix and test
unattended; everything else is batched below for morning review, prioritized by real production
impact. Full test suite (435 tests, including 2 new ones added tonight) passes clean.

---

## Fixes applied and tested tonight

All five were independently re-verified (exact line, call sites, existing test coverage) before
being applied — none touch more than one call site, none change user-visible behavior except to
correct an existing bug.

1. **`database.py:607`** — `get_vendor_summary`'s `MIN(due_date)` was missing the `NULLIF(due_date,'')`
   guard its sibling `get_entity_summary` already has. An empty-string due date sorted as the
   *smallest* possible value, so a vendor with no real due date could display as the most urgent
   one on the Bills-by-Vendor view. Fixed to match the sibling function exactly. New regression
   test: `tests/test_bills_merge.py::test_vendor_summary_ignores_blank_due_date`.
2. **`app.py:5061`** — filename generation dropped the amount token for a genuine `$0.00` bill
   (`if amount else ""` treats `0` as falsy). Fixed to `if amount is not None else ""`. New
   regression test: `tests/test_hygiene.py::test_make_filename_keeps_zero_amount`.
3. **`app.py` `logout()`** — used `session.pop("user", None)` instead of `session.clear()`,
   leaving `csrf`/`last_active` in the cookie after logout (harmless today, but inconsistent with
   `login()`, which clears the full session on success). Fixed for symmetry.
4. **`property_model.py` `entity_unit_weights`** — issued one SQL query per property code in a
   loop; batched into a single `WHERE code IN (...)` query. Single call site
   (`app.py:3404`, the #2-split calculation on the entry-detail view); existing test
   `test_entity_property_sync_roundtrip` still passes unchanged.
5. **`app.py` `rolodex()`** — called `_current_user_is_admin()` twice in the same request (each
   re-reads and re-parses `config.json` from disk); hoisted into one local variable.

---

## Overall assessment

For roughly two weeks of AI-assisted, incident-driven development, this codebase is in
meaningfully better shape than that description usually implies. Across all nine reviews, the
single most consistent positive signal was **docstring and comment quality that cites real
production incidents by name and date** (the FAIR Plan #51/#77 twin-payment bug, the Swan
CK#3012 amount-parsing bug, the T:\ vs F:\ path bug, record #815's date misread) — this is a
team that fixes root causes and writes down why, not just what. The test suite (435 tests) is
unusually disciplined for its age: properly isolated fixtures, no sleep/unseeded-random flakiness,
mostly relative dates. Security fundamentals (CSRF, parameterized SQL everywhere including the
chat assistant's tools, session/login throttling) are sound with one live exception (finding #2
below).

The technical debt that *is* accumulating has one dominant shape, visible in every single review:
**a fix or safety feature added in response to one real incident lands in only one of several
structurally similar code paths that do the same underlying job**, so the same bug class can
recur through the sibling path nobody patched. This shows up at every layer:

- Single-pay got a twin-bill safety check after a real double-payment incident; bulk-pay and
  pay-all-vendor never did (app.py finding #2).
- `ocr.py` got a per-page timeout after an hour-long production hang; `classifier.py`,
  `splitter.py`, and `assistant.py`'s Anthropic calls, plus `email_poller.py`'s IMAP connection,
  never did (ingestion finding #1, #2, #3, #4).
- `delete_entity` checks for references before deleting; `delete_property` doesn't (admin finding #4).
- Vendor-name stopword/tokenization logic has been independently reimplemented **four times**
  (`harvest_daily_filing.py`, `reconcile_payments.py`, and twice inside `database.py`), and they've
  already drifted from each other in ways that matter for the exact incident (FAIR Plan twins) they
  all exist to prevent.

None of this is deep architectural rot — it's the ordinary, very fixable cost of feature-by-feature
layering under time pressure. The fix pattern is the same everywhere it appears: when a safety
check or hardening gets added, grep for its sibling code paths and ask whether they need it too.

---

## Priority 1 — real production risk, worth doing soon

**P1-1. `reconcile_payments.py:34-40` — the `--db` CLI flag bypasses the "never write intake.db
from a workstation" rule the rest of the project depends on.**
`can_write` is set unconditionally to `True` whenever `--db` is passed, with no check that the
path isn't the live server's share/UNC path. The docstring says writes only happen "on the
server," but nothing enforces that for this flag — a workstation run with
`--db \\LACKSSERVER2021\File-Cabinet\IntakeSystem\intake.db --apply` would write directly to the
production database over the network share, the exact scenario this whole project has treated as
a hard rule (and the exact class of risk that caused a "file is not a database" scare during
tonight's session). *Fix:* gate `can_write` on the path NOT matching `_PATH_PREFIXES`, or require
an explicit second flag/hostname check.

**P1-2. Templates — live stored-XSS pattern via unescaped names in `onclick="confirm('...')"`.**
Five admin "manage" pages interpolate raw `{{ name }}`/`{{ code }}` values into an inline JS
string inside an HTML attribute: `templates/settings.html:82,182`, `templates/entities_manage.html:32`,
`templates/properties_manage.html:41`, `templates/bill_types_manage.html:38`. Jinja's autoescaping
prevents breaking out of the *HTML attribute*, but the browser HTML-decodes the attribute before
handing it to the JS parser — so a literal apostrophe in a name (e.g. a staff member named
"O'Brien", entirely ordinary) already breaks the confirm dialog, and a deliberately crafted
name/code injects arbitrary JS into any other admin's session that later views the page. None of
the backing routes restrict these characters. *Fix:* one-line-per-site — pass the value through
`|tojson` instead of raw interpolation, exactly as `templates/compare.html:38` and
`templates/entry_view.html:310` already do correctly elsewhere in this same codebase.

**P1-3. `app.py` — bulk payment routes bypass the twin-bill safety check single-pay was
specifically hardened with.** `pay_all_vendor` (app.py:3934) and `bulk_pay` (app.py:3951) mark
records paid in a loop with no refile-to-convention and no `find_invoice_twins()` warning, unlike
`pay_entry` (app.py:3998), which does both — because of a real incident (FAIR Plan #51 stayed
"unpaid" 19 days after #77 was paid). The exact bug class the codebase's own comments say already
cost real money is still open through the bulk UI. *Fix:* factor "mark paid" into one shared
helper (status/paid fields + refile + twins-check) used by all three routes. *Needs design
input, not a one-liner — the three routes' current call shapes differ.*

**P1-4. `email_poller.py:240-243` — the IMAP connection has no socket timeout, and a hang here
can silently halt ALL future email polling forever.** Unlike the POP3 path 60 lines later
(which explicitly passes `timeout=60`), the IMAP4_SSL/IMAP4 opener has no timeout, so a
hung/unreachable mail server blocks that thread indefinitely. Because `poll_email_once` is
guarded by a single non-reentrant `_poll_lock`, and the 5-minute poll loop just silently returns
`(0, None)` when the lock is held (no error surfaced anywhere), one hung IMAP account
permanently and invisibly stops email intake for every account until the app is restarted.
*Fix:* add `timeout=` to the IMAP opener the same way POP3 already has it — small, isolated,
single call site. **This is the single highest-value fix in the entire review.**

**P1-5. `classifier.py:239` vs `classifier.py:630` — the 4000-char prompt truncation silently
reintroduces a bug it was built to fix.** Form-field values (`[FILLED-IN FORM FIELDS]`, the
feature that fixed "executed lease classified as Blank") are appended to the document text
*before* truncation is applied, so any document whose own text is already ≥4000 chars — exactly
the long leases this feature targets — has the form-fields block truncated away before Claude
ever sees it. *Fix:* truncate `text` alone before concatenating `form_values`.

**P1-6. `classifier.py:366-369` — a single broad `except Exception` silently defaults every AI
failure (network blip, rate limit, malformed response) to the regex fallback, which always sets
`important=False`.** During any transient Anthropic outage, eviction notices, shutoff warnings,
and legal filings — the exact "needs attention" bucket this app exists to catch — get silently
classified unimportant, with no operational visibility beyond a note buried in that one record.
*Fix:* narrow the except, log outages at the process level, consider flagging regex-fallback
records for mandatory review rather than defaulting to unimportant.

**P1-7. `harvest_daily_filing.py` / `reconcile_payments.py` — the two scripts disagree on how
four entity tokens map, and it's already silently degrading reconciliation.** `harvest_daily_filing.py`'s
`ENT_MAP` (line 119) treats `TRUST4`, `KLR-LRC`, `BRITT`, `PERSONAL` as ambiguous → blank
`entity_code`; `reconcile_payments.py`'s `ENTS` (line 71) maps the same four tokens to literal
codes. Since `reconcile_payments.py`'s scoring requires both sides to have a non-blank
`entity_code` to award the entity-match point, any bill harvested with one of these four tokens
can never hit the auto-mark-paid threshold via entity agreement. *Fix:* extract one shared
entity-mapping table both scripts import.

**P1-8. `reconcile_payments.py`'s twin/duplicate-matching logic duplicates `database.py`'s with
a different, already-drifted stopword list.** `reconcile_payments.py:74`'s `STOP` set is missing
`ASSOCIATION`/`ASSOCIATES` — the exact vendor-name family `database.py`'s twin logic was
hardened for after the FAIR Plan incident (`"California FAIR Plan Association"`). A payment
document matching on the word "Association" alone can score a false vendor-match point here that
`find_invoice_twins()` would correctly ignore. This is the identical "two independent 'same bill'
implementations that can disagree" risk that caused the incident both scripts exist to prevent.
*Fix:* import `database.py`'s twin logic (or a shared extraction of it) instead of maintaining a
parallel implementation.

**P1-9. `app.py` — the two highest-volume intake paths don't use the already-fixed ID-field
normalizer.** `_id_field()` (app.py:3250, added 2026-07-23 specifically to stop blanks being
stored as `""`/literal `"None"`) is only used by `new_entry`/`edit_entry`; `confirm_item`
(app.py:2905) and `_entry_from_scan_data`/`bulk_confirm` (app.py:3099) — the review-queue confirm
and bulk-confirm paths, by far the highest-traffic intake routes — still do the old
`.strip()`-only normalization. *Fix:* route `invoice_number`/`account_number` through the
existing, already-tested `_id_field()` helper in these two places too.

**P1-10. `property_model.py` `delete_property` has no reference-integrity check, unlike its
sibling `delete_entity`.** Deleting a property that's still referenced by historical bills
silently orphans an unbounded number of records (their `property_code` now points at nothing in
Settings — breaks cabinet drawers, unit-weight splitting, dropdowns). `delete_entity`
already has this exact guard for entities. *Fix:* mirror the existing pattern.

---

## Priority 2 — real but lower urgency / needs a design call

- **`app.py:4062-4069` `delete_entry_route`** orphans attachment files on disk instead of
  retiring them like every other delete path in the app (`_retire_copy_file`) — breaks the
  "never destroy, always recoverable" convention used everywhere else.
- **`app.py:3420` `view_entry`** calls `find_link_candidates()` unconditionally on every single
  record view — an unfiltered full-table scan with no `doc_type` narrowing, on the single most
  common page load in the app. Will get slower every year as the 13-year archive grows.
- **`database.py:829-862`** — the rename-cascade functions (`rename_entity_code`,
  `rename_property_code`, `rename_bill_type_code`, `rename_vendor`) look parallel but aren't:
  none of them cascade into `account_hints`/`expected_bills`, so a rename silently leaves stale
  codes in the account auto-assign hints and the recurring-bill radar.
- **`database.py:516-524` / `549-557`** — `find_invoice_twins` and `find_link_candidates`
  duplicate the exact same 8-line matching condition verbatim; will drift if tuned in only one place.
- **`database.py:480-482` `_vendor_words`** — excludes tokens ≤3 characters, which disables the
  word-overlap fallback for exactly the short abbreviations (SCE, DWP) this codebase's own
  comments call out as the real-world case.
- **`app.py` — four independent "job already running" flags** (`_reindex_running`,
  `_reclassify_in_progress`, `_harvest_ai_fill_in_progress`, `_scan_in_progress`) are all
  check-then-set with no lock — a real (if low-severity) race that can start duplicate background
  workers, doubling API cost or OCR work.
- **`app.py` — no audit trail for Settings/staff mutations.** Every record-level app action logs
  to `activity_log`; none of add/remove staff, reset password, entity/property/bill-type CRUD do.
  "Who removed this staff account, when" is unrecoverable once the console scrollback is gone.
- **`app.py:4869-4891` `/api/scan_now`** runs IMAP polling synchronously on the request thread
  (unlike the folder scan a few lines later, which is correctly backgrounded) — a slow mail
  server ties up one of only 8 waitress worker threads on every click.
- **`ocr.py:99-113`** — `except Exception: return ""` conflates a genuine OCR crash with "page
  is legitimately blank," and `indexer.py` only auto-retries the latter, so a transient OCR error
  permanently drops that document from search with no visibility.
- **`classifier.py`/`splitter.py`/`assistant.py`** — three more Anthropic API calls with no
  explicit `timeout=`, same class of gap `ocr.py` already got fixed for Tesseract (though the SDK's
  own ~10-minute default timeout means these can't hang *forever* the way the OCR call could).
  Adding timeouts is safe; picking the right value per call shape (a quick classify vs. an
  8000-token vision call with thinking) is a real judgment call.
- **`harvest_daily_filing.py`** — the entire script is top-level code with no `main()`/functions
  and no tests; `parse_name`/`match_entry` can't be unit-tested without monkeypatching
  `sys.argv`/the filesystem. Given this script already has a documented history of subtle bugs
  (CK#3012, the T:\ vs F:\ path bug), a testable structure would likely have caught them faster.
- **`harvest_daily_filing.py:228`** — the `seen`-state table is loaded into Python in full on
  every run and never pruned (18,558 rows after 16 days); this, not the bill-matching loop, is
  the actual unbounded-growth risk over 13 years.
- **`karen_questions.py` `karen_save()`** — writes a timestamped DB row for every one of the 21
  questions on every save, including ones nobody answered, so untouched questions show a
  misleading "Last saved just now" line.

---

## Priority 3 — cheap cleanup, no urgency (batched for whenever)

- **database.py:** no index on `entity_code`/`property_code`/`doc_type`/`status`/`due_date`
  (genuinely low-impact at current row counts — flagged for completeness, not recommended yet);
  `get_vendor_summary`/`get_vendor_directory` return a non-deterministic vendor spelling under
  `GROUP BY LOWER(vendor)`; `get_dashboard_stats` issues 6 separate `COUNT(*)` queries instead of
  one `GROUP BY`; exact-float `==` used for dollar-amount matching in duplicate/twin detection
  (latent, not confirmed to have caused an incident); `insert_entry`/`update_entry`'s allowed-column
  lists are two independently-maintained ~40-item literals; `get_conn()` connections rely on
  garbage collection rather than explicit `.close()` (works reliably on CPython, but implicit).
- **app.py:** ~15 near-identical CRUD routes in Settings (entities/properties/bill-types/mail/staff)
  share no helper, which is exactly how `delete_property`'s missing guard (P1-10) happened;
  6 copy-pasted money-string-parsing blocks; `new_entry` (manual add) skips the duplicate/vendor-match
  checks `confirm_item` runs; `bulk_confirm` is explicitly exempted from the date-sanity guard per
  its own code comment, which is a self-acknowledged open question worth a deliberate decision;
  N+1 `get_entry()` calls in the full-text search results view and in `_triage_groups`;
  `claude_api_key` renders back to the browser in cleartext on the Settings page;
  `_MIN_STAFF_PASSWORD_LEN = 4` is thin policy for a financial-document system; removed staff
  keep a live session until idle-timeout (no re-validation against the current staff list);
  `_repair_filed_paths` walks the entire Filed tree unboundedly on every boot, unlike its sibling
  `_repair_filed_paths_by_hash` which already learned to cap itself.
- **property_model.py:** `vendor_aliases` table defined but never populated/queried anywhere;
  no `PRAGMA foreign_keys=ON` so the `REFERENCES` annotations are documentation-only; `units`
  table fully unused (accurately self-documented as a future step).
- **Ingestion pipeline:** duplicate PDF-rasterization boilerplate between `classifier.py`'s two
  pypdfium2 call sites (partial dedup is safe; unifying with `ocr.py`'s separate PyMuPDF path is
  not, per deep-dive); no hash-based short-circuit before the AI classify call, so a resent/duplicate
  document pays for a full API call before being discarded; `assistant.py`'s `APP_GUIDE` "show me"
  whitelist is already missing 4 real UI targets that exist in the templates; inconsistent
  dated-vs-undated model-id conventions across files; a mild prompt-injection surface where raw
  document text flows into the classification prompt with no "this is data, not instructions"
  framing (mitigated today by the assistant's read-only, non-acting tool design).
- **Templates:** three "manage" pages (entities/properties/bill-types) duplicate the same
  add-form + inline-edit-table structure with no shared macro; heavy inline-`style=""` duplication
  in `entry_view.html` (118 instances)/`settings.html` (91)/`bills.html` (71) that drifts from
  base.html's own CSS classes; `templates/summary.html` is fully dead code (its route now just
  redirects to `/bills`); `templates/coversheet.html` is the one standalone page still missing the
  favicon links every other standalone page already has.
- **Tests:** no coverage at all for `/entry/<id>/link_original`/`/entry/<id>/no_original`
  (the newest record-linking feature) or `/review/print`; two far-future hardcoded dates
  (`2027-08-01`/`2027-07-08`) in `test_catchup_received.py` will eventually flip buckets and
  should be `date.today() + timedelta(...)` instead, matching the pattern already used correctly
  elsewhere in the same suite; a shared test helper (`_log_custom_bill`) is imported cross-file
  rather than living in `conftest.py`.
- **karen_questions.py:** no uniqueness check on question IDs or that `section` values match
  `SECTIONS` (no collision exists today, but nothing prevents one); option text doubles as the
  persisted answer value with no stable short code, so a future wording edit would silently
  orphan old answers; `DATE_FIXES` is bare positional 4-tuples rather than a named structure.

---

## What's good and worth explicitly preserving

- Docstrings and inline comments citing real incidents by name/date, throughout every module —
  this is the single strongest signal in the whole review and should stay the house style.
- The CSRF audit found no gaps beyond the one already fixed (`rolodex.html`, 2026-07-19) — every
  standalone template's POST forms were individually checked.
- The Klick Klack Helper's data tools (`find_bills`/`get_record`) are confirmed genuinely
  SELECT-only and fully parameterized — no SQL-injection path found.
- `reconcile_payments.py`'s actual write path is properly race-safe: it re-checks a bill's status
  immediately before writing and gracefully no-ops if a concurrent `merge-record` has already
  deleted the row.
- Test suite discipline: no `time.sleep`, no unseeded random/uuid, fully isolated per-test
  fixtures (verified `conftest.py` monkeypatches `DB_PATH`/`CONFIG_PATH` and resets every in-memory
  global), and most hardcoded dates are correctly past-relative rather than future-relative.
