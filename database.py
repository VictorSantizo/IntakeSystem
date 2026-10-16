import re
import difflib
import sqlite3, os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "intake.db")

def get_conn():
    # WAL + a real busy timeout so concurrent writers (the folder watcher thread and
    # the email-poll thread) wait for the lock instead of instantly failing. Without
    # this, concurrent ingest threw "database is locked" and pending scans were lost.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intake_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_received TEXT NOT NULL,
                mail_type TEXT DEFAULT 'Physical Mail',
                entity_code TEXT,
                entity_name TEXT,
                property_code TEXT,
                property_address TEXT,
                doc_type TEXT,
                bill_type_code TEXT,
                vendor TEXT,
                amount REAL,
                due_date TEXT,
                status TEXT DEFAULT 'Pending',
                processed_by TEXT,
                notes TEXT,
                pdf_path TEXT,
                filed_path TEXT,
                ai_confidence TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Add payment columns if upgrading from older schema
        for col, typedef in [
            ("paid_date",      "TEXT"),
            ("paid_amount",    "REAL"),
            ("paid_by",        "TEXT"),
            ("invoice_number", "TEXT"),
            ("account_number", "TEXT"),
            ("scanned_date",   "TEXT"),
            ("batch_number",   "TEXT"),
            ("payment_method", "TEXT"),   # Credit Card | Check | Phone | ACH
            ("check_number",   "TEXT"),   # when payment_method = Check
            ("payment_note",   "TEXT"),   # free-text note (e.g. confirmation #)
            ("email_from",     "TEXT"),   # source email sender (when mail_type=Email)
            ("email_to",       "TEXT"),   # To header — whose mail this was
            ("email_account",  "TEXT"),   # which configured mailbox pulled it
            ("email_subject",  "TEXT"),
            ("email_date",     "TEXT"),
            ("email_body",     "TEXT"),
            ("file_hash",      "TEXT"),   # sha256 of the document bytes (dup detection)
            ("important",      "INTEGER DEFAULT 0"),  # flag: needs eyes regardless of doc_type (manual or AI)
            ("important_reason", "TEXT"),             # AI's one-line why (empty for manual flags)
            ("notice_summary", "TEXT"),               # AI: gist of an important notice bundled with a bill (rate change, shutoff, cancellation)
            ("flagged_at",     "TEXT"),               # when the important flag was set (drives board age)
            ("acknowledged_at","TEXT"),               # taken down from the attention board (handled, not unflagged)
            ("acknowledged_by","TEXT"),
            ("record_no",      "TEXT"),               # human-facing YYMMDD-NNN, assigned once at creation
            ("no_original_on_file", "INTEGER DEFAULT 0"),  # staff searched, confirmed no source invoice exists
            ("no_original_by", "TEXT"),
            ("no_original_at", "TEXT"),
            ("email_appended", "TEXT"),   # JSON list of (mailbox,message) keys whose email pages are at the back of the filed PDF (email_append.py)
            ("filed_sha256",   "TEXT"),   # sha256 of the bytes ON DISK once appending makes them diverge from file_hash (heal-by-hash accepts either)
        ]:
            try:
                conn.execute(f"ALTER TABLE intake_log ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists
        # Rows flagged before flagged_at existed age from their creation.
        conn.execute("UPDATE intake_log SET flagged_at = created_at "
                     "WHERE important=1 AND (flagged_at IS NULL OR flagged_at='')")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_hash ON intake_log(file_hash)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendor_hints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_key TEXT NOT NULL,
                entity_code TEXT,
                property_codes TEXT,
                doc_type TEXT,
                bill_type_code TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(vendor_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                user TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at)")
        # One-off repair (2026-08-06): record #941's received date came from the
        # hand-filed name "06-30-256 NOTE TO CINDY WITH STATEMENTS.pdf" — a year
        # typo ("256" for "26") that parse_name read as the year 256 and an
        # unguarded confirm then stored. The document is the June 2026 cash-flow
        # statement for 140 Evans Ave (its own text and its ...\2026\6. JUNE
        # folder both say so). Running here means the write happens in the app's
        # own process at startup — never from a workstation over the share (the
        # LACKSSERVER2021 WAL rule). The WHERE pins id AND the bad value, so it
        # applies exactly once and can never touch a hand-corrected row.
        # record_no 560630-001 is kept: record numbers never renumber.
        fixed = conn.execute(
            "UPDATE intake_log SET date_received='2026-06-30', "
            "updated_at=datetime('now','localtime') "
            "WHERE id=941 AND date_received='0256-06-30'")
        if fixed.rowcount > 0:
            conn.execute(
                "INSERT INTO activity_log (entry_id, action, detail, user) "
                "VALUES (941, 'Edited', ?, 'System')",
                ("received date corrected 0256-06-30 → 2026-06-30 (year typo in the "
                 "hand-filed name '06-30-256 NOTE TO CINDY WITH STATEMENTS'). "
                 "record_no 560630-001 and the filed copy's 06-30-56 filename keep "
                 "the old year — record numbers never renumber; rename the file by "
                 "hand if it matters.",))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                file_path TEXT,
                source TEXT,
                original_name TEXT,
                added_by TEXT,
                added_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attach_entry ON attachments(entry_id)")
        # How a bill's payment was allocated across entities (Tier 3 #2 split).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_splits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                entity_code TEXT,
                amount REAL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_split_entry ON payment_splits(entry_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bug_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                page_url TEXT,
                record_ref TEXT,
                description TEXT,
                context TEXT,
                status TEXT DEFAULT 'Open',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Issue-report lifecycle columns (VALIDATION_WORKFLOW.md, reports
        # #66-#70). Additive; existing rows keep their status — nothing flips.
        for col, typedef in [
            ("priority",   "INTEGER DEFAULT 0"),  # 1 = bumped, sorts first (#69)
            ("topic",      "TEXT"),                # group label for related issues (#68)
            ("ai_summary", "TEXT"),                # one-line AI description (#68)
            ("bounced_at", "TEXT"),                # set on "checked, not yet resolved" (#67)
            # One report, several asks — or several reports, one ask. parent_id
            # is both halves of #68's bounce ("reports like this should be
            # separated into their own issues. 68a, 68b") and #55's ("There is
            # 3 or so issues open for this. we should merge them"): a family of
            # issues that travels together. One level only; see
            # database.set_report_parent.
            ("parent_id",  "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE bug_reports ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists
        # Who did/said what on an issue. kind: note (human comment), dev (dev
        # work happened), resolution (required what-I-did/what-to-check when a
        # report goes Dev-resolved, #70), bounce (verification failed, #67),
        # status (state-change breadcrumbs like Verified/Reopened).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS report_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                author TEXT,
                kind TEXT DEFAULT 'note',
                text TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_report_notes ON report_notes(report_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id TEXT PRIMARY KEY,
                processed_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS filtered_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT,
                file_name TEXT,
                doc_type TEXT,
                vendor TEXT,
                amount REAL,
                ai_confidence TEXT,
                reason TEXT,
                data TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_scans (
                scan_id INTEGER PRIMARY KEY,
                data TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_hints (
                account_key TEXT PRIMARY KEY,
                entity_code TEXT,
                property_codes TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Bills Karen EXPECTS to arrive on a cadence (the recurring-bills radar).
        # Curated — seeded from suggestions, never auto-populated.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expected_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor TEXT NOT NULL,
                account_number TEXT,
                entity_code TEXT,
                property_code TEXT,
                cadence TEXT DEFAULT 'monthly',   -- monthly | quarterly | yearly
                typical_amount REAL,
                active INTEGER DEFAULT 1,
                source TEXT DEFAULT 'manual',     -- manual | suggested
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS karen_answers (
                question_id TEXT PRIMARY KEY,     -- ids from karen_questions.py
                answer TEXT,
                notes TEXT,
                updated_by TEXT,
                updated_at TEXT
            )
        """)
        # End-of-day review-queue depth — the tray gauge's history shelf. One row
        # per day, upserted hourly by the backup loop and live on /tray renders,
        # so the last write of a day is that day's closing depth.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue_depth_history (
                day TEXT PRIMARY KEY,             -- YYYY-MM-DD
                depth INTEGER,
                urgent INTEGER,
                recorded_at TEXT
            )
        """)
        # Klick Klack Helper transcripts — one row per turn (the question plus
        # either the reply or the error the user saw). Kept so we can see what
        # staff actually ask and where the Helper falls short: e.g. Nancy couldn't
        # find a bill she searched for (report #4). Read-only design aid.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS helper_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                page TEXT,
                question TEXT,
                reply TEXT,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # "Pass to a teammate" — the office's paper habit (hand someone a bill
        # with a sticky note) inside the app: a record referred to a named staff
        # member with a note. Open passes badge in the recipient's nav and list
        # on their dashboard until marked done.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS record_passes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                from_user TEXT,
                to_user TEXT NOT NULL,
                note TEXT,
                status TEXT DEFAULT 'Open',          -- Open | Done
                created_at TEXT DEFAULT (datetime('now','localtime')),
                done_at TEXT,
                done_by TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_passes_to "
                     "ON record_passes(to_user, status)")
        # Human-facing record numbers (YYMMDD-NNN from date_received). Backfill
        # rows from before the column existed, oldest received first; rows that
        # already have a number are never renumbered — the number is a label,
        # kept even if date_received is later corrected. The counters table
        # remembers each day's high-water mark so a number is never REUSED
        # either, even after its record is deleted (merge-record removes rows).
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_recno "
                     "ON intake_log(record_no)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS record_no_counters (
                prefix TEXT PRIMARY KEY,   -- YYMMDD
                top    INTEGER NOT NULL    -- highest sequence ever issued
            )
        """)
        pending = conn.execute(
            "SELECT id, date_received FROM intake_log "
            "WHERE record_no IS NULL OR record_no='' "
            "ORDER BY date_received, id").fetchall()
        if pending:
            counters = {r["prefix"]: r["top"] for r in
                        conn.execute("SELECT prefix, top FROM record_no_counters")}
            for r in conn.execute("SELECT record_no FROM intake_log "
                                  "WHERE record_no IS NOT NULL AND record_no!=''"):
                try:
                    p, n = r["record_no"].split("-", 1)
                    counters[p] = max(counters.get(p, 0), int(n))
                except ValueError:
                    pass
            today = datetime.now().strftime("%y%m%d")
            for row in pending:
                prefix = _record_no_prefix(row["date_received"]) or today
                counters[prefix] = counters.get(prefix, 0) + 1
                conn.execute("UPDATE intake_log SET record_no=? WHERE id=?",
                             (f"{prefix}-{counters[prefix]:03d}", row["id"]))
            for p, t in counters.items():
                conn.execute(
                    "INSERT INTO record_no_counters(prefix, top) VALUES(?,?) "
                    "ON CONFLICT(prefix) DO UPDATE SET top=MAX(top, excluded.top)",
                    (p, t))
        conn.commit()
    seed_activity_from_existing()

def log_helper_chat(user, page, question, reply=None, error=None):
    """Persist one Helper turn. Best-effort — callers wrap this so a logging
    failure can never break the chat reply."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO helper_chats (user, page, question, reply, error) "
            "VALUES (?,?,?,?,?)",
            (user or "", page or "", question or "", reply or None, error or None))
        conn.commit()

def get_helper_chats(limit=200):
    """Recent Helper turns, newest first — for reviewing what staff ask."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM helper_chats ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(r) for r in rows]

# ── "Pass to a teammate" (record referrals) ───────────────────────────────────

def add_record_pass(entry_id: int, from_user: str, to_user: str, note: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO record_passes (entry_id, from_user, to_user, note) "
            "VALUES (?,?,?,?)",
            (entry_id, from_user or "", (to_user or "").strip(), note or ""))
        conn.commit()
        return cur.lastrowid

def get_passes_for_user(to_user: str, status: str = "Open") -> list:
    """Records passed TO this person (their work list), oldest first, joined to
    the record so the list can show vendor/amount/record_no directly."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.*, i.record_no, i.vendor, i.amount, i.status AS entry_status, "
            "       i.doc_type, i.property_code "
            "FROM record_passes p LEFT JOIN intake_log i ON i.id = p.entry_id "
            "WHERE p.to_user = ? AND p.status = ? ORDER BY p.id",
            ((to_user or "").strip(), status)).fetchall()
        return [dict(r) for r in rows]

def get_passes_for_entry(entry_id: int) -> list:
    """Every pass of one record, newest first — shown on the record view."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM record_passes WHERE entry_id=? "
                            "ORDER BY id DESC", (entry_id,)).fetchall()
        return [dict(r) for r in rows]

def get_pass(pass_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM record_passes WHERE id=?",
                           (pass_id,)).fetchone()
        return dict(row) if row else None

def mark_pass_done(pass_id: int, by_user: str = "") -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE record_passes SET status='Done', "
            "done_at=datetime('now','localtime'), done_by=? "
            "WHERE id=? AND status='Open'", (by_user or "", pass_id))
        conn.commit()
        return cur.rowcount > 0

def open_pass_count(to_user: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM record_passes WHERE to_user=? AND status='Open'",
            ((to_user or "").strip(),)).fetchone()[0]

def save_vendor_hint(vendor: str, entity_code: str, property_codes: str,
                     doc_type: str, bill_type_code: str):
    """Upsert a learned correction for a vendor."""
    if not vendor:
        return
    key = vendor.strip().lower()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO vendor_hints (vendor_key, entity_code, property_codes, doc_type, bill_type_code, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(vendor_key) DO UPDATE SET
                entity_code=excluded.entity_code,
                property_codes=excluded.property_codes,
                doc_type=excluded.doc_type,
                bill_type_code=excluded.bill_type_code,
                updated_at=excluded.updated_at
        """, (key, entity_code, property_codes, doc_type, bill_type_code))
        conn.commit()

def get_vendor_hints(vendor: str) -> list:
    """Return learned hints for vendors whose name contains any word from the query."""
    if not vendor:
        return []
    words = [w.lower() for w in vendor.split() if len(w) > 2]
    if not words:
        return []
    with get_conn() as conn:
        placeholders = " OR ".join("vendor_key LIKE ?" for _ in words)
        params = [f"%{w}%" for w in words]
        rows = conn.execute(
            f"SELECT * FROM vendor_hints WHERE {placeholders} ORDER BY updated_at DESC LIMIT 5",
            params
        ).fetchall()
        return [dict(r) for r in rows]

def get_vendor_hint_exact(vendor: str):
    """The learned hint for exactly this vendor name (same key normalization as
    save_vendor_hint), or None. Auto-confirm's 'known vendor' gate — deliberately
    NOT the fuzzy word-match of get_vendor_hints, which is for prompting hints."""
    key = (vendor or "").strip().lower()
    if not key:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM vendor_hints WHERE vendor_key=?", (key,)).fetchone()
        return dict(row) if row else None

def find_duplicates(vendor: str, amount, due_date: str = None,
                    invoice_number: str = None, pdf_path: str = "",
                    received_date: str = None) -> list:
    """Return existing entries that look like the SAME bill as this one.

    Matches on the bill's stable identity first — an email copy and a physical
    copy of the same bill arrive on different days, so received-date matching
    alone would miss exactly the duplicates we care about. Match keys (any hit
    flags the item; nothing here auto-resolves):
      1. Same PDF file already filed.
      2. Same vendor + same invoice/statement number (the per-bill identifier).
      3. Same vendor + same amount + same due date (fallback when no invoice #).
      4. Same vendor + same amount + same received date (the same bill scanned
         twice out of one day's mail, or logged off the same batch).
    Note: account number is deliberately NOT a match key on its own — every
    month's bill from a utility shares one, so it would merge distinct bills."""
    with get_conn() as conn:
        results = []
        if pdf_path:
            rows = conn.execute(
                "SELECT * FROM intake_log WHERE pdf_path=? OR filed_path=?",
                (pdf_path, pdf_path)).fetchall()
            results += [dict(r) for r in rows]
        inv = (invoice_number or "").strip()
        if vendor and inv:
            rows = conn.execute(
                "SELECT * FROM intake_log WHERE LOWER(vendor)=LOWER(?) "
                "AND invoice_number IS NOT NULL AND TRIM(invoice_number)!='' "
                "AND LOWER(TRIM(invoice_number))=LOWER(?)",
                (vendor, inv)).fetchall()
            results += [dict(r) for r in rows]
        if vendor and amount is not None and due_date:
            rows = conn.execute(
                "SELECT * FROM intake_log WHERE LOWER(vendor)=LOWER(?) "
                "AND amount=? AND due_date=?",
                (vendor, amount, due_date)).fetchall()
            results += [dict(r) for r in rows]
        if vendor and amount is not None and received_date:
            rows = conn.execute(
                "SELECT * FROM intake_log WHERE LOWER(vendor)=LOWER(?) "
                "AND amount=? AND date_received=?",
                (vendor, amount, received_date)).fetchall()
            results += [dict(r) for r in rows]
        seen = set()
        unique = []
        for r in results:
            if r["id"] not in seen:
                seen.add(r["id"])
                unique.append(r)
        return unique

# ── Same-bill twins (the FAIR Plan #51-vs-#77 problem) ───────────────────────
# One real-world bill can be logged as several records whose BYTES differ (the
# insurer's copy and the agent's copy of one invoice), so the hash-based tools
# never pair them — and find_duplicates above can't either, because it requires
# the vendor strings to be equal ("California FAIR Plan Association" vs
# "… / Christopher Baxter Ins. Agy. Inc."). Paying one record then leaves its
# twins alive on the owed list. Per Karen's dedup tiers (2026-07-14): invoice #
# and amount are STRONG identity signals, vendor spelling is weak.

_TWIN_STOP_WORDS = {"THE", "INC", "LLC", "CO", "COMPANY", "OF", "AND", "FOR",
                    "CORP", "ASSOCIATION", "ASSOCIATES", "SERVICES", "SERVICE"}

def _invoice_key(value) -> str:
    """Invoice number normalized for identity comparison: uppercase
    alphanumerics only; '' when there's nothing real to compare."""
    key = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    return "" if key in ("TBD", "NA", "NONE", "PENDING", "UNKNOWN") else key

def _account_key(value) -> str:
    """Account number normalized for identity matching: trimmed; '' when the
    value is a placeholder rather than a real account — notably the literal
    'None' that NULL fields round-tripped into via the edit form (records
    780/791/796, template fixed 2026-07-23). Mirrors _invoice_key's guard."""
    key = (value or "").strip()
    return "" if key.upper() in ("TBD", "NA", "N/A", "NONE", "PENDING", "UNKNOWN") else key

def _vendor_words(vendor) -> set:
    # 2026-07-25 code review, P2: was `len(w) > 3`, which silently dropped every
    # 3-letter token -- including exactly the short abbreviations (SCE, DWP) real
    # bills get logged under ("Southern California Edison" as just "SCE"), the
    # real-world case this word-overlap fallback exists to catch. Lowered to
    # `len(w) >= 3`; safe because the explicit stopword set below already excludes
    # the common short words by name (THE, INC, LLC, CO, OF, AND, FOR), so this
    # doesn't reintroduce them -- it only stops filtering meaningful abbreviations.
    return {w for w in re.sub(r"[^A-Z0-9 ]", " ", (vendor or "").upper()).split()
            if len(w) >= 3 and not w.isdigit() and w not in _TWIN_STOP_WORDS}

def _same_invoice(a: str, b: str) -> bool:
    """Same normalized invoice #. Exact needs 4+ characters (so 'inv 123'
    placeholders can't pair); a prefix match needs 8+ plus a short suffix,
    for the insurer-vs-agency prints of one invoice ('CFP 0102713675' vs
    'CFP 0102713675 00')."""
    if not a or not b:
        return False
    if a == b:
        return len(a) >= 4
    lo, hi = sorted((a, b), key=len)
    return len(lo) >= 8 and hi.startswith(lo) and len(hi) - len(lo) <= 4

def _amount_entity_vendor_match(entry: dict, r: dict, words: set, amount, entity: str) -> bool:
    """Shared fallback match (no usable invoice number) for both find_invoice_twins
    and find_link_candidates: same entity, the exact same amount, a shared vendor
    word, and the received OR due date lining up (the date guard is what keeps a
    monthly recurring bill's next cycle -- same payee, same amount -- from reading
    as a twin/candidate). Extracted 2026-07-25 code review (the two callers had
    duplicated this verbatim) so future tuning only has to happen here."""
    return bool(
        amount is not None and entity
        and r.get("amount") == amount
        and (r.get("entity_code") or "").strip() == entity
        and ((entry.get("date_received") and
              r.get("date_received") == entry.get("date_received"))
             or (entry.get("due_date") and
                 r.get("due_date") == entry.get("due_date")))
        and words & _vendor_words(r.get("vendor")))

def find_invoice_twins(entry: dict) -> list:
    """OTHER Bill records (any status) that look like the same underlying bill
    as `entry`. A twin shares either:
      * the invoice number (normalized, small-suffix tolerant), or
      * the entity + the exact amount + a vendor word + the received OR due
        date (the date guard keeps a monthly recurring bill's next cycle —
        same payee, same amount — from reading as a twin).
    Oldest first. Callers split open vs paid twins for messaging."""
    inv = _invoice_key(entry.get("invoice_number"))
    amount = entry.get("amount")
    entity = (entry.get("entity_code") or "").strip()
    words = _vendor_words(entry.get("vendor"))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log WHERE doc_type='Bill' AND id != ?",
            (entry.get("id") or 0,)).fetchall()
    twins = []
    for r in (dict(r) for r in rows):
        if _same_invoice(inv, _invoice_key(r.get("invoice_number"))):
            twins.append(r)
        elif _amount_entity_vendor_match(entry, r, words, amount, entity):
            twins.append(r)
    return sorted(twins, key=lambda r: r["id"])

def find_link_candidates(entry: dict) -> list:
    """OTHER records (any doc_type) that might be the 'original' document for
    `entry`, or vice versa -- e.g. entry is a payment receipt and one of these
    is the underlying bill. Same heuristics as find_invoice_twins (invoice #,
    or entity+amount+vendor+date window) plus an account-number match, since
    utility accounts (GOLETA WATER etc.) are the common real-world case this
    exists for. Not restricted to doc_type='Bill' -- unlike twins, a payment
    record's original may be logged under a different doc_type entirely."""
    inv = _invoice_key(entry.get("invoice_number"))
    amount = entry.get("amount")
    entity = (entry.get("entity_code") or "").strip()
    acct = _account_key(entry.get("account_number"))
    words = _vendor_words(entry.get("vendor"))
    with get_conn() as conn:
        # Narrowed to the columns match_candidates actually needs (id, matching
        # fields, and what the "Link original" card displays) -- this used to
        # be SELECT * over the whole table on every record view.
        rows = conn.execute(
            "SELECT id, invoice_number, amount, entity_code, vendor, account_number, "
            "date_received, due_date, doc_type, filed_path, pdf_path "
            "FROM intake_log WHERE id != ?",
            (entry.get("id") or 0,)).fetchall()
    candidates = []
    for r in (dict(r) for r in rows):
        if acct and _account_key(r.get("account_number")) == acct:
            candidates.append(r)
        elif _same_invoice(inv, _invoice_key(r.get("invoice_number"))):
            candidates.append(r)
        elif _amount_entity_vendor_match(entry, r, words, amount, entity):
            candidates.append(r)
    return sorted(candidates, key=lambda r: r["id"])

def set_no_original(entry_id: int, flag: bool, user: str = ""):
    """Toggle the 'searched, no source invoice exists' flag (#5). Distinguishes
    a genuine gap (checked, nothing to find) from one nobody's looked at yet."""
    with get_conn() as conn:
        if flag:
            conn.execute(
                "UPDATE intake_log SET no_original_on_file=1, no_original_by=?, "
                "no_original_at=datetime('now','localtime') WHERE id=?",
                (user or "", entry_id))
        else:
            conn.execute(
                "UPDATE intake_log SET no_original_on_file=0, no_original_by=NULL, "
                "no_original_at=NULL WHERE id=?", (entry_id,))
        conn.commit()

def save_account_hint(account_number, entity_code, property_codes):
    """Learn account-number -> property (and entity), so recurring utility bills
    auto-assign the property field next time. Account # is a strong, stable signal."""
    key = (account_number or "").strip().lower()
    if not key:
        return
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO account_hints (account_key, entity_code, property_codes, updated_at)
            VALUES (?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(account_key) DO UPDATE SET
                entity_code=excluded.entity_code,
                property_codes=excluded.property_codes,
                updated_at=excluded.updated_at
        """, (key, entity_code or "", property_codes or ""))
        conn.commit()

def get_account_hint(account_number):
    key = (account_number or "").strip().lower()
    if not key:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM account_hints WHERE account_key=?", (key,)).fetchone()
        return dict(row) if row else None

def get_vendor_summary() -> list:
    """Unpaid/pending bills grouped by vendor with totals and earliest due date."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT vendor,
                   COUNT(*) as bill_count,
                   COALESCE(SUM(amount),0) as total_amount,
                   MIN(NULLIF(due_date,'')) as next_due,
                   GROUP_CONCAT(DISTINCT entity_code) as entities
            FROM intake_log
            WHERE doc_type='Bill'
              AND status NOT IN ('Paid','Filed')
              AND vendor IS NOT NULL AND vendor != ''
            GROUP BY LOWER(vendor)
            ORDER BY next_due ASC, total_amount DESC
        """).fetchall()
        return [dict(r) for r in rows]

def get_entity_summary() -> list:
    """Unpaid bills grouped by entity with total owed, count, and earliest due date."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT entity_code, entity_name,
                   COUNT(*) as bill_count,
                   COALESCE(SUM(amount),0) as total_amount,
                   MIN(NULLIF(due_date,'')) as next_due
            FROM intake_log
            WHERE doc_type='Bill'
              AND status NOT IN ('Paid','Filed')
              AND entity_code IS NOT NULL AND entity_code != ''
            GROUP BY entity_code
            ORDER BY total_amount DESC
        """).fetchall()
        return [dict(r) for r in rows]

def get_property_summary() -> list:
    """Unpaid bills grouped by property. A bill can be tagged to several properties
    (comma-separated property_code), so the full amount is counted under EACH of its
    properties — section totals can therefore exceed the grand total."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT amount, due_date, property_code
            FROM intake_log
            WHERE doc_type='Bill' AND status NOT IN ('Paid','Filed')
        """).fetchall()
    agg = {}
    for r in rows:
        codes = [c.strip() for c in (r["property_code"] or "").split(",") if c.strip()]
        if not codes:
            codes = ["(unassigned)"]
        for code in codes:
            a = agg.setdefault(code, {"property_code": code, "bill_count": 0,
                                      "total_amount": 0.0, "next_due": None})
            a["bill_count"] += 1
            a["total_amount"] += (r["amount"] or 0)
            dd = r["due_date"]
            if dd and (a["next_due"] is None or dd < a["next_due"]):
                a["next_due"] = dd
    return sorted(agg.values(), key=lambda x: -x["total_amount"])

def get_bills_by_due_date() -> list:
    """All unpaid bills ordered by due date."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM intake_log
            WHERE doc_type='Bill' AND status NOT IN ('Paid','Filed')
            ORDER BY
                CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
                due_date ASC
        """).fetchall()
        return [dict(r) for r in rows]

def get_oldest_unpaid_bill():
    """The next bill in Karen's pay-in-received-order stack — what the
    new-look dashboard's single filled button points at. Same unpaid
    definition as get_bills_by_due_date; oldest received first, blank
    received dates last (a bill with no date can't claim "next")."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM intake_log
            WHERE doc_type='Bill' AND status NOT IN ('Paid','Filed')
            ORDER BY
                CASE WHEN date_received IS NULL OR date_received='' THEN 1 ELSE 0 END,
                date_received ASC, id ASC
            LIMIT 1
        """).fetchone()
        return dict(row) if row else None

# Columns the universal search scans. Every field a staffer might remember a
# document by — including the amount, the dates, and the original filename
# (pdf_path). Kept as a list so the search stays one obvious place to extend.
_SEARCH_COLUMNS = ("vendor", "invoice_number", "account_number", "property_code",
                   "entity_code", "entity_name", "doc_type", "bill_type_code",
                   "notes", "pdf_path", "filed_path", "date_received", "due_date")

def search_documents(query: str, limit: int = 200) -> list:
    """Universal keyword search across ALL logged records (any doc_type, paid or
    not) — the Bills-page search box. Case-insensitive substring match over
    _SEARCH_COLUMNS plus the amount. Newest-received first, capped at `limit`.
    NB: this searches the intake log only, not the contents of the PDF files."""
    q = (query or "").strip()
    if not q:
        return []
    kw = f"%{q.lower()}%"
    clauses = [f"LOWER(COALESCE({c},'')) LIKE ?" for c in _SEARCH_COLUMNS]
    clauses.append("CAST(COALESCE(amount,'') AS TEXT) LIKE ?")   # match "229.36"
    params = [kw] * len(_SEARCH_COLUMNS) + [kw] + [limit]
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM intake_log WHERE {' OR '.join(clauses)} "
            "ORDER BY date_received DESC, id DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

def get_accounts_for_vendor(vendor: str) -> list:
    """Group a vendor's bills by account number: which property/entity each account
    maps to, how many are unpaid, and the amount due. (Balance/payment-method come
    later.)"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT account_number, entity_code, property_code, amount, status, due_date
            FROM intake_log
            WHERE LOWER(vendor)=LOWER(?) AND doc_type='Bill'
        """, (vendor,)).fetchall()
    agg = {}
    for r in rows:
        acct = (r["account_number"] or "").strip() or "(no account #)"
        a = agg.setdefault(acct, {"account_number": acct, "entity_code": "",
                                  "property_code": "", "unpaid_count": 0,
                                  "amount_due": 0.0, "total_count": 0, "next_due": None})
        a["total_count"] += 1
        if not a["entity_code"] and r["entity_code"]:
            a["entity_code"] = r["entity_code"]
        if not a["property_code"] and r["property_code"]:
            a["property_code"] = r["property_code"]
        if r["status"] not in ("Paid", "Filed"):
            a["unpaid_count"] += 1
            a["amount_due"] += (r["amount"] or 0)
            dd = r["due_date"]
            if dd and (a["next_due"] is None or dd < a["next_due"]):
                a["next_due"] = dd
    return sorted(agg.values(), key=lambda x: (-x["amount_due"], x["account_number"]))

def get_bills_for_vendor(vendor: str) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM intake_log
            WHERE LOWER(vendor)=LOWER(?) AND doc_type='Bill'
            ORDER BY CASE WHEN status NOT IN ('Paid','Filed') THEN 0 ELSE 1 END, due_date ASC
        """, (vendor,)).fetchall()
        return [dict(r) for r in rows]

def delete_entry(entry_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM intake_log WHERE id=?", (entry_id,))
        conn.commit()

def _record_no_prefix(date_str):
    """YYMMDD prefix of a record number, from a YYYY-MM-DD date (None if unparseable)."""
    try:
        return datetime.strptime((date_str or "")[:10], "%Y-%m-%d").strftime("%y%m%d")
    except ValueError:
        return None

def next_record_no(conn, date_received) -> str:
    """Next human-facing record number (YYMMDD-NNN) for a received date.
    Encodes date_received because Karen pays in received order; assigned once
    at creation and never changed, even if the date is corrected later. The
    counters table keeps the day's high-water mark so a number is never reused
    after its record is deleted (e.g. by merge-record)."""
    prefix = _record_no_prefix(date_received) or datetime.now().strftime("%y%m%d")
    row = conn.execute("SELECT top FROM record_no_counters WHERE prefix=?",
                       (prefix,)).fetchone()
    top = row["top"] if row else 0
    for r in conn.execute("SELECT record_no FROM intake_log WHERE record_no LIKE ?",
                          (prefix + "-%",)):
        try:
            top = max(top, int(r["record_no"].split("-", 1)[1]))
        except (IndexError, ValueError):
            pass
    nxt = top + 1
    conn.execute("INSERT INTO record_no_counters(prefix, top) VALUES(?,?) "
                 "ON CONFLICT(prefix) DO UPDATE SET top=MAX(top, excluded.top)",
                 (prefix, nxt))
    return f"{prefix}-{nxt:03d}"

# ── Impossible-year net (record #941, 2026-08-06) ────────────────────────────
# The hand-filed name "06-30-256 ..." (a year typo for 26) parsed to 0256-06-30,
# was confirmed before bulk confirm learned to skip flagged dates (2026-07-31),
# and minted the permanent record number 560630-001 plus a year-0256 sort key.
# The year-sanity guard in app.py only covers the forms; every insert funnels
# through insert_entry, so this is the last net. Deliberately narrow: it acts
# only on years that cannot be office data AT ALL (KAREN_FILENAME_SPEC §3.1 —
# every year in the archive is 20xx), stores a sane date instead of the raw
# value, and flags the record loudly. Suspicious-but-REAL dates (last year's
# backlog, the 2007+ archive) still pass untouched — judging those stays a
# human's job (test_direct_insert_not_blocked pins that).

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")

def _impossible_year(v) -> bool:
    """True only for an ISO-shaped date whose year is outside 2000–2099.
    Blank or free-text values return False — required-ness and format are the
    forms' concern, not this net's."""
    s = (v or "").strip()
    return bool(_ISO_DATE_RE.fullmatch(s)) and not "2000" <= s[:4] <= "2099"

def _year_net(data: dict) -> list:
    """Apply the impossible-year net to a pending insert's dict, in place.
    Returns the list of what was corrected (empty when nothing was)."""
    flags = []
    raw = (data.get("date_received") or "").strip()
    if _impossible_year(raw):
        # Land on the scan date when it's sane (the closest truth on file),
        # else today — so record_no mints from a real day, and the raw value
        # stays visible in the flag for the person who fixes it.
        stamp = (data.get("scanned_date") or "").strip()
        if not _ISO_DATE_RE.fullmatch(stamp) or _impossible_year(stamp):
            stamp = datetime.now().strftime("%Y-%m-%d")
        data["date_received"] = stamp
        flags.append(f"Received date read as {raw} — an impossible year; "
                     f"stored {stamp} until a person fixes it")
    raw_due = (data.get("due_date") or "").strip()
    if _impossible_year(raw_due):
        data["due_date"] = ""
        flags.append(f"Due date read as {raw_due} — an impossible year; cleared")
    if flags:
        data["important"] = 1
        data["flagged_at"] = data.get("flagged_at") or \
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["important_reason"] = "; ".join(
            filter(None, [(data.get("important_reason") or "").strip(), *flags]))
    return flags

def insert_entry(data: dict) -> int:
    data = dict(data)  # record_no is assigned below; don't mutate the caller's dict
    date_flags = _year_net(data)
    allowed = {
        "date_received","mail_type","entity_code","entity_name",
        "property_code","property_address","doc_type","bill_type_code",
        "vendor","amount","due_date","status","processed_by","notes",
        "pdf_path","filed_path","ai_confidence",
        "paid_date","paid_amount","paid_by",
        "invoice_number","account_number",
        "scanned_date","batch_number",
        "payment_method","check_number","payment_note",
        "email_from","email_to","email_account","email_subject","email_date","email_body",
        "file_hash","important","important_reason","notice_summary",
        "flagged_at","acknowledged_at","acknowledged_by","record_no"
    }
    supplied_no = bool(data.get("record_no"))
    with get_conn() as conn:
        # The generate→insert pair can race another writer thread (watcher,
        # email poller) to the same number; the unique index turns that into
        # an IntegrityError we answer by generating the next number.
        for attempt in range(5):
            if not supplied_no:
                data["record_no"] = next_record_no(conn, data.get("date_received"))
            cols = [k for k in data if k in allowed]
            placeholders = ", ".join("?" * len(cols))
            col_str = ", ".join(cols)
            vals = [data[c] for c in cols]
            try:
                cur = conn.execute(f"INSERT INTO intake_log ({col_str}) VALUES ({placeholders})", vals)
                if date_flags:
                    # Same transaction as the insert — the correction is never
                    # separable from the row it explains.
                    conn.execute("INSERT INTO activity_log (entry_id, action, detail, user) "
                                 "VALUES (?,?,?,'')",
                                 (cur.lastrowid, "Date guard", "; ".join(date_flags)))
                conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                if supplied_no or attempt == 4:
                    raise
                # Roll back the failed write txn so the retry's SELECT can see
                # the competing row that just claimed our number.
                conn.rollback()

def update_entry(entry_id: int, data: dict):
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    allowed = {
        "date_received","mail_type","entity_code","entity_name",
        "property_code","property_address","doc_type","bill_type_code",
        "vendor","amount","due_date","status","processed_by","notes",
        "pdf_path","filed_path","ai_confidence","updated_at",
        "paid_date","paid_amount","paid_by",
        "invoice_number","account_number",
        "scanned_date","batch_number",
        "payment_method","check_number","payment_note",
        "email_from","email_to","email_account","email_subject","email_date","email_body",
        "file_hash","important","important_reason","notice_summary",
        "flagged_at","acknowledged_at","acknowledged_by",
        "email_appended","filed_sha256"
    }
    pairs = [(k, v) for k, v in data.items() if k in allowed]
    set_clause = ", ".join(f"{k}=?" for k, _ in pairs)
    vals = [v for _, v in pairs] + [entry_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE intake_log SET {set_clause} WHERE id=?", vals)
        conn.commit()

def rename_entity_code(old_code, new_code, new_name=None):
    """Cascade an entity-code rename (and name change) into the records that
    reference it, so nothing is orphaned. Config (entities/properties) is handled
    by the caller; this covers the database tables.

    2026-07-25 code review, P2: this used to cascade into intake_log and
    vendor_hints only, silently leaving account_hints (the account auto-assign
    cache) and expected_bills (the recurring-bill radar) pointed at the old code
    -- both single-value entity_code columns, so a plain equality UPDATE (no
    comma-splitting needed, unlike property_code below)."""
    with get_conn() as conn:
        if new_code != old_code:
            for tbl in ("intake_log", "vendor_hints", "account_hints", "expected_bills"):
                conn.execute(f"UPDATE {tbl} SET entity_code=? WHERE entity_code=?",
                             (new_code, old_code))
        if new_name is not None:
            conn.execute("UPDATE intake_log SET entity_name=? WHERE entity_code=?",
                         (new_name, new_code))
        conn.commit()

def rename_property_code(old_code, new_code):
    """Cascade a property-code rename through every comma-separated property_code
    /property_codes field that references it. Exact-token match, so renaming
    'T40' never touches 'T40B'.

    2026-07-25 code review, P2: extended from (intake_log, vendor_hints) to also
    cover account_hints.property_codes (the account auto-assign cache) and
    expected_bills.property_code (the recurring-bill radar) -- both were silently
    left holding the old code. account_hints has no surrogate `id` column (its
    primary key is account_key), so the table list below carries each table's own
    primary-key column name rather than assuming `id` everywhere."""
    def _swap(csv):
        parts = [p.strip() for p in (csv or "").split(",") if p.strip()]
        return ", ".join(new_code if p == old_code else p for p in parts)
    changed = 0
    with get_conn() as conn:
        for tbl, col, pk in (
                ("intake_log", "property_code", "id"),
                ("vendor_hints", "property_codes", "id"),
                ("account_hints", "property_codes", "account_key"),
                ("expected_bills", "property_code", "id")):
            rows = conn.execute(f"SELECT {pk} AS pk, {col} AS v FROM {tbl} WHERE {col} LIKE ?",
                                (f"%{old_code}%",)).fetchall()
            for r in rows:
                nv = _swap(r["v"])
                if nv != (r["v"] or ""):
                    conn.execute(f"UPDATE {tbl} SET {col}=? WHERE {pk}=?", (nv, r["pk"]))
                    changed += 1
        conn.commit()
    return changed

def rename_bill_type_code(old_code, new_code):
    """Cascade a bill-type-code rename to records and vendor hints that use it."""
    with get_conn() as conn:
        conn.execute("UPDATE intake_log SET bill_type_code=? WHERE bill_type_code=?",
                     (new_code, old_code))
        conn.execute("UPDATE vendor_hints SET bill_type_code=? WHERE bill_type_code=?",
                     (new_code, old_code))
        conn.commit()

def get_entry(entry_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM intake_log WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None

def get_entry_by_record_no(record_no: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM intake_log WHERE record_no=?", (record_no,)).fetchone()
        return dict(row) if row else None

def get_entries_for_paths(paths: list) -> list:
    """Records whose filed/pdf path is one of `paths`, case-insensitive — the
    Files view's overlay join. Chunked so a 500-file folder costs a couple of
    queries, never one per file."""
    want = [p for p in {(p or "").strip() for p in (paths or [])} if p]
    if not want:
        return []
    out = []
    with get_conn() as conn:
        for i in range(0, len(want), 400):
            chunk = want[i:i + 400]
            qs = ", ".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id, record_no, status, doc_type, amount, "
                f"filed_path, pdf_path FROM intake_log "
                f"WHERE filed_path COLLATE NOCASE IN ({qs}) "
                f"   OR pdf_path COLLATE NOCASE IN ({qs})",
                chunk + chunk).fetchall()
            out.extend(dict(r) for r in rows)
    return out

def get_attachments_for_paths(paths: list) -> list:
    """Attachment copies at these paths, each with its owner record's label —
    the Files view's 'copy of <record>' chip."""
    want = [p for p in {(p or "").strip() for p in (paths or [])} if p]
    if not want:
        return []
    out = []
    with get_conn() as conn:
        for i in range(0, len(want), 400):
            chunk = want[i:i + 400]
            qs = ", ".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT a.file_path, a.entry_id, i.record_no, i.status, "
                f"i.doc_type FROM attachments a "
                f"JOIN intake_log i ON i.id = a.entry_id "
                f"WHERE a.file_path COLLATE NOCASE IN ({qs})",
                chunk).fetchall()
            out.extend(dict(r) for r in rows)
    return out

def get_entries_by_hash(file_hash: str) -> list:
    """Logged entries whose document bytes match this sha256 (dup detection).
    Matches the ORIGINAL bytes (file_hash) or the filed bytes as rewritten by
    the email append (filed_sha256) — so a filed PDF that gained email pages
    still hash-matches its record for the heal paths and cabinet_watch."""
    if not file_hash:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log WHERE file_hash=? OR filed_sha256=? ORDER BY id",
            (file_hash, file_hash)).fetchall()
        return [dict(r) for r in rows]


def get_entries_by_hashes(hashes: list) -> list:
    """Slim rows for a batch of document hashes — the /mail page's join from
    archived attachments to the records they became. Matches file_hash or
    filed_sha256, chunked like get_attachments_for_paths."""
    want = [h for h in {(h or "").strip() for h in (hashes or [])} if h]
    if not want:
        return []
    out = []
    with get_conn() as conn:
        for i in range(0, len(want), 400):
            chunk = want[i:i + 400]
            qs = ", ".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id, record_no, doc_type, status, vendor, filed_path, "
                f"pdf_path, file_hash, filed_sha256, email_appended, "
                f"important, important_reason FROM intake_log "
                f"WHERE file_hash IN ({qs}) OR filed_sha256 IN ({qs})",
                chunk + chunk).fetchall()
            out.extend(dict(r) for r in rows)
    return out

def get_hash_dupe_groups() -> list:
    """Groups of LOGGED records sharing the same document bytes — the same file
    logged as two or more records (double-logs from before ingest-time
    deduplication existed). Returns a list of groups, each a list of record
    dicts ordered oldest-first."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM intake_log
            WHERE file_hash IS NOT NULL AND file_hash != ''
              AND file_hash IN (SELECT file_hash FROM intake_log
                                WHERE file_hash IS NOT NULL AND file_hash != ''
                                GROUP BY file_hash HAVING COUNT(*) > 1)
            ORDER BY file_hash, id""").fetchall()
    groups, by_hash = [], {}
    for r in rows:
        d = dict(r)
        by_hash.setdefault(d["file_hash"], []).append(d)
    for g in by_hash.values():
        groups.append(g)
    groups.sort(key=lambda g: g[0]["id"])
    return groups

def reassign_attachments(from_entry_id: int, to_entry_id: int):
    """Re-point one record's 'Copies on file' rows at another record (used
    when merging duplicate records — files themselves are never touched)."""
    with get_conn() as conn:
        conn.execute("UPDATE attachments SET entry_id=? WHERE entry_id=?",
                     (to_entry_id, from_entry_id))
        conn.commit()

def reassign_payment_splits(from_entry_id: int, to_entry_id: int):
    """Re-point a record's payment splits at another record (merge only)."""
    with get_conn() as conn:
        conn.execute("UPDATE payment_splits SET entry_id=? WHERE entry_id=?",
                     (to_entry_id, from_entry_id))
        conn.commit()


# ── Expected bills (recurring-bills radar) ────────────────────────────────────

def add_expected_bill(vendor, account_number="", entity_code="", property_code="",
                      cadence="monthly", typical_amount=None, source="manual", notes=""):
    if not (vendor or "").strip():
        return None
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO expected_bills (vendor, account_number, entity_code, "
            "property_code, cadence, typical_amount, source, notes) VALUES (?,?,?,?,?,?,?,?)",
            (vendor.strip(), account_number or "", entity_code or "", property_code or "",
             cadence or "monthly", typical_amount, source, notes or ""))
        conn.commit()
        return cur.lastrowid

def get_expected_bills(active_only=False):
    with get_conn() as conn:
        sql = "SELECT * FROM expected_bills"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY LOWER(vendor)"
        return [dict(r) for r in conn.execute(sql)]

def set_expected_bill_active(eb_id, active):
    with get_conn() as conn:
        conn.execute("UPDATE expected_bills SET active=? WHERE id=?",
                     (1 if active else 0, eb_id))
        conn.commit()

def delete_expected_bill(eb_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM expected_bills WHERE id=?", (eb_id,))
        conn.commit()

def bills_seen_since(date_from: str) -> list:
    """(vendor, account_number, max date_received) for Bill records received on or
    after date_from — the arrival evidence the radar matches expected bills against."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT vendor, account_number, MAX(date_received) AS last_seen "
            "FROM intake_log WHERE doc_type='Bill' AND date_received >= ? "
            "AND vendor IS NOT NULL AND vendor != '' "
            "GROUP BY LOWER(vendor), LOWER(COALESCE(account_number,''))",
            (date_from,)).fetchall()
        return [dict(r) for r in rows]

def get_entries_missing_hash() -> list:
    """Entries that predate dup detection — targets for the startup backfill."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filed_path, pdf_path FROM intake_log "
            "WHERE file_hash IS NULL OR file_hash=''").fetchall()
        return [dict(r) for r in rows]

def get_important_correspondence(limit: int = 6, mail_type: str = None) -> list:
    """Documents that need eyes and haven't been filed away yet — Legal and
    Correspondence doc types, plus anything a person manually flagged important
    (a specific bill can need attention even though it isn't legal). Surfaced
    separately from bills on the dashboard because an eviction notice or
    attorney letter is time-sensitive in a way a due date doesn't capture, and
    it would otherwise get buried in the Intake Log. Most recent first.
    mail_type narrows to one arrival channel (e.g. 'Email' for the dashboard's
    from-email box — Karen's worry is email slipping past unseen)."""
    mt_clause = "AND mail_type = ? " if mail_type else ""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log "
            "WHERE (doc_type IN ('Legal','Correspondence') OR important=1 "
            "       OR (notice_summary IS NOT NULL AND notice_summary != '')) "
            "AND (status IS NULL OR status != 'Filed') "
            "AND (acknowledged_at IS NULL OR acknowledged_at='') "
            + mt_clause +
            "ORDER BY date_received DESC, created_at DESC LIMIT ?",
            ((mail_type, limit) if mail_type else (limit,))).fetchall()
        return [dict(r) for r in rows]

