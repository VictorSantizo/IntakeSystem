# Klickity Klack (IntakeSystem) — Code Review Report

**Repo:** https://github.com/yahooshua/IntakeSystem
**Review date:** 2026-07-27 (deep dive same day; report compiled 2026-07-30)
**Scope:** Full repo — `app.py` (5,777 lines), `database.py` (1,513), `classifier.py`, `splitter.py`, `assistant.py`, `scanflow.py`, `property_model.py`, `watcher.py`, `filing.py`, `indexer.py`, `ocr.py`, `email_poller.py`, `scan_connector.py`, config, tests listing. Not reviewed: `install.py`, `karen_questions.py`, templates/static, test suite internals.

**Audience:** the coding agent implementing the fixes. Each item below has a GitHub issue, exact file/function references, and a suggested fix.

---

## 1. Verdict

The codebase is in good shape. SQL is parameterized everywhere (verified at every f-string site — only allowlisted column names / hardcoded fragments are interpolated). Auth includes session login with server-side idle timeout, CSRF tokens compared with `hmac.compare_digest`, per-IP login throttling (8 fails / 10 min), password hashing with migration, admin gating, and a constant-time-compared connector token. Concurrency is deliberate (queue lock, ingest lock, batch RLock, config lock with atomic `os.replace`, render lock for PDFium, record-number retry on `IntegrityError`). The pipeline never deletes anything — all discards go to recoverable set-aside folders.

The open issues are mostly hardening, dependency hygiene, and long-term maintainability — no urgent security holes.

## 2. Open issues (11)

