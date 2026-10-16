r"""
cabinet_watch.py – live pickup of documents hand-filed into the file server
(reports #28/#46/#50; the Swan CK#3012 incident: a hand-filed paid bill was
invisible for ~19 hours because only the 6 AM harvest looked).

Staff sometimes bypass the scan flow entirely: rename a document to Karen's
convention and drop it straight into its proper archive folder. This module
notices those files within seconds and registers them the way the nightly
harvest (T:\_OPS INDEX\harvest_daily_filing.py) would — but live, in-app, and
cross-checked against the document's own OCR read. The filename and the folder
it landed in are strong priors (nameparse.py); the OCR read is the check.

Decision ladder per arrival, cheapest first (harvest's LINK/REGISTER/SKIP
semantics, plus live re-linking):

  1. The app's own move (filing_ledger)                     -> ignore
  2. A known file moved/renamed by hand                     -> re-link the
     record/attachment to its new path (report #46, live — no restart)
  3. Bytes match an existing record                         -> re-link if that
     record's file is missing, else attach as a copy (harvest LINK)
  4. Deposits/draws/transfers, summaries/recaps             -> skip (money
     movements and recap sheets are not registrable documents)
  4b. An OLD YEAR tree (Files\2024\... while it's 2026)     -> archive
     backfill, not new intake: register as Filed with its PERIOD date, from
     the filename/folder priors alone — no AI read, no review hold, no
     Pending row (the 2026-08-05 incident: 107 two-year-old cash-flow
     statements re-exported into the cabinet became today's Pending intake,
     one AI read each). Fuzzy matching is skipped too: a dateless archive
     bill must never attach itself to a live record on amount+vendor alone.
  5. Fuzzy match to an existing record (amount+vendor+date) -> attach as copy;
     a paid filing against an already-Paid record with a DIFFERENT check #
     holds for a human ("probably a different cycle of a recurring bill")
  6. New document -> classify (OCR), reconcile with the filename/folder priors,
     then register IN PLACE (never moved, never renamed): Paid under a
     "... PAID BILLS" tree (with harvest's near-match veto — never mint a Paid
     twin), Pending otherwise. doc_type is the OCR's call: INCOMING BILLS
     folders also contain statements (Joshua, 2026-07-29).
  7. Any conflict (filename amount vs OCR amount, vendor mismatch, missing paid
     date, wild dates, near-match veto) -> the review queue, flagged Hand-filed
     with leave-in-place defaults. The queue IS the held-for-human surface.

Wiring: register(app, module) adds the manual-sweep route; start(cfg) spawns
one recursive watchdog observer per pickup root, a worker that serializes
processing (AI calls must not run on watchdog's dispatch thread), an initial
catch-up sweep, and an hourly re-sweep. The 6 AM harvest remains the deep
belt-and-braces pass — both sides skip paths the other already registered.
"""
import calendar
import datetime
import os
import queue
import re
import threading
import time
import traceback

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import database
import filing_ledger
import longpaths
import nameparse
from watcher import _wait_until_stable

_M = None            # the app module (set by register); helpers reached through
                     # it so test monkeypatching (klr.classify_document) works

DEFAULT_EXTS = (".pdf",)
_SWEEP_LOOKBACK_DAYS = 7    # sweep only recent additions; history is harvest's job
_SWEEP_MIN_AGE_SECS = 120   # skip files younger than this: likely still arriving
_SWEEP_MAX_FILES = 300      # per pass, loudly reported if exceeded — never silent
_MAX_TRIES = 3              # per-path processing attempts (AI-burn guard)
_PATH_LEN_LIMIT = 250       # beyond this, downstream readers start failing on
                            # MAX_PATH; we never rename a hand-filed file, so hold

_work_q = queue.Queue()
_pending_lock = threading.Lock()
_pending = set()
_fail_counts = {}
_started = False

_entries_cache = {"at": 0.0, "rows": []}
_ENTRIES_TTL = 30.0


def _key(path):
    return os.path.normcase(os.path.normpath(path or ""))


# ── config / scope ────────────────────────────────────────────────────────────