def get_board_items() -> list:
    """The corkboard population: the same needs-eyes rule as
    get_important_correspondence, minus anything already taken down, no limit.
    Newest pin first (flagged_at, falling back to created_at)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log "
            "WHERE (doc_type IN ('Legal','Correspondence') OR important=1 "
            "       OR (notice_summary IS NOT NULL AND notice_summary != '')) "
            "AND (status IS NULL OR status != 'Filed') "
            "AND (acknowledged_at IS NULL OR acknowledged_at='') "
            "ORDER BY COALESCE(NULLIF(flagged_at,''), created_at) DESC").fetchall()
        return [dict(r) for r in rows]

def count_taken_down_since(iso_ts: str) -> int:
    """How many board items were taken down at/after the given timestamp."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM intake_log "
            "WHERE acknowledged_at IS NOT NULL AND acknowledged_at != '' "
            "AND acknowledged_at >= ?", (iso_ts,)).fetchone()
        return row[0] if row else 0

def get_bills_for_month(ym: str) -> list:
    """Every bill whose due date falls in YYYY-MM, paid or not — the wall
    calendar shows the whole month's picture, not just the open items."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log WHERE doc_type='Bill' AND due_date LIKE ? "
            "ORDER BY due_date ASC, id ASC", (ym + "-%",)).fetchall()
        return [dict(r) for r in rows]

def record_queue_depth(day: str, depth: int, urgent: int):
    """Upsert a day's review-queue depth (tray gauge history)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO queue_depth_history (day, depth, urgent, recorded_at) "
            "VALUES (?, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(day) DO UPDATE SET depth=excluded.depth, "
            "urgent=excluded.urgent, recorded_at=excluded.recorded_at",
            (day, depth, urgent))
        conn.commit()