| # | Title | Severity | Effort |
|---|---|---|---|
| [#1](https://github.com/yahooshua/IntakeSystem/issues/1) | ocr.py: whole-PDF rasterization memory blowup | Medium | Small |
| [#2](https://github.com/yahooshua/IntakeSystem/issues/2) | filing.py: `_history_dir()` full-table scan per suggestion | Medium | Small |
| [#3](https://github.com/yahooshua/IntakeSystem/issues/3) | Plain HTTP for app, sessions, connector token | Medium (accepted risk?) | Decision + config |
| [#4](https://github.com/yahooshua/IntakeSystem/issues/4) | indexer.py: `search()` silently swallows OperationalError | Low | Trivial |
| [#5](https://github.com/yahooshua/IntakeSystem/issues/5) | requirements.txt: Pillow **and pypdfium2** not declared | Low | Trivial |
| [#6](https://github.com/yahooshua/IntakeSystem/issues/6) | app.py is a 281 KB monolith | Low (strategic) | Large, incremental |
| [#7](https://github.com/yahooshua/IntakeSystem/issues/7) | No CI | Low | Small |
| [#8](https://github.com/yahooshua/IntakeSystem/issues/8) | `os._exit(42)` restart can interrupt background work | Low (downgraded) | Medium |
| [#9](https://github.com/yahooshua/IntakeSystem/issues/9) | `poplib._MAXLINE` monkeypatch unguarded against Python upgrades | Low | Small |
| [#11](https://github.com/yahooshua/IntakeSystem/issues/11) | Repo hygiene: `.lnk`/`.sswp` binaries, stale branches | Low | Trivial |
| [#12](https://github.com/yahooshua/IntakeSystem/issues/12) | Daily backups copy config.json (plaintext secrets) into `Backups/auto` | Low | Decision + small |

**Closed:** [#10](https://github.com/yahooshua/IntakeSystem/issues/10) (CSRF) was filed in error — `csrf_protect` already exists in `app.py` (registered after `require_login`, covers all POST/PUT/PATCH/DELETE, token in every form/fetch via `base.html`). Closed as not planned.

## 3. Fix specifications

### #1 — ocr.py memory blowup
- **Where:** `ocr.py`, `ocr_pdf()` — builds a Python list of every page's PIL image at 200 dpi before OCR'ing any of them (~1–2 GB on a 50-page scan).
- **Fix:** stream page-by-page — render page → `pytesseract.image_to_string(im, timeout=_OCR_PAGE_TIMEOUT)` → discard → next. Keep the existing per-page `RuntimeError` skip. `_ocr_images()` can become a per-page helper.

### #2 — filing.py O(N) suggestion scan
- **Where:** `filing.py`, `_history_dir()` calls `database.get_all_entries()` and linearly scans the whole `intake_log` on every "File as" panel render.
- **Fix:** add a SQL-filtered query (e.g. `SELECT id, entity_code, filed_path FROM intake_log WHERE LOWER(COALESCE(vendor,'')) = ? AND filed_path != '' ORDER BY id DESC LIMIT 50`) in `database.py`, and keep the archive-root / same-entity preference ranking in `filing.py` on that small result set.

### #3 — Plain HTTP
- **Where:** Flask on `0.0.0.0:5050`; session cookies, staff passwords, and `connector_token` cross the LAN in cleartext.
- **Fix:** deliberate decision — either document accepted risk in DESIGN.md §8, or put the app behind a local TLS reverse proxy (Caddy is the lightest path). If TLS lands, also set `SESSION_COOKIE_SECURE=True`.

### #4 — indexer.py silent search failures
- **Where:** `indexer.py`, `search()` — `except sqlite3.OperationalError: return []` makes a corrupt/locked FTS index indistinguishable from "no matches".
- **Fix:** `print(f"[Index] search failed: {e}")` before returning `[]` (or surface an "index unavailable" flag to the UI).

### #5 — undeclared direct dependencies
- **Where:** `requirements.txt` lacks `Pillow` (imported in `ocr.py`, `classifier.py`, `splitter.py`, `app.py`) and `pypdfium2` (imported in `classifier.py` `pdf_page_count`/`render_pdf_page_image`/`render_last_page_image`, `splitter.py` `build_contact_sheets`, `app.py` `_render_doc_page_png`). Both fail soft today, so a fresh install silently loses the vision fallback and the page-image viewer.
- **Fix:** add `Pillow>=10.0.0` and `pypdfium2>=4.30` to `requirements.txt`.

### #6 — app.py monolith
- **Where:** `app.py` — 5,777 lines: all routes, review queue, batch logic, filing, nav-context, background threads.
- **Fix (incremental, no big-bang):** extract cohesive route groups into Flask blueprints (review queue, settings/admin, payments, reports) — `scanflow.py` already demonstrates the intended pattern (`register(app, ctx)` to avoid circular imports); move queue/batch state into its own module; keep `app.py` as app factory + blueprint registration + startup threads.

### #7 — No CI
- **Fix:** add `.github/workflows/tests.yml`: checkout → setup-python → `pip install -r requirements.txt pytest` → `pytest`. The OCR paths degrade gracefully without Tesseract; mark any truly Windows/scanner-dependent tests with a pytest marker and deselect in CI (`pytest.ini` already exists).

### #8 — os._exit(42) restart
- **Status:** partially handled — `api_restart()` already uses `threading.Timer(0.8, lambda: os._exit(42))`, so the HTTP response completes. Remaining exposure is background threads (backup loop, email poll, reindex, an in-flight `_finalize_entry` file move). WAL + the idempotent repair passes make this largely self-healing.
- **Fix (optional):** a shutdown flag background loops observe before starting new work.

### #9 — poplib._MAXLINE monkeypatch
- **Where:** `email_poller.py` sets `poplib._MAXLINE = 10 * 1024 * 1024` (justified: oversized DKIM/References lines killed polling).
- **Fix:** add a test asserting the patch is in effect (value ≥ 10 MB, plus a synthetic long-header message round-trips) so a Python upgrade fails loudly at test time.

### #11 — repo hygiene
- **Fix:** `git rm --cached "Unfiled Scans - Shortcut.lnk" "Klick Klack Profile.sswp"`, add `*.lnk` to `.gitignore`, document recreation in SCANNER_SETUP.md. Delete stale branches `claude/magical-thompson-d08070` and `worktree-agent-a716f4dd6bd2186a2`.

### #12 — secrets in backups
- **Where:** `app.py`, `_write_daily_backup()` copies `config.json` (Claude API key, mailbox passwords, password hashes) into `Backups/auto/config_YYYYMMDD.json` with 30-day retention.
- **Fix options:** (a) back up config minus secrets (secrets re-entered on restore); (b) exclude config and back it up separately to a restricted location; (c) keep as-is + tighten NTFS ACLs on `Backups/auto` and document the decision in DESIGN.md §8.

## 4. Verified-clean areas (do NOT "fix" these)

- **SQL injection:** all f-string SQL sites interpolate allowlisted identifiers only (`database.py` lines 76, 387, 693, 797, 826, 854, 1464–1486; `app.py` 5652/5664; `property_model.py` 290; `assistant.py` 408–412 with clamped int `LIMIT`).
- **CSRF / auth / throttling:** see §2 note on #10; token minted per session, `hmac.compare_digest`, per-IP throttle, idle timeout enforced server-side.
- **Path traversal:** `send_file` paths all come from DB-stored, server-generated paths; user-chosen filing destinations validated against two allowed roots (`filing.validate_filed_dir`); filenames sanitized (`filing.sanitize_filename`, `_make_filename`, `_rename_staged_file`).
- **FTS injection:** `indexer._fts_query` double-quotes user input.
- **Command injection:** no `os.system` / `shell=True` anywhere; `scan_connector.py` uses fixed-argument subprocess calls with `CREATE_NO_WINDOW`.
- **Concurrency:** see §1 — locks exist and are commented with the incidents that motivated them.

## 5. Open question (not filed)

`_make_filename()` (app.py) builds the **paid** filename (`CK#13386…`) with the document's **received** date as the `MM-DD-YY` token; DESIGN.md §5's paid example (`06-14-26 TRUST2 CK#13386 - …`) is ambiguous about whether the date should be the payment date. Check `T:\_OPS INDEX\KAREN_FILENAME_SPEC.md` before touching — this is a 13-year naming convention, so the convention wins over intuition.

## 6. Suggested implementation order

1. **#5** — two lines in requirements.txt
2. **#4** — one log line
3. **#1** — small refactor of `ocr_pdf()`
4. **#7** — ~15 lines of workflow YAML
5. **#11** — git rm + .gitignore
6. **#9** — one test
7. **#2** — one new SQL query + rewired `_history_dir()`
8. **#3 / #12** — need owner decisions first
9. **#8** — optional
10. **#6** — long-term, incremental

## 7. House rules to preserve while changing code

These are load-bearing behaviors — any PR should keep them:

- **Nothing is ever deleted.** Discards go to `_Skipped` / `_Blank` / `_Duplicates` / `_Split Originals` / `_Removed Copies` / `Filtered` — all recoverable.
- **Bills are paid in received-date order**, not due-date order.
- **Config writes are atomic** (`save_config` temp-file + `os.replace`); keep `load_config` reading with explicit `encoding="utf-8"`.
- **Hints are human-taught ground truth**: auto-confirm passes `learn_hints=False` so the machine's own output can't teach itself.
- **SQLite WAL is not safe across hosts** — DB-writing scripts run on the server only, never from a workstation over SMB.
- **The email poller is strictly read-only** (IMAP `readonly=True` + `BODY.PEEK`; POP3 `TOP` not `RETR`, never `DELE`).
- **Ingest is serialized** (`_ingest_lock`) and idempotent (exists / already-queued / hash-dedup checks) — new ingest paths must go through `enqueue_scan`, not around it.
