"""In-memory intake state: the review queue, the scanning batch, and the
background-job registry — extracted from app.py in the modular split (GH #6).

Why this module exists. The earlier chunks of the split (routes_karen,
routes_settings, routes_reports, routes_paper, routes_filtered) moved route
TEXT out of app.py but left every one of them reaching back into the
composition root for shared state (_M._queue_items(), _M._queue_add(...)). The
state itself was the real knot: the review queue is touched by two folder
watchers, the startup sweep, Check-for-new-items's worker, the cabinet watcher, and every
request thread, so anything that wanted to read or write it had to import app.
Owning that state here gives the rest of the split something to depend on that
is NOT the composition root.

THREAD SAFETY. Everything below is shared mutable state reached from several
threads at once; each section documents the lock that guards it. Callers must
go through the functions here rather than touching pending_scans / _current_batch
directly — the locks are what stop two concurrent ingests from allotting the
same scan id or the same batch number.

REBINDING RULE. app.py aliases the objects here (pending_scans, queue_lock,
_jobs_running, and the functions) under their original private names so the
route modules' _M hops and the existing tests keep working. Those aliases are
bound once at import, so the dicts and locks below must only ever be MUTATED,
never reassigned. The values that genuinely do get rebound (_next_scan_id,
_current_batch, BATCH_STATE_FILE) are deliberately NOT aliased into app.py —
read them through this module so you always see the current value.
"""
import json
import os
import threading
from datetime import date

from database import (delete_pending_scan, get_all_entries, get_all_pending_scans,
                      save_pending_scan)

# ── Pending scans queue (in-memory) ──────────────────────────────────────────
pending_scans = {}   # scan_id (int) -> classified dict (includes pdf_path)
_next_scan_id = 1
# pending_scans is touched from two folder-watcher threads, the startup sweep,
# Check-for-new-items's background thread, AND request threads. queue_lock guards every
# mutation and snapshot: without it, two ingests could read the same
# _next_scan_id and one document would silently overwrite the other (both in
# memory and in the DB, whose write is INSERT OR REPLACE).
queue_lock = threading.Lock()
# Documents are ingested one at a time. The watcher fires for part-files the
# splitter writes into the watched folder, and the sweep / Check for new items can list a
# file a watcher is already working on — serializing enqueue_scan (together with
# its exists / already-queued checks) turns those duplicate calls into no-ops
# instead of double classifications.
ingest_lock = threading.Lock()


def queue_add(result: dict) -> int:
    """Add a classified doc to the review queue, in memory AND the DB so it
    survives a restart (no re-classifying, no lost work)."""
    global _next_scan_id
    with queue_lock:
        sid = _next_scan_id
        _next_scan_id += 1
        pending_scans[sid] = result
    try:
        save_pending_scan(sid, json.dumps(result))
    except Exception as e:
        print(f"[App] Could not persist pending scan {sid}: {e}")
    return sid


def queue_remove(scan_id: int):
    with queue_lock:
        pending_scans.pop(scan_id, None)
    try:
        delete_pending_scan(scan_id)
    except Exception:
        pass


def queue_items():
    """Point-in-time snapshot of the queue as [(scan_id, data), ...]. Every
    iteration over the queue must go through this — iterating the live dict
    while an ingest thread inserts raises 'dictionary changed size' mid-request
    (and, on a watcher thread, would kill the watcher)."""
    with queue_lock:
        return list(pending_scans.items())


# The parts of a queued document a person might remember it by. The AI's own
# reading first, then how it arrived — and the FILE NAME, which is the field
# report #95 turned on: Nancy filed eight Advanced Community Services invoices
# and could not find three of them. They were not lost, they were held here,
# and the queue row carried the AI's reading of the vendor ("Midvalley
# Station") instead of the name on the file she was typing. A held document is
# unfindable from the Log by definition — it has no record yet — so the queue
# has to answer for itself, by whatever a person actually knows about it.
_QUEUE_SEARCH_FIELDS = ("vendor", "doc_type", "bill_type_code", "invoice_number",
                        "account_number", "entity_code", "entity_name", "amount",
                        "mail_type", "batch_number", "received_date", "due_date",
                        "notes", "email_from", "email_subject", "pdf_path")