def get_vendor_directory() -> list:
    """Every vendor ever logged — the rolodex. Doc count, bill count, last seen."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT vendor, COUNT(*) AS cnt,
                   SUM(CASE WHEN doc_type='Bill' THEN 1 ELSE 0 END) AS bill_cnt,
                   MAX(date_received) AS last_seen
            FROM intake_log
            WHERE vendor IS NOT NULL AND vendor != ''
            GROUP BY LOWER(vendor)
            ORDER BY vendor COLLATE NOCASE
        """).fetchall()
        return [dict(r) for r in rows]

# ── Vendor spelling drift (Karen, 2026-07-18: saw 3-4 spellings of Southern
# California Edison) — catch it going forward at confirm time, and clean up
# what's already fragmented. Plain difflib, no new dependency: this only needs
# to catch punctuation/abbreviation/case drift on an already-short-listed
# vendor name, not general fuzzy search. ──────────────────────────────────────
_VENDOR_STOPWORDS = {"THE", "INC", "LLC", "CO", "COMPANY", "CORP", "CORPORATION"}

def _vendor_norm(name: str) -> str:
    """Normalize a vendor name for drift comparison: drop parenthetical
    abbreviations ('(SCE)'), punctuation, case, and generic suffix words, so
    'Southern California Edison Company (SCE)' and 'SOUTHERN CALIF. EDISON'
    land close together."""
    s = re.sub(r"\([^)]*\)", " ", name or "")
    s = re.sub(r"[^\w\s]", " ", s.upper())
    words = [w for w in s.split() if w not in _VENDOR_STOPWORDS]
    return " ".join(words)