def pickup_roots(cfg: dict) -> list:
    roots = cfg.get("pickup_roots")
    if not roots:
        roots = [cfg.get("archive_folder", ""), cfg.get("filed_folder", "")]
    return [r for r in (roots or []) if r]


def _exts(cfg: dict) -> tuple:
    exts = cfg.get("pickup_exts")
    return tuple(e.lower() for e in exts) if exts else DEFAULT_EXTS


def _skippable(path: str) -> bool:
    """Files/folders the watcher must leave alone: the app's holding areas
    (_Blank/_Duplicates/_Skipped/_Filtered/...), cover-sheet archives, Office
    lock files and other in-progress temp names."""
    base = os.path.basename(path)
    if base.startswith("~$") or base.lower() in ("thumbs.db", "desktop.ini"):
        return True
    if os.path.splitext(base)[1].lower() in (".tmp", ".crdownload", ".partial"):
        return True
    for seg in re.split(r"[\\/]+", path):
        if seg.startswith("_") or seg.strip().upper() == "BATCH COVERS":
            return True
    return False


def _under_a_root(path: str, cfg: dict):
    p = _key(path)
    for root in pickup_roots(cfg):
        r = _key(root)
        if r and (p == r or p.startswith(r + os.sep)):
            return root
    return None


# ── DB lookups (read + targeted relink updates) ───────────────────────────────

def _record_for_path(p: str):
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id, filed_path, pdf_path, status, check_number FROM intake_log "
            "WHERE filed_path=? COLLATE NOCASE OR pdf_path=? COLLATE NOCASE LIMIT 1",
            (p, p)).fetchone()
        return dict(row) if row else None


def _attachment_for_path(p: str):
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id, entry_id FROM attachments WHERE file_path=? COLLATE NOCASE LIMIT 1",
            (p,)).fetchone()
        return dict(row) if row else None


def _update_attachment_path(att_id: int, new_path: str):
    database.update_attachment_path(att_id, new_path)


def _path_known(p: str) -> bool:
    return _record_for_path(p) is not None or _attachment_for_path(p) is not None


