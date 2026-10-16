"""
app.py – Main Flask application for Klick Klack (Property Management Intake).
Run: python app.py  (or double-click start.bat)
Then open http://localhost:5050 in your browser.
"""
import sys, os, json, re, shutil, threading, time, secrets, hmac, hashlib
from collections import deque
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash

from database import init_db, insert_entry, update_entry, get_entry, get_all_entries, get_dashboard_stats, save_vendor_hint, find_duplicates, delete_entry, get_vendor_summary, get_bills_by_due_date, get_oldest_unpaid_bill, get_bills_for_vendor, get_accounts_for_vendor, log_activity, get_activity_feed, get_entity_summary, get_property_summary, add_attachment, get_attachments, get_attachment, delete_attachment, delete_attachments, update_attachment_path, add_bug_report, get_bug_reports, set_bug_report_status, get_open_report_count, get_active_reports, get_open_report_paths, report_url_path, mark_report_dev_resolved, is_email_processed, mark_email_processed, add_filtered_doc, get_filtered_docs, get_filtered_doc, delete_filtered_doc, get_filtered_count, rename_entity_code, rename_property_code, rename_bill_type_code, save_pending_scan, delete_pending_scan, clear_pending_scans, get_all_pending_scans, save_account_hint, get_account_hint, save_payment_splits, get_payment_splits, get_entries_by_hash, get_entries_missing_hash, add_expected_bill, get_expected_bills, set_expected_bill_active, delete_expected_bill, bills_seen_since, get_vendor_hint_exact, count_auto_filed_since, get_important_correspondence, save_karen_answer, get_karen_answers, get_board_items, count_taken_down_since, get_bills_for_month, record_queue_depth, get_queue_depth_history, confirm_counts_for_day, get_vendor_directory, entity_doc_counts, months_for_entity, docs_for_entity_month, count_filtered_since, get_hash_dupe_groups, reassign_attachments, reassign_payment_splits, log_helper_chat, search_documents, find_invoice_twins, get_entry_by_record_no, find_link_candidates, set_no_original, find_similar_vendor, find_vendor_clusters, rename_vendor, add_record_pass, get_passes_for_user, get_passes_for_entry, get_pass, mark_pass_done, open_pass_count
from classifier import classify_document
import anthropic
import assistant
import doc_types
import email_append   # the source email permanently at the back of the filed PDF
import email_backfill
import indexer
import mail_archive   # local searchable copy of every office mailbox (/mail)
import mail_health    # "mail stopped flowing" watchdog -> every-page red banner
import longpaths
import nameparse
import page_titles
import queue_state
import request_health  # "all workers blocked" watchdog -> thread dump + banner
import filing_ledger   # cabinet_watch must not mistake our own moves for hand-filing
from watcher import start_watcher
from property_model import (init_property_schema, seed_property_model,
                           get_properties, get_entities, update_property_units,
                           entity_unit_weights, sync_entity_upsert, sync_entity_rename,
                           sync_entity_deactivate, sync_property_upsert,
                           sync_property_deactivate, property_reference_count)

def _sync_model(fn, *args):
    """Best-effort relational-model sync after a config edit — the config change
    itself must never fail because of it."""
    try:
        fn(*args)
    except Exception as e:
        print(f"[PropertyModel] sync failed ({fn.__name__}): {e}")
from splitter import split_if_needed
import email_poller

app = Flask(__name__)
# Defense-in-depth for the session cookie: not readable from JS, not sent on
# cross-site requests (backs up the CSRF tokens). Secure flag needs HTTPS,
# which this LAN deployment doesn't have.
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  # Generous request-size ceiling: covers phone-photo receipt
                  # uploads and report screenshots while still bounding abuse.
                  MAX_CONTENT_LENGTH=32 * 1024 * 1024)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SECRET_KEY_PATH = os.path.join(os.path.dirname(__file__), "secret_key.txt")

def _load_secret_key():
    """Load a persistent random secret key, generating one on first run.
    Kept out of the source so the session signing key isn't a guessable constant."""
    try:
        with open(SECRET_KEY_PATH) as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = os.urandom(32).hex()
    try:
        with open(SECRET_KEY_PATH, "w") as f:
            f.write(key)
    except Exception as e:
        print(f"[App] Could not persist secret key: {e}")
    return key

app.secret_key = _load_secret_key()

# Guards every read-modify-write of config.json. Reentrant so a locked route can
# call load_config/save_config (which take it again) freely.
_config_lock = threading.RLock()

def load_config():
    # Explicit encoding: the file is kept ASCII (json.dump's ensure_ascii), but
    # never depend on the Windows locale default (cp1252) to read it back.
    with _config_lock:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)

def save_config(cfg):
    """Write config.json atomically: dump to a sibling temp file, then os.replace
    it in. A crash or power cut mid-write can therefore never leave a truncated
    config.json — which would stop the app from booting at all, since startup and
    every request read it."""
    with _config_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        # Windows can refuse the replace while another handle has the file open
        # (readers outside _config_lock, e.g. classifier.load_config on an ingest
        # thread). That window is microseconds long, so retry briefly.
        for _ in range(10):
            try:
                os.replace(tmp, CONFIG_PATH)
                return
            except PermissionError:
                time.sleep(0.05)
        os.replace(tmp, CONFIG_PATH)

def config_mutating(f):
    """Run the whole route under the config lock. These handlers load config,
    modify it, and save it back — two running at once (two admins, or an admin
    save racing an inline bill-type add) would silently drop one side's changes,
    e.g. revert a just-changed password."""
    @wraps(f)
    def _wrap(*args, **kwargs):
        with _config_lock:
            return f(*args, **kwargs)
    return _wrap

# ── Shared intake state (queue / batch / jobs) ───────────────────────────────
# The review queue, the scanning batch, and the background-job registry live in
# queue_state.py — see its docstring for the locking rules and for why the state
# moved out ahead of the routes. The aliases below keep the original private
# names working for app.py's own code, for the route modules that reach them
# through _M, and for the tests. They bind OBJECTS that queue_state only ever
# mutates, never reassigns; the values that DO get rebound (_next_scan_id,
# _current_batch, BATCH_STATE_FILE) are deliberately absent here and must be
# read through queue_state so nothing can end up holding a stale copy.
pending_scans = queue_state.pending_scans
_queue_lock = queue_state.queue_lock
_ingest_lock = queue_state.ingest_lock
_batch_lock = queue_state.batch_lock
_jobs_running = queue_state._jobs_running
_queue_add = queue_state.queue_add
_queue_remove = queue_state.queue_remove
_queue_items = queue_state.queue_items
_claim_scan = queue_state.claim_scan
_release_scan = queue_state.release_scan
_load_pending_from_db = queue_state.load_pending_from_db
get_scan = queue_state.get_scan
_save_current_batch = queue_state.save_current_batch
_load_current_batch = queue_state.load_current_batch
_next_batch_number = queue_state.next_batch_number
_start_batch = queue_state.start_batch
_ensure_batch = queue_state.ensure_batch
_try_start_job = queue_state.try_start_job
_finish_job = queue_state.finish_job
_job_running = queue_state.job_running

def _archive_cover_sheet(path):
    """Move a detected cover sheet out of the queue into a Batch Covers archive
    (audit trail for re-scans); never queued, never deleted."""
    try:
        cfg = load_config()
        base = cfg.get("filed_folder", "F:\\File-Cabinet\\IntakeSystem\\Filed")
        dest_dir = os.path.join(os.path.dirname(base), "Batch Covers")
        os.makedirs(dest_dir, exist_ok=True)
        bn = (queue_state.current_batch() or {}).get("batch_number", "unknown")
        dest = os.path.join(dest_dir, f"{bn} cover{os.path.splitext(path)[1]}")
        i = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{bn} cover ({i}){os.path.splitext(path)[1]}")
            i += 1
        if os.path.exists(path):
            filing_ledger.announce(dest)
            shutil.move(path, dest)
    except Exception as e:
        print(f"[Batch] could not archive cover sheet: {e}")

# Document types we ingest: PDFs, images (phone photos / scans), and Word docs.
SUPPORTED_DOC_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".docx")

def _read_email_meta(path: str) -> dict:
    """Read the sidecar an email attachment carries (its real received date from
    the email's Date header, plus sender/subject). The poller writes it next to
    the attachment as '<file>.meta.json'. Returns {} when there's none. Does NOT
    delete the sidecar — the caller discards it only after the document is safely
    queued, so a failed ingest can retry with the real received date instead of
    silently falling back to the pull date."""
    meta_path = path + ".meta.json"
    if not os.path.exists(meta_path):
        return {}
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _discard_email_meta(path: str):
    try:
        os.remove(path + ".meta.json")
    except OSError:
        pass

def enqueue_scan(pdf_path: str, source: str = "Physical Mail"):
    """Called by a folder watcher when a new PDF arrives. `source` is the intake
    channel ('Physical Mail' for the scan folder, 'Email' for the email folder).
    Serialized: the two watchers, the startup sweep, and Check for new items can all call
    this concurrently — even for the same file — and one at a time is what makes
    the duplicate checks below reliable."""
    with _ingest_lock:
        _enqueue_scan_locked(pdf_path, source)

def _enqueue_scan_locked(pdf_path: str, source: str):
    # Over-long filenames (ScanSnap's name-by-recognized-text setting) make
    # plain os.path calls report the file missing on Windows — shorten the
    # name first, or the exists() below quietly drops the scan (#42/#48).
    pdf_path = longpaths.rescue_long_name(pdf_path)
    # A duplicate call for a file an earlier ingest already renamed/filed/moved
    # aside lands here after the file is gone — nothing to do.
    if not os.path.exists(pdf_path):
        return
    # Skip if already queued
    if any(v.get("pdf_path") == pdf_path for _, v in _queue_items()):
        return
    cfg = load_config()
    api_key = cfg.get("claude_api_key", "")
    # Email attachments carry their real received date (+ sender/subject) in a
    # sidecar keyed to the dropped file; read it before the file is split/renamed.
    email_meta = _read_email_meta(pdf_path) if source == "Email" else {}
    # The ORIGINAL file's own date, read BEFORE splitting — split parts are
    # written fresh, so their mtimes are always "now" even when the source
    # scan is months old. This is the backlog tell: a file that predates the
    # batch's mail-received day cannot be part of that batch's mail (the
    # 08. AUGUST incident — June scans confirmed 8/5 filed as August mail).
    src_file_date = None
    if source != "Email":
        try:
            src_file_date = date.fromtimestamp(os.path.getmtime(pdf_path))
        except OSError:
            pass
    # Split multi-document PDFs before classifying
    parts = split_if_needed(pdf_path, api_key)
    for part_path in parts:
        if any(v.get("pdf_path") == part_path for _, v in _queue_items()):
            continue

        result = classify_document(part_path)
        # Cover sheet: starts a new batch and sets its received date for every file
        # that follows, then gets archived (never enters the review queue). Physical
        # mail only — email carries its own date and isn't batched this way.
        if source != "Email" and result.get("is_cover_sheet"):
            _start_batch(result.get("received_date"))
            _archive_cover_sheet(part_path)
            continue
        # Skip blank PDF pages only — the word-count filter applies solely to PDFs
        # whose own text layer is nearly empty. The classifier reports non-text
        # reads with a "[...]" sentinel (e.g. "[scanned PDF – read via vision]"):
        # counting a sentinel's words used to delete every image-only scanned PDF
        # right after it was successfully classified. Sentinels and failed
        # extractions ("") go to review instead, and genuine blanks are moved to a
        # recoverable _Blank folder — the intake pipeline never deletes documents.
        # Email only: a physically scanned document was fed by a person on purpose,
        # so it must always reach review — silence reads as "the scan flow is
        # broken" (first live test 7/24: a handwritten sheet whose only text-layer
        # content was one stray "/" scanner artifact vanished into _Blank).
        if source == "Email" and part_path.lower().endswith(".pdf"):
            extracted = result.get("extracted_text") or ""
            has_text_layer = bool(extracted) and not extracted.startswith("[")
            if has_text_layer and len([w for w in extracted.split() if len(w) > 1]) < 15:
                print(f"[App] Setting aside blank document: {os.path.basename(part_path)}")
                _move_aside(part_path, "_Blank")
                continue
        # Exact-duplicate detection: same bytes as an already-logged document →
        # file it as a copy of that record, never queued (a re-sent email, a
        # re-scan). Same bytes as another QUEUED item → set aside. Hashed before
        # the rename below so nothing else has to care.
        try:
            file_hash = _sha256_file(part_path)
        except OSError as e:
            print(f"[Dedup] could not hash {os.path.basename(part_path)}: {e}")
            file_hash = ""
        if file_hash:
            owners = get_entries_by_hash(file_hash)
            if owners:
                _auto_attach_copy(owners[0], part_path, source,
                                  email_meta=email_meta if source == "Email" else None)
                continue
            if any(v.get("file_hash") == file_hash for _, v in _queue_items()):
                print(f"[Dedup] {os.path.basename(part_path)} is byte-identical to a "
                      f"queued item; setting aside.")
                _move_aside(part_path, "_Duplicates")
                continue
        result["file_hash"] = file_hash
        _apply_account_hint(result, cfg)

        result["mail_type"] = source   # tag the intake channel (Physical Mail / Email)
        # Rename the staging file to a readable name based on what Claude found
        staged_path = _rename_staged_file(part_path, result)
        result["pdf_path"] = staged_path
        # Stamp received/scanned date + batch number. Physical mail inherits the
        # current batch (from its cover sheet); email carries its own arrival date.
        today_iso = date.today().isoformat()
        if source == "Email":
            # Karen operates off the date the mail was RECEIVED (the email's own Date
            # header), not when we pulled it. Fall back to today only if unparseable.
            result["received_date"] = email_meta.get("received_date") or today_iso
            result["scanned_date"] = today_iso
            result.setdefault("batch_number", None)
            # Source-email details for storing/showing alongside the bill (#14/#17).
            if email_meta.get("from"):
                result["email_from"] = email_meta["from"]
            if email_meta.get("to"):
                result["email_to"] = email_meta["to"]
            if email_meta.get("account"):
                result["email_account"] = email_meta["account"]
            if email_meta.get("subject"):
                result["email_subject"] = email_meta["subject"]
            if email_meta.get("email_date_raw"):
                result["email_date"] = email_meta["email_date_raw"]
            if email_meta.get("body"):
                result["email_body"] = email_meta["body"]
        else:
            b = _ensure_batch()
            result["scanned_date"] = b["scanned_date"]
            result["batch_number"] = b["batch_number"]
            # A received-date stamp ON the bill (Karen stamps physical mail before
            # scanning) is document-specific, so it wins over the batch/assumed date.
            stamped = result.get("stamped_received_date")
            if stamped:
                result["received_date"] = stamped
                result.pop("received_date_assumed", None)
            else:
                # Backlog scan (stranded file, old copy swept in later): the
                # batch/assumed date is provably wrong, so the document's own
                # printed date — else the scan file's date — stands in. Never
                # the ingest/confirm day (report #90 follow-on, 08. AUGUST).
                backlog = _backlog_received_date(result.get("document_date"),
                                                 src_file_date,
                                                 b["received_date"])
                if backlog:
                    result["received_date"], result["received_date_source"] = backlog
                    result["received_date_assumed"] = True
                else:
                    result["received_date"] = b["received_date"]
                    if b.get("assumed_date"):
                        result["received_date_assumed"] = True
        # AI sort: keep noisy EMAIL junk (correspondence/marketing/etc.) out of the
        # review queue. It is moved to a recoverable "Filtered" area, never deleted.
        if source == "Email":
            junk_reason = _email_junk_reason(result)
            if junk_reason:
                _file_as_filtered(staged_path, result, junk_reason)
                continue
        # Tiered auto-confirm: a fully-resolved, high-confidence bill whose facts
        # agree with what a person previously taught the system files itself —
        # review is for the exceptions. Any doubt or error falls through to the
        # queue; auto-confirm never loses a document.
        if _try_auto_confirm(result, cfg):
            continue
        _queue_add(result)
    # Every part is queued/filed — the sidecar has served its purpose. (On an
    # exception above it survives, so a retry still sees the real received date.)
    if source == "Email":
        _discard_email_meta(pdf_path)

_sweep_failures = {}       # path -> consecutive enqueue failures (memory only)
_SWEEP_MAX_TRIES = 3       # then leave it alone (still visible on /scan)
_SWEEP_MIN_AGE_SECS = 120  # skip files younger than this: likely mid-ingest

def _sweep_unprocessed(scan_folder: str, email_folder: str):
    """Catch-up ingest of files already sitting in the watched folders.

    The folder watcher only fires on newly-created/renamed files, so anything
    that landed while the app was DOWN (e.g. the server was off during a power
    outage) is never seen — it just sits in the folder, silently missed. That
    is how the Malaga bank statement slipped through. Runs at startup and then
    every 10 minutes (see _sweep_loop) and enqueues any supported file that
    isn't already in the review queue. It's idempotent: queued files are
    skipped here, and confirmed files have already been moved out to the Filed
    tree, so a file is only ever picked up once. A file that keeps failing is
    retried _SWEEP_MAX_TRIES times, then left for /scan to surface — retrying
    a poison file forever would burn an AI-classification call every cycle.
    """
    already = set(os.path.normcase(v.get("pdf_path", "")) for _, v in _queue_items())
    now = time.time()
    targets = []
    for folder, source in ((scan_folder, "Physical Mail"), (email_folder, "Email")):
        if not folder or not os.path.isdir(folder):
            continue
        try:
            for name in os.listdir(folder):          # non-recursive: leaves _Filtered/ alone
                p = os.path.join(folder, name)
                # longpaths: a plain isfile() reports over-MAX_PATH files as
                # missing, which is exactly how they got stranded (#48).
                if not longpaths.isfile(p):
                    continue
                if not p.lower().endswith(SUPPORTED_DOC_EXTS):
                    continue
                if os.path.normcase(p) in already:
                    continue
                if _sweep_failures.get(p, 0) >= _SWEEP_MAX_TRIES:
                    continue
                try:
                    if now - longpaths.getmtime(p) < _SWEEP_MIN_AGE_SECS:
                        continue   # the watcher is probably mid-ingest on it
                except OSError:
                    pass
                targets.append((p, source))
        except Exception as e:
            print(f"[Sweep] could not list {folder}: {e}")
    if not targets:
        return
    print(f"[Sweep] Found {len(targets)} unprocessed file(s) left in the watched folders; "
          f"ingesting them now.")
    for p, source in targets:
        try:
            enqueue_scan(p, source)
            _sweep_failures.pop(p, None)
        except Exception as e:
            _sweep_failures[p] = _sweep_failures.get(p, 0) + 1
            print(f"[Sweep] failed on {os.path.basename(p)} "
                  f"(attempt {_sweep_failures[p]}/{_SWEEP_MAX_TRIES}): {e}")

def _sweep_loop(scan_folder: str, email_folder: str):
    """Startup catch-up sweep, then a re-sweep every 10 minutes for the life of
    the process. The recurring pass is the safety net for anything the watcher
    misses live (dropped share, observer hiccup, an unforeseen rename pattern) —
    the rule after #42/#48 is that nothing may sit in a watched folder silently."""
    while True:
        try:
            if not _job_running("scan"):   # don't pile onto a running Check for new items
                _sweep_unprocessed(scan_folder, email_folder)
        except Exception:
            import traceback
            print(f"[Sweep] sweep pass crashed:\n{traceback.format_exc()}")
        time.sleep(600)

# ── Review-queue filter (kept in sync with the client-side filter in
#    review_queue.html) so the detail view's Prev/Next and the post-confirm
#    "advance to next" stay within the filtered group. ──────────────────────────
FILTER_KEYS = ("batch", "received", "doctype", "conf", "q", "entity", "billtype")

def _filter_qs(args):
    """Querystring of just the active review filters, to carry through navigation."""
    from urllib.parse import urlencode
    d = {k: args.get(k) for k in FILTER_KEYS if args.get(k)}
    return ("?" + urlencode(d)) if d else ""

def _filtered_ids(args):
    """Sorted scan ids matching the active review filters (all ids if none set)."""
    snapshot = dict(_queue_items())
    ids = sorted(snapshot.keys())
    b = args.get("batch"); rd = args.get("received"); dt = args.get("doctype")
    cf = args.get("conf"); q = (args.get("q") or "").lower()
    en = args.get("entity"); bt = args.get("billtype")
    if not (b or rd or dt or cf or q or en or bt):
        return ids
    out = []
    for sid in ids:
        d = snapshot.get(sid) or {}
        if b and (d.get("batch_number") or "") != b: continue
        if rd and (d.get("received_date") or "") != rd: continue
        if dt and (d.get("doc_type") or "") != dt: continue
        if cf and (d.get("ai_confidence") or "none").lower() != cf: continue
        if en and (d.get("entity_code") or "") != en: continue
        if bt and (d.get("bill_type_code") or "") != bt: continue
        if q:
            hay = " ".join([str(d.get("vendor") or ""), os.path.basename(d.get("pdf_path") or ""),
                            str(d.get("property_code") or ""), str(d.get("entity_code") or "")]).lower()
            if q not in hay: continue
        out.append(sid)
    return out

# Which doc types make an email worth keeping (everything else is filed aside
# as junk). Single-sourced from doc_types.py, in the SAME commit that taught
# the classifier to emit Statement/Insurance/Tax/Audit — the pairing the
# doc_types module and tests/test_doc_types.py insist on.
_EMAIL_RELEVANT_DOCTYPES = set(doc_types.EMAIL_RELEVANT_WHEN_AI_LEARNS_THESE)
# The classifier's regex fallback stamps this marker into notes when the API
# call failed (no key, no credits, network). Such classifications are keyword
# guesses, and several features treat them specially.
_AI_UNAVAILABLE_MARK = "[AI unavailable"

def _email_junk_reason(result: dict):
    """For EMAIL-sourced docs only: why this looks like junk (a string for the
    Filtered-area record), or None when it belongs in the review queue.

    Tightened 2026-07-07 after measuring a day of real intake: the forwarding
    inbox receives job applications, marketing, agendas and receipts alongside
    bills, and whenever classification fell back to the keyword regex (API
    outage) those got doc_type guesses of Income/Bank Statement/Legal and
    flooded the queue. Rules, in order:
      - Bill is NEVER junk — a possible bill always gets human eyes.
      - Doc types outside the relevant set are junk (unchanged behavior).
      - A non-Bill classified WITHOUT AI (regex fallback, confidence 'None')
        is junk: fallback doc types are keyword guesses.
      - Income/Bank Statement at Low confidence are junk — the AI itself is
        unsure, and a false negative there is cheap. Legal is exempt: a real
        court notice is the one document that must never be silently set
        aside, so AI-classified Legal stays queued even at Low.
      - An AI-attested importance flag (Medium+ confidence, so a real AI read
        and not the keyword fallback) exempts ANY doc type — an insurance-
        cancellation notice classified Correspondence must reach review.
      - An AI-OUTAGE document (notes carry _AI_UNAVAILABLE_MARK) is ALSO
        exempt, regardless of confidence. 2026-07-26: classify_with_claude's
        outage path forces important=True on exactly these documents so a
        legal/urgent notice can't be missed during an API outage — but that
        document's ai_confidence is always 'None' (the regex fallback), so
        the confidence-gated exemption above never covers it, and it would
        otherwise fall straight into the 'conf == none' junk rule just below.
        Without this, forcing important=True during an outage silently did
        nothing for a non-Bill email attachment."""
    dt = (result.get("doc_type") or "").strip()
    if dt == "Bill":
        return None
    conf = (result.get("ai_confidence") or "None").strip().lower()
    if result.get("important") and conf in ("high", "medium"):
        return None
    if _AI_UNAVAILABLE_MARK in (result.get("notes") or ""):
        return None
    if dt not in _EMAIL_RELEVANT_DOCTYPES:
        return f"AI classified this as {dt or 'Other'} (not a bill)"
    if conf == "none":
        return (f"Labeled {dt} by the keyword fallback while AI was unavailable — "
                "fallback doc types are guesses, so it was kept out of the queue")
    if dt in ("Income", "Bank Statement") and conf == "low":
        return f"AI classified this as {dt} with Low confidence"
    return None

def _is_email_junk(result: dict) -> bool:
    return _email_junk_reason(result) is not None