def find_similar_vendor(name: str, threshold: float = 0.86):
    """The closest EXISTING logged vendor to `name`, if it's likely the same
    company under a different spelling. Returns None on an exact
    (case-insensitive) match — nothing to confirm — or when nothing already on
    file is close enough. Used to prompt 'same vendor?' at confirm time before
    a new spelling fragments that vendor's history."""
    name = (name or "").strip()
    norm = _vendor_norm(name)
    if not norm:
        return None
    with get_conn() as conn:
        existing = [r[0] for r in conn.execute(
            "SELECT DISTINCT vendor FROM intake_log WHERE vendor IS NOT NULL AND vendor != ''")]
    best, best_score = None, 0.0
    for v in existing:
        if v.strip().lower() == name.lower():
            return None
        vn = _vendor_norm(v)
        if not vn:
            continue
        score = difflib.SequenceMatcher(None, norm, vn).ratio()
        if score > best_score:
            best, best_score = v, score
    return {"vendor": best, "score": round(best_score, 3)} if best_score >= threshold else None

def find_vendor_clusters(threshold: float = 0.86) -> list:
    """Group EXISTING distinct vendors into likely-duplicate clusters by the
    same fuzzy match as find_similar_vendor — the one-time cleanup view (the
    rolodex's 'possible duplicate vendors' panel). Each cluster is a list of
    {vendor, cnt} sorted by doc count (most-used first, the suggested keeper);
    singles (no close match) are omitted."""
    items = [(d["vendor"], d["cnt"]) for d in get_vendor_directory()]
    norm_of = {v: _vendor_norm(v) for v, _ in items}
    assigned = set()
    clusters = []
    for i, (v, cnt) in enumerate(items):
        if v in assigned or not norm_of[v]:
            continue
        group = [(v, cnt)]
        for v2, cnt2 in items[i + 1:]:
            if v2 in assigned or not norm_of[v2]:
                continue
            if difflib.SequenceMatcher(None, norm_of[v], norm_of[v2]).ratio() >= threshold:
                group.append((v2, cnt2))
        if len(group) > 1:
            group.sort(key=lambda x: -x[1])
            assigned.update(v2 for v2, _ in group)
            clusters.append([{"vendor": v2, "cnt": c2} for v2, c2 in group])
    return clusters