def _like_escape(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _relink_dir_move(src_dir: str, dest_dir: str):
    """A whole FOLDER was renamed/moved by hand (the June-30 incident class:
    221 records orphaned by one bulk rename). Rewrite the path prefix on every
    record and attachment that lived under it — DB only, files untouched."""
    src_prefix = os.path.normpath(src_dir)
    dest_prefix = os.path.normpath(dest_dir)
    n = 0
    with database.get_conn() as conn:
        for col, table, idcol in (("filed_path", "intake_log", "id"),
                                  ("pdf_path", "intake_log", "id"),
                                  ("file_path", "attachments", "id")):
            rows = conn.execute(
                f"SELECT {idcol} AS id, {col} AS p FROM {table} "
                f"WHERE {col} LIKE ? ESCAPE '\\'",
                (_like_escape(src_prefix + os.sep) + "%",)).fetchall()
            for r in rows:
                new_p = dest_prefix + r["p"][len(src_prefix):]
                conn.execute(f"UPDATE {table} SET {col}=? WHERE {idcol}=?",
                             (new_p, r["id"]))
                n += 1
        conn.commit()
    if n:
        database.log_activity(None, "Cabinet watch", "Cabinet-Watch",
                              f"Folder moved by hand; re-linked {n} path(s): "
                              f"{src_prefix} -> {dest_prefix}")
        print(f"[Cabinet] Folder move re-linked {n} path(s): {src_prefix} -> {dest_prefix}")


def _all_entries():
    """Cached snapshot for fuzzy matching (a burst of hand-filed docs must not
    re-read the whole intake_log per file)."""
    now = time.monotonic()
    if now - _entries_cache["at"] > _ENTRIES_TTL:
        _entries_cache["rows"] = database.get_all_entries()
        _entries_cache["at"] = now
    return _entries_cache["rows"]


# ── fuzzy matching (ported from harvest_daily_filing.py) ──────────────────────

def _match_entry(doc, fhash, entries):
    """Exact-ish match: byte hash, else amount (±$0.01) + vendor-token overlap
    + received-date within 60 days."""
    if fhash:
        for e in entries:
            if e.get("file_hash") == fhash:
                return e, "identical file"
    if not doc["amounts"]:
        return None, None
    dt = doc["tokens"]
    for e in entries:
        if e.get("amount") is None or not any(abs(a - e["amount"]) < 0.01 for a in doc["amounts"]):
            continue
        if not (nameparse.vtokens(e.get("vendor")) & dt):
            continue
        try:
            rec = datetime.date.fromisoformat((e.get("date_received") or "")[:10])
        except ValueError:
            rec = None
        if doc["date"] and rec and abs((doc["date"] - rec).days) > 60:
            continue
        return e, "amount + vendor + date window"
    return None, None


def _near_match(doc, entries):
    """Looser than _match_entry, used ONLY as a veto before register-as-Paid:
    if ANY record looks related (vendor overlap with matching or BLANK amount,
    or exact amount on a shared property), a fresh Paid record risks minting a
    twin — hold for a human instead. (Blank-amount records can never
    exact-match, so without this every one of their paid filings would
    duplicate.)"""
    dt = doc["tokens"]
    dprops = set(doc["props"])
    for e in entries:
        tok_hit = bool(nameparse.vtokens(e.get("vendor")) & dt)
        amt_hit = (e.get("amount") is not None and doc["amounts"]
                   and any(abs(a - e["amount"]) < 0.01 for a in doc["amounts"]))
        prop_hit = bool(dprops & {p.strip().upper() for p in
                                  (e.get("property_code") or "").split(",") if p.strip()})
        if (tok_hit and (amt_hit or e.get("amount") is None)) or (amt_hit and prop_hit):
            return e
    return None


# ── archive backfill (ladder step 4b) ─────────────────────────────────────────

def _is_archive_backfill(priors) -> bool:
    """A file landing under a year tree older than the current year is someone
    (re)building the historical cabinet, not new work arriving."""
    year = priors.get("year")
    return bool(year) and year < datetime.date.today().year


def _archive_period(doc, priors) -> datetime.date:
    """The period the filename/folder says the document covers. The TREE year
    is authoritative (the filer put it there deliberately; filename digits can
    be account numbers): a full filename date is used only when it agrees with
    the tree, else month word / month folder at month-end, else Dec 31."""
    year = priors["year"]
    if doc["date"] and doc["date"].year == year:
        return doc["date"]
    _y, month = nameparse.month_year(doc["stem"])
    month = month or priors["month"]
    if month:
        return datetime.date(year, month, calendar.monthrange(year, month)[1])
    return datetime.date(year, 12, 31)


def _archive_doc_type(path, priors) -> str:
    """Folder-derived document type — the archive's folder names ARE its
    taxonomy (CASH FLOW STATEMENTS, VENDORS - INSURANCE, ... PAID BILLS)."""
    segs = " ".join(re.split(r"[\\/]+", os.path.dirname(path))).upper()
    if "BANK STATEMENT" in segs:
        return "Bank Statement"
    if "STATEMENT" in segs or "CASH FLOW" in segs:
        return "Statement"
    if "INSURANCE" in segs:
        return "Insurance"
    if "AUDIT" in segs:
        return "Audit"
    if re.search(r"(?<![A-Z])TAX", segs):
        return "Tax"
    if priors["in_paid_tree"] or priors["in_ap_incoming"]:
        return "Bill"
    return "Other"


def archive_entry_fields(path, doc, priors, fhash, cfg) -> dict:
    """Record fields for an archive-backfill registration, filename/folder
    priors only — deliberately NO AI read (a backfill is hundreds of files,
    and the ops-index precedent holds: the archive's metadata lives in its
    names). Also used by the archive-filed-convert console command, so the
    one-shot conversion and the live watcher can never drift apart."""
    entity = doc["entity"] or priors["entity"] or ""
    entity_name = next((e["name"] for e in cfg.get("entities", [])
                        if e["code"] == entity), "")
    vendor = doc["payee"] or ""
    if vendor.isupper():
        vendor = vendor.title()
    props = doc["props"]
    addr_by = {p["code"]: p["address"] for p in cfg.get("properties", [])}
    return {
        "date_received": _archive_period(doc, priors).isoformat(),
        "scanned_date": "", "batch_number": "",
        "mail_type": "Hand-filed",
        "status": "Filed",
        "processed_by": "Cabinet-Watch",
        "notes": f"[CABINET] archive backfill, registered in place from the "
                 f"filename/folder priors (no AI read): {path}",
        "pdf_path": path, "filed_path": path,
        "file_hash": fhash,
        "entity_code": entity, "entity_name": entity_name,
        "vendor": vendor,
        "property_code": ", ".join(props),
        "property_address": ", ".join(addr_by[c] for c in props if c in addr_by),
        "amount": max(doc["amounts"]) if doc["amounts"] else None,
        "doc_type": _archive_doc_type(path, priors),
        "bill_type_code": "",
        "due_date": "", "invoice_number": "", "account_number": "",
        "ai_confidence": "",
        "payment_method": doc["method"] or "",
        "check_number": doc["check"] or "",
    }


def _register_archive(path, doc, priors, fhash, cfg):
    entry = archive_entry_fields(path, doc, priors, fhash, cfg)
    entry_id = database.insert_entry(entry)
    database.log_activity(entry_id, "Created", "Cabinet-Watch",
                          f"archive backfill (Filed, period "
                          f"{entry['date_received']}): {path}")
    print(f"[Cabinet] Registered archive document as record #{entry_id} "
          f"(Filed): {os.path.basename(path)[:80]}")


# ── the decision ladder ───────────────────────────────────────────────────────

def _process(path: str, moved_from: str = None):
    cfg = _M.load_config()
    if _under_a_root(path, cfg) is None or _skippable(path):
        return
    if filing_ledger.is_app_move(path):
        return                                           # 1. our own filing

    # A known file moved/renamed by hand → re-link, done (report #46, live).
    if moved_from:
        rec = _record_for_path(moved_from)
        if rec:
            database.update_entry(rec["id"], {"filed_path": path})
            database.log_activity(rec["id"], "Re-linked", "Cabinet-Watch",
                                  f"Document moved by hand: {moved_from} -> {path}")
            print(f"[Cabinet] Re-linked record #{rec['id']} after hand move: "
                  f"{os.path.basename(path)}")
            return
        att = _attachment_for_path(moved_from)
        if att:
            _update_attachment_path(att["id"], path)
            database.log_activity(att["entry_id"], "Re-linked", "Cabinet-Watch",
                                  f"Copy moved by hand: {moved_from} -> {path}")
            return

    if not longpaths.exists(path):
        return
    if len(path) > _PATH_LEN_LIMIT:
        # We never rename hand-filed files, and past this length the rest of
        # the pipeline (viewer, OCR) starts failing on MAX_PATH — say so once.
        if _fail_counts.get(_key(path), 0) == 0:
            database.log_activity(None, "Cabinet watch", "Cabinet-Watch",
                                  f"Skipped an over-long path ({len(path)} chars) — "
                                  f"shorten the filename by hand: {path[:180]}...")
        _fail_counts[_key(path)] = _MAX_TRIES
        return
    if not _wait_until_stable(path):
        return
    if _path_known(path):
        return                                           # already registered

    try:
        fhash = _M._sha256_file(path)
    except OSError:
        fhash = ""

    # 3. Bytes match an existing record → re-link or attach as a copy.
    if fhash:
        owners = database.get_entries_by_hash(fhash)
        if owners:
            owner = owners[0]
            live = owner.get("filed_path") or owner.get("pdf_path") or ""
            if not (live and longpaths.exists(live)):
                database.update_entry(owner["id"], {"filed_path": path})
                database.log_activity(owner["id"], "Re-linked", "Cabinet-Watch",
                                      f"Missing document found hand-filed at: {path}")
                print(f"[Cabinet] Re-linked record #{owner['id']} to hand-filed copy: "
                      f"{os.path.basename(path)}")
            else:
                database.add_attachment(owner["id"], path, "Hand-filed",
                                        os.path.basename(path), "")
                database.log_activity(owner["id"], "Hand-filed copy attached",
                                      "Cabinet-Watch",
                                      f"byte-identical copy found at: {path}")
            return
        if any(v.get("file_hash") == fhash for _, v in _M._queue_items()):
            # Same bytes are sitting in the review queue — let that human
            # decision play out; the nightly harvest links this copy after.
            database.log_activity(None, "Cabinet watch", "Cabinet-Watch",
                                  f"Hand-filed file matches a queued scan; leaving "
                                  f"for review to settle: {path}")
            return

    doc = nameparse.parse_name(path)
    priors = nameparse.path_priors(path, cfg)

    # 4. Money movements and recap sheets are not registrable documents.
    if doc["is_nonbill"]:
        print(f"[Cabinet] Skipping non-bill money movement: {doc['stem'][:80]}")
        return
    if doc["is_summary"] and priors["in_paid_tree"]:
        print(f"[Cabinet] Skipping summary/recap sheet: {doc['stem'][:80]}")
        return

    # 4b. Archive backfill: an old year tree registers as Filed, priors only.
    if _is_archive_backfill(priors):
        _register_archive(path, doc, priors, fhash, cfg)
        return

    entries = _all_entries()

    # 5. Fuzzy match → attach as copy (or hold when it contradicts a Paid twin).
    matched, why = _match_entry(doc, "", entries)   # hash already handled above
    if matched:
        if (doc["is_payment"] and matched.get("status") == "Paid" and doc["check"]
                and (matched.get("check_number") or "") not in ("", doc["check"])):
            _hold_for_review(path, doc, priors, fhash, cfg,
                             f"Payment filing CK#{doc['check']} matches record "
                             f"#{matched['id']}, already Paid under a different "
                             f"check # — probably a different cycle of a "
                             f"recurring bill")
            return
        database.add_attachment(matched["id"], path, "Hand-filed",
                                os.path.basename(path), "")
        note = f"hand-filed paper copy matched on {why}: {path}"
        if doc["is_payment"] and matched.get("status") != "Paid":
            note += " — payment filing; the reconciler confirms Paid at its next run"
        database.log_activity(matched["id"], "Hand-filed copy attached",
                              "Cabinet-Watch", note)
        print(f"[Cabinet] Linked hand-filed copy to record #{matched['id']} ({why})")
        return

    # 6. New document: OCR read, then reconcile with the filename/folder priors.
    result = _M.classify_document(path)
    if result.get("is_cover_sheet"):
        _hold_for_review(path, doc, priors, fhash, cfg,
                         "Reads like a batch cover sheet, but it was hand-filed "
                         "into the archive", result=result)
        return

    ocr_amount = result.get("amount")
    try:
        ocr_amount = float(ocr_amount) if ocr_amount not in (None, "") else None
    except (ValueError, TypeError):
        ocr_amount = None

    hold = None
    if doc["amounts"] and ocr_amount is not None and \
            not any(abs(a - ocr_amount) < 0.01 for a in doc["amounts"]):
        hold = (f"Filename says ${max(doc['amounts']):,.2f} but the document "
                f"reads ${ocr_amount:,.2f}")
    elif doc["payee"] and result.get("vendor"):
        a, b = nameparse.vtokens(doc["payee"]), nameparse.vtokens(result.get("vendor"))
        if a and b and not (a & b):
            hold = (f"Filename names '{doc['payee']}' but the document reads "
                    f"'{result.get('vendor')}'")
    if not hold and doc.get("bad_date"):
        # Record #941: "06-30-256" (a year typo) parsed as the year 256 and a
        # cash-flow statement got logged thirteen centuries early. Spec §3.1:
        # an impossible date in the name is a person's call, never a guess —
        # nameparse won't even yield a date here, so the folder-tree correction
        # below never sees these.
        hold = ("Filename leads with a date that can't be real — probably a "
                "typo'd year; check it against the document")
    # The filename's date, checked against the year tree it was filed in
    # (nameparse.received_from_filing, report #107) — a mistyped year that the
    # month folder contradicts is corrected here, before the age test, so a
    # "07-01-23" bill sitting in 2026\07. JULY registers as July 2026 instead
    # of being held as three years stale. Anything the folder can't vouch for
    # still fails the age test below and goes to a human.
    filing_date, date_note = nameparse.received_from_filing(doc["date"], priors)
    if not hold and doc["date"] and filing_date is None:
        hold = date_note
    if not hold and filing_date:
        age = (datetime.date.today() - filing_date).days
        if age > 400 or age < -30:
            hold = (f"Filename date {doc['date'].isoformat()} looks wrong on a "
                    f"fresh filing")

    register_paid = priors["in_paid_tree"] and doc["is_payment"]
    if not hold and register_paid:
        if not doc["date"]:
            hold = "Paid filing with no date in the filename — never guess a paid date"
        else:
            near = _near_match(doc, entries)
            if near:
                hold = (f"May belong to existing record #{near['id']} "
                        f"({near.get('vendor') or '?'}, {near.get('status')}) — "
                        f"registering a fresh Paid record risks a twin")

    if hold:
        _hold_for_review(path, doc, priors, fhash, cfg, hold, result=result)
        return

    _register_in_place(path, doc, priors, result, fhash, ocr_amount, cfg,
                       paid=register_paid, filing_date=filing_date,
                       date_note=date_note)


def _merged_fields(doc, priors, result, ocr_amount, cfg):
    """Reconcile filename/folder priors with the OCR read into record fields.
    Filename wins on amount (Karen typed it; spec §3.5 largest-$ rule) and on
    entity (her filing token/tree is deliberate); the OCR wins on doc_type
    (statements get filed under INCOMING BILLS too) and supplies the fields a
    filename never carries (due date, invoice #, account #)."""
    entity = doc["entity"] or priors["entity"] or result.get("entity_code") or ""
    entity_name = next((e["name"] for e in cfg.get("entities", [])
                        if e["code"] == entity), "")
    vendor = (result.get("vendor") or "").strip()
    if doc["payee"]:
        if not (vendor and (nameparse.vtokens(doc["payee"]) & nameparse.vtokens(vendor))):
            vendor = doc["payee"].title() if doc["payee"].isupper() else doc["payee"]
    props = doc["props"] or [c for c in (result.get("property_codes") or []) if c]
    addr_by = {p["code"]: p["address"] for p in cfg.get("properties", [])}
    amount = max(doc["amounts"]) if doc["amounts"] else ocr_amount
    return {
        "entity_code": entity, "entity_name": entity_name, "vendor": vendor,
        "property_code": ", ".join(props),
        "property_address": ", ".join(addr_by[c] for c in props if c in addr_by),
        "amount": amount,
        "doc_type": result.get("doc_type") or "Other",
        "bill_type_code": result.get("bill_type_code") or "",
        "due_date": result.get("due_date") or "",
        "invoice_number": result.get("invoice_number") or "",
        "account_number": result.get("account_number") or "",
        "ai_confidence": result.get("ai_confidence") or "Medium",
    }


def _register_in_place(path, doc, priors, result, fhash, ocr_amount, cfg, paid,
                       filing_date=None, date_note=None):
    """`filing_date` is the filename's date after nameparse.received_from_filing
    has checked it against the year folder (#107); `date_note` records the
    correction on the record itself, so a person can see what was changed and
    why instead of finding a silently different date."""
    fields = _merged_fields(doc, priors, result, ocr_amount, cfg)
    if filing_date is None and doc["date"]:
        filing_date, date_note = nameparse.received_from_filing(doc["date"], priors)
    notes = f"[CABINET] registered from manual filing: {path}"
    if date_note:
        notes += f" | {date_note}"
    entry = {
        "date_received": (filing_date.isoformat() if filing_date
                          else datetime.date.today().isoformat()),
        "scanned_date": "", "batch_number": "",
        "mail_type": "Hand-filed",
        "status": "Paid" if paid else "Pending",
        "processed_by": "Cabinet-Watch",
        "notes": notes,
        "pdf_path": path, "filed_path": path,
        "file_hash": fhash,
        **fields,
    }
    if paid:
        entry.update({
            "paid_date": entry["date_received"],
            "paid_amount": fields["amount"],
            "paid_by": "Cabinet-Watch",
            "payment_method": doc["method"] or "",
            "check_number": doc["check"] or "",
        })
    entry_id = database.insert_entry(entry)
    database.log_activity(
        entry_id, "Created", "Cabinet-Watch",
        f"hand-filed pickup ({'Paid' if paid else 'Pending'}): {path}")
    print(f"[Cabinet] Registered hand-filed document as record #{entry_id} "
          f"({'Paid' if paid else 'Pending'}): {os.path.basename(path)[:80]}")


def _hold_for_review(path, doc, priors, fhash, cfg, reason,
                     result: dict = None):
    """Queue the file for human review, flagged Hand-filed, defaulting to
    'leave it where (and how) the filer put it'. Never silently dropped
    (KAREN_FILENAME_SPEC §0)."""
    data = dict(result or {})
    data.setdefault("doc_type", "Bill" if (priors["in_ap_incoming"] or
                                           priors["in_paid_tree"]) else "Other")
    data.setdefault("vendor", doc["payee"] or "")
    if doc["amounts"]:
        data.setdefault("amount", max(doc["amounts"]))
    data["mail_type"] = "Hand-filed"
    data["pdf_path"] = path
    data["file_hash"] = fhash
    data["hand_filed"] = True
    # Same year check as the register path (#107): a held item's received date
    # is what the reviewer sees pre-filled, so it must not carry a typo'd year
    # into the form either.
    filing_date, _note = nameparse.received_from_filing(doc["date"], priors)
    data["received_date"] = (filing_date.isoformat() if filing_date
                             else datetime.date.today().isoformat())
    data["scanned_date"] = datetime.date.today().isoformat()
    data.setdefault("batch_number", None)
    data["notes"] = (f"[Hand-filed, needs a decision] {reason}. "
                     + (data.get("notes") or "")).strip()
    data.setdefault("entity_code", doc["entity"] or priors["entity"] or "")
    if doc["props"]:
        data.setdefault("property_codes", doc["props"])
    sid = _M._queue_add(data)
    database.log_activity(None, "Cabinet watch", "Cabinet-Watch",
                          f"Held for review (scan {sid}): {reason} — {path}")
    print(f"[Cabinet] Held for review: {reason} — {os.path.basename(path)[:80]}")


# ── worker / observers / sweep ────────────────────────────────────────────────

def _submit(kind, a, b=None):
    if kind == "file":
        k = _key(a)
        with _pending_lock:
            if k in _pending or _fail_counts.get(k, 0) >= _MAX_TRIES:
                return
            _pending.add(k)
    _work_q.put((kind, a, b))


def _worker_loop():
    while True:
        kind, a, b = _work_q.get()
        try:
            if kind == "file":
                _process(a, moved_from=b)
                _fail_counts.pop(_key(a), None)
            elif kind == "dir":
                _relink_dir_move(a, b)
        except Exception:
            if kind == "file":
                k = _key(a)
                _fail_counts[k] = _fail_counts.get(k, 0) + 1
                print(f"[Cabinet] processing failed "
                      f"(attempt {_fail_counts[k]}/{_MAX_TRIES}) on "
                      f"{os.path.basename(a)[:80]}:\n{traceback.format_exc()}")
            else:
                print(f"[Cabinet] dir-move handling failed:\n{traceback.format_exc()}")
        finally:
            if kind == "file":
                with _pending_lock:
                    _pending.discard(_key(a))


class _CabinetHandler(FileSystemEventHandler):
    """Every hook swallows exceptions: an exception here propagates into
    watchdog's dispatch thread and kills the observer — the app then looks
    healthy while silently ignoring the cabinet (same lesson as watcher.py)."""

    def __init__(self, exts):
        self.exts = exts

    def _maybe(self, path, moved_from=None):
        if path and path.lower().endswith(self.exts) and not _skippable(path):
            _submit("file", path, moved_from)

    def on_created(self, event):
        try:
            if not event.is_directory:
                self._maybe(event.src_path)
        except Exception:
            pass

    def on_moved(self, event):
        try:
            dest = getattr(event, "dest_path", "") or ""
            if not dest:
                return
            if event.is_directory:
                _submit("dir", event.src_path, dest)
            else:
                self._maybe(dest, moved_from=event.src_path)
        except Exception:
            pass


def _observe_root(root, exts):
    """Keep one recursive observer alive over `root`, restarting on failure
    (dropped share, dead observer) — same shape as watcher.start_watcher."""
    handler = _CabinetHandler(exts)
    while True:
        observer = Observer()
        try:
            if not os.path.isdir(root):
                raise OSError(f"pickup root not accessible: {root}")
            observer.schedule(handler, root, recursive=True)
            observer.start()
            print(f"[Cabinet] Watching (recursive): {root}")
            while observer.is_alive():
                time.sleep(5)
            raise OSError("observer stopped")
        except Exception as e:
            print(f"[Cabinet] Watch trouble on {root} ({e}); retrying in 60s.")
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:
                pass
            time.sleep(60)


def sweep(cfg: dict = None) -> dict:
    """Catch-up pass over the pickup roots: recently added files (mtime OR
    Windows creation time within the lookback — a copy gets a fresh ctime even
    when it keeps an old mtime) that nothing has registered yet. Backstop for
    events missed while the app was down; the deep past stays the 6 AM
    harvest's job."""
    if not _M._try_start_job("cabinet_sweep"):
        return {"started": False}
    try:
        cfg = cfg or _M.load_config()
        exts = _exts(cfg)
        cutoff = time.time() - _SWEEP_LOOKBACK_DAYS * 86400
        found, dropped = [], 0
        for root in pickup_roots(cfg):
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith("_")
                               and d.strip().upper() != "BATCH COVERS"]
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    if not name.lower().endswith(exts) or _skippable(p):
                        continue
                    try:
                        st = os.stat(longpaths.extended(p))
                    except OSError:
                        continue
                    fresh = max(st.st_mtime, getattr(st, "st_ctime", 0))
                    if fresh < cutoff or time.time() - fresh < _SWEEP_MIN_AGE_SECS:
                        continue
                    if filing_ledger.is_app_move(p) or _path_known(p):
                        continue
                    if len(found) >= _SWEEP_MAX_FILES:
                        dropped += 1
                        continue
                    found.append(p)
        for p in found:
            _submit("file", p)
        if found or dropped:
            print(f"[Cabinet] Sweep queued {len(found)} recent unregistered file(s)"
                  + (f"; {dropped} more deferred to the next pass" if dropped else ""))
        return {"started": True, "queued": len(found), "deferred": dropped}
    finally:
        _M._finish_job("cabinet_sweep")