def _move_aside(path: str, subfolder: str):
    """Move a file into `subfolder` next to it (collision-safe) and return the new
    path, or None if the move failed. This takes a file out of the intake flow
    WITHOUT deleting it — the watcher and the startup sweep only look at the top
    level of the watched folders, so a moved file is never re-ingested but stays
    recoverable. Nothing in the intake pipeline may hard-delete a document."""
    try:
        folder = os.path.join(os.path.dirname(path), subfolder)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, os.path.basename(path))
        root, ext = os.path.splitext(dest)
        c = 1
        while os.path.exists(dest):
            dest = f"{root} ({c}){ext}"
            c += 1
        filing_ledger.announce(dest)
        shutil.move(path, dest)
        return dest
    except Exception as e:
        print(f"[App] Could not move {os.path.basename(path or '?')} to {subfolder}: {e}")
        return None

def _file_as_filtered(path: str, result: dict, reason: str = None):
    """Move a likely-junk email PDF into a recoverable Filtered holding area and
    record it. Never deletes — a misjudged bill can always be sent to review."""
    dest = _move_aside(path, "_Filtered") or path
    result["pdf_path"] = dest
    dt = result.get("doc_type") or "Other"
    add_filtered_doc(dest, os.path.basename(dest), dt, result.get("vendor"),
                     result.get("amount"), result.get("ai_confidence"),
                     reason or f"AI classified this as {dt} (not a bill)", json.dumps(result))
    print(f"[App] Filtered out likely-junk email doc: {os.path.basename(dest)}")

# ── Duplicate detection (byte-level) ──────────────────────────────────────────

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _file_copy_next_to_entry(entry: dict, src_path: str, source: str) -> str:
    """Move src_path to live beside the entry's primary document (or a COPIES
    folder), named as a '(<source> copy)'. Returns the destination — src_path
    itself if the move failed, so the attachment still points somewhere real."""
    orig_name = os.path.basename(src_path)
    primary = entry.get("filed_path") or ""
    if primary and os.path.dirname(primary):
        dest_dir = os.path.dirname(primary)
    else:
        cfg = load_config()
        filed_base = cfg.get("filed_folder", "F:\\File-Cabinet\\IntakeSystem\\Filed")
        dest_dir = os.path.join(filed_base, entry.get("entity_code") or "UNKNOWN", "COPIES")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass
    base, ext = os.path.splitext(orig_name)
    dest_path = os.path.join(dest_dir, f"{base} ({source} copy){ext}")
    counter = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(dest_dir, f"{base} ({source} copy {counter}){ext}")
        counter += 1
    try:
        filing_ledger.announce(dest_path)
        shutil.move(src_path, dest_path)
    except Exception as e:
        print(f"[App] Could not file attached copy: {e}")
        dest_path = src_path
    return dest_path

def _auto_attach_copy(entry: dict, src_path: str, source: str, by: str = "",
                      email_meta: dict = None):
    """A document whose bytes exactly match an already-logged record: file it as
    a copy of that record instead of queueing it for review. Byte-identical
    means the same document, so this is safe and fully reversible — the file is
    kept and listed under the record's 'Copies on file'.

    2026-08-10 (Karen): an EMAIL re-send gets two more things. The arriving
    email's own pages go at the back of the copy file (so the copy explains
    itself the way the primary now does), and a re-sent payable raises the
    record's attention flag — a vendor sending the same bill again is the bill
    trying to get attention, and it must surface where Karen pays."""
    orig_name = os.path.basename(src_path)
    dest = _file_copy_next_to_entry(entry, src_path, source)
    if email_meta:
        try:
            email_append.append_meta_to_file(dest, email_meta)
        except Exception as e:
            print(f"[EmailAppend] copy {os.path.basename(dest)}: {e}")
    add_attachment(entry["id"], dest, source, orig_name, by)
    log_activity(entry["id"], "Duplicate copy attached", by,
                 f"byte-identical to record #{entry['id']} ({source})")
    print(f"[Dedup] Attached exact duplicate to record #{entry['id']}: {orig_name}")
    if source == "Email":
        try:
            box = (email_meta or {}).get("account") or ""
            email_append.flag_resend(
                entry, f"another copy arrived by email"
                       + (f" in {box}" if box else "")
                       + f" on {date.today().isoformat()}", by=by or "System")
        except Exception as e:
            print(f"[EmailAppend] flag resend #{entry.get('id')}: {e}")

# The "important characteristics" compared side-by-side when someone views a
# possible duplicate. Each entry: (label, keys tried on either side — queue
# items and logged records spell some fields differently, kind, signal
# strength, points FOR a duplicate when it matches, points AGAINST when it
# differs).
#
# The signals are deliberately asymmetric (Karen's rule): a bill's identity
# lives in its invoice #, amount, and due date — matching those argues
# duplicate, differing argues different bill. But receipt circumstances
# (received date, source channel, the file bytes) differ freely between two
# copies of the SAME bill, so differing there counts ZERO against a match,
# while matching still adds a little. "info" fields are display-only context.
_DUP_COMPARE_FIELDS = [
    ("Vendor",        ("vendor",),                         "text",   "medium", 2, 2),
    ("Amount",        ("amount",),                         "amount", "strong", 3, 3),
    ("Due date",      ("due_date",),                       "text",   "strong", 3, 2),
    ("Invoice #",     ("invoice_number",),                 "id",     "strong", 4, 3),
    ("Account #",     ("account_number",),                 "id",     "medium", 2, 2),
    ("Received date", ("received_date", "date_received"),  "text",   "weak",   1, 0),
    ("Scanned date",  ("scanned_date",),                   "text",   "info",   0, 0),
    ("Batch",         ("batch_number",),                   "id",     "info",   0, 0),
    ("Type",          ("doc_type",),                       "text",   "info",   0, 0),
    ("Entity",        ("entity_code",),                    "id",     "medium", 1, 1),
    ("Property",      ("property_codes", "property_code"), "props",  "medium", 1, 1),
    ("Source",        ("mail_type",),                      "text",   "info",   0, 0),
    ("Email sender",  ("email_from",),                     "text",   "weak",   1, 0),
    ("Email subject", ("email_subject",),                  "text",   "weak",   1, 0),
]

# How much a MISMATCH counts, scaled by how confidently the AI read the
# unlogged scan — on a low-confidence read, a "different" amount is as likely
# a misread as a real difference (also Karen's rule). Matches keep full
# weight: a misread producing a coincidental match is far less likely than a
# miss.
_DIFF_CONFIDENCE_WEIGHT = {"High": 1.0, "Medium": 0.75}   # anything else: 0.5

# Verdict ladder over the signal score (match points minus weighted mismatch
# points). A byte-identical file short-circuits to "certain" above all this.
_DUP_VERDICTS = [
    (6, "very_likely", "Very likely the same bill"),
    (4, "likely",      "Likely the same bill"),
    (2, "possible",    "Possibly the same bill"),
]