def rename_vendor(old_name: str, new_name: str) -> int:
    """Merge one vendor spelling into another everywhere it's referenced:
    intake_log.vendor (exact case-insensitive match), filtered_docs.vendor,
    expected_bills.vendor, and vendor_hints (rekeyed — if the target spelling
    already has a hint, the old one is dropped rather than overwriting it, so
    a manually-corrected hint always wins). Returns how many intake_log rows
    changed."""
    old_name, new_name = (old_name or "").strip(), (new_name or "").strip()
    old_key, new_key = old_name.lower(), new_name.lower()
    with get_conn() as conn:
        cur = conn.execute("UPDATE intake_log SET vendor=? WHERE LOWER(vendor)=?",
                           (new_name, old_key))
        changed = cur.rowcount
        conn.execute("UPDATE filtered_docs SET vendor=? WHERE LOWER(vendor)=?",
                     (new_name, old_key))
        conn.execute("UPDATE expected_bills SET vendor=? WHERE LOWER(vendor)=?",
                     (new_name, old_key))
        if old_key != new_key:
            has_target = conn.execute(
                "SELECT 1 FROM vendor_hints WHERE vendor_key=?", (new_key,)).fetchone()
            if has_target:
                conn.execute("DELETE FROM vendor_hints WHERE vendor_key=?", (old_key,))
            else:
                conn.execute("UPDATE vendor_hints SET vendor_key=? WHERE vendor_key=?",
                             (new_key, old_key))
        conn.commit()
    return changed