def _sweep_loop(cfg):
    time.sleep(90)          # let boot-time repair/maintenance passes finish first
    while True:
        try:
            sweep()
        except Exception:
            print(f"[Cabinet] sweep pass crashed:\n{traceback.format_exc()}")
        time.sleep(3600)


# ── wiring ────────────────────────────────────────────────────────────────────

def register(flask_app, app_module):
    """Add the manual-sweep endpoint (report #28's 'I just added a document,
    bring it in' button). Login + CSRF come from the app's global gates."""
    global _M
    _M = app_module

    def cabinet_sweep_now():
        t = threading.Thread(target=sweep, daemon=True)
        t.start()
        from flask import jsonify
        return jsonify({"ok": True})

    flask_app.add_url_rule("/api/cabinet_sweep", "cabinet_sweep_now",
                           cabinet_sweep_now, methods=["POST"])


def start(cfg: dict):
    """Spawn the worker, one recursive observer per pickup root, the startup
    catch-up sweep and the hourly re-sweep. Call once from app __main__."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker_loop, daemon=True).start()
    for root in pickup_roots(cfg):
        threading.Thread(target=_observe_root, args=(root, _exts(cfg)),
                         daemon=True).start()
    threading.Thread(target=_sweep_loop, args=(cfg,), daemon=True).start()