def _cmp_value(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None

def _cmp_norm(v, kind):
    """Normalize a value for equality: '$147.48' == 147.48, 'SCE ' == 'sce',
    account '12-3456' == '123456', property list == its comma-joined string."""
    if kind == "amount":
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return str(v).strip().casefold()
    if kind == "props":
        parts = v if isinstance(v, (list, tuple)) else str(v).split(",")
        return tuple(sorted(str(p).strip().casefold() for p in parts if str(p).strip()))
    s = str(v).strip()
    if kind == "id":
        s = s.replace(" ", "").replace("-", "")
    return s.casefold()

def _cmp_display(v, kind):
    if v is None:
        return ""
    if kind == "amount":
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return str(v)
    if kind == "props":
        parts = v if isinstance(v, (list, tuple)) else str(v).split(",")
        return ", ".join(str(p).strip() for p in parts if str(p).strip())
    return str(v)

def _dup_comparison(left: dict, right: dict) -> dict:
    """Field-by-field comparison of two documents' important characteristics —
    a queued scan vs a logged record, or two queued scans (queue and DB key
    spellings both work). Feeds the side-by-side duplicate view: rows for the
    table (each carrying its signal strength) plus a verdict.

    Returned dict:
      rows                 [{label, left, right, state, signal}]
      same                 count of matching fields (any strength)
      diff / diff_labels   MEANINGFUL differences only (strong/medium signals)
      expected_diff_labels weak/info differences — normal between two copies
                           of the same bill, count zero against a match
      compared             same + diff + expected diffs
      verdict              {level, label, score, reason}
    Fields blank on BOTH sides are omitted; a field blank on just one side is
    shown grey and counted in neither bucket."""
    rows, same, diff = [], 0, 0
    diff_labels, expected_diff_labels = [], []
    score = 0.0
    strong_matches, meaningful_diffs = [], []
    conf = str(left.get("ai_confidence") or "").strip().title()
    diff_weight = _DIFF_CONFIDENCE_WEIGHT.get(conf, 0.5)
    for label, keys, kind, signal, w_match, w_diff in _DUP_COMPARE_FIELDS:
        lv, rv = _cmp_value(left, keys), _cmp_value(right, keys)
        if lv is None and rv is None:
            continue
        if lv is None or rv is None:
            state = "blank"
        elif _cmp_norm(lv, kind) == _cmp_norm(rv, kind):
            state, same = "same", same + 1
            score += w_match
            if signal == "strong":
                strong_matches.append(label)
        else:
            state = "diff"
            if signal in ("strong", "medium"):
                diff += 1
                diff_labels.append(label)
                meaningful_diffs.append(label)
                score -= w_diff * diff_weight
            else:
                expected_diff_labels.append(label)
        rows.append({"label": label, "left": _cmp_display(lv, kind),
                     "right": _cmp_display(rv, kind), "state": state,
                     "signal": signal})
    # The document file itself. Identical bytes = certain duplicate, full stop.
    # DIFFERENT bytes prove nothing (a re-scan or an email vs its paper copy
    # never match byte-for-byte), so it scores zero against — Karen's rule.
    lh, rh = left.get("file_hash") or "", right.get("file_hash") or ""
    file_state = None
    if lh and rh:
        file_state = "same" if lh == rh else "diff"
        if file_state == "same":
            same += 1
        else:
            expected_diff_labels.append("Document file")
        lp = left.get("pdf_path") or left.get("filed_path") or ""
        rp = right.get("filed_path") or right.get("pdf_path") or ""
        # Full server paths ride along so someone working the manual process
        # can go straight to the file (Karen's request).
        rows.append({"label": "Document file", "state": file_state,
                     "signal": "file",
                     "left": os.path.basename(lp) or "this scan",
                     "right": os.path.basename(rp) or "filed document",
                     "left_path": lp, "right_path": rp})
    # Verdict + a plain-language reason Karen can sanity-check.
    reason = []
    if file_state == "same":
        level, label_txt = "certain", "Certain duplicate — the exact same file"
        reason.append("The two files are byte-for-byte identical, so this is "
                      "the same document regardless of anything else.")
    else:
        level, label_txt = "weak", "Weak match — may be a different bill"
        for threshold, lvl, txt in _DUP_VERDICTS:
            if score >= threshold:
                level, label_txt = lvl, txt
                break
        if strong_matches:
            reason.append("Strong signals match: " + ", ".join(strong_matches) + ".")
        if meaningful_diffs:
            reason.append("Meaningful difference" + ("s" if len(meaningful_diffs) != 1 else "")
                          + ": " + ", ".join(meaningful_diffs)
                          + " — could be a different bill, or a misread.")
            if diff_weight < 1.0:
                reason.append(f"The AI read this scan with "
                              f"{conf or 'unknown'} confidence, so those "
                              f"differences are weighed lightly.")
        if file_state == "diff":
            reason.append("The files themselves differ — normal for a second "
                          "copy (a re-scan, or email vs paper). It counts "
                          "zero against a match.")
    return {"rows": rows, "same": same, "diff": diff,
            "compared": same + diff + len(expected_diff_labels),
            "diff_labels": diff_labels,
            "expected_diff_labels": expected_diff_labels,
            "verdict": {"level": level, "label": label_txt,
                        "score": round(score, 2), "reason": " ".join(reason)}}

def _backfill_file_hashes():
    """Startup maintenance: compute document hashes for records that predate
    duplicate detection. Idempotent; must run in the app's process (DB writes)."""
    try:
        done = 0
        for t in get_entries_missing_hash():
            p = t.get("filed_path") or t.get("pdf_path") or ""
            if not (p and os.path.exists(p)):
                continue
            try:
                update_entry(t["id"], {"file_hash": _sha256_file(p)})
                done += 1
            except OSError:
                continue
        if done:
            print(f"[Dedup] Backfilled document hashes for {done} record(s).")
    except Exception as e:
        print(f"[Dedup] hash backfill failed: {e}")

def _apply_account_hint(result: dict, cfg: dict):
    """Auto-assign property (and entity) from a learned account number — the
    strongest, most stable property signal; recurring utility bills then need
    zero edits. Used at ingest and again when an item is re-classified."""
    acct = (result.get("account_number") or "").strip()
    if not acct:
        return
    hint = get_account_hint(acct)
    if hint and hint.get("property_codes"):
        pcs = [c.strip() for c in hint["property_codes"].split(",") if c.strip()]
        if pcs:
            addr = {p["code"]: p["address"] for p in cfg.get("properties", [])}
            result["property_codes"] = pcs
            result["property_addresses"] = [addr[c] for c in pcs if c in addr]
            if hint.get("entity_code") and not result.get("entity_code"):
                result["entity_code"] = hint["entity_code"]

# ── Tiered auto-confirm (file predictable bills at ingest; review = exceptions) ──
# Joshua's direction 2026-07-02: bypass per-document review — but not blindly.
# Blind bypass fails on exactly three things: a misfiled entity/property means
# the document is silently lost in the filing paradigm; email junk pollutes the
# bills-owed totals; a duplicate gets paid twice. So a document files itself
# ONLY when every fact is machine-known or human-corroborated, and any doubt
# sends it to the queue like before.

def _auto_confirm_mode(cfg) -> str:
    mode = (cfg.get("auto_confirm") or "known-vendors").strip().lower()
    return mode if mode in ("off", "known-vendors", "aggressive") else "known-vendors"

def _should_auto_confirm(result: dict, cfg: dict) -> bool:
    """ALL of: mode enabled; doc_type Bill; HIGH AI confidence; a real (not
    assumed) received date; entity AND property resolved; a positive amount.
    In 'known-vendors' mode (default) the vendor or account must also carry a
    human-confirmed hint. In every mode, a hint that DISAGREES with the AI's
    entity/property vetoes auto-filing — humans teach, the machine only acts
    where its answer matches what a human taught."""
    mode = _auto_confirm_mode(cfg)
    if mode == "off":
        return False
    if (result.get("doc_type") or "").strip() != "Bill":
        return False
    if (result.get("ai_confidence") or "").strip().lower() != "high":
        return False
    if result.get("received_date_assumed"):
        return False
    if result.get("important"):
        return False              # "needs eyes" is the opposite of auto-file
    entity = (result.get("entity_code") or "").strip()
    props = [c for c in (result.get("property_codes") or []) if c]
    if not entity or not props:
        return False
    try:
        amount = float(result.get("amount"))
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    vendor_hint = get_vendor_hint_exact(result.get("vendor") or "")
    acct_hint = get_account_hint(result.get("account_number") or "")
    if mode == "known-vendors" and not (vendor_hint or acct_hint):
        return False
    for h in (vendor_hint, acct_hint):
        if not h:
            continue
        if (h.get("entity_code") or "").strip() and h["entity_code"].strip() != entity:
            return False
        hint_props = {c.strip() for c in (h.get("property_codes") or "").split(",") if c.strip()}
        if hint_props and not (hint_props & set(props)):
            return False
    return True

# The /scan page's answer to "my scan vanished": an auto-confirmed scan never
# reaches the review queue, so the arrival watch used to stay silent about it —
# the person at the scanner couldn't tell "filed itself" from "lost" (#35's
# spirit). Recent auto-files are kept here for /api/scan/arrivals to report.
_recent_auto_filed = deque(maxlen=20)

def _auto_filed_since(ts: float) -> list:
    return [dict(x) for x in list(_recent_auto_filed)
            if x.get("at", 0) > ts and x.get("mail_type") == "Physical Mail"]

def _try_auto_confirm(result: dict, cfg: dict) -> bool:
    """Attempt to file this classified document without review. True = filed
    (caller skips the queue). Any failure or doubt returns False so the document
    falls back to the review queue — auto-confirm may never lose a document."""
    try:
        if not _should_auto_confirm(result, cfg):
            return False
        entry, pcs = _entry_from_scan_data(result, cfg, "Auto")
        # Same veto bulk-confirm applies: anything that smells like a duplicate
        # is left for a person (double-pay protection).
        if find_duplicates(entry["vendor"], entry["amount"], entry["due_date"],
                           entry["invoice_number"], entry["pdf_path"],
                           entry["date_received"]):
            return False
        mode = _auto_confirm_mode(cfg)
        entry_id = _finalize_entry(
            entry, entry["pdf_path"], cfg, pcs,
            f"auto-confirm ({mode}): high-confidence bill, entity+property "
            f"resolved, no duplicate signals",
            learn_hints=False)
        print(f"[AutoConfirm] Filed #{entry_id}: {entry.get('vendor')} "
              f"${entry.get('amount')} -> {entry.get('entity_code')}/{entry.get('property_code')}")
        fresh = get_entry(entry_id) or {}
        _recent_auto_filed.append({
            "at": time.time(), "id": entry_id,
            "record_no": fresh.get("record_no") or "",
            "mail_type": entry.get("mail_type") or "",
            "name": os.path.basename(fresh.get("filed_path") or "")
                    or (entry.get("vendor") or "Document"),
            "dir_unc": filing.display_path(
                os.path.dirname(fresh.get("filed_path") or ""), cfg),
        })
        return True
    except Exception as e:
        print(f"[AutoConfirm] failed on {os.path.basename(result.get('pdf_path') or '?')}: "
              f"{e} — sending to review instead.")
        return False

# ── Re-classify after an API outage ───────────────────────────────────────────
# When the Claude API is down (out of credits, bad key, network), ingest falls
# back to keyword-regex classification and every field on those items is a
# guess. Once the API is fixed, an admin can re-run the AI over exactly those
# items from the Review Queue; junk drains to Filtered and predictable bills
# auto-file, the same as if the AI had been up at ingest time.

def _ai_outage_sids():
    """Queue ids of items whose classification fell back to regex (outage marker)."""
    return [sid for sid, d in _queue_items()
            if _AI_UNAVAILABLE_MARK in (d.get("notes") or "")]

# Ingest-time facts about the file/channel that a re-run must never overwrite.
_RECLASSIFY_KEEP = ("pdf_path", "file_hash", "mail_type", "received_date",
                    "scanned_date", "batch_number", "received_date_assumed",
                    "received_date_source",
                    "email_from", "email_to", "email_account",
                    "email_subject", "email_date", "email_body")

def _reclassify_items(sids):
    """Background worker: re-run classification on the given queue items. Each
    item is claimed like a confirm would be, so a concurrent reviewer can't
    collide with the swap. Items whose re-run STILL hits the outage marker are
    left untouched (the banner stays up). Caller must have already claimed the
    'reclassify' job slot via _try_start_job; this releases it when done."""
    refreshed = junked = auto_filed = still_failing = 0
    try:
        cfg = load_config()
        for sid in sids:
            d = _claim_scan(sid)
            if d is None:
                continue
            try:
                path = d.get("pdf_path") or ""
                if not (path and os.path.exists(path)):
                    continue
                result = classify_document(path)
                if _AI_UNAVAILABLE_MARK in (result.get("notes") or ""):
                    still_failing += 1
                    continue
                for k in _RECLASSIFY_KEEP:
                    if k in d:
                        result[k] = d[k]
                _apply_account_hint(result, cfg)
                if d.get("mail_type") == "Email":
                    junk_reason = _email_junk_reason(result)
                    if junk_reason:
                        _file_as_filtered(path, result, junk_reason)
                        _queue_remove(sid)
                        junked += 1
                        continue
                if _try_auto_confirm(result, cfg):
                    _queue_remove(sid)
                    auto_filed += 1
                    continue
                with _queue_lock:
                    result.pop("_processing", None)
                    if sid in pending_scans:   # unless removed meanwhile
                        pending_scans[sid] = result
                try:
                    save_pending_scan(sid, json.dumps(result))
                except Exception as e:
                    print(f"[Reclassify] could not persist scan {sid}: {e}")
                refreshed += 1
            finally:
                _release_scan(sid)
    finally:
        _finish_job("reclassify")
    summary = (f"{refreshed} refreshed, {junked} filtered as junk, "
               f"{auto_filed} auto-filed, {still_failing} still failing")
    print(f"[Reclassify] Done: {summary}.")
    try:
        log_activity(None, "Re-ran AI classification", "", summary)
    except Exception:
        pass

# A due date this far BEFORE the document's own received date is a misread year,
# not a bill that is late. Bills do arrive after they were due (that is ordinary,
# and still flags) — but not by half a year.
DUE_BEFORE_RECEIVED_MISREAD_DAYS = 180

def _scan_is_overdue(data: dict) -> bool:
    """True if a not-yet-reviewed scan is a bill whose AI-extracted due date is
    already in the past — lets us flag it 'urgent' before anyone reviews it.

    Report #111 (Joshua, 2026-08-07) found queue item #1194 — a QuickBooks
    payroll-error EMAIL — sitting at the top of the queue in red. Two holes,
    both closed here, because a false 'past due' does real damage: it is what
    the queue sorts on and what the dashboard counts, so it pushes a scam
    notice ahead of the bills Karen actually has to pay.

      * It was never a bill. This docstring has always said 'a bill'; the code
        checked no such thing. doc_types.is_payable is the one place that
        decides now, shared with the review queue's bills/housekeeping split.
      * Its due date was never a due date. The AI lifted 2025-08-28 out of the
        sentence 'You should receive this payment by 08/28/2025' in an email
        that arrived 2026-08-03. A due date almost a year before the document
        itself is a misread, so it no longer raises the alarm — while a bill
        that genuinely turns up a few weeks late still does.
    """
    if not doc_types.is_payable(data.get("doc_type")):
        return False
    dd = (data.get("due_date") or "").strip()
    if not dd:
        return False
    try:
        due = date.fromisoformat(dd)
    except ValueError:
        return False  # non-ISO date from regex fallback — don't guess
    if due >= date.today():
        return False
    try:
        received = date.fromisoformat(
            ((data.get("received_date") or data.get("scanned_date") or "").strip())[:10])
    except ValueError:
        return True   # no usable received date to check against — take the due date
    return (received - due).days <= DUE_BEFORE_RECEIVED_MISREAD_DAYS

# ── Year-sanity guard (record #815, 2026-07-24) ──────────────────────────────
# A degraded Swan Cabinets scan read "JUN 1 2025" on a fresh 2026 document and
# nobody caught it at review: the record got permanent record_no 250629-001
# (record numbers never renumber), a 2025 filing suggestion, and two hand-edits
# to repair. Dates far from today have to be actively kept before Confirm & Log.
# Review + manual entry ask the person; bulk confirm skips flagged items back
# to individual review (2026-07-31). Harvest/backfill go straight to
# insert_entry, which stays unguarded for any date a human could actually mean
# — but since record #941 (2026-08-06, the "06-30-256" filename typo stored as
# year 0256) database._year_net swaps in a sane date and flags the record when
# the year is IMPOSSIBLE (outside 20xx). Suspicious-but-real dates still land
# there untouched and are judged here.

DATE_GUARD_PAST_DAYS = 60        # received/due this far back is usually a misread year
DATE_GUARD_DUE_FUTURE_DAYS = 180 # due dates legitimately run ahead — but not half a year

def _date_sanity_warnings(date_received, due_date, today=None):
    """Warnings for dates that usually mean a misread year: a received date
    more than ~2 months back or any amount in the future, a due date more than
    ~2 months back or more than ~6 months ahead. Non-ISO values pass — the
    forms' date inputs only ever submit ISO or empty, and required-ness is the
    form's job. Thresholds are mirrored in templates/_date_guard.html."""
    today = today or date.today()

    def parse(v):
        try:
            return date.fromisoformat((v or "").strip())
        except ValueError:
            return None

    warnings = []
    d = parse(date_received)
    if d:
        behind = (today - d).days
        if behind > DATE_GUARD_PAST_DAYS:
            age = "a year-old" if behind >= 300 else "a months-old"
            warnings.append(f"This reads as {d.strftime('%B %Y')} — {age} "
                            "received date on a fresh scan is usually a misread.")
        elif d > today:
            warnings.append(f"Date received reads as {d.strftime('%B %Y')} — "
                            "that's in the future; mail can't be received after today.")
    d = parse(due_date)
    if d:
        behind = (today - d).days
        if behind > DATE_GUARD_PAST_DAYS:
            age = "a year-old" if behind >= 300 else "a months-old"
            warnings.append(f"Due date reads as {d.strftime('%B %Y')} — {age} "
                            "due date on a fresh scan usually means a misread year.")
        elif (d - today).days > DATE_GUARD_DUE_FUTURE_DAYS:
            warnings.append(f"Due date reads as {d.strftime('%B %Y')} — more than "
                            "six months out usually means a misread year.")
    return warnings


def _backlog_received_date(document_date, src_file_date, batch_received):
    """Received date for a BACKLOG scan, or None when the batch date stands.

    A scan file whose own timestamp predates the batch's mail-received day
    (today, for a cover-sheet-less assumed batch) cannot be part of that
    batch's mail — it's a stranded June file swept in weeks later, not mail
    that arrived today. Fix contract (FOLDER_NAMING_STANDARDS.md, approved
    8/6): such a document's received date defaults to the AI-read date printed
    on the document itself, else the scan file's own date — never the
    ingest/confirm day, which is what filed four June bills into 08. AUGUST
    as records #999–#1002. Returns (iso_date, source) with source 'document'
    or 'file-date'; the caller marks the result assumed so the review form's
    provenance chip and the year-sanity guard still make a person look.

    A fresh scan (file date >= the batch's received day — scanning always
    postdates receipt) returns None and keeps today's behavior, so nothing
    changes for the normal same-day flow. The document's printed date is
    trusted only when it's on or before the file's date (a bill can't be
    dated after the scan that captured it) AND within about a year of it —
    a misread YEAR (2025 for 2026) would otherwise sail through and file the
    record a year back with only the year-guard interstitial as the net."""
    if src_file_date is None:
        return None
    try:
        batch_d = date.fromisoformat((batch_received or "").strip())
    except (ValueError, TypeError):
        batch_d = date.today()
    if src_file_date >= batch_d:
        return None
    try:
        doc = date.fromisoformat((document_date or "").strip())
    except (ValueError, TypeError):
        doc = None
    if doc and doc <= src_file_date and (src_file_date - doc).days <= 366:
        return doc.isoformat(), "document"
    return src_file_date.isoformat(), "file-date"


def _fallback_received_date(pdf_path):
    """Confirm-time default when a queued item carries no received date at all
    (legacy queue rows from before ingest stamped one): the scan file's own
    date when it predates today, else today. Never confirm-day for a document
    whose file is visibly older — the June mtimes of the four 08. AUGUST
    misfiles were available evidence the whole time."""
    try:
        if pdf_path and os.path.exists(pdf_path):
            d = date.fromtimestamp(os.path.getmtime(pdf_path))
            if d < date.today():
                return d.isoformat()
    except OSError:
        pass
    return date.today().isoformat()

# ── Auth ─────────────────────────────────────────────────────────────────────

def _is_hashed(pw) -> bool:
    """True if a stored password is already a Werkzeug hash (not plaintext)."""
    return isinstance(pw, str) and pw.startswith(("pbkdf2:", "scrypt:"))

def migrate_passwords():
    """Hash any plaintext staff passwords in config.json. Runs at startup and
    is idempotent, so Karen can add a new staff member with a plain password in
    config.json and it gets hashed automatically on the next restart."""
    cfg = load_config()
    changed = False
    for s in cfg.get("staff", []):
        pw = s.get("password", "")
        # An empty password is never hashed: hashing "" would turn a staff
        # entry that FORGOT a password into one that accepts a blank password.
        # Left empty, the account simply can't log in until one is set.
        if pw and not _is_hashed(pw):
            s["password"] = generate_password_hash(pw, method="pbkdf2:sha256")
            changed = True
    if changed:
        save_config(cfg)
        print("[App] Hashed plaintext staff password(s) in config.json.")

def migrate_mail_accounts():
    """Convert the pre-multi-account gmail_user/gmail_app_password keys into
    the mail_accounts list (one IMAP entry keeping the old 30-day lookback —
    that account's history is already ingested, so re-checking is cheap).
    Runs at startup and is idempotent, same spirit as migrate_passwords();
    production's currently-working Gmail polling survives the restart."""
    cfg = load_config()
    if "mail_accounts" in cfg:
        return
    user = (cfg.get("gmail_user") or "").strip()
    pw = (cfg.get("gmail_app_password") or "").strip()
    cfg["mail_accounts"] = [email_poller.legacy_gmail_account(user, pw)] if user else []
    cfg.pop("gmail_user", None)
    cfg.pop("gmail_app_password", None)
    save_config(cfg)
    if user:
        print(f"[App] Migrated Gmail intake ({user}) into the mail_accounts list.")

def _check_staff_password(staff_entry, password) -> bool:
    """Verify a password against a staff entry, tolerating a not-yet-migrated
    plaintext value (e.g. added to config.json while the app was running).
    An entry with NO password can never log in — otherwise adding a staff
    member without one would create an account with a blank password."""
    stored = staff_entry.get("password", "")
    if not stored:
        return False
    if _is_hashed(stored):
        return check_password_hash(stored, password)
    return stored == password

def _current_user_is_admin() -> bool:
    """Admin = a staff entry in config.json with `"admin": true`. Read fresh so a
    flag change takes effect on next request, not next login."""
    user = session.get("user")
    if not user:
        return False
    entry = next((s for s in load_config().get("staff", []) if s.get("name") == user), None)
    return bool(entry and entry.get("admin"))

def admin_required(f):
    """Gate a route to admins only. Settings/system config is admin-only; everyday
    workflow (and changing your own password) stays open to all staff."""
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not _current_user_is_admin():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin access required"}), 403
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return _wrap

# Mail-flow watchdog result, cached briefly so page renders never pay for the
# check twice in a row (it's cheap, but it runs in a context processor).
_mail_alert_cache = {"at": 0.0, "problems": []}

def _mail_alert_problems():
    import time as _t
    if _t.time() - _mail_alert_cache["at"] > 60:
        try:
            _mail_alert_cache["problems"] = mail_health.check(
                load_config(), email_poller.get_poll_status(), _APP_STARTED)
        except Exception as e:
            print(f"[MailHealth] {type(e).__name__}: {e}")
            _mail_alert_cache["problems"] = []
        _mail_alert_cache["at"] = _t.time()
    return _mail_alert_cache["problems"]

@app.context_processor
def _inject_admin_flag():
    admin = _current_user_is_admin()
    user = session.get("user", "")
    # Open-issue indicator (report #68): ids of not-yet-verified reports filed
    # about the page being rendered (path match — host/query ignored). Admins
    # only, so staff screens stay exactly as quiet as before.
    page_issue_ids = []
    if admin:
        try:
            page_issue_ids = get_open_report_paths().get(
                report_url_path(request.path), [])
        except Exception:
            page_issue_ids = []
    return {"is_admin": admin,
            "open_report_count": get_open_report_count() if admin else 0,
            "page_issue_ids": page_issue_ids,
            # Per-page browser titles (report #80). A route that passes its own
            # page_title= to render_template overrides this; see page_titles.py.
            "page_title": page_titles.title_for(request.endpoint),
            # Mail-flow watchdog (Joshua 2026-08-10): any problem here renders
            # as the red banner at the top of every page (_chrome_shared.html).
            "mail_alert": _mail_alert_problems() if user else [],
            # Starvation watchdog (2026-08-11): a stall is reported after the
            # fact too — during a total stall no page can render, so the
            # after-the-fact notice is the one anybody actually sees.
            "stall_alert": request_health.problems() if user else [],
            # "Passed to you" nav badge — records teammates handed this person.
            "my_pass_count": open_pass_count(user) if user else 0}

@app.template_global("email_mailto")
def email_mailto(e, mode="reply"):
    """A mailto: link that opens the staff member's OWN mail program pre-filled
    for replying to / forwarding a source email — the app itself never sends
    mail (no SMTP path, no stored send credentials). `e` is any dict with the
    email_* fields: a logged record or a queue item. Body carries a quoted
    copy of the original, trimmed so the URL stays within client limits."""
    from urllib.parse import quote
    subj = (e.get("email_subject") or "").strip()
    prefix = "Fwd: " if mode == "forward" else "Re: "
    if subj.lower().startswith(("re: ", "fwd: ", "fw: ")):
        new_subj = subj if mode == "reply" else "Fwd: " + subj
    else:
        new_subj = prefix + (subj or "(no subject)")
    quoted = ("\n\n---------- Original message ----------\n"
              f"From: {e.get('email_from') or ''}\n"
              f"To: {e.get('email_to') or ''}\n"
              f"Date: {e.get('email_date') or ''}\n"
              f"Subject: {subj}\n\n"
              + (e.get("email_body") or "")[:1500])
    # Reply goes to the bare address — mail clients choke on the display-name
    # form ("City Water <billing@...>") inside a mailto target.
    from email.utils import parseaddr
    to = parseaddr(e.get("email_from") or "")[1] if mode == "reply" else ""
    return (f"mailto:{quote(to, safe='@.')}"
            f"?subject={quote(new_subj)}&body={quote(quoted)}")

# Paths that don't require a logged-in user. NOTE: /scan/preview/ was exempt
# here for a while — that let anyone on the network download queued financial
# documents by guessing small integer ids, without logging in. Only truly
# public assets belong in these lists.
_EXEMPT_PATHS = ("/login", "/logout", "/api/connector/heartbeat",
                 # Connector command channel (reports #34/#22): token-authed
                 # like the heartbeat — see desk_commands.py.
                 "/api/connector/commands", "/api/connector/command_result",
                 # Night-shift dev API: token-authed (night_shift_token), no
                 # login session by design — see routes_reports.py + DESIGN.md §8.
                 "/api/dev/report_note", "/api/dev/report_status",
                 "/api/dev/report_new")
_EXEMPT_PREFIXES = ("/static",)
# Background polls that must NOT count as user activity — otherwise an idle tab
# (which pings pending-count every 10s) would keep the session alive forever.
_POLL_PATHS = ("/api/pending_count", "/api/scan/status", "/api/scan/arrivals")
# State-changing endpoints that carry their own authentication instead of a
# session CSRF token. The scan connector runs on workstations with no login
# session; its heartbeat is guarded by connector_token (see scanflow.py).
_CSRF_EXEMPT_PATHS = ("/api/connector/heartbeat",
                      "/api/connector/commands", "/api/connector/command_result",
                      "/api/dev/report_note", "/api/dev/report_status",
                      "/api/dev/report_new")

def _session_timeout_minutes():
    """Idle-timeout window in minutes (configurable; 0 disables). Default 30."""
    try:
        return int(load_config().get("session_timeout_minutes", 30))
    except (ValueError, TypeError, OSError):
        return 30

@app.before_request
def require_login():
    path = request.path
    if path.startswith(_EXEMPT_PREFIXES):
        return
    # Mint the session's CSRF token for every page (including the login page,
    # whose POST is protected too). Static files skip this to avoid cookie churn.
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if path in _EXEMPT_PATHS:
        return
    if not session.get("user"):
        # APIs answer with JSON 401 so client-side fetches fail cleanly
        # instead of receiving a login-page redirect.
        if path.startswith("/api/"):
            return jsonify({"error": "login required"}), 401
        return redirect(url_for("login"))
    # Logged in: enforce an idle timeout so a closed-browser / walked-away session
    # can't be resumed (browsers restore session cookies, so we can't rely on the
    # cookie dying — the server checks elapsed time itself).
    timeout_min = _session_timeout_minutes()
    if timeout_min > 0:
        now = time.time()
        last = session.get("last_active")
        if last is not None and (now - last) > timeout_min * 60:
            session.clear()
            if path.startswith("/api/"):
                return jsonify({"error": "session expired"}), 401
            return redirect(url_for("login", timed_out=1))
        # Only real activity (navigation/actions) resets the idle clock, not polls.
        if path not in _POLL_PATHS:
            session["last_active"] = now

@app.before_request
def csrf_protect():
    """Reject state-changing requests that don't carry the session's CSRF token,
    so a malicious page in a logged-in browser can't fire actions at the app
    (delete records, mark bills paid, clear the queue, ...). base.html injects
    the token into every POST form as a hidden csrf_token field and into every
    fetch() call as an X-CSRF-Token header — routes and templates don't need to
    handle it individually. Registered after require_login, so unauthenticated
    requests still get the normal login response first."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if request.path in _CSRF_EXEMPT_PATHS:   # token-authed, no session (scanflow)
        return
    token = session.get("csrf") or ""
    sent = (request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token") or "")
    if token and sent and hmac.compare_digest(token, sent):
        return
    if request.path.startswith("/api/") or request.is_json or request.headers.get("X-CSRF-Token"):
        return jsonify({"error": "Security check failed — refresh the page and try again."}), 403
    return ('<div style="font-family:Arial;padding:40px;max-width:520px;margin:0 auto;">'
            "<h2>Security check failed</h2>"
            "<p>This can happen when a page was left open across a sign-in or an "
            "app update. Nothing was changed.</p>"
            '<p><a href="javascript:history.back()">&larr; Go back</a>, refresh the '
            "page, and try again.</p></div>"), 403

# Brake on password guessing: after _LOGIN_MAX_FAILS failures from one address
# within the window, further attempts are refused until the window rolls over.
_login_failures = {}   # ip -> [failure timestamps]
_login_fail_lock = threading.Lock()
_LOGIN_MAX_FAILS = 8
_LOGIN_WINDOW_SECS = 600

def _login_throttled(ip) -> bool:
    now = time.time()
    with _login_fail_lock:
        recent = [t for t in _login_failures.get(ip, ()) if now - t < _LOGIN_WINDOW_SECS]
        if recent:
            _login_failures[ip] = recent
        else:
            _login_failures.pop(ip, None)
        return len(recent) >= _LOGIN_MAX_FAILS

def _login_failed(ip):
    with _login_fail_lock:
        _login_failures.setdefault(ip, []).append(time.time())

@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_config()
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _login_throttled(ip):
            error = "Too many failed attempts from this computer. Wait 10 minutes and try again."
        else:
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "")
            staff_entry = next((s for s in cfg.get("staff", []) if s["name"] == name), None)
            if staff_entry and _check_staff_password(staff_entry, password):
                with _login_fail_lock:
                    _login_failures.pop(ip, None)
                session.clear()
                session["user"] = name
                session["csrf"] = secrets.token_hex(16)   # fresh token for the new session
                session["last_active"] = time.time()      # start the idle clock
                return redirect(url_for("dashboard"))
            _login_failed(ip)
            error = "Incorrect name or password."
    timed_out = bool(request.args.get("timed_out"))
    return render_template("login.html", cfg=cfg, error=error, timed_out=timed_out)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Routes ────────────────────────────────────────────────────────────────────

def _doc_label(entry: dict) -> str:
    """Best human-readable label for a document row. The vendor when the AI
    found one; otherwise fall back to the source filename (minus split-part
    suffixes and duplicate counters), then to the start of the notes. Blank
    lease forms and templates have no sender, and showing "Unlabeled" on the
    dashboard told Karen nothing about what needed her attention."""
    vendor = (entry.get("vendor") or "").strip()
    if vendor:
        return vendor
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"_part\d+$", "", stem).strip()
    stem = re.sub(r"\s*\(\d+\)$", "", stem).strip()
    # A stem like "Legal" (the doc type plus a download counter) says nothing.
    if len(stem) > 3 and stem.lower() != (entry.get("doc_type") or "").lower():
        return stem
    notes = (entry.get("notes") or "").strip()
    if notes:
        return notes[:70] + "…" if len(notes) > 70 else notes
    return "Untitled document"

@app.route("/")
def dashboard():
    # Karen's landing is /home — one question, one thing to get done
    # (GUI_REDESIGN_PROMPT.md, 2026-07-29). Only her BARE "/" bounces there:
    # any query string still renders the full dashboard (the period pickers'
    # ?month/?range, and ?full=1 from home's "Show me everything" button),
    # and other staff never bounce at all.
    if session.get("user") == "Karen" and not request.args:
        return redirect(url_for("home"))
    cfg = load_config()
    pending = _queue_items()
    pending_count = len(pending)
    period = _resolve_period(request.args.get("month"), request.args.get("range"))
    stats = get_dashboard_stats(period["date_from"], period["date_to"])
    activity = get_activity_feed(15)
    today_iso = date.today().isoformat()
    overdue = [b for b in get_bills_by_due_date() if (b.get("due_date") or "") and b["due_date"] < today_iso]
    overdue_count = len(overdue)
    overdue_total = sum(b.get("amount") or 0 for b in overdue)
    urgent_pending = sum(1 for _, d in pending if _scan_is_overdue(d))
    try:
        expected_waiting = sum(1 for s in _expected_statuses() if s["state"] == "waiting")
    except Exception:
        expected_waiting = 0
    important = get_important_correspondence(limit=5)
    for it in important:
        it["label"] = _doc_label(it)
    # "From email" box — Karen's worry: things arriving by EMAIL (unlike paper
    # mail, she never touches the envelope) slipping past unseen. Email-only
    # attention items from BOTH stages: still-in-review-queue and
    # logged-but-not-yet-acknowledged. Routine email bills stay out — this is
    # the "really need to look at" lens, not another list of everything.
    email_eyes = []
    for sid, d in pending:
        if (d.get("mail_type") or "") != "Email":
            continue
        if not (d.get("important") or d.get("notice_summary")
                or (d.get("doc_type") or "") in ("Legal", "Correspondence")):
            continue
        email_eyes.append({
            "kind": "queue", "sid": sid, "doc_type": d.get("doc_type"),
            "who": d.get("vendor") or d.get("email_from") or "Unknown sender",
            "why": (d.get("important_reason") or d.get("notice_summary")
                    or d.get("email_subject") or ""),
            "important": bool(d.get("important")),
            "notice": bool(d.get("notice_summary")),
            "received": (d.get("received_date") or "")[:10],
        })
    # Deliberately NOT deduped against the generic important box: the whole
    # point is that Karen can trust this one lens for "what came by email" —
    # an eviction notice arriving by email should show here even though the
    # box above lists it too.
    for it in get_important_correspondence(limit=6, mail_type="Email"):
        email_eyes.append({
            "kind": "logged", "id": it["id"], "doc_type": it.get("doc_type"),
            "who": _doc_label(it) or it.get("vendor") or "Untitled document",
            "why": (it.get("important_reason") or it.get("notice_summary")
                    or it.get("email_subject") or ""),
            "important": bool(it.get("important")),
            "notice": bool(it.get("notice_summary")),
            "received": (it.get("date_received") or "")[:10],
        })
    email_eyes.sort(key=lambda x: x["received"] or "", reverse=True)
    email_eyes = email_eyes[:6]
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    email_junk_week = count_filtered_since(week_ago)
    # render_skinned: same data, two looks — classic dashboard.html or the
    # new-look new/dashboard.html, per the person's toggle (routes_newlook.py).
    # next_bill feeds the new look's one filled button ("Pay the next bill");
    # classic ignores it.
    return routes_newlook.render_skinned("dashboard.html", stats=stats, cfg=cfg,
                           email_eyes=email_eyes, email_junk_week=email_junk_week,
                           pending_count=pending_count, activity=activity,
                           period=period, overdue_count=overdue_count,
                           overdue_total=overdue_total, urgent_pending=urgent_pending,
                           expected_waiting=expected_waiting, important=important,
                           my_passes=get_passes_for_user(session.get("user", "")),
                           next_bill=get_oldest_unpaid_bill(),
                           today=date.today().strftime("%B %d, %Y"))

@app.route("/activity")
def activity_feed():
    return render_template("activity.html", activity=get_activity_feed(200))

# ── Recurring-bills radar ──────────────────────────────────────────────────────
# A curated checklist of bills Karen EXPECTS each period (month/quarter/year),
# matched against what actually arrived — so "the water bill never came" gets
# noticed before it's overdue. Deliberately not machine-learned: suggestions are
# mined from history, but only what Karen adds is ever tracked.

def _vendor_match(a: str, b: str) -> bool:
    """Tolerant vendor comparison: exact or containment, case-insensitive
    ('SoCal Gas' matches 'SoCal Gas - Southern California Gas Company'), plus
    substantive token overlap so an archive spelling still counts — 'SO.CALIF.
    EDISON' must match 'Southern California Edison Company' now that radar rows
    can be adopted from the paid-archive miner. Substantive means two shared
    significant tokens (abbreviation-tolerant: CALIF counts as CALIFORNIA) or
    every token of one side; a lone shared 'ELECTRIC' between two different
    electricians is NOT a match (the paid-backfill veto's lesson)."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not (a and b):
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = nameparse.vtokens(a), nameparse.vtokens(b)
    if not (ta and tb):
        return False
    shared, claimed = 0, set()
    for x in ta:
        for y in tb:
            if y in claimed:
                continue
            if x == y or (len(x) >= 4 and len(y) >= 4
                          and (x.startswith(y) or y.startswith(x))):
                shared += 1
                claimed.add(y)
                break
    return shared >= 2 or shared in (len(ta), len(tb))

def _period_start(cadence: str) -> str:
    t = date.today()
    if cadence == "yearly":
        return date(t.year, 1, 1).isoformat()
    if cadence == "quarterly":
        return date(t.year, ((t.month - 1) // 3) * 3 + 1, 1).isoformat()
    return date(t.year, t.month, 1).isoformat()   # monthly (default)

def _expected_statuses() -> list:
    """Each active expected bill with its state for the current period:
    'arrived' (a Bill from that vendor was logged), 'queue' (something from that
    vendor is sitting in the review queue), or 'waiting' (not seen yet)."""
    expected = get_expected_bills(active_only=True)
    if not expected:
        return []
    earliest = min(_period_start(e.get("cadence") or "monthly") for e in expected)
    seen = bills_seen_since(earliest)
    queue = _queue_items()
    out = []
    for e in expected:
        pstart = _period_start(e.get("cadence") or "monthly")
        acct = (e.get("account_number") or "").strip().lower()
        arrived = None
        for s in seen:
            if (s.get("last_seen") or "") < pstart:
                continue
            if not _vendor_match(e["vendor"], s.get("vendor")):
                continue
            if acct and acct != (s.get("account_number") or "").strip().lower():
                continue
            arrived = s
            break
        state = "arrived" if arrived else None
        if not state:
            for _, d in queue:
                if _vendor_match(e["vendor"], d.get("vendor")):
                    state = "queue"
                    break
        out.append({**e, "state": state or "waiting", "period_start": pstart,
                    "last_seen": (arrived or {}).get("last_seen")})
    order = {"waiting": 0, "queue": 1, "arrived": 2}
    out.sort(key=lambda x: (order[x["state"]], (x.get("vendor") or "").lower()))
    return out

def _ops_index_path():
    """The read-only ops index (13 years of filename-mined history), if present."""
    cfg = load_config()
    for p in (cfg.get("ops_index_db", ""),
              r"F:\File-Cabinet\_OPS INDEX\ops_index.sqlite",
              r"T:\_OPS INDEX\ops_index.sqlite"):
        if p and os.path.exists(p):
            return p
    return None

_radar_sugg_cache = {"t": 0.0, "rows": None}

def _recurring_suggestions(db_path=None):
    """Vendors that appear in ≥24 distinct months of the last ~36 in the ops
    index — strong monthly-recurrence candidates to seed the radar. Cached 15
    minutes; empty list when the index isn't reachable."""
    import sqlite3 as _sq
    if db_path is None and _radar_sugg_cache["rows"] is not None \
            and time.time() - _radar_sugg_cache["t"] < 900:
        return _radar_sugg_cache["rows"]
    path = db_path or _ops_index_path()
    if not (path and os.path.exists(path)):
        return []
    try:
        con = _sq.connect(f"file:{path}?mode=ro", uri=True)
        cutoff = (date.today() - timedelta(days=36 * 30)).isoformat()[:8] + "01"
        rows = con.execute(
            """SELECT payee,
                      COUNT(DISTINCT substr(doc_date,1,7)) AS months,
                      ROUND(AVG(amount),2) AS avg_amount,
                      MAX(entity) AS entity
               FROM docs
               WHERE payee IS NOT NULL AND date_ok=1 AND doc_date >= ?
               GROUP BY UPPER(payee)
               HAVING months >= 24
               ORDER BY months DESC, UPPER(payee) LIMIT 40""", (cutoff,)).fetchall()
        con.close()
        out = [{"vendor": r[0], "months": r[1], "avg_amount": r[2],
                "entity": r[3] or ""} for r in rows]
        if db_path is None:
            _radar_sugg_cache["rows"] = out
            _radar_sugg_cache["t"] = time.time()
        return out
    except Exception as e:
        print(f"[Radar] ops-index suggestions unavailable: {e}")
        return []

def _vendor_covered(vendor: str, existing: list) -> bool:
    """Is this suggested vendor already represented by any of the given names?
    Uses _vendor_match (not a flat key): 'SO.CALIF. EDISON', the ops-index
    spelling, and a hand-typed 'So Cal Edison' must all count as one vendor,
    and abbreviation drift (CALIF/CALIFORNIA) defeats set equality."""
    return any(_vendor_match(vendor, e) for e in existing)


# The radar's period model knows three cadences; the miner is finer-grained.
# Conservative mapping: an every-2-months beat checked quarterly (and a
# half-year beat checked yearly) always expects at least one arrival per
# period, so the radar never cries wolf on an off cycle.
_RHYTHM_TO_CADENCE = {"monthly": "monthly", "every 2 months": "quarterly",
                      "quarterly": "quarterly", "twice a year": "yearly",
                      "annual": "yearly"}

_rhythm_sugg_cache = {"t": 0.0, "rows": None}


def _rhythm_suggestions():
    """Radar suggestions mined from the paid-archive backfill datasets
    (payment_rules over _OPS INDEX\\PAID_BACKFILL_<year>.csv): vendors with a
    measured payment rhythm — cadence, typical amount, the paying entity, and
    when the next payment looks due. Richer than the ops-index 24-of-36-months
    rule: it knows quarterly and annual beats and sees vendors the 13-year
    index is too strict for. Cached 15 minutes; empty when no datasets exist.
    Vendor names are canonicalized to the intake log's spelling when one
    matches, so adopted rows use names the radar already recognizes."""
    if _rhythm_sugg_cache["rows"] is not None \
            and time.time() - _rhythm_sugg_cache["t"] < 900:
        return _rhythm_sugg_cache["rows"]
    rows = []
    try:
        import payment_rules
        ops_dir = (load_config().get("ops_index_dir")
                   or r"F:\File-Cabinet\_OPS INDEX")
        years = sorted({int(m.group(1)) for n in os.listdir(ops_dir)
                        if (m := re.match(r"PAID_BACKFILL_(\d{4})\.csv$", n))})
        if years:
            directory = [d["vendor"] for d in get_vendor_directory()]
            res = payment_rules.analyze(ops_dir, years)
            for v in res["cadences"]:
                ent = (v["entities"].most_common(1)[0][0]
                       if v["entities"] else "")
                name = next((dv for dv in directory
                             if _vendor_match(v["vendor"], dv)), v["vendor"])
                rows.append({
                    "vendor": name,
                    "n": v["n"], "rhythm": v["cadence"],
                    "cadence": _RHYTHM_TO_CADENCE.get(v["cadence"], "monthly"),
                    "typical": v["typical"],
                    "entity": "" if ent == "?" else ent,
                    "last": v["last"].isoformat(),
                    "next": v["next"].isoformat(), "overdue": v["overdue"],
                })
    except Exception as e:
        print(f"[Radar] rhythm suggestions unavailable: {e}")
    _rhythm_sugg_cache["rows"] = rows
    _rhythm_sugg_cache["t"] = time.time()
    return rows


@app.route("/expected")
def expected_bills_page():
    statuses = _expected_statuses()
    all_expected = get_expected_bills()
    paused = [e for e in all_expected if not e.get("active")]
    tracked = [e.get("vendor") or "" for e in all_expected]
    rhythms = [s for s in _rhythm_suggestions()
               if not _vendor_covered(s["vendor"], tracked)]
    covered = tracked + [s["vendor"] for s in rhythms]
    suggestions = [s for s in _recurring_suggestions()
                   if not _vendor_covered(s["vendor"], covered)]
    return render_template("expected.html", statuses=statuses, paused=paused,
                           suggestions=suggestions, rhythms=rhythms,
                           cfg=load_config(),
                           waiting=sum(1 for s in statuses if s["state"] == "waiting"),
                           month_label=date.today().strftime("%B %Y"),
                           msg=request.args.get("msg"))


@app.route("/expected/adopt_rhythms", methods=["POST"])
@admin_required
def expected_adopt_rhythms():
    """Adopt every currently-suggested mined rhythm onto the radar in one go
    (each row stays individually pausable/deletable). The table's contract
    holds: seeded from suggestions by a person's click, never auto-populated."""
    tracked = [e.get("vendor") or "" for e in get_expected_bills()]
    added = 0
    for s in _rhythm_suggestions():
        if _vendor_covered(s["vendor"], tracked):
            continue
        add_expected_bill(s["vendor"], "", s["entity"], "", s["cadence"],
                          s["typical"], source="suggested",
                          notes=f"payment-rules miner: {s['n']} payments, "
                                f"{s['rhythm']}, last {s['last']}")
        tracked.append(s["vendor"])
        added += 1
    return redirect(url_for("expected_bills_page",
                            msg=f"Adopted {added} mined rhythm"
                                f"{'s' if added != 1 else ''} onto the radar."))

@app.route("/expected/add", methods=["POST"])
@admin_required
def expected_add():
    f = request.form
    amt = _parse_money(f.get("typical_amount"))
    vendor = (f.get("vendor") or "").strip()
    if not vendor:
        return redirect(url_for("expected_bills_page", msg="A vendor name is required."))
    cadence = f.get("cadence") or "monthly"
    if cadence not in ("monthly", "quarterly", "yearly"):
        cadence = "monthly"
    add_expected_bill(vendor, f.get("account_number", "").strip(),
                      f.get("entity_code", "").strip(), f.get("property_code", "").strip(),
                      cadence, amt,
                      source=f.get("source") or "manual")
    return redirect(url_for("expected_bills_page", msg=f"Now expecting {vendor}."))

@app.route("/expected/<int:eb_id>/toggle", methods=["POST"])
@admin_required
def expected_toggle(eb_id):
    active = request.form.get("active") == "1"
    set_expected_bill_active(eb_id, active)
    return redirect(url_for("expected_bills_page"))

@app.route("/expected/<int:eb_id>/delete", methods=["POST"])
@admin_required
def expected_delete(eb_id):
    delete_expected_bill(eb_id)
    return redirect(url_for("expected_bills_page"))

_CATCHUP_SECTION_ORDER = ("overdue", "today", "week", "later", "nodate")

def _catchup_buckets():
    """Unpaid bills grouped by urgency, in the order the Catch Up page displays
    them. Shared by the page and by _nav_context, so a record's Prev/Next walk
    the list exactly as it appears on screen."""
    today = date.today()
    week_out = (today + timedelta(days=7)).isoformat()
    today_iso = today.isoformat()
    buckets = {k: [] for k in _CATCHUP_SECTION_ORDER}
    for b in get_bills_by_due_date():
        dd = (b.get("due_date") or "").strip()
        try:
            d = date.fromisoformat(dd) if dd else None
        except ValueError:
            d = None
        if d is None:
            b["days"] = None
            buckets["nodate"].append(b)
            continue
        b["days"] = (d - today).days   # negative = overdue
        if dd < today_iso:
            buckets["overdue"].append(b)
        elif dd == today_iso:
            buckets["today"].append(b)
        elif dd <= week_out:
            buckets["week"].append(b)
        else:
            buckets["later"].append(b)
    return buckets

def _catchup_received_rows():
    """Unpaid bills in the order the mail arrived, oldest first — Karen pays
    her stack in received order, so this is Catch Up's default. Rows keep the
    same 'days'-to-due field the bucketed view uses (late-fee risk stays
    visible while she works the stack) plus how long each bill has sat here."""
    today = date.today()
    rows = get_bills_by_due_date()
    for b in rows:
        dd = (b.get("due_date") or "").strip()
        try:
            b["days"] = (date.fromisoformat(dd) - today).days if dd else None
        except ValueError:
            b["days"] = None
        rd = (b.get("date_received") or "").strip()
        try:
            b["waiting_days"] = (today - date.fromisoformat(rd)).days if rd else None
        except ValueError:
            b["waiting_days"] = None
    # Bills with no received date sink to the bottom rather than jumping the stack.
    rows.sort(key=lambda b: ((b.get("date_received") or "").strip() or "9999-99-99", b["id"]))
    return rows

@app.route("/catchup")
def catchup():
    """Payment worklist. The default order matches how Karen actually pays:
    the order the mail was received (oldest first). ?order=due keeps the
    urgency buckets — the dashboard's overdue alert links straight there."""
    order = "due" if request.args.get("order") == "due" else "received"
    if order == "received":
        rows = _catchup_received_rows()
        return render_template("catchup.html", order=order, rows=rows, buckets=None,
                               total_amount=sum(b.get("amount") or 0 for b in rows),
                               cfg=load_config(), today=date.today().isoformat())
    buckets = _catchup_buckets()
    totals = {k: sum(b.get("amount") or 0 for b in v) for k, v in buckets.items()}
    return render_template("catchup.html", order=order, buckets=buckets, rows=None,
                           totals=totals,
                           cfg=load_config(), today=date.today().isoformat())

@app.route("/owed/<group>")
def owed_summary(group):
    """Old per-grouping "Owed by ..." pages folded into /bills as tabs (same
    data, same routes underneath — get_entity_summary/get_property_summary are
    now called from bills() instead). Kept as a redirect so any existing
    bookmark or link still lands somewhere correct."""
    if group in ("vendor", "entity", "property"):
        return redirect(url_for("bills", view=group))
    return redirect(url_for("dashboard"))

# ── Paper views ────────────────────────────────────────────────────────────────
# The "physical office" concept series made real: /calendar (bills-due wall
# calendar), /board (corkboard of flagged correspondence), /tray (review-queue
# depth as an edge-on inbox tray). Alternate presentations over the same
# queries the dashboard and Catch Up already use — read-only except the
# board's take-down. The calendar deliberately has no pay action: Karen pays
# in received order from the stack; the calendar is for watching due dates.

# The seven paper views (/desk /cabinet /rolodex /intray /calendar /board
# /tray) live in routes_paper.py (2026-07-29 modular split, GH #6).

# ── Record navigation context ──────────────────────────────────────────────────
# Lets a record view (and its Edit form) remember which list it was opened from, so
# it can offer Prev/Next within that list and a Back link, and so saving an edit
# returns to the RECORD (not a random list — the old "save then hunt for it" pain).
# A list opts in by linking with ?ctx=<source>&<its params>. The Log is wired now;
# other lists can pass the same ctx to get the same behavior.
_LOG_FILTER_KEYS = ("entity_code","property_code","doc_type","status",
                    "date_from","date_to","search","important")
NAV_KEYS = ("ctx", "vendor", "order") + _LOG_FILTER_KEYS

def _nav_qs(args):
    """The active nav-context as a querystring to carry through view→edit→view."""
    from urllib.parse import urlencode
    d = {k: args.get(k) for k in NAV_KEYS if args.get(k)}
    return ("?" + urlencode(d)) if d else ""

def _log_status_filters(filters):
    """The Log's status rule, shared by the page and its prev/next context so
    the sibling list always matches what the list showed: no status picked →
    hide Filed (the archive layer lives in search and the Files view, not the
    daily chronology); the explicit "all" choice → everything incl. Filed."""
    if filters.get("status") == "all":
        filters["status"] = ""
    elif not filters.get("status"):
        filters["status_not"] = "Filed"
    return filters

def _nav_context(args):
    """Resolve (back_url, back_label, ordered_ids) for the record view's Back +
    Prev/Next from the request args. ordered_ids is the sibling list in display
    order so the view can find this record's neighbours. Defaults to the Log."""
    from urllib.parse import urlencode, quote
    ctx = args.get("ctx")
    if ctx == "log":
        filters = {k: args.get(k, "") for k in _LOG_FILTER_KEYS}
        qs = urlencode({k: v for k, v in filters.items() if v})
        back_url = "/log" + (("?" + qs) if qs else "")
        ids = [e["id"] for e in get_all_entries(_log_status_filters(filters))]
        return back_url, "Log", ids
    if ctx == "vendor" and args.get("vendor"):
        vendor = args.get("vendor")
        ids = [e["id"] for e in get_bills_for_vendor(vendor)]
        return "/bills/vendor/" + quote(vendor), vendor, ids
    if ctx == "bills":
        ids = [e["id"] for e in get_bills_by_due_date()]
        return "/bills", "Bills", ids
    if ctx == "catchup":
        if args.get("order") == "due":
            buckets = _catchup_buckets()
            ids = [b["id"] for sec in _CATCHUP_SECTION_ORDER for b in buckets[sec]]
            return "/catchup?order=due", "Catch Up", ids
        ids = [b["id"] for b in _catchup_received_rows()]
        return "/catchup", "Catch Up", ids
    return "/log", "Log", []

@app.route("/log")
def log():
    cfg = load_config()
    filters = {
        "entity_code":    request.args.get("entity_code",""),
        "property_code":  request.args.get("property_code",""),
        "doc_type":       request.args.get("doc_type",""),
        "status":         request.args.get("status",""),
        "date_from":      request.args.get("date_from",""),
        "date_to":        request.args.get("date_to",""),
        "search":         request.args.get("search",""),
        # Link-driven (the auto-file spot-check feed / the dashboard's flagged
        # list), not visible controls.
        "processed_by":   request.args.get("processed_by",""),
        "important":      request.args.get("important",""),
    }
    status_sel = filters["status"]
    entries = get_all_entries(_log_status_filters(filters))
    # The template's dropdown and the row links carry what the user PICKED
    # ("all" stays selected; the internal status_not never leaks into URLs).
    filters.pop("status_not", None)
    filters["status"] = status_sel
    # One context querystring for every row link, so clicking a record carries the
    # current filter set into its view (Back returns here; Prev/Next stay in-list).
    from urllib.parse import urlencode
    nav_qs = "?" + urlencode({"ctx": "log", **{k: v for k, v in filters.items() if v}})
    return render_template("log.html", entries=entries, filters=filters, cfg=cfg,
                           nav_qs=nav_qs)

# ── Duplicate triage (bulk cleanup of a duplicate-heavy queue) ─────────────────
# The triage ROUTES live in routes_review.py; the grouping below stays here
# because the in-tray paper view reads the same groups through _M.

def _bill_identity_keys(d: dict) -> set:
    """The fuzzy 'same bill' identities of a queued item, mirroring the match
    keys of find_duplicates(): vendor+invoice number, and vendor+amount+due
    date. Empty set when the item lacks enough extracted fields to say."""
    keys = set()
    v = (d.get("vendor") or "").strip().lower()
    if not v:
        return keys
    inv = (d.get("invoice_number") or "").strip().lower()
    if inv:
        keys.add(("inv", v, inv))
    if d.get("amount") is not None and d.get("due_date"):
        keys.add(("vad", v, d["amount"], d["due_date"]))
    return keys

def _triage_groups():
    """Group queued items for duplicate triage.
    certain:    byte-identical to an already-logged record — attaching is safe.
    queue_dups: byte-identical to ANOTHER queued item (first one is kept).
    probable:   no byte match, but find_duplicates() flags a logged record.
    near_queue: nothing logged matches, but the same bill identity (vendor +
                invoice #, or vendor+amount+due date) sits on more than one
                queued item — the same bill waiting twice (a re-sent email, an
                email plus its paper copy). Earliest-received copy is kept.
    Items that predate hashing get hashed here (and persisted), so the first
    triage visit after an upgrade does the one-time hashing work."""
    certain, queue_dups, probable = [], [], []
    first_with_hash = {}
    near_groups = []   # [{'keys': set of identity keys, 'items': [{'sid','data'}]}]
    for sid, d in _queue_items():
        p = d.get("pdf_path") or ""
        h = d.get("file_hash")
        if not h and p and os.path.exists(p):
            try:
                h = _sha256_file(p)
            except OSError:
                h = ""
            d["file_hash"] = h
            try:
                save_pending_scan(sid, json.dumps(d))
            except Exception:
                pass
        if h:
            owners = get_entries_by_hash(h)
            if owners:
                certain.append({"sid": sid, "data": d, "entry": owners[0]})
                continue
            kept = first_with_hash.get(h)
            if kept is not None:
                queue_dups.append({"sid": sid, "data": d, "kept_sid": kept})
                continue
            first_with_hash[h] = sid
        matches = find_duplicates(d.get("vendor"), d.get("amount"), d.get("due_date"),
                                  d.get("invoice_number"), d.get("pdf_path"),
                                  d.get("received_date"))
        if matches:
            cmps = {m["id"]: _dup_comparison(d, m) for m in matches[:3]}
            best = sorted(matches[:3],
                          key=lambda m: -cmps[m["id"]]["verdict"]["score"])
            probable.append({"sid": sid, "data": d, "matches": best,
                             "cmp": cmps[best[0]["id"]]})
            continue
        # No logged match — does another QUEUED item share this bill's identity?
        # Items sharing any key merge into one group; queue order (ascending
        # scan id = arrival order) makes the first member the earliest received.
        keys = _bill_identity_keys(d)
        if keys:
            g = next((g for g in near_groups if g["keys"] & keys), None)
            if g is None:
                g = {"keys": set(), "items": []}
                near_groups.append(g)
            g["keys"] |= keys
            g["items"].append({"sid": sid, "data": d})
    near_queue = []
    for g in near_groups:
        if len(g["items"]) < 2:
            continue
        kept = g["items"][0]
        extras = [dict(it, cmp=_dup_comparison(it["data"], kept["data"]))
                  for it in g["items"][1:]]
        near_queue.append({"kept": kept, "extras": extras})
    # Strongest matches first, so the top of the list is the safest to act on.
    probable.sort(key=lambda it: -it["cmp"]["verdict"]["score"])
    return certain, queue_dups, probable, near_queue

# ── Legacy duplicate merge (double-logged records from before dedup) ──────────
# Before 2026-07-07 there was no ingest-time deduplication, so the same
# document could be confirmed twice and live on as two records — inflating the
# owed totals. These tools fold each byte-identical group into ONE record.
# Documents are never touched on disk; the absorbed record's file is kept
# listed on the survivor as a copy, and everything is activity-logged.

_MERGE_FILL_FIELDS = ("vendor", "amount", "due_date", "invoice_number",
                      "account_number", "entity_code", "entity_name",
                      "property_code", "property_address", "bill_type_code",
                      "scanned_date", "batch_number", "notes")


def _payment_sig(r):
    """What payment (if any) a record carries, normalized for comparison."""
    if not (r.get("paid_date") or r.get("check_number") or r.get("paid_amount")):
        return None
    return (r.get("paid_date") or "", round(r.get("paid_amount") or 0, 2),
            (r.get("check_number") or "").strip(), r.get("payment_method") or "")


def _merge_conflict(group):
    """A group is unsafe to merge automatically only when its members carry
    DIFFERENT payments — that smells like the same bill paid twice, and a
    person must look. Identical signatures (one payment recorded on both
    copies) merge fine."""
    sigs = {s for s in (_payment_sig(r) for r in group) if s is not None}
    return len(sigs) > 1


def _pick_keeper(group):
    """The record that survives a merge: a paid/filed record beats a pending
    one, a check number beats none, then the OLDEST id (first logged)."""
    return max(group, key=lambda r: (r.get("status") in ("Paid", "Filed"),
                                     bool((r.get("check_number") or "").strip()),
                                     -r["id"]))


def _merge_dupe_group(group, by="", keeper=None):
    """Fold one group of copies of the same bill into its keeper. Returns the
    keeper id. Caller must have screened with _merge_conflict first. Groups are
    normally byte-identical (the Legacy Duplicates page); the server CLI's
    merge-record passes an explicit keeper for human-judged twins whose bytes
    differ (insurer vs agency prints of one invoice)."""
    keeper = keeper or _pick_keeper(group)
    dupes = [r for r in group if r["id"] != keeper["id"]]
    updates = {}
    # Fill the keeper's blanks from the absorbed copies (never overwrite).
    for f in _MERGE_FILL_FIELDS:
        if keeper.get(f) in (None, ""):
            v = next((d.get(f) for d in dupes if d.get(f) not in (None, "")), None)
            if v is not None:
                updates[f] = v
    # If the keeper is unpaid but an absorbed copy carries the payment, the
    # payment moves to the keeper (it's the same single payment).
    if _payment_sig(keeper) is None:
        src = next((d for d in dupes if _payment_sig(d) is not None), None)
        if src:
            for f in ("status", "paid_date", "paid_amount", "paid_by",
                      "payment_method", "check_number", "payment_note"):
                if src.get(f) not in (None, ""):
                    updates[f] = src[f]
    keeper_path = os.path.normcase(keeper.get("filed_path")
                                   or keeper.get("pdf_path") or "")
    for d in dupes:
        dp = d.get("filed_path") or d.get("pdf_path") or ""
        # Keep the absorbed record's filed file visible on the keeper as a
        # copy — recorded in place, never moved (it's already filed somewhere).
        if dp and os.path.normcase(dp) != keeper_path and os.path.exists(dp):
            add_attachment(keeper["id"], dp, "Merge", os.path.basename(dp), by)
        reassign_attachments(d["id"], keeper["id"])
        reassign_payment_splits(d["id"], keeper["id"])
        delete_entry(d["id"])
        log_activity(keeper["id"], "Merged duplicate record", by,
                     f"absorbed #{d['id']} ({(d.get('vendor') or '?')[:40]}, "
                     f"status {d.get('status')}) — byte-identical document")
    if updates:
        update_entry(keeper["id"], updates)
    return keeper["id"]


# In-flight confirm-path finalizations (insert + file move). /api/restart waits
# briefly for this to drain before os._exit so a restart can't cut a document's
# move in half (2026-07-27 code review, GH #8) — WAL protects the DB either way,
# but a half-moved PDF needs the boot-time repair passes to find it again.
_finalize_lock = threading.Lock()
_inflight_finalizes = 0

def _finalize_entry(entry, pdf_path, cfg, property_codes, note, learn_hints=True,
                    filed_dir=None, filed_name=None, copy_for=None):
    """Persist a confirmed entry: learn vendor + account hints, insert, log, and
    file the PDF into Entity/Property/Year. Shared by single-, bulk- and
    auto-confirm. Auto-confirm passes learn_hints=False: hints are human-taught
    ground truth, and letting the machine's own output re-save them would let
    one High-confidence mistake teach itself into future auto-files.

    filed_dir / filed_name (reports #23/#18): the review form's "File as" panel
    can direct the document into Karen's archive tree under a chosen name.
    filed_dir must validate against the allowed roots (filing.py) or the legacy
    destination is used — with an activity-log note, never silently. filed_name
    is a base name only; the source file's real extension always wins."""
    global _inflight_finalizes
    with _finalize_lock:
        _inflight_finalizes += 1
    try:
        return _finalize_entry_inner(entry, pdf_path, cfg, property_codes, note,
                                     learn_hints, filed_dir, filed_name, copy_for)
    finally:
        with _finalize_lock:
            _inflight_finalizes -= 1

def _archive_user_copy(dest_path, entry, cfg, user):
    r"""Confirm-time personal copy (Nancy's Desktop\SCANS ask, 2026-07-29):
    each physical-mail document a person confirms is copied, under its final
    convention name, into IntakeSystem\Scan Archive\<staff>\<year>; the
    workstation connector mirrors that folder down to the desk's Desktop\SCANS.
    Best-effort — a failed copy never blocks filing."""
    try:
        base_dir = filing.scan_archive_dir(cfg, user)
        if not base_dir:
            return
        year = (entry.get("date_received") or "")[:4] or date.today().strftime("%Y")
        copy_dir = os.path.join(base_dir, year)
        os.makedirs(copy_dir, exist_ok=True)
        copy_path = os.path.join(copy_dir, os.path.basename(dest_path))
        if not os.path.exists(copy_path):
            shutil.copy2(dest_path, copy_path)
    except Exception as e:
        print(f"[ScanArchive] could not keep a copy for {user}: {e}")

def _append_source_email_now(entry_id):
    """Filing is complete — put the source email's pages at the back of the
    filed PDF (reports #55/#106/#114: the email belongs IN the file, not only
    in the on-demand print packet). Never blocks filing: on any failure the
    bare document stands, and the archive-sync pass / retro CLI catch it."""
    try:
        res = email_append.append_to_entry(entry_id, by="System", cfg=load_config())
        if res.get("appended"):
            print(f"[EmailAppend] record #{entry_id}: {res['appended']} source "
                  f"email(s) appended to the filed PDF")
    except Exception as e:
        print(f"[EmailAppend] record #{entry_id}: {type(e).__name__}: {e}")


def _finalize_entry_inner(entry, pdf_path, cfg, property_codes, note, learn_hints,
                          filed_dir, filed_name, copy_for=None):
    if learn_hints and entry.get("vendor"):
        save_vendor_hint(entry["vendor"], entry.get("entity_code", ""),
                         entry.get("property_code", ""), entry.get("doc_type", ""),
                         entry.get("bill_type_code", ""))
    if learn_hints and entry.get("account_number"):
        save_account_hint(entry["account_number"], entry.get("entity_code", ""),
                          entry.get("property_code", ""))
    entry_id = insert_entry(entry)
    log_activity(entry_id, "Created", entry.get("processed_by", ""), note)
    filed_base = cfg.get("filed_folder", "F:\\File-Cabinet\\IntakeSystem\\Filed")
    folder_prop = property_codes[0] if property_codes else "GENERAL"
    override_dir = None
    if filed_dir:
        # resolve_client_dir also accepts the same folder typed under another
        # desk's drive letter or the UNC share (report #36).
        override_dir = filing.resolve_client_dir(filed_dir, cfg)
        if not override_dir:
            log_activity(entry_id, "Filing", entry.get("processed_by", ""),
                         f"Chosen folder was outside the archive and Filed trees — "
                         f"filed to the default location instead of: {filed_dir}")
    dest_dir = override_dir or os.path.join(
        filed_base, entry.get("entity_code") or "UNKNOWN",
        folder_prop, date.today().strftime("%Y"))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        # Keep the source file's real extension (PDF / image / Word), not always .pdf
        src_ext = os.path.splitext(pdf_path or "")[1] or ".pdf"
        base = (filing.sanitize_filename(filed_name) if filed_name else "") \
               or os.path.splitext(_make_filename(entry, dest_dir))[0]
        base = base.upper()   # server filenames are ALL CAPS (report #45)
        dest_path = os.path.join(dest_dir, base + src_ext)
        if pdf_path and os.path.normcase(os.path.normpath(dest_path)) == \
                os.path.normcase(os.path.normpath(pdf_path)):
            # Hand-filed pickup confirmed in place (cabinet_watch): the file is
            # already exactly where and what the reviewer chose — moving would
            # only collide with itself. Record the path and done.
            update_entry(entry_id, {"filed_path": pdf_path})
            _append_source_email_now(entry_id)
            return entry_id
        if os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base} ({entry_id}){src_ext}")
        if pdf_path and os.path.exists(pdf_path):
            filing_ledger.announce(dest_path)
            shutil.move(pdf_path, dest_path)
            update_entry(entry_id, {"filed_path": dest_path})
            _append_source_email_now(entry_id)
            if copy_for and entry.get("mail_type") == "Physical Mail":
                _archive_user_copy(dest_path, entry, cfg, copy_for)
    except Exception as e:
        print(f"[App] Could not move file: {e}")
    return entry_id

def _entry_from_scan_data(data, cfg, user):
    """Build an entry dict + property_codes from a queued scan's AI-extracted data
    (the bulk-confirm equivalent of reading the review form)."""
    property_codes = [c for c in (data.get("property_codes") or []) if c]
    addr_by = {p["code"]: p["address"] for p in cfg.get("properties", [])}
    entity_code = data.get("entity_code") or ""
    entity_name = next((e["name"] for e in cfg["entities"] if e["code"] == entity_code), "")
    amt = data.get("amount")
    try:
        amt = float(amt) if amt not in (None, "") else None
    except (ValueError, TypeError):
        amt = None
    entry = {
        "date_received":    (data.get("received_date")
                             or _fallback_received_date(data.get("pdf_path"))),
        "scanned_date":     data.get("scanned_date") or "",
        "batch_number":     data.get("batch_number") or "",
        "mail_type":        data.get("mail_type", "Email"),
        "entity_code":      entity_code,
        "entity_name":      entity_name,
        "property_code":    ", ".join(property_codes),
        "property_address": ", ".join(addr_by[c] for c in property_codes if c in addr_by),
        "doc_type":         data.get("doc_type", "Other"),
        "bill_type_code":   data.get("bill_type_code") or "",
        "vendor":           data.get("vendor") or "",
        "amount":           amt,
        "due_date":         data.get("due_date") or "",
        "status":           "Pending",
        "processed_by":     user,
        "notes":            data.get("notes") or "",
        "pdf_path":         data.get("pdf_path"),
        "ai_confidence":    data.get("ai_confidence", ""),
        "invoice_number":   _id_field(data.get("invoice_number")),
        "account_number":   _id_field(data.get("account_number")),
        "email_from":       data.get("email_from", ""),
        "email_to":         data.get("email_to", ""),
        "email_account":    data.get("email_account", ""),
        "email_subject":    data.get("email_subject", ""),
        "email_date":       data.get("email_date", ""),
        "email_body":       data.get("email_body", ""),
        "file_hash":        data.get("file_hash", ""),
        "important":        1 if data.get("important") else 0,
        "important_reason": ((data.get("important_reason") or "")
                             if data.get("important") else ""),
        "notice_summary":   data.get("notice_summary") or None,
    }
    return entry, property_codes

# ── Filtered email docs (AI thought these were junk — recoverable, never deleted) ──

# The /filtered recoverable-junk area lives in routes_filtered.py
# (2026-07-29 modular split, GH #6).

def _id_field(raw):
    """Invoice/account number off a form: trimmed, with '' and the literal
    string 'None' (a NULL that round-tripped through entry_form.html before
    the 2026-07-23 template fix) both normalized back to NULL."""
    val = (raw or "").strip()
    return None if not val or val.lower() == "none" else val

def _parse_money(raw):
    """A user-entered dollar amount ('$1,234.56', '1234.56', '', None) as a
    float, or None if blank/unparseable. 2026-07-25 code review, P3: this exact
    strip-$-and-comma-then-try/except block was copy-pasted across 7 form
    handlers (bill amount, paid amount, expected-bill typical amount, per-entity
    split amounts) — pulled out here so future tweaks only happen once."""
    val = (raw or "").replace(",", "").replace("$", "").strip()
    try:
        return float(val) if val else None
    except ValueError:
        return None

def _store_receipt_upload(entry, f):
    """File an uploaded receipt next to the entry's primary document (or a
    RECEIPTS folder) and record it as a 'Receipt' attachment. Returns the
    saved path, or None if the save failed."""
    entry_id = entry["id"]
    primary = entry.get("filed_path") or ""
    if primary and os.path.dirname(primary):
        dest_dir = os.path.dirname(primary)
    else:
        cfg = load_config()
        filed_base = cfg.get("filed_folder", "F:\\File-Cabinet\\IntakeSystem\\Filed")
        dest_dir = os.path.join(filed_base, entry.get("entity_code") or "UNKNOWN", "RECEIPTS")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass
    safe = re.sub(r'[\\/:*?"<>|]', "", os.path.basename(f.filename)).strip() or "receipt.pdf"
    base, ext = os.path.splitext(safe)
    dest = os.path.join(dest_dir, f"{base} (RECEIPT {entry_id}){ext}")
    c = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base} (RECEIPT {entry_id}-{c}){ext}")
        c += 1
    try:
        filing_ledger.announce(dest)
        f.save(dest)
    except Exception as e:
        print(f"[Receipt] could not save: {e}")
        return None
    add_attachment(entry_id, dest, "Receipt", f.filename, session.get("user", ""))
    log_activity(entry_id, "Receipt attached", session.get("user", ""), f.filename)
    return dest

def _run_reindex(force: bool = False):
    """Build/refresh the full-text index on a background thread (Option B).
    Incremental by file_hash, so routine runs only touch new/changed docs.
    Called from both the admin route and the startup thread, so the atomic
    claim lives here (not in a caller) — whichever gets here first wins."""
    if not _try_start_job("reindex"):
        return
    try:
        cfg = load_config()
        summary = indexer.reindex(tesseract_path=cfg.get("tesseract_path", ""), force=force)
        print(f"[Indexer] reindex done: {summary}")
    except Exception as e:
        print(f"[Indexer] reindex failed: {e}")
    finally:
        _finish_job("reindex")

@app.route("/goto")
def goto_record():
    """Nav jump box: a record number (dash optional, leading # ignored) opens
    the record; a bare integer falls back to the legacy record id; anything
    else lands on the /bills universal search."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return redirect(url_for("bills"))
    token = q.lstrip("#").strip()
    compact = token.replace("-", "")
    entry = None
    if compact.isdigit() and 7 <= len(compact) <= 10:
        # YYMMDD prefix + 1-4 digit sequence; dash and zero-padding optional
        entry = get_entry_by_record_no(f"{compact[:6]}-{int(compact[6:]):03d}")
    if entry is None and token.isdigit():
        entry = get_entry(int(token))
    if entry:
        return redirect(url_for("view_entry", entry_id=entry["id"]))
    return redirect(url_for("bills", q=q))

# The Properties & Units editor lives in routes_units.py (GH #6 modular split) —
# registered at the bottom of this file with the other routes_*.py modules.

@app.route("/scan/preview/<int:scan_id>")
def scan_preview(scan_id):
    """Serve the raw PDF for inline preview."""
    from flask import send_file
    data = get_scan(scan_id)
    if not data:
        return "Not found", 404
    pdf_path = data["pdf_path"]
    if not os.path.exists(pdf_path):
        return "File not found on disk", 404
    return send_file(pdf_path)

# ── Hover-to-highlight document viewer ─────────────────────────────────────────
# The Review page and record view show the document as server-rendered page
# images with positioned highlight boxes, so hovering a field (vendor, amount,
# due date, invoice #, account #) lights up where that value sits in the
# document. Values are located by searching the PDF's own text layer — no AI
# call. Image-only documents have no text layer; the client falls back to the
# plain PDF iframe when highlights aren't available.

_HL_FIELDS = ("vendor", "amount", "due_date", "invoice_number", "account_number")
_HL_MAX_PAGES = 10          # search only this many pages (values live up front)
_HL_MAX_BOXES_PER_FIELD = 8
_hl_cache = {}
_hl_lock = threading.Lock()
# PDFium is not thread-safe; one page render at a time.
_render_lock = threading.Lock()

def _highlight_variants(field, value):
    """The strings a field's value might literally appear as in the document.
    E.g. amount 147.48 prints as '$147.48' or '147.48'; an ISO due date prints
    as '07/15/2026', 'July 15, 2026', etc. First variant that matches wins."""
    value = ("" if value is None else str(value)).strip()
    if not value:
        return []
    if field == "amount":
        try:
            a = float(value.replace(",", "").replace("$", ""))
        except ValueError:
            return [value]
        grouped = f"{a:,.2f}"
        return [f"${grouped}", grouped, f"{a:.2f}"]
    if field == "due_date":
        try:
            d = date.fromisoformat(value[:10])
        except ValueError:
            return [value]
        m, dd, y4, y2 = d.month, d.day, d.year, d.year % 100
        return [f"{m:02d}/{dd:02d}/{y4}", f"{m}/{dd}/{y4}",
                f"{m:02d}/{dd:02d}/{y2:02d}", f"{m}/{dd}/{y2:02d}",
                f"{d.strftime('%B')} {dd}, {y4}", f"{d.strftime('%b')} {dd}, {y4}",
                f"{d.strftime('%B')} {dd} {y4}", value]
    variants = [value]
    collapsed = " ".join(value.split())
    if collapsed != value:
        variants.append(collapsed)
    if field in ("invoice_number", "account_number"):
        nospace = value.replace(" ", "")
        if nospace != value:
            variants.append(nospace)
    return variants

def _doc_highlights(path, fields):
    """Locate each field value in the PDF text layer. Returns
    {"pages": N, "fields": {name: [{page, x0, x1, top, bottom}, ...]}} with all
    coordinates as fractions of the page size (so they scale with the image).
    Cached per (file, mtime, values) — the parse costs ~a second."""
    import pdfplumber
    ck = (os.path.normcase(path), os.path.getmtime(path),
          tuple(sorted((k, str(v)) for k, v in fields.items() if v)))
    with _hl_lock:
        if ck in _hl_cache:
            return _hl_cache[ck]
    found = {k: [] for k in fields}
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages[:_HL_MAX_PAGES], 1):
            pw, ph = float(page.width) or 1.0, float(page.height) or 1.0
            for fname, value in fields.items():
                if len(found[fname]) >= _HL_MAX_BOXES_PER_FIELD:
                    continue
                for variant in _highlight_variants(fname, value):
                    try:
                        hits = page.search(variant, regex=False, case=False)
                    except Exception:
                        hits = []
                    if not hits:
                        continue
                    for h in hits[:_HL_MAX_BOXES_PER_FIELD]:
                        found[fname].append({
                            "page": pno,
                            "x0": h["x0"] / pw, "x1": h["x1"] / pw,
                            "top": h["top"] / ph, "bottom": h["bottom"] / ph})
                    break   # first variant that appears on this page is enough
    result = {"pages": n_pages,
              "fields": {k: v for k, v in found.items() if v}}
    with _hl_lock:
        if len(_hl_cache) > 40:   # tiny bound; entries are small dicts
            _hl_cache.clear()
        _hl_cache[ck] = result
    return result

def _render_doc_page_png(path, page_index, scale=2.0):
    """Render one PDF page to a PNG in memory (or None if out of range)."""
    import io
    import pypdfium2 as pdfium
    with _render_lock:
        pdf = pdfium.PdfDocument(path)
        try:
            if page_index < 0 or page_index >= len(pdf):
                return None
            pil = pdf[page_index].render(scale=scale).to_pil()
        finally:
            pdf.close()
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    buf.seek(0)
    return buf

def _highlight_response(path, fields):
    if not (path and os.path.exists(path)):
        return jsonify({"ok": False, "reason": "no file"}), 404
    if not path.lower().endswith(".pdf"):
        return jsonify({"ok": False, "reason": "not a PDF"}), 200
    try:
        hl = _doc_highlights(path, fields)
    except Exception as e:
        print(f"[Highlights] failed for {os.path.basename(path)}: {e}")
        return jsonify({"ok": False, "reason": "unreadable"}), 200
    return jsonify({"ok": True, **hl})

def _page_image_response(path, page_num):
    if not (path and os.path.exists(path)):
        return "File not found on disk", 404
    if not path.lower().endswith(".pdf"):
        return send_file(path)   # image documents: the file itself is the page
    try:
        buf = _render_doc_page_png(path, page_num - 1)
    except Exception as e:
        print(f"[Highlights] page render failed for {os.path.basename(path)}: {e}")
        return "Render failed", 500
    if buf is None:
        return "No such page", 404
    return send_file(buf, mimetype="image/png",
                     download_name=f"page{page_num}.png")

@app.route("/review/<int:scan_id>/highlights")
def scan_highlights(scan_id):
    data = get_scan(scan_id)
    if not data:
        return jsonify({"ok": False, "reason": "not found"}), 404
    fields = {k: data.get(k) for k in _HL_FIELDS}
    return _highlight_response(data.get("pdf_path") or "", fields)

@app.route("/review/<int:scan_id>/page/<int:page_num>")
def scan_page_image(scan_id, page_num):
    data = get_scan(scan_id)
    if not data:
        return "Not found", 404
    return _page_image_response(data.get("pdf_path") or "", page_num)

@app.route("/entry/<int:entry_id>/highlights")
def entry_highlights(entry_id):
    entry = get_entry(entry_id)
    if not entry:
        return jsonify({"ok": False, "reason": "not found"}), 404
    fields = {k: entry.get(k) for k in _HL_FIELDS}
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    return _highlight_response(path, fields)

@app.route("/entry/<int:entry_id>/page/<int:page_num>")
def entry_page_image(entry_id, page_num):
    entry = get_entry(entry_id)
    if not entry:
        return "Not found", 404
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    return _page_image_response(path, page_num)

# ── Settings, account & admin management ───────────────────────────────────────
# /settings + entities/properties/bill-types/mail/staff + /account live in
# routes_settings.py (2026-07-29 modular split, GH #6) — registered at the
# bottom of this file alongside scanflow and routes_karen.

# ── Issue reporting ("Report a problem" — available to every logged-in user) ───

_REPORT_SHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Bug Reports")

# /report + /reports admin views live in routes_reports.py (2026-07-29
# modular split, GH #6). _REPORT_SHOTS_DIR stays HERE because conftest.py
# monkeypatches klr._REPORT_SHOTS_DIR; routes_reports reads it via the module.

@app.route("/api/clear_queue", methods=["POST"])
@admin_required
def api_clear_queue():
    # Admin-only: one click empties the whole review queue for everyone, and the
    # items only come back (re-classified, at API cost) on the next sweep.
    queue_state.clear_queue()
    clear_pending_scans()
    return jsonify({"ok": True})

@app.route("/api/restart", methods=["POST"])
@admin_required
def api_restart():
    # The 0.8s delay lets THIS response complete; the drain loop then waits (up
    # to 10s) for any in-flight confirm finalization in another request, so the
    # exit can't cut a document's file move in half (2026-07-27 review, GH #8).
    def _exit_when_idle():
        deadline = time.time() + 10
        while time.time() < deadline:
            with _finalize_lock:
                if _inflight_finalizes == 0:
                    break
            time.sleep(0.2)
        os._exit(42)
    threading.Timer(0.8, _exit_when_idle).start()
    return jsonify({"ok": True})

# ── Karen's setup questionnaire ───────────────────────────────────────────────
# Lives in routes_karen.py (2026-07-29 modular split, GH #6) — registered at
# the bottom of this file alongside scanflow.

@app.route("/api/pending_count")
def api_pending_count():
    pending = _queue_items()
    urgent = sum(1 for _, d in pending if _scan_is_overdue(d))
    return jsonify({"count": len(pending), "urgent": urgent})

def _background_folder_scan(scan_folder, email_folder, cfg):
    """Poll every configured mailbox, then ingest every file sitting in the
    scan/email folders — off the request thread so large batches (or a slow,
    unreachable mail server) can't hang the browser. Each file persists to the
    queue as it's done. Caller must have already claimed the 'scan' job slot
    via _try_start_job; this releases it when done. A poll error isn't
    returned anywhere from here (there's no request left to return it to) —
    email_poller's own _poll_status already records it per-account for the
    Settings page, so it's not lost, just not instant."""
    try:
        try:
            email_poller.poll_email_once(cfg, email_folder, is_email_processed, mark_email_processed)
        except Exception as e:
            print(f"[App] background email poll error: {e}")
        for folder, source in [(scan_folder, "Physical Mail"), (email_folder, "Email")]:
            if not folder or not os.path.isdir(folder):
                continue
            for fname in sorted(os.listdir(folder)):
                if fname.lower().endswith(SUPPORTED_DOC_EXTS):
                    try:
                        enqueue_scan(os.path.join(folder, fname), source)
                    except Exception as e:
                        print(f"[App] scan ingest error on {fname}: {e}")
    finally:
        _finish_job("scan")
    print("[App] Background scan (mail + folders) complete.")

@app.route("/api/scan_now", methods=["POST"])
def api_scan_now():
    cfg = load_config()
    scan_folder, email_folder = cfg.get("scan_folder", ""), cfg.get("email_folder", "")
    pending_files = 0
    for folder in (scan_folder, email_folder):
        if folder and os.path.isdir(folder):
            pending_files += sum(1 for f in os.listdir(folder) if f.lower().endswith(SUPPORTED_DOC_EXTS))
    if _job_running("scan"):
        return jsonify({"ok": True, "scanning": True, "files": pending_files, "already": True})
    if pending_files == 0 and not email_poller.configured_accounts(cfg):
        return jsonify({"ok": False, "error": "No scan or email folder is accessible."})
    started = False
    if pending_files > 0 or email_poller.configured_accounts(cfg):
        if _try_start_job("scan"):
            threading.Thread(target=_background_folder_scan,
                             args=(scan_folder, email_folder, cfg), daemon=True).start()
            started = True
    return jsonify({"ok": True, "scanning": started, "files": pending_files})

# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_period(month_arg, range_arg):
    """Resolve the dashboard's selected time window from query args.
    Defaults to the current month; range_arg == 'all' means all-time.
    Returns date_from/date_to (or None), display label, prev/next month keys,
    and a `qs` fragment to carry the window into Intake Log click-throughs."""
    from calendar import monthrange
    today = date.today()
    if range_arg == "all":
        return {"is_all": True, "label": "All time", "date_from": None,
                "date_to": None, "month": today.strftime("%Y-%m"),
                "prev_month": None, "next_month": None, "qs": ""}
    year, mon = today.year, today.month
    if month_arg:
        try:
            y, m = str(month_arg).split("-")
            year, mon = int(y), int(m)
            if not (1 <= mon <= 12):
                year, mon = today.year, today.month
        except (ValueError, AttributeError):
            year, mon = today.year, today.month
    first = date(year, mon, 1)
    last = date(year, mon, monthrange(year, mon)[1])
    prev_month = (first - timedelta(days=1)).strftime("%Y-%m")
    # Hide "next" once we're at or past the current month (no point browsing future).
    is_current_or_future = (year > today.year) or (year == today.year and mon >= today.month)
    next_month = None if is_current_or_future else (last + timedelta(days=1)).strftime("%Y-%m")
    return {
        "is_all": False,
        "label": first.strftime("%B %Y"),
        "date_from": first.isoformat(),
        "date_to": last.isoformat(),
        "month": first.strftime("%Y-%m"),
        "prev_month": prev_month,
        "next_month": next_month,
        "qs": f"date_from={first.isoformat()}&date_to={last.isoformat()}",
    }

# ── Klick Klack Helper (in-app AI assistant) ──────────────────────────────────

@app.route("/api/assistant", methods=["POST"])
def api_assistant():
    """One chat turn with the Klick Klack Helper. The widget in base.html posts
    {message, history, page}; the reply comes back as {reply} — or {error} with
    a friendly sentence the widget shows in the chat. The helper only ever
    EXPLAINS the app; it has no ability to change anything."""
    payload = request.get_json(silent=True) or {}
    question = (payload.get("message") or "").strip()
    if not question:
        return jsonify({"error": "Type a question first."}), 400
    cfg = load_config()
    context = {
        "user": session.get("user", ""),
        "page": str(payload.get("page") or "")[:200],
        "queue_count": queue_state.queue_size(),
        "today": date.today().isoformat(),
    }
    reply = err_msg = None
    try:
        reply = assistant.ask_assistant(question, payload.get("history"),
                                        context, cfg)
    except assistant.AssistantNotConfigured:
        err_msg = ("The helper needs the AI connection, which isn't set up "
                   "right now — ask Joshua.")
    except anthropic.AuthenticationError:
        err_msg = "The AI connection was rejected (bad key) — tell Joshua."
    except anthropic.RateLimitError:
        err_msg = ("The AI service is busy or out of credits — try again in a "
                   "minute, or tell Joshua.")
    except anthropic.APIConnectionError:
        err_msg = ("Couldn't reach the AI service — check the internet "
                   "connection and try again.")
    except anthropic.APIStatusError as e:
        print(f"[Helper] API error {e.status_code}: {e.message}")
        err_msg = "The AI service had a hiccup — please try again in a moment."
    except Exception as e:
        print(f"[Helper] failed: {e}")
        err_msg = ("Something went wrong on my end — try again, and tell Joshua "
                   "if it keeps happening.")
    # Save every turn (question + reply or the error the user saw). Best-effort:
    # a logging hiccup must never swallow the reply. (report #4)
    try:
        log_helper_chat(context["user"], context["page"], question,
                        reply=reply, error=err_msg)
    except Exception as e:
        print(f"[Helper] chat log failed: {e}")
    if err_msg:
        return jsonify({"error": err_msg})
    return jsonify({"reply": reply})

def _rename_staged_file(current_path: str, result: dict) -> str:
    """Rename a split/staged file to a readable name right after classification,
    preserving its original extension (PDF, image, or Word)."""
    import re
    vendor = (result.get("vendor") or "").strip()
    doc_type = (result.get("doc_type") or "Document").strip()
    label = vendor if vendor else doc_type
    label = re.sub(r'[\\/:*?"<>|]', "", label).strip()
    if not label:
        return current_path
    folder = os.path.dirname(current_path)
    ext = os.path.splitext(current_path)[1] or ".pdf"
    new_name = label + ext
    new_path = os.path.join(folder, new_name)
    # Avoid collisions by appending a counter
    counter = 1
    while os.path.exists(new_path) and new_path != current_path:
        new_name = f"{label} ({counter}){ext}"
        new_path = os.path.join(folder, new_name)
        counter += 1
    try:
        os.rename(current_path, new_path)
        return new_path
    except Exception as e:
        print(f"[App] Could not rename staged file: {e}")
        return current_path

def _mmddyy(date_received) -> str:
    """Format a received date as Karen's MM-DD-YY. Accepts an ISO 'YYYY-MM-DD'
    string (or anything date.fromisoformat understands); falls back to today."""
    d = None
    if date_received:
        try:
            d = datetime.strptime(str(date_received).strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            d = None
    if d is None:
        d = date.today()
    return d.strftime("%m-%d-%y")

def _clean_name_part(s: str) -> str:
    """One filename component: Windows-illegal characters stripped (keep #, $,
    -, spaces — Karen uses them), whitespace single-spaced."""
    import re
    s = re.sub(r'[\\/:*?"<>|]', "", s or "")
    return re.sub(r"\s+", " ", s).strip()

# PAID BILLS trees that never carry a trailing property code — street addresses
# sit in the detail slot instead (FOLDER_NAMING_STANDARDS.md §2: KLR/LRC/
# PERSONAL 0–2% property-tailed vs PP/TRUST/TRUST2/TRUST3 at 88–99%).
_NO_PROP_PAID_TREES = {"KLR", "LRC", "PERSONAL", "KLR-LRC"}

def _folder_profile(dest_dir):
    """Which naming grammar the destination folder uses, per
    T:\\_OPS INDEX\\FOLDER_NAMING_STANDARDS.md — folders encode context, so a
    name that is spec-valid globally can still be wrong for its folder (the
    08. AUGUST incident). Returns (profile, paid_tree):
      ('incoming', None) — an ACCOUNTS PAYABLE - INCOMING BILLS / INCOMING
                           TIME CARDS tree;
      ('paid', tree)     — a per-entity PAID BILLS tree; tree is the entity
                           whose tree it is ('TRUST 2 PAID BILLS' → 'TRUST2',
                           'PP PAID BILLS ALL PROPERTIES' → 'PP'), or None if
                           the segment doesn't say;
      ('global', None)   — everywhere else (spec-§5 shape).
    """
    if dest_dir:
        for seg in os.path.normpath(str(dest_dir)).split(os.sep):
            s = " ".join(seg.upper().split())
            if "PAID BILLS" in s:
                tree = s.split("PAID BILLS", 1)[0].strip().replace(" ", "")
                return "paid", (tree or None)
            if "INCOMING BILLS" in s or "INCOMING TIME CARDS" in s:
                return "incoming", None
    return "global", None

def _make_filename(entry: dict, dest_dir: str = None) -> str:
    """Build a PDF filename in Karen's 13-year archive convention so new files
    are indistinguishable from the archive and stay searchable by the same
    pattern. The GLOBAL token grammar is KAREN_FILENAME_SPEC.md §5; which
    tokens the destination folder actually uses is FOLDER_NAMING_STANDARDS.md
    — pass dest_dir so the name matches its folder's own grammar:

    incoming trees:  MM-DD-YY <PAYEE> - [INV# n | doc kind] - [PROP… | ENTITY]
                     (never a leading entity, bill-type token, CK#/ACH, or $ —
                     nothing is paid yet and the tree isn't per-entity)
    paid trees:      MM-DD-YY <ENTITY> CK#n|ACH - <PAYEE> - [INV#|ACCT.#] -
                     $<AMOUNT>[ - PROP…]  (property tail only in the trees
                     that use one; archive idiom: double space before ACH)
    everywhere else: MM-DD-YY <ENTITY> <BILL_TYPE|CK#n> - <PAYEE> - $<AMOUNT>
                     - <PROPERTY[ PROPERTY...]>  (unchanged spec-§5 shape,
                     e.g. 06-26-26 TRUST2 ELEC - Frontier - $147.48 - TL222)

    Any null field (and its surrounding ' - ') is omitted rather than emitted
    blank. ALL CAPS: server filenames match the rest of the 13-year archive
    (Nancy, report #45 — "It is ok to have lower case here in Klick Klack").
    """
    import re
    profile, paid_tree = _folder_profile(dest_dir)

    date_tok = _mmddyy(entry.get("date_received"))
    entity = _clean_name_part(entry.get("entity_code") or "")
    vendor = _clean_name_part(entry.get("vendor") or "")
    doc_type = _clean_name_part(entry.get("doc_type") or "")
    # Fall back to doc_type when there's no vendor (document records — leases,
    # statements — still get a meaningful descriptor instead of a bare date).
    payee = vendor or doc_type
    amount = entry.get("amount")
    amount_str = f"${amount:,.2f}" if amount is not None else ""
    props = entry.get("property_code") or ""
    prop_str = " ".join(_clean_name_part(c) for c in props.split(",") if c.strip())
    invoice = _clean_name_part(entry.get("invoice_number") or "")
    account = _clean_name_part(entry.get("account_number") or "")
    method = (entry.get("payment_method") or "").strip()
    ck = _clean_name_part(entry.get("check_number") or "")
    if method == "Check":
        pay_tok = f"CK#{ck}" if ck else "CK"
    else:
        pay_tok = {"ACH": "ACH", "Credit Card": "CC", "Phone": "PHONE"}.get(method, "")

    if profile == "incoming":
        # The trailing token is the filing attribution: property code when one
        # applies, else the entity (statements, EOBs, 1099s). The detail slot
        # takes the invoice number when we have one, else a doc-kind word for
        # non-bill documents (STATEMENT, INSURANCE, …) — never the app's
        # bill-type code, which is not part of this folder's grammar.
        detail = f"INV# {invoice}" if invoice else ""
        if not detail and doc_type not in ("", "Bill", "Receipt", "Other"):
            detail = doc_type
        name = " ".join(t for t in (date_tok, payee) if t)
        tail = [s for s in (detail, prop_str or entity) if s]
        if tail:
            name += " - " + " - ".join(tail)
        return name.upper() + ".pdf"

    if profile == "paid":
        head = date_tok
        if entity:
            head += " " + entity
        if pay_tok:
            # Archive idiom (§2): double space before ACH ("KLR  ACH"),
            # single space before CK#nnnnn.
            head += ("  " if pay_tok == "ACH" else " ") + pay_tok
        detail = f"INV# {invoice}" if invoice else (f"ACCT.#{account}" if account else "")
        tree = paid_tree or entity.replace(" ", "")
        tail_prop = "" if tree in _NO_PROP_PAID_TREES else prop_str
        segs = [s for s in (payee, detail, amount_str, tail_prop) if s]
        name = head + (" - " + " - ".join(segs) if segs else "")
        return name.upper() + ".pdf"

    # Global spec-§5 shape. Leading tokens (space-separated): date, entity, and
    # the type/method slot — at intake that slot is the bill type (ELEC); once
    # paid it becomes the method + ref (CK#13386, ACH, …) exactly as Karen's
    # archive names payments.
    bill_type = _clean_name_part(entry.get("bill_type_code") or "")
    head_tokens = [t for t in (date_tok, entity, pay_tok or bill_type) if t]
    tail_segments = [s for s in (payee, amount_str, prop_str) if s]
    name = " ".join(head_tokens)
    if tail_segments:
        name += " - " + " - ".join(tail_segments)
    name = re.sub(r"\s+", " ", name).strip()
    return name.upper() + ".pdf"

def _repair_filed_paths():
    """Startup self-heal: re-link records whose filed document was renamed on disk.

    On 2026-06-30 the Filed tree was bulk-renamed to Karen's filename convention
    without updating intake_log.filed_path, so every record filed before then
    pointed at a name that no longer existed and showed no PDF. This pass finds
    records whose filed_path is dead and re-links them using two CONSERVATIVE
    rules only (no fuzzy matching):
      1. a file anywhere in the Filed tree whose name ends with this record's
         own '(<entry_id>)' collision suffix — written from the id itself;
      2. a file in the record's recorded folder whose name is exactly what
         _make_filename builds from the record (with or without the id suffix).
    Each file is claimed at most once; only the DB is touched, never files.
    Idempotent and cheap when nothing is broken, so it runs on every boot —
    this must run inside the app's process (SQLite WAL forbids writing the
    database from another machine over the share)."""
    try:
        cfg = load_config()
        filed_base = cfg.get("filed_folder", "")
        if not (filed_base and os.path.isdir(filed_base)):
            return
        entries = get_all_entries()
        referenced = set()   # existing files already owned by a record
        broken = []
        for e in entries:
            fp = e.get("filed_path") or ""
            if fp and os.path.exists(fp):
                referenced.add(os.path.normcase(fp))
                continue
            if not fp:
                continue   # never filed (manual entry / staged only) — not ours to guess
            # A set-but-dead filed_path breaks the record's document view even if
            # an old staging copy still exists (the view prefers filed_path), so
            # every one of these is worth repairing.
            broken.append(e)
        if not broken:
            return
        # Index the Filed tree once: by trailing '(N)' id and by folder.
        by_id, id_dupes, by_folder = {}, set(), {}
        for root, _dirs, files in os.walk(filed_base):
            for name in files:
                full = os.path.join(root, name)
                by_folder.setdefault(os.path.normcase(root), {})[name.lower()] = full
                m = re.search(r"\((\d+)\)\.[A-Za-z0-9]+$", name)
                if m:
                    n = int(m.group(1))
                    if n in by_id:
                        id_dupes.add(n)
                    by_id[n] = full
        fixed = 0
        for e in broken:
            eid = e["id"]
            expected = _make_filename(e, os.path.dirname(e["filed_path"]))
            stem, ext = os.path.splitext(expected)
            cand = by_id.get(eid)
            if cand is not None and (eid in id_dupes or os.path.normcase(cand) in referenced):
                cand = None
            if not cand:
                folder = by_folder.get(os.path.normcase(os.path.dirname(e["filed_path"])), {})
                c = folder.get(expected.lower()) or folder.get(f"{stem} ({eid}){ext}".lower())
                if c and os.path.normcase(c) not in referenced:
                    cand = c
            if cand and os.path.exists(cand):
                referenced.add(os.path.normcase(cand))
                update_entry(eid, {"filed_path": cand})
                fixed += 1
        if fixed:
            log_activity(None, "Repair",
                         detail=f"Re-linked {fixed} record(s) whose filed document had "
                                f"been renamed on disk ({len(broken) - fixed} unmatched).")
            print(f"[Repair] Re-linked {fixed} record(s) to renamed filed documents "
                  f"({len(broken) - fixed} could not be matched).")
    except Exception as ex:
        print(f"[Repair] filed-path self-heal failed: {ex}")

# Folders _repair_filed_paths_by_hash never searches inside: _Skipped and
# _Duplicates are a human's decision to pull the sheet out of the flow, and
# _Filtered is where the junk filter's own copy of a doc lives — none of the
# three should be raided to "fix" an unrelated record.
_HASH_REPAIR_OFF_LIMITS_DIRS = ("_skipped", "_duplicates", "_filtered", "scan archive")
_HASH_REPAIR_DOC_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".docx")
_HASH_REPAIR_MAX_CANDIDATES = 2000   # per grandparent tree — bounds a single boot's walk

def _repair_filed_paths_by_hash():
    """Startup self-heal, second pass: re-link records whose filed document was
    RENAMED OR MOVED to a sibling folder on disk, using the document's sha256
    (file_hash) instead of its name. _repair_filed_paths only catches renames it
    can predict (its own '(id)' suffix, or the exact convention filename); this
    catches the rest — e.g. the 2026-07 Harvest-Bot batch, whose files were
    reorganized into sibling folders under the manual filing tree after
    harvesting, landing on names no rule could guess.

    CONSERVATIVE and bounded: only records with a dead recorded path AND a
    stored file_hash are candidates (nothing to search for otherwise). The
    search walks only the record's grandparent tree — its parent folder and
    that folder's siblings, recursed — never the whole Filed share, and stops
    after _HASH_REPAIR_MAX_CANDIDATES files so one boot can't be stuck walking
    a huge tree. Only files with a plausible document extension are hashed,
    and each (path, mtime) is hashed at most once per run, so many broken
    records in the same folder don't re-hash the same candidates. A file
    already owned by another record or queue row is never stolen. Idempotent
    and cheap when nothing is broken. Must run inside the app's process
    (SQLite WAL forbids writing the database from another machine)."""
    try:
        entries = get_all_entries()
        referenced = set()   # paths already owned by a live record
        broken = []
        for e in entries:
            for key in ("filed_path", "pdf_path"):
                p = e.get(key) or ""
                if p and os.path.exists(p):
                    referenced.add(os.path.normcase(p))
            path = e.get("filed_path") or e.get("pdf_path") or ""
            if path and not os.path.exists(path) and e.get("file_hash"):
                broken.append(e)
        if not broken:
            return
        for _sid, d in _queue_items():   # a queue row's scan file is off-limits too
            p = d.get("pdf_path") or ""
            if p and os.path.exists(p):
                referenced.add(os.path.normcase(p))

        dir_cache = {}    # normcase(dir) -> capped candidate file list (this run only)
        def _dir_files(top):
            key = os.path.normcase(top)
            if key in dir_cache:
                return dir_cache[key]
            cands = []
            for root, dirs, files in os.walk(top):
                dirs[:] = [d for d in dirs if d.lower() not in _HASH_REPAIR_OFF_LIMITS_DIRS]
                for name in files:
                    if len(cands) >= _HASH_REPAIR_MAX_CANDIDATES:
                        break
                    if os.path.splitext(name)[1].lower() in _HASH_REPAIR_DOC_EXTS:
                        cands.append(os.path.join(root, name))
                if len(cands) >= _HASH_REPAIR_MAX_CANDIDATES:
                    break
            dir_cache[key] = cands
            return cands

        def _candidates(parent, grandparent):
            """Search order matters more than search size: the record's OWN
            folder first (renames-in-place), then sibling folders most recently
            MODIFIED first — a re-filed document is almost always in the folder
            someone just touched. A plain alphabetical walk of the year tree
            eats the candidate cap on January before ever reaching July (live
            case: the 07-06-26 harvest batch re-filed from '06. JUNE' into
            '07. JULY'). The cap applies to the combined stream per record."""
            order = [parent]
            try:
                sibs = [os.path.join(grandparent, d) for d in os.listdir(grandparent)]
                sibs = [d for d in sibs if os.path.isdir(d)
                        and os.path.normcase(d) != os.path.normcase(parent)
                        and os.path.basename(d).lower() not in _HASH_REPAIR_OFF_LIMITS_DIRS]
                sibs.sort(key=lambda d: os.path.getmtime(d), reverse=True)
                order += sibs
            except OSError:
                pass
            seen = 0
            for top in order:
                for full in _dir_files(top):
                    yield full
                    seen += 1
                    if seen >= _HASH_REPAIR_MAX_CANDIDATES:
                        return

        hash_cache = {}   # (normcase(path), mtime) -> sha256, shared across every broken record
        fixed = ambiguous = 0
        for e in broken:
            path = e.get("filed_path") or e.get("pdf_path") or ""
            parent = os.path.dirname(path)
            grandparent = os.path.dirname(parent)
            if not (grandparent and os.path.isdir(grandparent)):
                continue
            matches = []
            for full in _candidates(parent, grandparent):
                if os.path.normcase(full) in referenced:
                    continue
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                hkey = (os.path.normcase(full), mtime)
                h = hash_cache.get(hkey)
                if h is None:
                    try:
                        h = _sha256_file(full)
                    except OSError:
                        continue
                    hash_cache[hkey] = h
                # filed_sha256: the filed bytes once email pages were appended
                # (email_append.py) — either hash identifies the record's file.
                if h in (e["file_hash"], e.get("filed_sha256")):
                    matches.append(full)
            if not matches:
                continue
            found = matches[0]
            referenced.add(os.path.normcase(found))
            update_entry(e["id"], {"filed_path": found})
            detail = f"Re-linked record #{e['id']} to its renamed/moved document by hash: {found}"
            if len(matches) > 1:
                detail += (f" ({len(matches)} byte-identical unclaimed files found; "
                           f"linked the first.)")
                ambiguous += 1
            log_activity(e["id"], "Repair", detail=detail)
            fixed += 1
        if fixed:
            print(f"[Repair] Hash-matched {fixed} record(s) to renamed/moved filed documents "
                  f"({len(broken) - fixed} unmatched, {ambiguous} ambiguous multi-match).")
    except Exception as ex:
        print(f"[Repair] hash-based filed-path self-heal failed: {ex}")

_RELINK_ON_VIEW_MAX = 400   # candidate files hashed per on-demand relink

def _relink_entry_now(entry) -> str | None:
    """On-demand, single-record version of _repair_filed_paths_by_hash: called
    when a record is OPENED and its document path is dead, so a hand-moved
    file heals the moment someone looks at the record instead of at the next
    restart (record #854 sat broken for 46 minutes, report #46). Same
    conservative rules: only a stored file_hash is trusted, only the dead
    path's parent + sibling folders are searched (most recently modified
    first, capped), holding areas are off-limits, and a file another record
    or queue row claims is never stolen. DB-only; never touches files.
    Returns the healed path, or None."""
    try:
        # Either hash identifies the record's file: file_hash is the original
        # bytes, filed_sha256 the filed bytes after email pages were appended.
        want_hashes = {h for h in (entry.get("file_hash"),
                                   entry.get("filed_sha256")) if h}
        dead = entry.get("filed_path") or entry.get("pdf_path") or ""
        if not (want_hashes and dead):
            return None
        parent = os.path.dirname(dead)
        grandparent = os.path.dirname(parent)
        if not (grandparent and os.path.isdir(grandparent)):
            return None
        claimed = set()
        for e in get_all_entries():
            if e["id"] == entry["id"]:
                continue
            for key in ("filed_path", "pdf_path"):
                p = e.get(key) or ""
                if p:
                    claimed.add(os.path.normcase(p))
        for _sid, d in _queue_items():
            p = d.get("pdf_path") or ""
            if p:
                claimed.add(os.path.normcase(p))
        order = [parent]
        try:
            sibs = [os.path.join(grandparent, d) for d in os.listdir(grandparent)]
            sibs = [d for d in sibs if os.path.isdir(d)
                    and os.path.normcase(d) != os.path.normcase(parent)
                    and os.path.basename(d).lower() not in _HASH_REPAIR_OFF_LIMITS_DIRS]
            sibs.sort(key=lambda d: os.path.getmtime(d), reverse=True)
            order += sibs
        except OSError:
            pass
        hashed = 0
        for top in order:
            for root, dirs, files in os.walk(top):
                dirs[:] = [d for d in dirs
                           if d.lower() not in _HASH_REPAIR_OFF_LIMITS_DIRS]
                for name in files:
                    if hashed >= _RELINK_ON_VIEW_MAX:
                        return None
                    if os.path.splitext(name)[1].lower() not in _HASH_REPAIR_DOC_EXTS:
                        continue
                    full = os.path.join(root, name)
                    if os.path.normcase(full) in claimed:
                        continue
                    try:
                        h = _sha256_file(full)
                    except OSError:
                        continue
                    hashed += 1
                    if h in want_hashes:
                        update_entry(entry["id"], {"filed_path": full})
                        log_activity(entry["id"], "Repair",
                                     detail=f"Re-linked to its moved/renamed document "
                                            f"by hash when the record was opened: {full}")
                        return full
        return None
    except Exception as ex:
        print(f"[Repair] on-view relink failed for entry {entry.get('id')}: {ex}")
        return None

# Holding areas a queue row must never be re-linked into. _Skipped is a human's
# decision to take the sheet out of the flow — re-linking would put it back in
# front of them; _Duplicates is the same decision made about a repeat scan.
# _Filtered and _Split Originals are machine decisions, so they stay in scope.
_REPAIR_OFF_LIMITS_DIRS = ("_skipped", "_duplicates", "scan archive")

def _repair_queue_paths():
    """Startup self-heal: re-link queue rows whose scan file was moved out from
    under them.

    A pending_scans row owns a pdf_path, but other subsystems move the file it
    names — the junk filter / whole-queue reclassify moves a doc into _Filtered
    and records it there, without removing the queue row. The row then points at
    nothing: the in-tray shows an error notice instead of a document, and a
    confirm would file a record whose paper can't be found. One CONSERVATIVE
    rule only (no fuzzy matching): re-link when EXACTLY ONE file with the same
    basename exists under the watched trees and no one else owns it. Each file
    is claimed at most once; only the DB is touched, never files. Idempotent and
    cheap when nothing is broken, so it runs on every boot — this must run inside
    the app's process (SQLite WAL forbids writing the database from another
    machine over the share)."""
    try:
        items = _queue_items()
        broken = [(sid, d) for sid, d in items
                  if (d.get("pdf_path") or "") and not os.path.exists(d["pdf_path"])]
        if not broken:
            return
        cfg = load_config()
        by_name, ambiguous = {}, set()
        for base in (cfg.get("scan_folder", ""), cfg.get("email_folder", "")):
            if not (base and os.path.isdir(base)):
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d.lower() not in _REPAIR_OFF_LIMITS_DIRS]
                for name in files:
                    key = name.lower()
                    if key in by_name:
                        ambiguous.add(key)   # same name in two places — can't tell which is ours
                    by_name[key] = os.path.join(root, name)
        # Every path already spoken for: other queue rows, and both the staged
        # and filed path of every record. Stealing one would break that owner.
        claimed = {os.path.normcase(d["pdf_path"]) for _sid, d in items if d.get("pdf_path")}
        for e in get_all_entries():
            for key in ("pdf_path", "filed_path"):
                if e.get(key):
                    claimed.add(os.path.normcase(e[key]))
        fixed = 0
        for sid, d in broken:
            name = os.path.basename(d["pdf_path"]).lower()
            if name in ambiguous:
                continue
            cand = by_name.get(name)
            if not cand or os.path.normcase(cand) in claimed:
                continue
            with _queue_lock:
                live = pending_scans.get(sid)
                if live is None:
                    continue                 # confirmed/skipped while we walked the tree
                live["pdf_path"] = cand
                blob = json.dumps({k: v for k, v in live.items() if k != "_processing"})
            save_pending_scan(sid, blob)
            claimed.add(os.path.normcase(cand))
            fixed += 1
        if fixed:
            log_activity(None, "Repair",
                         detail=f"Re-linked {fixed} queued sheet(s) whose scan file had been "
                                f"moved on disk ({len(broken) - fixed} unmatched).")
        print(f"[Repair] Re-linked {fixed} queued sheet(s) to moved scan files "
              f"({len(broken) - fixed} still missing).")
    except Exception as ex:
        print(f"[Repair] queue-path self-heal failed: {ex}")

def _startup_maintenance():
    """Background startup chain, in dependency order: first re-link any records
    whose filed document was renamed on disk (by name, then — for the renames
    that defeat the name rule — by hash) and any queue rows whose scan file
    was moved, THEN hash documents (hashing must see the corrected paths)."""
    _repair_filed_paths()
    _repair_filed_paths_by_hash()
    _repair_queue_paths()
    _backfill_file_hashes()

# ── Startup log (restart visibility) ───────────────────────────────────────────
# One line per app start in STARTUP_LOG.txt, next to app.py, so anyone — Karen,
# Joshua, or an AI session on the workstation — can see when the app last
# restarted and which code version it picked up, instead of guessing.
_STARTUP_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "STARTUP_LOG.txt")
_STARTUP_LOG_KEEP = 200   # starts to retain; older lines roll off

def _code_version() -> str:
    """Short commit id of the code on disk, read straight from .git (the server
    has no git binary). Empty string if unreadable — never raises."""
    try:
        git_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".git")
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head[:7]                       # detached HEAD holds the hash
        ref = head.split(None, 1)[1]
        ref_path = os.path.join(git_dir, *ref.split("/"))
        if os.path.exists(ref_path):
            with open(ref_path, encoding="utf-8") as f:
                return f.read().strip()[:7]
        packed = os.path.join(git_dir, "packed-refs")
        if os.path.exists(packed):
            with open(packed, encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0][:7]
        return ""
    except OSError:
        return ""