def entity_doc_counts() -> dict:
    """entity_code -> total documents logged (the cabinet's drawer labels).
    Unassigned documents count under ''."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(entity_code,'') AS ec, COUNT(*) AS cnt "
            "FROM intake_log GROUP BY COALESCE(entity_code,'')").fetchall()
        return {r["ec"]: r["cnt"] for r in rows}

def months_for_entity(entity_code: str, limit: int = 14) -> list:
    """Months (YYYY-MM, newest first) holding documents for one entity — the
    cabinet drawer's hanging-folder tabs."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT substr(date_received,1,7) AS ym, COUNT(*) AS cnt "
            "FROM intake_log WHERE COALESCE(entity_code,'') = ? "
            "AND date_received IS NOT NULL AND date_received != '' "
            "GROUP BY ym ORDER BY ym DESC LIMIT ?", (entity_code or "", limit)).fetchall()
        return [dict(r) for r in rows]

def docs_for_entity_month(entity_code: str, ym: str) -> list:
    """One hanging folder: everything for an entity received in YYYY-MM."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_log WHERE COALESCE(entity_code,'') = ? "
            "AND date_received LIKE ? ORDER BY date_received ASC, id ASC",
            (entity_code or "", ym + "-%")).fetchall()
        return [dict(r) for r in rows]

def confirm_counts_for_day(day: str) -> dict:
    """Tray in/out counters: how many queue items were confirmed into the log
    on a day, and how many of those had also been scanned that same day."""
    with get_conn() as conn:
        cleared = conn.execute(
            "SELECT COUNT(*) FROM intake_log WHERE created_at LIKE ?",
            (day + "%",)).fetchone()[0]
        same_day = conn.execute(
            "SELECT COUNT(*) FROM intake_log WHERE created_at LIKE ? "
            "AND scanned_date LIKE ?", (day + "%", day + "%")).fetchone()[0]
        return {"cleared": cleared, "landed_and_cleared": same_day}

def get_queue_depth_history(days: int = 14) -> list:
    """The last N recorded days of queue depth, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT day, depth, urgent FROM queue_depth_history "
            "ORDER BY day DESC LIMIT ?", (days,)).fetchall()
        return [dict(r) for r in reversed(rows)]

def get_all_entries(filters: dict = None):
    sql = "SELECT * FROM intake_log WHERE 1=1"
    vals = []
    if filters:
        # status_not: exclude one status ("Filed" for the Intake Log's default
        # view — archived documents are search/Files-view material, not daily
        # work). Only a FILTER can exclude: a bare get_all_entries() must keep
        # returning every row, because cabinet_watch's twin-matching and the
        # batch-number scan rely on seeing Filed records too.
        if filters.get("status_not") and not filters.get("status"):
            sql += " AND (status IS NULL OR status != ?)"
            vals.append(filters["status_not"])
        for k, v in filters.items():
            if not v or k not in {"entity_code","property_code","doc_type","status","mail_type","processed_by","important"}:
                continue
            # status may be a comma-separated list (e.g. "Processed,Paid") -> IN clause
            if k == "status" and "," in v:
                parts = [s.strip() for s in v.split(",") if s.strip()]
                sql += f" AND status IN ({', '.join('?' for _ in parts)})"
                vals += parts
            else:
                sql += f" AND {k}=?"
                vals.append(v)
        if filters.get("date_from"):
            sql += " AND date_received >= ?"
            vals.append(filters["date_from"])
        if filters.get("date_to"):
            sql += " AND date_received <= ?"
            vals.append(filters["date_to"])
        if filters.get("search"):
            sql += " AND (vendor LIKE ? OR notes LIKE ? OR property_address LIKE ?)"
            s = f"%{filters['search']}%"
            vals += [s, s, s]
    sql += " ORDER BY created_at DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, vals).fetchall()
        return [dict(r) for r in rows]