def _queue_haystack(scan_id, data):
    bits = [str(scan_id)]
    for f in _QUEUE_SEARCH_FIELDS:
        v = data.get(f)
        if v not in (None, ""):
            bits.append(str(v))
    bits.extend(str(c) for c in (data.get("property_codes") or []) if c)
    path = data.get("pdf_path") or ""
    if path:
        bits.append(os.path.basename(path))
    return " ".join(bits).lower()


def search_queue(query: str, limit: int = 50) -> list:
    """Documents still WAITING in Needs Review that match a search (report #16,
    Nancy: "could we have the top universal search also search the review
    queue"). Every word must appear somewhere, case-insensitive substring — the
    same rule the issue-report search uses, so "acs 8/6" and a bare scan id
    both work. Returns [(scan_id, data), ...] in queue order."""
    words = (query or "").strip().lower().split()
    if not words:
        return []
    out = []
    for sid, data in queue_items():
        hay = _queue_haystack(sid, data)
        if all(w in hay for w in words):
            out.append((sid, data))
            if len(out) >= limit:
                break
    return out


def claim_scan(scan_id: int):
    """Atomically claim a queued scan for processing (confirm / attach / skip).
    A second submit — a double-click, or two reviewers on the same item — gets
    None instead of producing a duplicate entry or a double file move. Callers
    must release_scan in a finally block (a no-op once the item is removed)."""
    with queue_lock:
        data = pending_scans.get(scan_id)
        if data is None or data.get("_processing"):
            return None
        data["_processing"] = True
        return data


def release_scan(scan_id: int):
    with queue_lock:
        d = pending_scans.get(scan_id)
        if d is not None:
            d.pop("_processing", None)


def get_scan(scan_id: int):
    return pending_scans.get(scan_id)


def queue_size() -> int:
    return len(pending_scans)


def clear_queue():
    """Drop the whole in-memory queue. The DB side is the caller's job
    (clear_pending_scans) — this module never owns that decision."""
    with queue_lock:
        pending_scans.clear()


def load_pending_from_db():
    """Restore the review queue from the DB on startup."""
    global _next_scan_id
    for sid, data_json in get_all_pending_scans():
        try:
            data = json.loads(data_json)
            # A crash mid-confirm could persist the in-flight claim marker;
            # never let it survive a restart (the item would be unconfirmable).
            data.pop("_processing", None)
            pending_scans[sid] = data
        except Exception:
            continue
    if pending_scans:
        _next_scan_id = max(pending_scans.keys()) + 1
        print(f"[App] Restored {len(pending_scans)} item(s) into the review queue.")


# ── Batch intake state (physical-mail scanning) ───────────────────────────────
# A "batch" = all files from one scanning session, started by a cover sheet (which
# carries the mail-received date). Tracks the current batch so each scanned file is
# tagged with received date / scanned date / batch number. See coversheet.html.
_current_batch = None   # {"batch_number","received_date","scanned_date","assumed_date"?}
# Ingest runs on the folder-watcher thread(s); the batch state above is shared
# mutable global. Guard every read-modify-write with this reentrant lock so two
# files (or a cover sheet + a document) ingested concurrently can't race into
# different batch ids or be allotted the same number. RLock because ensure_batch
# calls start_batch while already holding it.
batch_lock = threading.RLock()
BATCH_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "current_batch.json")


def current_batch():
    """The active batch dict, or None. Read-only accessor: the module global is
    rebound by start_batch / load_current_batch, so callers outside this module
    must come through here instead of importing the name."""
    return _current_batch


def save_current_batch():
    """Persist the in-progress batch so a restart mid-session doesn't split it."""
    try:
        with open(BATCH_STATE_FILE, "w") as f:
            json.dump(_current_batch, f)
    except Exception as e:
        print(f"[Batch] could not persist current batch: {e}")