# Captured once at import — the commit THIS running process started on, and when.
# Settings shows it next to the Restart button and compares against a fresh
# _code_version() read so a landed commit shows up as "restart to pick it up".
# (Reads .git HEAD, so uncommitted working-tree edits don't move either value.)
_RUNNING_CODE = _code_version() or "unknown"
_APP_STARTED = datetime.now().strftime("%Y-%m-%d %H:%M")

def _log_startup():
    """Append this start to STARTUP_LOG.txt (trimmed to the last
    _STARTUP_LOG_KEEP starts). Best-effort: a failed write never blocks boot."""
    try:
        ver = _code_version() or "unknown"
        line = (f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  app started  "
                f"code {ver}\n")
        lines = []
        if os.path.exists(_STARTUP_LOG_PATH):
            with open(_STARTUP_LOG_PATH, encoding="utf-8") as f:
                lines = f.readlines()[-(_STARTUP_LOG_KEEP - 1):]
        with open(_STARTUP_LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines + [line])
        print(f"[App] {line.strip()}")
    except Exception as e:
        print(f"[App] could not write startup log: {e}")

def _refile_to_convention(entry_id: int):
    """Re-name an already-filed document to its current convention filename — e.g.
    after a payment adds CK#13386 to the name. Renames within the same folder and
    updates filed_path. Best-effort: never raises into the caller (a failed rename
    must not block recording the payment)."""
    try:
        entry = get_entry(entry_id)
        if not entry:
            return
        old = entry.get("filed_path") or ""
        if not old or not os.path.exists(old):
            return
        # Report #90 (Nancy, 8/5): rename ONLY documents the app itself filed —
        # anything under the archive trees (Files\..., Documents\...) is STAFF'S
        # OWN filing. Renaming Nancy's hand-placed incoming scan to the paid-form
        # name broke that folder's naming convention and startled her; their
        # files are theirs.
        own_tree = os.path.normcase(os.path.abspath(
            load_config().get("filed_folder") or "")).rstrip("\\")
        if not own_tree or not os.path.normcase(
                os.path.abspath(old)).startswith(own_tree + os.sep):
            return
        folder = os.path.dirname(old)
        ext = os.path.splitext(old)[1] or ".pdf"
        new_base = os.path.splitext(_make_filename(entry, folder))[0]
        new_path = os.path.join(folder, new_base + ext)
        if os.path.normcase(new_path) == os.path.normcase(old):
            return  # already named correctly
        if os.path.exists(new_path):
            new_path = os.path.join(folder, f"{new_base} ({entry_id}){ext}")
        filing_ledger.announce(new_path)
        os.rename(old, new_path)
        update_entry(entry_id, {"filed_path": new_path})
    except Exception as e:
        print(f"[App] Could not re-file entry {entry_id}: {e}")