def get_recent_vendor_filings(vendor: str, limit: int = 50) -> list:
    """(filed_path, entity_code) pairs for a vendor's most recently filed
    documents, newest first. 2026-07-27 code review: filing.py's history-based
    filing suggestion used to pull get_all_entries() (the whole table) and scan
    it in Python on every single suggestion; this pushes the vendor filter (and
    the recency bound the suggestion actually wants) into SQL instead."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT filed_path, entity_code FROM intake_log "
            "WHERE vendor = ? COLLATE NOCASE AND filed_path IS NOT NULL AND filed_path != '' "
            "ORDER BY id DESC LIMIT ?",
            (vendor, limit)).fetchall()
        return [(r["filed_path"], r["entity_code"]) for r in rows]

def count_auto_filed_since(dt_iso: str) -> int:
    """How many entries auto-confirm has filed since the given date/datetime —
    drives the 'Recently auto-filed' spot-check feed on the Review Queue."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM intake_log WHERE processed_by='Auto' AND created_at >= ?",
            (dt_iso,)).fetchone()[0]

# ── Activity log (high-level change history) ──────────────────────────────────

def log_activity(entry_id, action: str, user: str = "", detail: str = ""):
    """Record a high-level event (who did what, when) for the activity feed."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (entry_id, action, detail, user) VALUES (?,?,?,?)",
            (entry_id, action, detail, user or ""))
        conn.commit()

def get_activity_feed(limit: int = 15):
    """Recent activity events joined to their record for context and linking."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT a.id AS act_id, a.entry_id, a.action, a.detail, a.user, a.created_at,
                   e.vendor, e.entity_code, e.doc_type, e.amount, e.status
            FROM activity_log a
            LEFT JOIN intake_log e ON e.id = a.entry_id
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

# ── Payment splits (how a bill's payment was allocated across entities, #2) ──────

def save_payment_splits(entry_id, splits):
    """Replace the split rows for an entry. `splits` is an iterable of
    (entity_code, amount). A no-op-friendly reset: clears existing rows first."""
    with get_conn() as conn:
        conn.execute("DELETE FROM payment_splits WHERE entry_id=?", (entry_id,))
        for entity_code, amount in splits:
            if entity_code and amount not in (None, ""):
                conn.execute(
                    "INSERT INTO payment_splits (entry_id, entity_code, amount) VALUES (?,?,?)",
                    (entry_id, entity_code, amount))
        conn.commit()

def get_payment_splits(entry_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT entity_code, amount FROM payment_splits WHERE entry_id=? ORDER BY id",
            (entry_id,)).fetchall()
        return [dict(r) for r in rows]

# ── Which units a record is about (report #20) ────────────────────────────────
# Nancy had nowhere to record "this is unit 5 of TL222", so she was typing it into
# Account # — a field that means the vendor's ongoing account number, which made the
# bill look like it had an account it didn't. These give the unit a real home.

def set_entry_units(entry_id, unit_ids):
    """Replace the unit links for a record. Same replace-wholesale shape as
    save_payment_splits, so a re-save can't accumulate stale rows."""
    with get_conn() as conn:
        conn.execute("DELETE FROM entry_units WHERE entry_id=?", (entry_id,))
        for unit_id in unit_ids or []:
            if unit_id in (None, ""):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entry_units (entry_id, unit_id) VALUES (?,?)",
                (entry_id, int(unit_id)))
        conn.commit()

def get_entry_units(entry_id):
    """This record's units, with their property code, in mailbox order."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT u.id, u.label, u.unit_code, p.code AS property_code
            FROM entry_units eu
            JOIN units u      ON u.id = eu.unit_id
            JOIN properties p ON p.id = u.property_id
            WHERE eu.entry_id = ?""", (entry_id,)).fetchall()
    from property_model import unit_sort_key
    return sorted((dict(r) for r in rows),
                  key=lambda u: (u["property_code"], unit_sort_key(u["label"])))

def get_entry_units_map(entry_ids):
    """{entry_id: [unit, ...]} for a page of records — list views would otherwise
    issue one query per row."""
    ids = [int(i) for i in (entry_ids or [])]
    if not ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT eu.entry_id, u.id, u.label, u.unit_code, p.code AS property_code
            FROM entry_units eu
            JOIN units u      ON u.id = eu.unit_id
            JOIN properties p ON p.id = u.property_id
            WHERE eu.entry_id IN ({",".join("?" * len(ids))})""", ids).fetchall()
    from property_model import unit_sort_key
    out: dict = {}
    for r in rows:
        out.setdefault(r["entry_id"], []).append(dict(r))
    for units in out.values():
        units.sort(key=lambda u: (u["property_code"], unit_sort_key(u["label"])))
    return out

# ── Attachments (extra copies of a record — e.g. an email copy of a mailed bill) ──

def add_attachment(entry_id, file_path, source="", original_name="", added_by=""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO attachments (entry_id, file_path, source, original_name, added_by) "
            "VALUES (?,?,?,?,?)",
            (entry_id, file_path, source or "", original_name or "", added_by or ""))
        conn.commit()

def update_attachment_path(att_id, new_path):
    """Re-point a copy/receipt at its file's new location (move-with-record,
    cabinet-watch hand-move re-linking)."""
    with get_conn() as conn:
        conn.execute("UPDATE attachments SET file_path=? WHERE id=?", (new_path, att_id))
        conn.commit()

def get_attachments(entry_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE entry_id=? ORDER BY added_at, id",
            (entry_id,)).fetchall()
        return [dict(r) for r in rows]

def get_attachment(att_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
        return dict(row) if row else None

def delete_attachment(att_id):
    """Remove one attachment (copy) row. Returns the deleted row as a dict so the
    caller can set its underlying file aside, or None if it didn't exist."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
        conn.commit()
        return dict(row)

def delete_attachments(entry_id):
    """Remove every attachment row for a record. Returns the deleted rows as
    dicts (their files can then be set aside). Used by bulk copy-removal and by
    record deletion."""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM attachments WHERE entry_id=?", (entry_id,)).fetchall()]
        conn.execute("DELETE FROM attachments WHERE entry_id=?", (entry_id,))
        conn.commit()
        return rows

# ── Issue / bug reports (in-app "Report a problem") ───────────────────────────

def add_bug_report(user, page_url, record_ref, description, context=""):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bug_reports (user, page_url, record_ref, description, context) "
            "VALUES (?,?,?,?,?)",
            (user or "", page_url or "", record_ref or "", description or "", context or ""))
        conn.commit()
        return cur.lastrowid

def get_bug_reports(status=None):
    with get_conn() as conn:
        if status:
            rows = conn.execute("SELECT * FROM bug_reports WHERE status=? ORDER BY id DESC",
                                (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bug_reports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def set_bug_report_status(report_id, status):
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET status=? WHERE id=?", (status, report_id))
        conn.commit()

# Lifecycle (VALIDATION_WORKFLOW.md): Open -> In progress -> Dev-resolved
# (awaiting verification) -> Verified. 'Resolved' is the pre-workflow closed
# value — kept as history, read as closed. Everything else counts as open work.
REPORT_CLOSED_STATUSES = ("Verified", "Resolved")

def add_report_note(report_id, author, text, kind="note"):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO report_notes (report_id, author, kind, text) VALUES (?,?,?,?)",
            (report_id, author or "", kind or "note", (text or "").strip()))
        conn.commit()
        return cur.lastrowid

def get_report_notes(report_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM report_notes WHERE report_id=? "
                            "ORDER BY id", (report_id,)).fetchall()
        return [dict(r) for r in rows]

def get_report_notes_map():
    """All notes grouped by report, oldest-first — one query for the page."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM report_notes ORDER BY id").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["report_id"], []).append(dict(r))
    return out

def get_active_reports():
    """Everything not closed (Open / In progress / Dev-resolved), oldest first."""
    with get_conn() as conn:
        q = ",".join("?" * len(REPORT_CLOSED_STATUSES))
        rows = conn.execute(f"SELECT * FROM bug_reports WHERE status NOT IN ({q}) "
                            "ORDER BY id", REPORT_CLOSED_STATUSES).fetchall()
        return [dict(r) for r in rows]

def mark_report_dev_resolved(report_id, author, note_text):
    """Dev believes this is fixed. The what-I-did/what-to-check note is
    REQUIRED (report #70) — refuse a blank one. Clears any bounce flag."""
    if not (note_text or "").strip():
        raise ValueError("Dev-resolved requires a resolution note: "
                         "what was done and what to check.")
    add_report_note(report_id, author, note_text, kind="resolution")
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET status='Dev-resolved', bounced_at=NULL "
                     "WHERE id=?", (report_id,))
        conn.commit()

def bounce_report(report_id, author, note_text=""):
    """'Checked, not yet resolved' (report #67): verification failed, back to
    In progress. bounced_at makes these first in the night shift's queue."""
    add_report_note(report_id, author,
                    (note_text or "").strip() or "Checked — not yet resolved.",
                    kind="bounce")
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET status='In progress', "
                     "bounced_at=datetime('now','localtime') WHERE id=?", (report_id,))
        conn.commit()

def verify_report(report_id, author):
    """Human confirmation that the fix really works. Staff/UI only — the dev
    API and CLI can never reach this state."""
    add_report_note(report_id, author, "Verified — confirmed fixed.", kind="status")
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET status='Verified' WHERE id=?", (report_id,))
        conn.commit()

def set_report_priority(report_id, priority):
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET priority=? WHERE id=?",
                     (1 if priority else 0, report_id))
        conn.commit()