def load_current_batch():
    """Restore the in-progress batch after a restart so a scan session that spans
    the restart keeps ONE batch id instead of splitting into orphaned records.
    Only restore a batch scanned TODAY — a stale batch from a prior day must never
    adopt today's mail (received dates would be wrong)."""
    global _current_batch
    try:
        if not os.path.exists(BATCH_STATE_FILE):
            return
        with open(BATCH_STATE_FILE) as f:
            saved = json.load(f)
        if saved and saved.get("scanned_date") == date.today().isoformat():
            _current_batch = saved
            print(f"[Batch] Resumed {saved.get('batch_number')} after restart.")
        else:
            os.remove(BATCH_STATE_FILE)   # stale — discard so a fresh batch starts
    except Exception as e:
        print(f"[Batch] could not restore current batch: {e}")


def next_batch_number():
    """Generate the next batch id for today: YYYY-MMDD-NN (NN unique within the day),
    checking logged entries, the in-memory queue, AND the active batch so two batches
    starting back-to-back (before either has queued a doc) can't collide."""
    prefix = date.today().strftime("%Y-%m%d")
    used = set()
    try:
        for e in get_all_entries():
            bn = e.get("batch_number") or ""
            if bn.startswith(prefix):
                used.add(bn)
    except Exception:
        pass
    for _, d in queue_items():
        bn = d.get("batch_number") or ""
        if bn.startswith(prefix):
            used.add(bn)
    # The just-started batch isn't queued yet — count it so the next call skips it.
    if _current_batch:
        bn = _current_batch.get("batch_number") or ""
        if bn.startswith(prefix):
            used.add(bn)
    n = 1
    while f"{prefix}-{n:02d}" in used:
        n += 1
    return f"{prefix}-{n:02d}"


def start_batch(received_date):
    """Begin a new scanning batch (called when a cover sheet is detected)."""
    global _current_batch
    with batch_lock:
        today_iso = date.today().isoformat()
        _current_batch = {
            "batch_number": next_batch_number(),
            "received_date": (received_date or today_iso),
            "scanned_date": today_iso,
        }
        save_current_batch()
    print(f"[Batch] Started {_current_batch['batch_number']} "
          f"received={_current_batch['received_date']}")
    return _current_batch


def ensure_batch():
    """Return the active batch FOR TODAY, creating a default one if scanning began
    without a cover sheet (received date defaults to today and is flagged for
    verification). A batch from an earlier day must never adopt today's mail —
    load_current_batch enforces that at startup, but the server can stay up for
    days, so the same same-day check has to happen here too. (Before this check,
    mail scanned Thursday without a fresh cover sheet inherited Monday's batch
    number AND Monday's received/scanned dates.)"""
    global _current_batch
    with batch_lock:
        today_iso = date.today().isoformat()
        if _current_batch is None or _current_batch.get("scanned_date") != today_iso:
            start_batch(None)
            _current_batch["assumed_date"] = True
            save_current_batch()
        return _current_batch


# ── Background-job registry ───────────────────────────────────────────────────
# The jobs below used to be a bare module-level bool, checked then set with no
# lock — two near-simultaneous requests (a double-click, or two admins) could
# both read False before either wrote True, starting two duplicate workers
# (double API billing for reclassify/AI-fill, doubled OCR/reindex work). One
# lock, one dict, one atomic claim per job name closes that gap everywhere at
# once instead of needing four separate fixes.
_jobs_lock = threading.Lock()
_jobs_running = {}


def try_start_job(name: str) -> bool:
    """Atomically claim a named background job slot. True = the caller may
    start it and must call finish_job(name) when done; False = something
    else already claimed it, don't start a second one."""
    with _jobs_lock:
        if _jobs_running.get(name):
            return False
        _jobs_running[name] = True
        return True


def finish_job(name: str):
    with _jobs_lock:
        _jobs_running[name] = False


def job_running(name: str) -> bool:
    with _jobs_lock:
        return _jobs_running.get(name, False)