# ── Automated backups ──────────────────────────────────────────────────────────
_BACKUP_RETENTION_DAYS = 30

# Secrets stripped from the daily config backup (2026-07-27 review, GH #12,
# Joshua's call 07-29): the backup exists so entities/properties/bill types/
# mailbox settings survive a config wipe -- the API key, mailbox passwords, and
# staff password hashes don't belong in a second location on disk (any drive
# backup or sync that picks up Backups/ would inherit them). On restore, secrets
# are re-entered: API key on Settings->Intake, mailbox passwords on the mail
# tab, staff passwords via reset.
_CONFIG_SECRET_KEYS = ("claude_api_key", "connector_token", "night_shift_token")

def _write_redacted_config_backup(dest_path):
    cfg = load_config()
    for key in _CONFIG_SECRET_KEYS:
        if cfg.get(key):
            cfg[key] = "REDACTED-REENTER-ON-RESTORE"
    for acct in cfg.get("mail_accounts", []):
        if acct.get("password"):
            acct["password"] = "REDACTED-REENTER-ON-RESTORE"
    for member in cfg.get("staff", []):
        if member.get("password"):
            member["password"] = "REDACTED-RESET-ON-RESTORE"
    tmp = dest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=True)
    os.replace(tmp, dest_path)

def _write_daily_backup(backup_dir=None):
    """Write today's backup of intake.db (SQLite online-backup API — safe while
    the app is running) and config.json into Backups/auto, unless today's
    already exists. Prunes backups older than the retention window. Returns the
    backup path when one was written, else None."""
    import sqlite3 as _sq
    from database import DB_PATH
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "Backups", "auto")
    os.makedirs(backup_dir, exist_ok=True)
    tag = date.today().strftime("%Y%m%d")
    db_dest = os.path.join(backup_dir, f"intake_{tag}.db")
    if os.path.exists(db_dest):
        return None
    src = _sq.connect(DB_PATH, timeout=30)
    try:
        dst = _sq.connect(db_dest + ".tmp")
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(db_dest + ".tmp", db_dest)
    try:
        _write_redacted_config_backup(os.path.join(backup_dir, f"config_{tag}.json"))
    except Exception as e:
        print(f"[Backup] config copy failed: {e}")
    # Retention: prune only files matching OUR naming pattern, nothing else.
    cutoff = (date.today() - timedelta(days=_BACKUP_RETENTION_DAYS)).strftime("%Y%m%d")
    for name in os.listdir(backup_dir):
        m = re.match(r"^(?:intake|config)_(\d{8})\.(?:db|json)$", name)
        if m and m.group(1) < cutoff:
            try:
                os.remove(os.path.join(backup_dir, name))
            except OSError:
                pass
    return db_dest