def set_report_topic(report_id, topic):
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET topic=? WHERE id=?",
                     ((topic or "").strip(), report_id))
        conn.commit()

def add_child_report(parent_id, user, page_url, record_ref, description,
                     context="", topic="", priority=0):
    """A sub-issue split out of report `parent_id` (#68). It inherits where the
    parent was filed from — reporter, page, record reference, screenshot
    context, topic, priority — because the child is the same complaint, just
    the part of it that can be finished on its own. Always lands Open: nobody
    has worked THIS one yet."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bug_reports (user, page_url, record_ref, description, "
            "context, topic, priority, parent_id) VALUES (?,?,?,?,?,?,?,?)",
            (user or "", page_url or "", record_ref or "", description or "",
             context or "", (topic or "").strip(), 1 if priority else 0, parent_id))
        conn.commit()
        return cur.lastrowid


def set_report_parent(report_id, parent_id):
    """Join a report to a family, or (parent_id=None) take it back out.

    Deliberately ONE level deep: a parent cannot itself have a parent, and a
    report that already has children cannot become someone's child. A thread
    you can read at a glance is the whole point of both #68 and #55 — a tree
    of trees would put the reading work back. Returns (ok, message)."""
    if parent_id is None:
        with get_conn() as conn:
            conn.execute("UPDATE bug_reports SET parent_id=NULL WHERE id=?",
                         (report_id,))
            conn.commit()
        return True, "Taken out of its group."
    if parent_id == report_id:
        return False, "A report cannot be part of itself."
    with get_conn() as conn:
        parent = conn.execute("SELECT id, parent_id FROM bug_reports WHERE id=?",
                              (parent_id,)).fetchone()
        if not parent:
            return False, f"There is no report #{parent_id}."
        if parent["parent_id"]:
            return False, (f"#{parent_id} is itself part of #{parent['parent_id']} — "
                           f"group under #{parent['parent_id']} instead.")
        kids = conn.execute("SELECT COUNT(*) FROM bug_reports WHERE parent_id=?",
                            (report_id,)).fetchone()[0]
        if kids:
            return False, (f"#{report_id} has {kids} sub-issue(s) of its own; "
                           f"move those first.")
        conn.execute("UPDATE bug_reports SET parent_id=? WHERE id=?",
                     (parent_id, report_id))
        conn.commit()
    return True, f"Grouped under #{parent_id}."


def get_report_children(parent_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM bug_reports WHERE parent_id=? ORDER BY id",
                            (parent_id,)).fetchall()
        return [dict(r) for r in rows]


def set_report_ai_summary(report_id, text):
    with get_conn() as conn:
        conn.execute("UPDATE bug_reports SET ai_summary=? WHERE id=?",
                     ((text or "").strip(), report_id))
        conn.commit()

def get_open_report_count():
    """Nav-badge count: everything still needing SOMEONE's attention — Open,
    In progress, and Dev-resolved (Joshua's verification queue included)."""
    with get_conn() as conn:
        q = ",".join("?" * len(REPORT_CLOSED_STATUSES))
        return conn.execute(f"SELECT COUNT(*) FROM bug_reports WHERE status NOT IN ({q})",
                            REPORT_CLOSED_STATUSES).fetchone()[0]

def report_url_path(url):
    """The comparable part of a report's page_url: path only — host and query
    ignored (page indicator, #68). '' for blank/unparseable urls."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url or "").path or ""
        return path if path == "/" else path.rstrip("/")
    except Exception:
        return ""

def get_open_report_paths():
    """{url path: [report ids]} for every non-closed report that has a page —
    feeds the per-page open-issue indicator."""
    out = {}
    for r in get_active_reports():
        path = report_url_path(r.get("page_url"))
        if path:
            out.setdefault(path, []).append(r["id"])
    return out

# ── Email dedup + AI-filtered (likely-junk) email docs ────────────────────────

def get_email_meta_targets():
    """[(id, file_hash)] of records whose source email was never stored (both
    email_from and email_body empty) but whose document hash is known — the
    match universe for the email backfill (reports #55/#8/#26). Hash is the
    join key: the backfill only ever matches by exact bytes."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, file_hash FROM intake_log "
            "WHERE file_hash IS NOT NULL AND file_hash != '' "
            "AND (email_from IS NULL OR email_from = '') "
            "AND (email_body IS NULL OR email_body = '')").fetchall()
        return [(r["id"], r["file_hash"]) for r in rows]


def is_email_processed(message_id):
    if not message_id:
        return False
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM processed_emails WHERE message_id=?",
                            (message_id,)).fetchone() is not None

def mark_email_processed(message_id):
    if not message_id:
        return
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO processed_emails (message_id) VALUES (?)",
                     (message_id,))
        conn.commit()

def add_filtered_doc(file_path, file_name, doc_type, vendor, amount, ai_confidence, reason, data):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO filtered_docs (file_path, file_name, doc_type, vendor, amount, "
            "ai_confidence, reason, data) VALUES (?,?,?,?,?,?,?,?)",
            (file_path, file_name, doc_type, vendor, amount, ai_confidence, reason, data))
        conn.commit()

def get_filtered_docs():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM filtered_docs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def get_filtered_doc(fid):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM filtered_docs WHERE id=?", (fid,)).fetchone()
        return dict(row) if row else None

def delete_filtered_doc(fid):
    with get_conn() as conn:
        conn.execute("DELETE FROM filtered_docs WHERE id=?", (fid,))
        conn.commit()

def get_filtered_count():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM filtered_docs").fetchone()[0]

# ── Persisted review queue (survives restarts) ────────────────────────────────

def save_pending_scan(scan_id, data_json):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO pending_scans (scan_id, data) VALUES (?,?)",
                     (scan_id, data_json))
        conn.commit()

def delete_pending_scan(scan_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_scans WHERE scan_id=?", (scan_id,))
        conn.commit()

def clear_pending_scans():
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_scans")
        conn.commit()

def get_all_pending_scans():
    with get_conn() as conn:
        rows = conn.execute("SELECT scan_id, data FROM pending_scans ORDER BY scan_id").fetchall()
        return [(r["scan_id"], r["data"]) for r in rows]

def seed_activity_from_existing():
    """One-time backfill: if the activity log is empty but records exist, seed a
    'Created' event per record (using its own timestamp/processor) so the feed is
    populated from day one instead of starting blank."""
    with get_conn() as conn:
        have = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        if have:
            return
        rows = conn.execute(
            "SELECT id, processed_by, created_at FROM intake_log").fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO activity_log (entry_id, action, detail, user, created_at) "
                "VALUES (?,?,?,?,?)",
                (r["id"], "Created", "", r["processed_by"] or "",
                 r["created_at"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

def get_dashboard_stats(date_from: str = None, date_to: str = None):
    """Dashboard aggregates, optionally scoped to a date_received range.
    Pass both None for all-time. Scoping by date_received keeps it consistent
    with the Intake Log's date filters and the dashboard's click-through links."""
    # Build a reusable WHERE fragment + params for the selected period.
    clause = ""
    params = []
    if date_from:
        clause += " AND date_received >= ?"
        params.append(date_from)
    if date_to:
        clause += " AND date_received <= ?"
        params.append(date_to)

    with get_conn() as conn:
        stats = {}
        stats["total"] = conn.execute(
            f"SELECT COUNT(*) FROM intake_log WHERE 1=1{clause}", params).fetchone()[0]
        for status in ["Pending","In Progress","Processed","Paid","Filed","On Hold"]:
            key = status.lower().replace(" ","_")
            stats[key] = conn.execute(
                f"SELECT COUNT(*) FROM intake_log WHERE status=?{clause}",
                [status] + params).fetchone()[0]
        stats["total_bills"] = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM intake_log WHERE doc_type='Bill'{clause}",
            params).fetchone()[0]
        stats["total_income"] = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM intake_log WHERE doc_type='Income'{clause}",
            params).fetchone()[0]
        by_type = conn.execute(
            f"SELECT doc_type, COUNT(*) as cnt, COALESCE(SUM(amount),0) as total "
            f"FROM intake_log WHERE 1=1{clause} GROUP BY doc_type ORDER BY cnt DESC",
            params).fetchall()
        stats["by_type"] = [dict(r) for r in by_type]
        by_entity = conn.execute(
            f"SELECT entity_code, entity_name, COUNT(*) as cnt, "
            f"COALESCE(SUM(CASE WHEN doc_type='Bill' THEN amount ELSE 0 END),0) as bills_amt, "
            f"COALESCE(SUM(CASE WHEN doc_type='Income' THEN amount ELSE 0 END),0) as income_amt "
            f"FROM intake_log WHERE 1=1{clause} GROUP BY entity_code ORDER BY cnt DESC",
            params).fetchall()
        stats["by_entity"] = [dict(r) for r in by_entity]
        return stats

# ── Karen's setup questionnaire (see karen_questions.py) ─────────────────────

def save_karen_answer(question_id, answer, notes, user):
    with get_conn() as conn:
        conn.execute("""INSERT OR REPLACE INTO karen_answers
                        (question_id, answer, notes, updated_by, updated_at)
                        VALUES (?,?,?,?,datetime('now','localtime'))""",
                     (question_id, answer, notes, user))
        conn.commit()

def get_karen_answers():
    with get_conn() as conn:
        rows = conn.execute("SELECT question_id, answer, notes, updated_by, updated_at"
                            " FROM karen_answers").fetchall()
        return {r["question_id"]: dict(r) for r in rows}

def count_filtered_since(dt_iso: str) -> int:
    """How many email docs the junk filter set aside since the given date —
    shown on the dashboard's from-email box so email never feels like a black
    hole (Filtered is recoverable; nothing is ever deleted)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM filtered_docs WHERE created_at >= ?",
            (dt_iso,)).fetchone()[0]