def _backup_loop():
    """One backup per calendar day, whenever the app happens to be up — the
    server has no guaranteed quiet hour, and the online-backup API doesn't
    block writers. Checks hourly; the daily file-exists test makes it cheap."""
    while True:
        try:
            written = _write_daily_backup()
            if written:
                print(f"[Backup] Daily backup written: {os.path.basename(written)}")
        except Exception as e:
            print(f"[Backup] failed: {e}")
        try:
            # Same hourly ride-along: today's queue depth for the tray gauge.
            # Last write of the day is that day's closing depth.
            items = _queue_items()
            record_queue_depth(date.today().isoformat(), len(items),
                               sum(1 for _, d in items if _scan_is_overdue(d)))
        except Exception as e:
            print(f"[Tray] depth snapshot failed: {e}")
        time.sleep(3600)

def _email_poll_loop():
    """Background: pull new mail attachments every few minutes, from every
    enabled account in mail_accounts. Reads config fresh each cycle, so a
    mailbox added in Settings takes effect without a restart. No-ops silently
    until at least one is configured."""
    import time
    while True:
        try:
            cfg = load_config()
            pulled, err = email_poller.poll_email_once(
                cfg, cfg.get("email_folder", ""), is_email_processed, mark_email_processed)
            if err:
                print(f"[Email] poll error: {err}")
            elif pulled:
                print(f"[Email] pulled {pulled} attachment(s) from mail accounts")
        except Exception as e:
            print(f"[Email] loop error: {e}")
        time.sleep(300)   # every 5 minutes

_MAIL_ARCHIVE_EVERY = 900   # seconds between incremental mailbox syncs

def _mail_archive_loop():
    """Background: keep the local mail archive (/mail, Nancy's all-mailbox
    search) current, gently. Each cycle is incremental — IMAP peeks headers
    only for already-archived mail, POP3 skips known UIDLs outright — and
    every NEWLY archived message is matched to its records, so second-mailbox
    copies and vendor re-sends land at the back of the filed PDFs and raise
    the attention flag (email_append.process_new_messages). The first fill of
    an empty archive is the heavy run (every message since mail_archive_since);
    it normally happens once via /mail's Sync button or the sync-mail CLI —
    but if neither ran, this loop does it, just slower and in the background."""
    import time
    time.sleep(180)   # let startup (watchers, sweeps, reindex) settle first
    while True:
        try:
            cfg = load_config()
            if email_poller.configured_accounts(cfg):
                new_ids = []
                summary = mail_archive.sync_all(
                    cfg, since=mail_archive.incremental_since(
                        cfg.get("mail_archive_since") or mail_archive.DEFAULT_SINCE),
                    on_new=new_ids.append)
                if summary and new_ids:
                    email_append.process_new_messages(new_ids, cfg)
                if mail_archive.blobless_count() > 0:
                    # Go back for attachment copies of mail archived before
                    # the copy store existed (2026-08-10). One attempt per
                    # message; a clean pass marks its stragglers, so this
                    # converges and then no-ops forever.
                    mail_archive.rehydrate_all(cfg)
        except Exception as e:
            print(f"[MailArchive] loop error: {e}")
        time.sleep(_MAIL_ARCHIVE_EVERY)

class _AccessLog:
    """Minimal WSGI access log (waitress doesn't log requests): address, method,
    path, status. Skips the every-10-seconds pending-count poll and static
    files, which made the old werkzeug log mostly noise."""
    _SKIP = ("/api/pending_count", "/static")

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if any(path.startswith(s) for s in self._SKIP):
            return self.wsgi(environ, start_response)
        holder = {}
        def _sr(status, headers, exc_info=None):
            holder["status"] = status.split(" ", 1)[0]
            return start_response(status, headers, exc_info)
        result = self.wsgi(environ, _sr)
        print(f'{environ.get("REMOTE_ADDR", "-")} [{datetime.now():%d/%b/%Y %H:%M:%S}] '
              f'"{environ.get("REQUEST_METHOD", "?")} {path}" {holder.get("status", "?")}')
        return result

# ── Server-side maintenance CLI ───────────────────────────────────────────────
def _rollback_renames_cli(argv):
    """rollback-renames <reviewed_csv> [apply] — undo pay-time renames of
    staff-placed archive files (report #90, ROLLBACK_RENAMES.csv workflow).

    Processes only rows whose action column says ROLLBACK and whose keep_as_is
    is blank, and re-validates EVERY row against the live record + disk at run
    time (the csv is an approval list, not a source of truth): the record must
    still carry the csv's current_name, the file must exist, and the restore
    name must be free — anything else is skipped with a printed reason, never
    guessed. Each applied row: os.rename back to the original name, filed_path
    (and pdf_path when it pointed at the renamed file) re-pointed, matching
    attachment rows re-pointed, an activity entry, and a crash-safe per-row
    append to a timestamped ROLLBACK_UNDO_*.csv beside the input csv — the
    operation is reversible by feeding that undo file back through this same
    command. A db failure after a rename renames the file BACK, so disk and
    record never disagree. NOTE: the serving app is a separate process, so its
    cabinet watcher WILL see these renames (filing_ledger is per-process); it
    converges on the same paths but may add its own re-link activity rows —
    prefer a quiet moment. Without 'apply' prints the would-do list. Run ON
    the server (LACKSSERVER2021 rule)."""
    import csv as _csv
    if not argv:
        print("Usage: python app.py rollback-renames <reviewed_csv> [apply]")
        return
    csv_path = argv[0]
    do_apply = len(argv) > 1 and argv[1].lower() == "apply"
    if not os.path.exists(csv_path):
        print(f"  No such csv: {csv_path}")
        return
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.DictReader(f))
    # Each run writes its OWN timestamped ledger (2026-08-06 review): appending
    # reversals to the file being replayed would accumulate generations and
    # ping-pong on re-runs.
    _undo_base = os.path.join(
        os.path.dirname(os.path.abspath(csv_path)),
        f"ROLLBACK_UNDO_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    undo_path, _n = _undo_base + ".csv", 2
    while os.path.exists(undo_path):        # two runs in one second stay separate
        undo_path = f"{_undo_base}-{_n}.csv"
        _n += 1
    _UNDO_FIELDS = ["record_id", "current_name", "restore_name", "action",
                    "keep_as_is", "rolled_back_at"]

    def _append_undo(rec):
        # Per-row append (crash-safe): a mid-run failure must not lose the undo
        # trail of renames already applied.
        new_file = not os.path.exists(undo_path)
        with open(undo_path, "a", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=_UNDO_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(rec)

    if do_apply:
        print("  Note: if the app is serving right now, its cabinet watcher sees "
              "these renames as file moves and may add its own re-link activity "
              "rows — harmless (it converges on the same paths), just noisy. "
              "A quiet moment is nicer.")
    done = skipped = ignored = 0
    claimed = set()          # normcase targets claimed THIS run (dry and apply)
    for row in rows:
        rid = row.get("record_id") or ""
        action = (row.get("action") or "").strip().upper()
        if (row.get("keep_as_is") or "").strip():
            print(f"  #{rid}: kept as-is (keep_as_is marked)")
            ignored += 1
            continue
        if not action.startswith("ROLLBACK"):
            # Never silently drop a reviewed row — hand-review items especially
            # must stay visible in the run summary.
            if action.startswith("HAND-REVIEW"):
                print(f"  #{rid}: left for hand review — {row.get('current_name', '')!r}")
            ignored += 1
            continue

        def skip(reason):
            print(f"  #{rid}: SKIP — {reason}")

        try:
            entry = get_entry(int(rid))
        except (TypeError, ValueError):
            entry = None
        cur_name = (row.get("current_name") or "").strip()
        new_name = (row.get("restore_name") or "").strip()
        if not entry or not cur_name or not new_name:
            skip("record missing or csv row incomplete"); skipped += 1; continue
        old_path = entry.get("filed_path") or ""
        if os.path.basename(old_path) != cur_name:
            skip(f"record changed since review (now {os.path.basename(old_path)!r})")
            skipped += 1; continue
        if not longpaths.exists(old_path):
            skip("file not found on disk"); skipped += 1; continue
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        case_only = os.path.normcase(new_path) == os.path.normcase(old_path)
        if os.path.normcase(new_path) in claimed:
            skip(f"restore name already claimed by an earlier row this run: {new_name!r}")
            skipped += 1; continue
        # exists() is case-insensitive on Windows — a case-only rename is legal
        # and must not read as "occupied".
        if not case_only and longpaths.exists(new_path):
            skip(f"restore name occupied: {new_name!r} — resolve by hand")
            skipped += 1; continue
        claimed.add(os.path.normcase(new_path))
        if not do_apply:
            print(f"  #{rid}: would rename {cur_name!r} -> {new_name!r}")
            done += 1; continue
        try:
            filing_ledger.announce(new_path)
            os.rename(longpaths.extended(old_path), longpaths.extended(new_path))
        except OSError as e:
            skip(f"rename failed: {e}"); skipped += 1; continue
        # Everything after the rename must not abort the run — and on a db
        # failure the file goes BACK so disk and record never disagree.
        try:
            updates = {"filed_path": new_path}
            if os.path.normcase(entry.get("pdf_path") or "") == os.path.normcase(old_path):
                updates["pdf_path"] = new_path
            update_entry(entry["id"], updates)
            for att in get_attachments(entry["id"]):
                if os.path.normcase(att.get("file_path") or "") == os.path.normcase(old_path):
                    update_attachment_path(att["id"], new_path)
            log_activity(entry["id"], "Rename rolled back (report #90)", "Server-CLI",
                         f"{cur_name} -> {new_name}")
            _append_undo({"record_id": entry["id"], "current_name": new_name,
                          "restore_name": cur_name, "action": "ROLLBACK",
                          "keep_as_is": "",
                          "rolled_back_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        except Exception as e:
            try:
                os.rename(longpaths.extended(new_path), longpaths.extended(old_path))
                skip(f"db update failed ({e}) — file renamed BACK, row untouched")
            except OSError as e2:
                print(f"  #{rid}: ERROR — db update failed ({e}) AND the file could "
                      f"not be renamed back ({e2}); now at {new_path!r} while the "
                      f"record still says {cur_name!r} — fix by hand or re-run.")
            skipped += 1
            continue
        print(f"  #{rid}: renamed {cur_name!r} -> {new_name!r}")
        done += 1
    if do_apply and done:
        print(f"  Undo ledger: {undo_path} — feed it back through this command "
              f"to reverse this run.")
    print(f"  {'Rolled back' if do_apply else 'Would roll back'} {done}; "
          f"skipped {skipped}; left alone {ignored} (skip/hand-review/keep rows).")


def _refile_intruders_cli(argv):
    """refile-intruders <reviewed_csv> [apply] — re-date and re-file the
    app-made intake-shape intruders (the 08. AUGUST incident: backlog scans
    confirmed under the confirm-day date and the global name shape, in folders
    with their own grammar — FOLDER_NAMING_STANDARDS.md §1).

    Sibling of rollback-renames, same discipline: only rows whose action says
    REFILE and whose keep_as_is is blank; the csv is an APPROVAL list, and
    every row re-validates against the live record + disk at run time (the
    record must still carry the csv's current_name, the file must exist, the
    destination must validate into the allowed trees, the target name must be
    free). proposed_name comes from the reviewed csv verbatim (like rollback's
    restore_name) so replaying the undo ledger restores the exact old name.
    Each applied row: file moved to new_dir under proposed_name, date_received
    set to new_date (when given — record numbers never change, by design),
    filed_path / pdf_path / attachment paths re-pointed, an activity entry,
    and a crash-safe per-row append to a timestamped REFILE_UNDO_*.csv beside
    the input — feed that file back through this command to reverse the run.
    A db failure after the move renames the file BACK so disk and record never
    disagree. Run ON the server (LACKSSERVER2021 rule)."""
    import csv as _csv
    if not argv:
        print("Usage: python app.py refile-intruders <reviewed_csv> [apply]")
        return
    csv_path = argv[0]
    do_apply = len(argv) > 1 and argv[1].lower() == "apply"
    if not os.path.exists(csv_path):
        print(f"  No such csv: {csv_path}")
        return
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.DictReader(f))
    cfg = load_config()
    _undo_base = os.path.join(
        os.path.dirname(os.path.abspath(csv_path)),
        f"REFILE_UNDO_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    undo_path, _n = _undo_base + ".csv", 2
    while os.path.exists(undo_path):
        undo_path = f"{_undo_base}-{_n}.csv"
        _n += 1
    _UNDO_FIELDS = ["record_id", "current_name", "new_dir", "new_date",
                    "proposed_name", "action", "keep_as_is", "refiled_at"]

    def _append_undo(rec):
        new_file = not os.path.exists(undo_path)
        with open(undo_path, "a", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=_UNDO_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(rec)

    done = skipped = ignored = 0
    claimed = set()
    for row in rows:
        rid = row.get("record_id") or ""
        action = (row.get("action") or "").strip().upper()
        if (row.get("keep_as_is") or "").strip():
            print(f"  #{rid}: kept as-is (keep_as_is marked)")
            ignored += 1
            continue
        if not action.startswith("REFILE"):
            if action.startswith("HAND-REVIEW"):
                print(f"  #{rid}: left for hand review — {row.get('current_name', '')!r}")
            ignored += 1
            continue

        def skip(reason):
            print(f"  #{rid}: SKIP — {reason}")

        try:
            entry = get_entry(int(rid))
        except (TypeError, ValueError):
            entry = None
        cur_name = (row.get("current_name") or "").strip()
        new_name = (row.get("proposed_name") or "").strip()
        new_dir_raw = (row.get("new_dir") or "").strip()
        if not entry or not cur_name or not new_name or not new_dir_raw:
            skip("record missing or csv row incomplete"); skipped += 1; continue
        # The name is used as reviewed — but never a path, never illegal chars.
        stem, ext = os.path.splitext(new_name)
        safe_stem = filing.sanitize_filename(stem)
        if not safe_stem or safe_stem != stem or ext.lower() not in (
                ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".docx", ".doc"):
            skip(f"proposed name failed validation: {new_name!r}")
            skipped += 1; continue
        # validate_filed_dir allows a legal-but-not-yet-created month folder
        # (created at apply time below); resolve_client_dir additionally
        # understands the same folder written under another desk's drive
        # letter or the UNC share, but requires it to exist.
        new_dir = (filing.validate_filed_dir(new_dir_raw, cfg)
                   or filing.resolve_client_dir(new_dir_raw, cfg))
        if not new_dir:
            skip(f"destination not in the allowed trees: {new_dir_raw!r}")
            skipped += 1; continue
        new_date = (row.get("new_date") or "").strip()
        if new_date:
            try:
                nd = date.fromisoformat(new_date)
            except ValueError:
                skip(f"new_date isn't ISO: {new_date!r}"); skipped += 1; continue
            if nd > date.today():
                skip(f"new_date is in the future: {new_date!r}"); skipped += 1; continue
        old_path = entry.get("filed_path") or ""
        if os.path.basename(old_path) != cur_name:
            skip(f"record changed since review (now {os.path.basename(old_path)!r})")
            skipped += 1; continue
        if not longpaths.exists(old_path):
            skip("file not found on disk"); skipped += 1; continue
        new_path = os.path.join(new_dir, new_name)
        same_place = os.path.normcase(new_path) == os.path.normcase(old_path)
        date_change = bool(new_date) and new_date != (entry.get("date_received") or "")
        if same_place and not date_change:
            skip("already filed and dated as proposed — nothing to do")
            ignored += 1; continue
        if os.path.normcase(new_path) in claimed:
            skip(f"target already claimed by an earlier row this run: {new_name!r}")
            skipped += 1; continue
        if not same_place and longpaths.exists(new_path):
            skip(f"target name occupied: {new_name!r} — resolve by hand")
            skipped += 1; continue
        claimed.add(os.path.normcase(new_path))
        if not do_apply:
            what = []
            if not same_place:
                what.append(f"move to {new_path!r}")
            if date_change:
                what.append(f"re-date {entry.get('date_received')!r} -> {new_date!r}")
            print(f"  #{rid}: would " + " and ".join(what))
            done += 1; continue
        moved = not same_place
        if moved:
            try:
                os.makedirs(new_dir, exist_ok=True)
                filing_ledger.announce(new_path)
                os.rename(longpaths.extended(old_path), longpaths.extended(new_path))
            except OSError as e:
                skip(f"move failed: {e}"); skipped += 1; continue
        try:
            updates = {"filed_path": new_path}
            if os.path.normcase(entry.get("pdf_path") or "") == os.path.normcase(old_path):
                updates["pdf_path"] = new_path
            if date_change:
                updates["date_received"] = new_date
            update_entry(entry["id"], updates)
            for att in get_attachments(entry["id"]):
                if os.path.normcase(att.get("file_path") or "") == os.path.normcase(old_path):
                    update_attachment_path(att["id"], new_path)
            detail = f"{cur_name} -> {new_path}"
            if date_change:
                detail += (f"; date received corrected "
                           f"{entry.get('date_received')} -> {new_date}")
            log_activity(entry["id"], "Re-filed per folder standards (08. AUGUST incident)",
                         "Server-CLI", detail)
            _append_undo({"record_id": entry["id"], "current_name": new_name,
                          "new_dir": os.path.dirname(old_path),
                          "new_date": (entry.get("date_received") or "") if date_change else "",
                          "proposed_name": cur_name, "action": "REFILE",
                          "keep_as_is": "",
                          "refiled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        except Exception as e:
            if moved:
                try:
                    os.rename(longpaths.extended(new_path), longpaths.extended(old_path))
                    skip(f"db update failed ({e}) — file moved BACK, row untouched")
                except OSError as e2:
                    print(f"  #{rid}: ERROR — db update failed ({e}) AND the file "
                          f"could not be moved back ({e2}); now at {new_path!r} while "
                          f"the record still says {old_path!r} — fix by hand or re-run.")
            else:
                skip(f"db update failed ({e}) — nothing moved, row untouched")
            skipped += 1
            continue
        print(f"  #{rid}: {'moved' if moved else 'updated'} -> {new_path!r}"
              + (f" (received {new_date})" if date_change else ""))
        done += 1
    if do_apply and done:
        print(f"  Undo ledger: {undo_path} — feed it back through this command "
              f"to reverse this run.")
    print(f"  {'Re-filed' if do_apply else 'Would re-file'} {done}; "
          f"skipped {skipped}; left alone {ignored} (skip/hand-review/keep rows).")


def _archive_filed_convert_cli(argv):
    """One-shot for a cabinet-watch flood that predates the archive-backfill
    rule (2026-08-05: a 2024 statement re-export became 107 Pending records
    plus 24 review holds). Re-labels that day's Cabinet-Watch registrations
    under a path prefix as the archive layer they are — status Filed,
    received date = the document's PERIOD, doc type from the folder — and
    registers-then-clears the matching hand-filed review holds. Record
    numbers are never rewritten (the number is a label; database.py rule).

        python app.py archive-filed-convert <YYYY-MM-DD> <path-prefix> [apply]

    Dry run unless the last argument is `apply`. APPLY, THEN RESTART: the
    running app's review queue lives in memory and rebuilds from the DB at
    startup, so run this immediately before the restart that activates the
    accompanying code."""
    import json as _json
    import nameparse
    from database import get_conn
    if len(argv) < 2:
        print("Usage: python app.py archive-filed-convert <YYYY-MM-DD> "
              "<path-prefix> [apply]")
        return
    day, prefix = argv[0], argv[1]
    do_apply = len(argv) > 2 and argv[2].lower() == "apply"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        print(f"First argument must be a YYYY-MM-DD day, got {day!r}.")
        return
    cfg = load_config()
    nc_prefix = os.path.normcase(os.path.normpath(prefix))

    def under_prefix(p):
        nc = os.path.normcase(os.path.normpath(p or ""))
        return nc == nc_prefix or nc.startswith(nc_prefix + os.sep)

    converted = skipped = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log WHERE processed_by='Cabinet-Watch' "
            "AND status='Pending' AND substr(created_at,1,10)=?",
            (day,)).fetchall()
    for r in (dict(x) for x in rows):
        path = r.get("pdf_path") or r.get("filed_path") or ""
        if not under_prefix(path):
            continue
        doc = nameparse.parse_name(path)
        priors = nameparse.path_priors(path, cfg)
        if not cabinet_watch._is_archive_backfill(priors):
            print(f"  #{r['record_no']}: SKIP — {path!r} is not under an "
                  f"old-year tree; look by hand.")
            skipped += 1
            continue
        fields = cabinet_watch.archive_entry_fields(path, doc, priors,
                                                    r.get("file_hash") or "", cfg)
        changes = {"status": "Filed",
                   "date_received": fields["date_received"],
                   "doc_type": fields["doc_type"],
                   "notes": ((r.get("notes") or "") +
                             f"\n[CONVERTED] archive backfill mis-registered as "
                             f"Pending intake on {day}; re-labeled Filed with "
                             f"period date {fields['date_received']}.").strip()}
        print(f"  #{r['record_no']}: {r['status']} {r['date_received']} "
              f"{r['doc_type']!r} -> Filed {changes['date_received']} "
              f"{changes['doc_type']!r}")
        if do_apply:
            update_entry(r["id"], changes)
            log_activity(r["id"], "Converted", "archive-filed-convert",
                         f"Pending intake of {day} -> Filed archive record "
                         f"(period {changes['date_received']})")
        converted += 1

    held_done = held_left = 0
    with get_conn() as conn:
        srows = conn.execute(
            "SELECT scan_id, data FROM pending_scans "
            "WHERE substr(created_at,1,10)=?", (day,)).fetchall()
    for sid, data_json in ((x["scan_id"], x["data"]) for x in srows):
        try:
            d = _json.loads(data_json)
        except ValueError:
            continue
        path = d.get("pdf_path") or ""
        if not d.get("hand_filed") or not under_prefix(path):
            continue
        owners = get_entries_by_hash(d.get("file_hash") or "")
        if owners:
            print(f"  hold {sid}: bytes already registered as "
                  f"#{owners[0].get('record_no')} — clearing the hold")
            if do_apply:
                queue_state.queue_remove(sid)
            held_done += 1
            continue
        if not longpaths.exists(path):
            print(f"  hold {sid}: LEFT IN QUEUE — file no longer at {path!r} "
                  f"(renamed since the hold?); a person decides.")
            held_left += 1
            continue
        doc = nameparse.parse_name(path)
        priors = nameparse.path_priors(path, cfg)
        if not cabinet_watch._is_archive_backfill(priors):
            print(f"  hold {sid}: LEFT IN QUEUE — not an old-year tree: {path!r}")
            held_left += 1
            continue
        fields = cabinet_watch.archive_entry_fields(
            path, doc, priors, d.get("file_hash") or _sha256_file(path), cfg)
        print(f"  hold {sid}: register Filed {fields['date_received']} "
              f"{fields['doc_type']!r} + clear — {os.path.basename(path)[:60]}")
        if do_apply:
            new_id = insert_entry(fields)
            log_activity(new_id, "Created", "archive-filed-convert",
                         f"review hold {sid} of {day} was archive backfill; "
                         f"registered Filed (period {fields['date_received']}): {path}")
            queue_state.queue_remove(sid)
        held_done += 1

    verb = "Converted" if do_apply else "Would convert"
    print(f"  {verb} {converted} record(s); {skipped} skipped. "
          f"Review holds: {held_done} cleared/registered, {held_left} left for a person.")
    if do_apply:
        print("  RESTART THE APP NOW — the running review queue still holds "
              "the cleared items in memory until it reloads from the DB.")
    else:
        print("  Dry run only — append `apply` to write.")


def _cli_reports(argv):
    """Maintenance commands that run IN the app process, so writes are made by
    the server that owns intake.db — never a direct workstation write over the
    share (the LACKSSERVER2021 rule). Run on the server:
        python app.py list-reports [all]
        python app.py resolve-report <id> [<id> ...]
        python app.py merge-record <dup_id> <keeper_id>
        python app.py reindex [force]
        python app.py scrub-none [apply]
        python app.py migrate-unit-accounts [apply]
        python app.py rollback-renames <reviewed_csv> [apply]
        python app.py refile-intruders <reviewed_csv> [apply]
        python app.py archive-filed-convert <YYYY-MM-DD> <path-prefix> [apply]
        python app.py sync-mail [since]
        python app.py append-emails [apply]
    Returns True when it handled a command (caller then exits without serving)."""
    if not argv:
        return False
    cmd = argv[0]
    if cmd == "reindex":
        # Same incremental full-text (re)index the Settings page's Reindex button
        # runs, from the command line — for a server visit where Tesseract was
        # just installed/fixed and nobody wants to open the app to click it.
        cfg = load_config()
        tpath = cfg.get("tesseract_path", "")
        if tpath and not os.path.exists(tpath):
            print(f"  Warning: config.json tesseract_path is set to {tpath!r} but "
                  f"that file doesn't exist — OCR will still show ocr-unavailable.")
        elif not tpath and not shutil.which("tesseract"):
            print("  Note: no tesseract_path in config.json and 'tesseract' isn't on "
                  "PATH either — image-only docs will index as ocr-unavailable. "
                  "Text-layer PDFs still index fine.")
        force = len(argv) > 1 and argv[1].lower() == "force"
        summary = indexer.reindex(tesseract_path=tpath, force=force)
        print(f"  Reindex done: {summary}")
        return True
    if cmd == "rollback-renames":
        _rollback_renames_cli(argv[1:])
        return True
    if cmd == "backfill-emails":
        # DRY RUN only from the command line: apply must run inside the live
        # app (Settings → App → Retro-attach source emails), because queue
        # items live in the running app's memory and a CLI write underneath it
        # would be overwritten on the app's next save. Mailboxes are read-only
        # either way (IMAP PEEK / POP3 TOP — never RETR, the MagicMail lesson).
        since = argv[1] if len(argv) > 1 else email_backfill.DEFAULT_SINCE
        email_backfill.run(load_config(), apply=False, since=since)
        print("  Dry run only — click 'Retro-attach source emails' in "
              "Settings → App to apply.")
        return True
    if cmd == "sync-mail":
        # Pull every mailbox into the local mail archive (/mail). Read-only on
        # the mail side (IMAP PEEK / POP3 TOP+UIDL — never the fetch verb that
        # marks mail read) and additive on the archive side, so it can run from
        # any desk. It does NOT touch records or filed files — the append/flag
        # pass runs inside the app's sync loop, or via append-emails below.
        since = argv[1] if len(argv) > 1 else None
        mail_archive.init_archive()
        summary = mail_archive.sync_all(load_config(), since=since)
        if summary is None:
            print("  Another sync is already running.")
        else:
            for a in summary["accounts"]:
                print(f"  {a['name']} ({a['protocol']}): {a['new']} new of "
                      f"{a['seen']} looked at"
                      + (f" — {a['error']}" if a["error"] else ""))
            print(f"  Archive now holds {mail_archive.status()['total']} messages.")
        return True
    if cmd == "append-emails":
        # Retro pass (reports #55/#106/#114): the source email(s) onto the back
        # of every filed PDF. Dry run by default. `apply` REWRITES filed PDFs
        # and writes intake.db — apply only ON THE SERVER.
        apply = len(argv) > 1 and argv[1].lower() == "apply"
        email_append.retro_append(apply=apply, cfg=load_config())
        if not apply:
            print("  Dry run only — review 'Email append' in Ops Reports, then "
                  "run `python app.py append-emails apply` on the server.")
        return True
    if cmd == "refile-intruders":
        _refile_intruders_cli(argv[1:])
        return True
    if cmd == "archive-filed-convert":
        _archive_filed_convert_cli(argv[1:])
        return True
    if cmd == "list-reports":
        show_all = len(argv) > 1 and argv[1].lower() == "all"
        items = get_bug_reports() if show_all else get_active_reports()
        if not items:
            print("No reports." if show_all else "No open reports.")
            return True
        for r in items:
            ref = (r.get("record_ref") or "").strip()
            desc = " ".join((r.get("description") or "").split())[:60]
            marks = ("!" if r.get("priority") else " ") + ("B" if r.get("bounced_at") else " ")
            print(f"  #{r['id']:<3}{marks} [{r.get('status',''):<12}] {r.get('user',''):<8} "
                  f"{ref + ' - ' if ref else ''}{desc}")
        print("  (! = priority-bumped, B = bounced back 'checked, not yet resolved')")
        return True
    if cmd == "merge-record":
        # Fold a duplicate record of the SAME bill into its keeper — for twins
        # whose BYTES differ (insurer vs agency copy of one invoice), which the
        # hash-based Legacy Duplicates page can never pair. Attachments/splits
        # move to the keeper, the dup's values fill the keeper's blanks, the dup
        # row is deleted, all activity-logged. Refuses when the two records
        # carry DIFFERENT payments (possible double payment — a person looks).
        #     python app.py merge-record <dup_id> <keeper_id>
        if len(argv) != 3:
            print("Usage: python app.py merge-record <dup_id> <keeper_id>")
            return True
        try:
            dup_id, keeper_id = int(argv[1]), int(argv[2])
        except ValueError:
            print("Both ids must be record numbers.")
            return True
        if dup_id == keeper_id:
            print("The two ids must differ (dup first, keeper second).")
            return True
        dup, keeper = get_entry(dup_id), get_entry(keeper_id)
        missing = [rid for rid, r in ((dup_id, dup), (keeper_id, keeper)) if not r]
        if missing:
            for rid in missing:
                print(f"  #{rid}: no such record")
            return True
        if _merge_conflict([dup, keeper]):
            print(f"  NOT merged — #{dup_id} and #{keeper_id} carry different "
                  f"payment details:\n"
                  f"    #{dup_id}: {_payment_sig(dup)}\n"
                  f"    #{keeper_id}: {_payment_sig(keeper)}\n"
                  f"  That smells like the same bill paid twice; resolve it by "
                  f"hand in the app.")
            return True
        _merge_dupe_group([dup, keeper], by="Server-CLI", keeper=keeper)
        print(f"  Merged #{dup_id} into #{keeper_id} "
              f"({keeper.get('vendor') or '?'} — status {keeper.get('status')}). "
              f"The absorbed record's document stays listed on #{keeper_id} as a copy.")
        return True
    if cmd == "scrub-none":
        # One-time repair for rows damaged before the 2026-07-23 entry_form fix:
        # editing a record whose invoice/account/vendor/notes was NULL rendered
        # the literal text "None" into the input, and saving wrote that string
        # back (records 780/791/796, edited 2026-07-22).
        #     python app.py scrub-none          -> list affected rows (dry run)
        #     python app.py scrub-none apply    -> set those fields back to NULL
        from database import get_conn
        fields = ("invoice_number", "account_number", "vendor", "due_date", "notes")
        do_apply = len(argv) > 1 and argv[1].lower() == "apply"
        with get_conn() as conn:
            damaged = {}   # (id, record_no) -> [field, ...]
            for f in fields:
                for r in conn.execute(
                        f"SELECT id, record_no FROM intake_log WHERE {f}='None'"):
                    damaged.setdefault((r["id"], r["record_no"]), []).append(f)
            if not damaged:
                print("No rows carry a literal 'None' — nothing to scrub.")
                return True
            for (rid, rec_no), fs in sorted(damaged.items()):
                print(f"  #{rid} ({rec_no or 'no record #'}): {', '.join(fs)}")
            if not do_apply:
                print(f"{len(damaged)} row(s) affected. Dry run — nothing written. "
                      f"To repair: python app.py scrub-none apply")
                return True
            for f in fields:
                conn.execute(f"UPDATE intake_log SET {f}=NULL WHERE {f}='None'")
            conn.commit()
        for (rid, rec_no), fs in sorted(damaged.items()):
            log_activity(rid, "Edited", "Server-CLI",
                         f"scrubbed literal 'None' back to blank in {', '.join(fs)}")
        print(f"Scrubbed {len(damaged)} row(s) — the 'None' text is NULL again.")
        return True
    if cmd == "resolve-report":
        # Workflow mapping (VALIDATION_WORKFLOW.md): this command means "dev
        # believes it's fixed" — it sets Dev-resolved, never Verified; the
        # human check still happens on /reports. --note carries the required
        # what-I-did/what-to-check text (everything after --note is the note).
        args = argv[1:]
        note = ""
        if "--note" in args:
            i = args.index("--note")
            note = " ".join(args[i + 1:]).strip()
            args = args[:i]
        if not note:
            note = ("Marked Dev-resolved from the server console — no details "
                    "recorded. Check the report's page yourself.")
        ids = []
        for a in args:
            try:
                ids.append(int(a))
            except ValueError:
                print(f"  skipping non-numeric id: {a!r}")
        if not ids:
            print('Usage: python app.py resolve-report <id> [<id> ...] [--note "what was done + what to check"]')
            return True
        existing = {r["id"]: r for r in get_bug_reports()}
        for rid in ids:
            r = existing.get(rid)
            if not r:
                print(f"  #{rid}: no such report")
            elif r.get("status") == "Dev-resolved":
                print(f"  #{rid}: already Dev-resolved (verification pending)")
            elif r.get("status") in ("Verified", "Resolved"):
                print(f"  #{rid}: already closed ({r.get('status')})")
            else:
                mark_report_dev_resolved(rid, "Server-CLI", note)
                print(f"  #{rid}: marked Dev-resolved — staff verification "
                      "still pending (see /reports)")
        return True
    if cmd == "migrate-unit-accounts":
        # Report #20 cleanup: before units existed, a unit number typed into
        # Account # was the only way to record it ("TL222-5"). Now that units are
        # real, move those into the unit link and clear the field -- Account #
        # means the vendor's ongoing account number, and leaving a unit there
        # makes the bill look like it has an account it doesn't.
        #
        # Deliberately narrow: only a value that is exactly "<PROPCODE>-<UNIT>"
        # for a property the record already names, AND whose unit exists. Real
        # account numbers are full of hyphens ("20-5192-00", "1060-3793-12"), so
        # a looser rule would eat live data. Dry-run unless told to apply.
        from database import get_conn, set_entry_units
        apply = len(argv) > 1 and argv[1].lower() == "apply"
        moved = 0
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, record_no, property_code, vendor, account_number
                   FROM intake_log
                   WHERE account_number IS NOT NULL AND account_number != ''"""
            ).fetchall()
            units = {r["unit_code"].upper(): r["id"] for r in
                     conn.execute("SELECT id, unit_code FROM units")}
        for r in rows:
            acct = (r["account_number"] or "").strip()
            named = {c.strip().upper() for c in (r["property_code"] or "").split(",") if c.strip()}
            root = acct.rsplit("-", 1)[0].upper() if "-" in acct else ""
            uid = units.get(acct.upper())
            if not uid or root not in named:
                continue
            print(f"  #{r['id']} ({r['record_no'] or '-'}) {r['vendor'] or ''}: "
                  f"Account # {acct!r} -> unit {acct}")
            moved += 1
            if apply:
                set_entry_units(r["id"], [uid])
                update_entry(r["id"], {"account_number": None})
                log_activity(r["id"], "Edited", "Server-CLI",
                             f"unit {acct} moved out of Account # (report #20)")
        if not moved:
            print("  Nothing to migrate.")
        elif apply:
            print(f"  Migrated {moved} record(s).")
        else:
            print(f"  {moved} record(s) would move. Re-run with "
                  "'migrate-unit-accounts apply' to write.")
        return True
    return False


# ── Scan flow (reports #23/#18/#13) ──────────────────────────────────────────
# /scan page + workstation-connector registry live in scanflow.py; registered
# here (module level, so tests importing app get the routes too). The module
# only OBSERVES the queue — ingest stays with the watcher/sweep paths.
import filing     # "File as" suggestions + destination validation
import scanflow
scanflow.register(app, {
    "load_config": load_config,
    "queue_items": _queue_items,
    "auto_filed": _auto_filed_since,
})
# Connector command channel (reports #34/#22): the desk's connector pops the
# real Windows folder chooser / opens File Explorer on the browser's behalf.
import desk_commands
desk_commands.register(app, sys.modules[__name__])
# Live pickup of documents hand-filed straight into the archive trees
# (reports #28/#46/#50). Observers start in __main__; the route registers here.
import cabinet_watch
cabinet_watch.register(app, sys.modules[__name__])
# Folder browser + search for the filing/move pickers (reports #25/#34/#37/#44).
import folder_index
folder_index.register(app, sys.modules[__name__])
# The Files view: the real cabinet tree with record chips (document manager).
import routes_files
routes_files.register(app, sys.modules[__name__])

@app.template_filter("unc")
def _unc_display_filter(p):
    """House rule (reports #33/#36): staff-facing paths render as full UNC."""
    return filing.display_path(p, load_config())

# ── Route modules (2026-07-29 modular split, GH #6) ───────────────────────────
# Each routes_*.py registers its views on the app object under the ORIGINAL
# endpoint names (add_url_rule, not a Blueprint — a Blueprint would prefix
# every endpoint and break url_for/template references). The app MODULE is
# passed in so extracted routes reach helpers via dynamic attribute lookup,
# keeping test monkeypatching of app attributes effective. Registered here at
# the bottom so the module is fully populated first (no circular-import risk).
import routes_filtered
import routes_home
import routes_karen
import routes_mail
import routes_paper
import routes_bills
import routes_entry
import routes_newlook
import routes_ops_reports
import routes_paid_backfill
import routes_reports
import routes_review
import routes_settings
import routes_units
routes_bills.register(app, sys.modules[__name__])
routes_entry.register(app, sys.modules[__name__])
routes_filtered.register(app, sys.modules[__name__])
routes_newlook.register(app, sys.modules[__name__])
routes_home.register(app, sys.modules[__name__])
routes_karen.register(app, sys.modules[__name__])
routes_mail.register(app, sys.modules[__name__])
routes_paper.register(app, sys.modules[__name__])
routes_ops_reports.register(app, sys.modules[__name__])
routes_paid_backfill.register(app, sys.modules[__name__])
routes_reports.register(app, sys.modules[__name__])
routes_review.register(app, sys.modules[__name__])
routes_settings.register(app, sys.modules[__name__])
routes_units.register(app, sys.modules[__name__])
# Re-export: test_paper_views2 calls klr._prior_payment_match directly.
from routes_paper import _prior_payment_match  # noqa: E402

# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    # Maintenance subcommands run and exit — before _log_startup / watchers /
    # serve, so they never record a fake restart or bind the port.
    if len(sys.argv) > 1 and sys.argv[1] in ("list-reports", "resolve-report",
                                             "merge-record", "reindex",
                                             "scrub-none", "migrate-unit-accounts",
                                             "rollback-renames", "refile-intruders",
                                             "backfill-emails",
                                             "archive-filed-convert"):
        init_db()
        init_property_schema()   # migrate-unit-accounts reads units/entry_units
        _cli_reports(sys.argv[1:])
        sys.exit(0)
    _log_startup()   # first, so a restart is visible even if boot then fails
    init_db()
    migrate_passwords()
    migrate_mail_accounts()
    # Relational core (entities/properties/units/vendors). Additive + idempotent —
    # seeds from config.json + the proxy unit counts; see property_model / DESIGN.md.
    init_property_schema()
    seed_property_model()
    _load_pending_from_db()   # restore the review queue from before the restart
    _load_current_batch()     # resume an in-progress scan batch (don't split it)
    # Self-heal records whose filed document was renamed on disk (background so a
    # large Filed tree never delays startup). See _repair_filed_paths.
    threading.Thread(target=_startup_maintenance, daemon=True).start()
    cfg = load_config()
    scan_folder = cfg.get("scan_folder","Z:\\UNFILED SCANS")
    email_folder = cfg.get("email_folder","")
    for folder in (scan_folder, email_folder):
        if not folder:
            continue
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            print(f"[App] Warning: folder not accessible: {folder}")
            print("[App] Update the path in Settings, then restart.")
    # Physical-mail scans (default source) + email attachments (tagged 'Email').
    threading.Thread(target=start_watcher, args=(scan_folder, enqueue_scan), daemon=True).start()
    if email_folder:
        threading.Thread(target=start_watcher,
                         args=(email_folder, lambda p: enqueue_scan(p, "Email")),
                         daemon=True).start()
    # Background mail poller (no-op until a mailbox is configured in Settings).
    threading.Thread(target=_email_poll_loop, daemon=True).start()
    # Local mail archive (/mail): incremental sync + append/flag pass.
    mail_archive.init_archive()
    threading.Thread(target=_mail_archive_loop, daemon=True).start()
    # Daily on-server backup of intake.db + config.json (Backups/auto, 30 days).
    threading.Thread(target=_backup_loop, daemon=True).start()
    # Catch-up sweep: ingest anything that landed in the watched folders while the app
    # was down, then re-sweep every 10 minutes as the safety net for files the live
    # watcher misses (#42/#48). Background so classification doesn't delay startup.
    threading.Thread(target=_sweep_loop, args=(scan_folder, email_folder),
                     daemon=True).start()
    # Live cabinet watcher: recursive observers over the archive trees pick up
    # hand-filed documents within seconds (reports #28/#46/#50; the Swan
    # CK#3012 lesson — the 6 AM harvest alone left them invisible for hours).
    cabinet_watch.start(cfg)
    # Full-text index of document contents (Option B). Incremental, so this only
    # reads new/changed files; a background thread keeps startup snappy.
    indexer.init_index()
    threading.Thread(target=_run_reindex, daemon=True).start()
    # (Auto-opening a browser tab on the server was removed — it ran on every
    #  restart and is unwanted on a headless/background server.)
    print(f"\n{'='*55}")
    print(f"  Klick Klack running at http://localhost:5050")
    print(f"  Watching for scans in: {scan_folder}")
    if email_folder:
        print(f"  Watching for email PDFs in: {email_folder}")
    print(f"  Press Ctrl+C to stop.")
    print(f"{'='*55}\n")
    try:
        from waitress import serve
        print("  Serving with waitress (production WSGI server).\n")
        # Starvation watchdog (2026-08-11): if every worker blocks, waitress
        # keeps accepting connections and answers none of them — the app looks
        # dead with no explanation. Tracked wraps OUTSIDE the access log so it
        # also counts /static and the pending-count poll, which are skipped by
        # the log but still occupy a worker. See request_health.py.
        request_health.WORKER_THREADS = 8
        request_health.start_watchdog()
        serve(request_health.Tracked(_AccessLog(app.wsgi_app)),
              host="0.0.0.0", port=5050, threads=8)
    except ImportError:
        # waitress not installed (e.g. fresh machine before `pip install -r
        # requirements.txt`) — the dev server still works, just isn't hardened.
        print("  waitress not installed - falling back to the Flask dev server.\n")
        app.run(host="0.0.0.0", port=5050, debug=False)
