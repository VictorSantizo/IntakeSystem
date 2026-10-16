r"""mail_archive.py — a local, searchable copy of every email in every office
mailbox (Nancy's all-mail search, 2026-08-10).

WHY THIS EXISTS. Until now the system kept only what intake needed: bill-shaped
attachments plus a few denormalized email_* columns on the records they became.
The messages themselves stayed on the mail servers — so "find that email from
Entrusted Services, whichever inbox it landed in, and print it with its
attachment list" had no answer inside the app. This module keeps the answer
local: every message from every configured mailbox, with its full headers, its
body text, and the name/size/sha256 of every attachment. The sha256 is the
bridge to the rest of the system — the same hash ingest stamps on records
(intake_log.file_hash), so an archived email can say "this attachment is filed
as record 260807-001" with certainty, never by guesswork.

WHAT IT DELIBERATELY IS NOT. Not intake (nothing here is ever enqueued, and
processed_emails is neither read nor written), and not a mail client (nothing
is ever deleted, moved, or flagged). One message can appear once PER MAILBOX —
UNIQUE(account_id, message_key) — because "the copy in Larry's inbox" and "the
copy in Karen's inbox" are different facts a person needs to see separately.

MAILBOX SAFETY — read-only, always, same contract as email_backfill.py: IMAP
selects INBOX readonly (EXAMINE) and fetches with BODY.PEEK, so the server
itself refuses flag changes; POP3 fetches via TOP only and never issues DELE.
There is no RETR fallback anywhere in this file — on a TOP-less server the
message is skipped and counted, never fetched the flag-risky way (the MagicMail
\Seen lesson, 2026-07-07; Larry's box has been disturbed twice and never again).
tests/test_mail_archive.py greps this source to keep it that way.

STORAGE. A SQLite sidecar next to intake.db (mail_archive.db — the indexer.py
pattern, so tests that repoint database.DB_PATH get isolation for free). The
default rollback journal is kept deliberately: unlike intake.db this file may
see its FIRST big write from a workstation over the share, where WAL is the
known hazard. A message with no Message-ID header gets a synthesized stable
key instead of being dropped (the poller's silent-skip gap, not repeated here).
"""
import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

import email as email_lib
import imaplib
import poplib

import database
from email_poller import (_decode_filename, _extract_body, _msg_local_date,
                          configured_accounts)

DEFAULT_SINCE = "2026-06-01"       # the app's first live intake month
MAX_MESSAGES_PER_SYNC = 4000       # per mailbox per run; reported when hit
_BODY_CAP = 100_000                # the archive is the reference copy — roomier
                                   # than intake_log's 20k (which live data hits)
_OLD_STREAK = 25                   # POP3 newest-first walk: stop after this many
                                   # CONSECUTIVE messages older than `since`
_POP3_TOP_ALL = 99_999_999         # TOP with a line count no real body reaches
                                   # returns the whole message — without the
                                   # fetch verb that marks mail read

# One sync at a time: the background loop, the /mail button and the CLI can
# overlap; latecomers just skip their turn (the poller's _poll_lock manners).
_sync_lock = threading.Lock()
_sync_running = False


def archive_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)),
                        "mail_archive.db")


def _conn() -> sqlite3.Connection:
    """Open the sidecar with its schema ensured (cheap no-ops once created)."""
    conn = sqlite3.connect(archive_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            account_name TEXT,
            message_key TEXT,
            uidl TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            subject TEXT,
            date_raw TEXT,
            date_iso TEXT,
            body TEXT,
            attachment_count INTEGER DEFAULT 0,
            fetched_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(account_id, message_key)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_date ON messages(date_iso)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            filename TEXT,
            size INTEGER,
            content_type TEXT,
            sha256 TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_att ON message_attachments(message_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_att_sha ON message_attachments(sha256)")
    # Additive columns (the database.py try/except ALTER pattern):
    # blob_path — one content-addressed copy per UNIQUE attachment, shared by
    # every email carrying those exact bytes (Joshua 2026-08-10: "if the
    # identical attachments are in each email, can we just link them to the
    # same file so that they are all clickable?"). blob_tried marks a message
    # the rehydrate sweep already went back for, so mail deleted from the
    # server can't keep the sweep walking forever.
    for col in ("blob_path TEXT", "blob_tried INTEGER DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE message_attachments ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mail_fts USING fts5("
                 "msg_id UNINDEXED, subject, from_addr, to_addr, body, filenames)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            account_id INTEGER PRIMARY KEY,
            account_name TEXT,
            protocol TEXT,
            last_synced TEXT,
            last_result TEXT
        )""")
    return conn


def init_archive() -> None:
    """Create the sidecar + schema if needed. Safe to call on every startup."""
    _conn().close()


# ── Storing one message ───────────────────────────────────────────────────────

def _noid_key(msg) -> str:
    """Stable synthetic key for a message with no Message-ID header. The poller
    silently skips these; an archive that claims 'every email' must not."""
    fp = "\x00".join((msg.get("From") or "", msg.get("To") or "",
                      msg.get("Date") or "", msg.get("Subject") or ""))
    return "noid:" + hashlib.sha256(fp.encode("utf-8", "replace")).hexdigest()[:32]


def message_key_of(msg) -> str:
    return (msg.get("Message-ID") or "").strip() or _noid_key(msg)


_MAX_BLOB_BYTES = 30 * 1024 * 1024   # no real office attachment is bigger


def _blob_dir() -> str:
    return os.path.join(os.path.dirname(archive_path()), "Mail Attachments")


def _write_blob(sha256: str, filename: str, payload: bytes) -> str:
    """One copy per unique attachment, named by its hash (extension kept for
    humans browsing the folder). Already present → the existing path: every
    email carrying these bytes shares the same file."""
    ext = os.path.splitext(filename or "")[1][:10]
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    path = os.path.join(_blob_dir(), sha256 + ext)
    if not os.path.exists(path):
        os.makedirs(_blob_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(payload)
    return path


def _walk_attachments(msg):
    """[{filename, size, content_type, sha256, payload}] for every real
    attachment part. Unlike ingest's bill-shaped filter, the archive records
    EVERYTHING with a filename (or an attachment disposition) — the printout's
    'Attachments:' list exists to remove uncertainty, so it must be the honest
    full list. payload is carried for the blob write and never stored in the
    database row."""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fname = _decode_filename(part.get_filename())
        disp = (part.get("Content-Disposition") or "").lower()
        if not fname and "attachment" not in disp:
            continue                       # body / inline unnamed parts
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        out.append({
            "filename": fname or "(unnamed attachment)",
            "size": len(payload),
            "content_type": part.get_content_type(),
            "sha256": hashlib.sha256(payload).hexdigest() if payload else "",
            "payload": payload,
        })
    return out


def store_message(account: dict, msg, uidl: str = None):
    """Store one parsed message for one mailbox. Returns (row_id, is_new).
    Duplicate (same mailbox, same message) is a clean no-op."""
    key = message_key_of(msg)
    acct_id = account.get("id")
    acct_name = account.get("name") or account.get("username") or ""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE account_id=? AND message_key=?",
            (acct_id, key)).fetchone()
        if row:
            return row["id"], False
        d = _msg_local_date(msg)
        atts = _walk_attachments(msg)
        cur = conn.execute(
            "INSERT INTO messages (account_id, account_name, message_key, uidl, "
            "from_addr, to_addr, cc_addr, subject, date_raw, date_iso, body, "
            "attachment_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (acct_id, acct_name, key, uidl or "",
             _decode_filename(msg.get("From")), _decode_filename(msg.get("To")),
             _decode_filename(msg.get("Cc")), _decode_filename(msg.get("Subject")),
             msg.get("Date") or "", d.isoformat() if d else "",
             _extract_body(msg, limit=_BODY_CAP), len(atts)))
        mid = cur.lastrowid
        for a in atts:
            blob = ""
            if a["sha256"] and 0 < len(a["payload"]) <= _MAX_BLOB_BYTES:
                try:
                    blob = _write_blob(a["sha256"], a["filename"], a["payload"])
                except OSError as e:
                    print(f"[MailArchive] could not keep a copy of "
                          f"{a['filename']}: {e}")
            conn.execute(
                "INSERT INTO message_attachments (message_id, filename, size, "
                "content_type, sha256, blob_path, blob_tried) VALUES (?,?,?,?,?,?,1)",
                (mid, a["filename"], a["size"], a["content_type"], a["sha256"],
                 blob))
        conn.execute(
            "INSERT INTO mail_fts (msg_id, subject, from_addr, to_addr, body, filenames) "
            "VALUES (?,?,?,?,?,?)",
            (mid, _decode_filename(msg.get("Subject")),
             _decode_filename(msg.get("From")), _decode_filename(msg.get("To")),
             _extract_body(msg, limit=_BODY_CAP),
             " ".join(a["filename"] for a in atts)))
        conn.commit()
        return mid, True


# ── Fetching (read-only, per account) ─────────────────────────────────────────

def _existing_keys(acct_id) -> set:
    with _conn() as conn:
        return {r["message_key"] for r in conn.execute(
            "SELECT message_key FROM messages WHERE account_id=?", (acct_id,))}


def _existing_uidls(acct_id) -> set:
    with _conn() as conn:
        return {r["uidl"] for r in conn.execute(
            "SELECT uidl FROM messages WHERE account_id=? AND uidl != ''",
            (acct_id,))}


def _sync_imap(account, since_iso, on_new):
    """One IMAP INBOX, read-only. Peeks each Message-ID first (cheap), fully
    fetches only messages this mailbox hasn't archived yet. Returns
    (seen, new, error_or_None)."""
    seen = new = 0
    known = _existing_keys(account.get("id"))
    M = None
    try:
        opener = imaplib.IMAP4_SSL if account.get("ssl", True) else imaplib.IMAP4
        M = opener(account["host"],
                   int(account.get("port") or (993 if account.get("ssl", True) else 143)),
                   timeout=60)
        M.login(account["username"], account["password"])
        M.select("INBOX", readonly=True)   # EXAMINE — the server refuses flag changes
        since = datetime.strptime(since_iso, "%Y-%m-%d").strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        nums = data[0].split() if (data and data[0]) else []
        for num in nums[:MAX_MESSAGES_PER_SYNC]:
            seen += 1
            typ, hdr = M.fetch(num, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            msgid = ""
            if hdr and hdr[0] and isinstance(hdr[0], tuple):
                m = re.search(r'Message-ID:\s*(\S+)',
                              hdr[0][1].decode("utf-8", "ignore"), re.IGNORECASE)
                if m:
                    msgid = m.group(1).strip()
            if msgid and msgid in known:
                continue
            typ, full = M.fetch(num, '(BODY.PEEK[])')
            if not full or not full[0] or not isinstance(full[0], tuple):
                continue
            msg = email_lib.message_from_bytes(full[0][1])
            key = message_key_of(msg)      # covers the no-Message-ID case
            if key in known:
                continue
            mid, is_new = store_message(account, msg)
            known.add(key)
            if is_new:
                new += 1
                on_new(mid)
        err = (f"stopped at the {MAX_MESSAGES_PER_SYNC}-message safety cap"
               if len(nums) > MAX_MESSAGES_PER_SYNC else None)
        return seen, new, err
    except imaplib.IMAP4.error as e:
        return seen, new, f"IMAP login error: {e}"
    except Exception as e:
        return seen, new, f"{type(e).__name__}: {e}"
    finally:
        try:
            if M is not None:
                M.logout()
        except Exception:
            pass


def _pop3_top_only(conn, num, nlines):
    """TOP, and ONLY TOP. The archive never uses the fetch verb that marks a
    whole mailbox read (MagicMail, 2026-07-07) — a TOP-less server's messages
    are skipped and counted instead. poplib raises error_proto; caller skips."""
    return conn.top(num, nlines)[1]


def _sync_pop3(account, since_iso, on_new):
    """One POP3 mailbox via UIDL + TOP, never DELE. UIDL makes the incremental
    walk cheap: a message whose server uid is already archived costs nothing.
    POP3 has no server-side search, so the newest-first walk stops after a
    STREAK of messages older than `since` (Date headers sit slightly out of
    arrival order; one old message must not end the scan). Returns
    (seen, new, error_or_None)."""
    seen = new = 0
    skipped_no_top = 0
    known_uidls = _existing_uidls(account.get("id"))
    known_keys = _existing_keys(account.get("id"))
    conn = None
    try:
        opener = poplib.POP3_SSL if account.get("ssl", True) else poplib.POP3
        conn = opener(account["host"],
                      int(account.get("port") or (995 if account.get("ssl", True) else 110)),
                      timeout=60)
        conn.user(account["username"])
        conn.pass_(account["password"])
        try:
            uidl_lines = conn.uidl()[1]
            uidl_of = {int(l.split()[0]): l.split()[1].decode("ascii", "ignore")
                       for l in uidl_lines if len(l.split()) >= 2}
        except (poplib.error_proto, ValueError):
            uidl_of = {}                   # server without UIDL: key-dedup only
        count = conn.stat()[0]
        old_streak = 0
        looked = 0
        for num in range(count, 0, -1):    # newest first
            if looked >= MAX_MESSAGES_PER_SYNC:
                break
            uidl = uidl_of.get(num, "")
            if uidl and uidl in known_uidls:
                continue                   # archived on a previous run — free skip
            looked += 1
            seen += 1
            try:
                hdr = email_lib.message_from_bytes(
                    b"\r\n".join(_pop3_top_only(conn, num, 0)))
            except poplib.error_proto:
                skipped_no_top += 1
                continue
            d = _msg_local_date(hdr)
            if d is not None:
                if d.isoformat() < since_iso:
                    old_streak += 1
                    if old_streak >= _OLD_STREAK:
                        break
                    continue
                old_streak = 0
            key_guess = (hdr.get("Message-ID") or "").strip()
            if key_guess and key_guess in known_keys:
                continue                   # same message, uidl not yet recorded
            try:
                msg = email_lib.message_from_bytes(
                    b"\r\n".join(_pop3_top_only(conn, num, _POP3_TOP_ALL)))
            except poplib.error_proto:
                skipped_no_top += 1
                continue
            key = message_key_of(msg)
            if key in known_keys:
                continue
            mid, is_new = store_message(account, msg, uidl=uidl)
            known_keys.add(key)
            if uidl:
                known_uidls.add(uidl)
            if is_new:
                new += 1
                on_new(mid)
        err = (f"{skipped_no_top} messages skipped (server refused TOP; the "
               f"flag-risky fetch verb is never used)" if skipped_no_top else None)
        return seen, new, err
    except poplib.error_proto as e:
        return seen, new, f"POP3 error: {e}"
    except Exception as e:
        return seen, new, f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn is not None:
                conn.quit()
        except Exception:
            pass


def sync_running() -> bool:
    return _sync_running


def sync_all(cfg, since=None, progress=print, on_new=None):
    """Pull every enabled mailbox into the archive. Incremental and additive:
    already-archived messages cost a header peek (IMAP) or nothing (POP3 UIDL).
    on_new(row_id) fires for each newly archived message — the caller decides
    what happens next (matching/appending is NOT this module's job). Returns a
    summary dict, or None when another sync is already running."""
    global _sync_running
    if not _sync_lock.acquire(blocking=False):
        return None
    _sync_running = True
    try:
        since = since or cfg.get("mail_archive_since") or DEFAULT_SINCE
        on_new = on_new or (lambda mid: None)
        summary = {"since": since, "accounts": [], "new": 0}
        for account in configured_accounts(cfg):
            name = account.get("name") or account.get("username")
            proto = (account.get("protocol") or "imap").strip().lower()
            progress(f"[MailArchive] {name} ({proto}) since {since}…")
            syncer = _sync_pop3 if proto == "pop3" else _sync_imap
            seen, new, err = syncer(account, since, on_new)
            summary["accounts"].append(dict(name=name, protocol=proto,
                                            seen=seen, new=new, error=err))
            summary["new"] += new
            with _conn() as conn:
                conn.execute(
                    "INSERT INTO sync_state (account_id, account_name, protocol, "
                    "last_synced, last_result) VALUES (?,?,?,datetime('now','localtime'),?) "
                    "ON CONFLICT(account_id) DO UPDATE SET account_name=excluded.account_name, "
                    "protocol=excluded.protocol, last_synced=excluded.last_synced, "
                    "last_result=excluded.last_result",
                    (account.get("id"), name, proto,
                     (err or f"{new} new of {seen} looked at")))
                # A renamed account keeps ONE label in the archive (2026-08-10:
                # "Larry's Inbox" → "larryram@silcom.com" mid-day split the
                # Mailbox filter in two). Same account id = same mailbox; the
                # label follows the config.
                conn.execute("UPDATE messages SET account_name=? "
                             "WHERE account_id=? AND account_name != ?",
                             (name, account.get("id"), name))
                conn.commit()
            progress(f"[MailArchive]   {seen} looked at, {new} new"
                     + (f" — {err}" if err else ""))
        return summary
    finally:
        _sync_running = False
        _sync_lock.release()


def incremental_since(default_since: str = DEFAULT_SINCE) -> str:
    """A cheap `since` for the background loop: a few days before the oldest
    account's last successful sync — enough overlap that late-delivered mail
    is never missed, without re-peeking the whole window every cycle."""
    with _conn() as conn:
        row = conn.execute("SELECT MIN(last_synced) AS t FROM sync_state").fetchone()
    if not row or not row["t"]:
        return default_since
    try:
        dt = datetime.strptime(row["t"][:10], "%Y-%m-%d") - timedelta(days=3)
        return max(default_since, dt.strftime("%Y-%m-%d"))
    except ValueError:
        return default_since


# ── Reading (the /mail page and the append engine) ────────────────────────────

def _fts_query(query: str) -> str:
    """User text as one quoted FTS5 phrase (indexer.py's safety pattern)."""
    return '"' + (query or "").replace('"', '""').strip() + '"'


def _attach_lists(conn, ids):
    """{message_id: [attachment dicts]} for a set of message ids."""
    out = {i: [] for i in ids}
    ids = list(ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        qs = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT * FROM message_attachments WHERE message_id IN ({qs}) "
                f"ORDER BY id", chunk):
            out[r["message_id"]].append(dict(r))
    return out


def query(q="", account="", date_from="", date_to="", has_att="", limit=300):
    """Messages matching a search AND the filters — the two compose in either
    order because the filters are plain WHERE clauses applied to whichever row
    set the search produced (the /bills lesson, report #93). Newest first.
    account matches the stored account_name. has_att: '' any, 'yes', 'no'.
    Each row carries its attachments list."""
    if not os.path.exists(archive_path()):
        return []
    with _conn() as conn:
        where, params = [], []
        if account:
            where.append("m.account_name = ?")
            params.append(account)
        if date_from:
            where.append("m.date_iso >= ?")
            params.append(date_from)
        if date_to:
            where.append("m.date_iso <= ?")
            params.append(date_to)
        if has_att == "yes":
            where.append("m.attachment_count > 0")
        elif has_att == "no":
            where.append("m.attachment_count = 0")
        q = (q or "").strip()
        if q:
            try:
                hit_ids = [r["msg_id"] for r in conn.execute(
                    "SELECT msg_id FROM mail_fts WHERE mail_fts MATCH ? "
                    "ORDER BY rank LIMIT 2000", (_fts_query(q),))]
            except sqlite3.OperationalError as e:
                print(f"[MailArchive] search({q!r}) failed: {e}")
                hit_ids = []
            if not hit_ids:
                return []
            qs = ",".join("?" * len(hit_ids))
            where.append(f"m.id IN ({qs})")
            params.extend(hit_ids)
        sql = "SELECT m.* FROM messages m"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.date_iso DESC, m.id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params)]
        atts = _attach_lists(conn, [r["id"] for r in rows])
        for r in rows:
            r["attachments"] = atts.get(r["id"], [])
        return rows


def get_message(msg_id: int):
    """One archived message with its attachments, or None."""
    if not os.path.exists(archive_path()):
        return None
    with _conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not row:
            return None
        m = dict(row)
        m["attachments"] = _attach_lists(conn, [m["id"]]).get(m["id"], [])
        return m


def messages_for_hash(sha256: str) -> list:
    """Every archived message (across ALL mailboxes) carrying an attachment
    with these exact bytes — the email(s) behind a filed document. Oldest
    first, so appended email pages read in arrival order."""
    if not sha256 or not os.path.exists(archive_path()):
        return []
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT DISTINCT m.* FROM messages m "
            "JOIN message_attachments a ON a.message_id = m.id "
            "WHERE a.sha256 = ? ORDER BY m.date_iso, m.id", (sha256,))]
        atts = _attach_lists(conn, [r["id"] for r in rows])
        for r in rows:
            r["attachments"] = atts.get(r["id"], [])
        return rows


def distinct_sends_for_hashes(shas: list) -> dict:
    """{sha256: distinct message_key count} for a batch — the /mail page's
    re-send badges (a file that keeps arriving is a sender asking for
    attention, and the mailbox should say so on the row)."""
    want = [s for s in {(s or "").strip() for s in (shas or [])} if s]
    if not want or not os.path.exists(archive_path()):
        return {}
    out = {}
    with _conn() as conn:
        for i in range(0, len(want), 400):
            chunk = want[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for sha, n in conn.execute(
                    f"SELECT a.sha256, COUNT(DISTINCT m.message_key) "
                    f"FROM message_attachments a JOIN messages m "
                    f"ON m.id = a.message_id WHERE a.sha256 IN ({qs}) "
                    f"GROUP BY a.sha256", chunk):
                out[sha] = n
    return out


def distinct_sends_for_hash(sha256: str) -> int:
    """How many DISTINCT messages (by message_key, mailboxes collapsed) carry
    these bytes. 1 = one send, possibly delivered to several inboxes;
    2+ = the vendor genuinely sent it again — attention-seeking."""
    if not sha256 or not os.path.exists(archive_path()):
        return 0
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(DISTINCT m.message_key) FROM messages m "
            "JOIN message_attachments a ON a.message_id = m.id "
            "WHERE a.sha256 = ?", (sha256,)).fetchone()[0]


def blob_for(sha256: str):
    """{filename, content_type, blob_path} for the shared copy of these bytes,
    or None when the archive never kept one (pre-blob rows the rehydrate sweep
    hasn't reached, or the message is gone from its mailbox)."""
    if not sha256 or not os.path.exists(archive_path()):
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT filename, content_type, blob_path FROM message_attachments "
            "WHERE sha256=? AND blob_path IS NOT NULL AND blob_path != '' "
            "LIMIT 1", (sha256,)).fetchone()
    if row and os.path.exists(row["blob_path"]):
        return dict(row)
    return None


def blobless_count() -> int:
    """Attachments still lacking their shared copy AND not yet swept for —
    the rehydrate loop's are-we-done gate."""
    if not os.path.exists(archive_path()):
        return 0
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM message_attachments WHERE sha256 != '' "
            "AND (blob_path IS NULL OR blob_path = '') "
            "AND COALESCE(blob_tried, 0) = 0").fetchone()[0]


def attach_blobs(msg) -> int:
    """A re-fetched message's attachment bytes → the shared copy store, and
    every archive row carrying those bytes (any mailbox) gets the path.
    Returns how many rows gained a copy."""
    fixed = 0
    with _conn() as conn:
        for a in _walk_attachments(msg):
            if not a["sha256"] or not (0 < len(a["payload"]) <= _MAX_BLOB_BYTES):
                continue
            try:
                blob = _write_blob(a["sha256"], a["filename"], a["payload"])
            except OSError as e:
                print(f"[MailArchive] rehydrate copy failed for "
                      f"{a['filename']}: {e}")
                continue
            cur = conn.execute(
                "UPDATE message_attachments SET blob_path=? WHERE sha256=? "
                "AND (blob_path IS NULL OR blob_path = '')",
                (blob, a["sha256"]))
            fixed += cur.rowcount
        conn.commit()
    return fixed


def _keys_needing_blobs(acct_id) -> set:
    with _conn() as conn:
        return {r["message_key"] for r in conn.execute(
            "SELECT DISTINCT m.message_key FROM messages m "
            "JOIN message_attachments a ON a.message_id = m.id "
            "WHERE m.account_id=? AND a.sha256 != '' "
            "AND (a.blob_path IS NULL OR a.blob_path = '') "
            "AND COALESCE(a.blob_tried, 0) = 0", (acct_id,))}


def _mark_blob_tried(acct_id):
    """After a clean rehydrate pass over a mailbox, whatever is STILL missing
    its copy is gone from that mailbox — stop sweeping for it. Never called
    when the pass errored, so a bad login doesn't burn the one attempt."""
    with _conn() as conn:
        conn.execute(
            "UPDATE message_attachments SET blob_tried=1 WHERE message_id IN "
            "(SELECT id FROM messages WHERE account_id=?) AND sha256 != '' "
            "AND (blob_path IS NULL OR blob_path = '')", (acct_id,))
        conn.commit()


def _rehydrate_imap(account, since_iso, keys):
    fetched = 0
    M = None
    try:
        opener = imaplib.IMAP4_SSL if account.get("ssl", True) else imaplib.IMAP4
        M = opener(account["host"],
                   int(account.get("port") or (993 if account.get("ssl", True) else 143)),
                   timeout=60)
        M.login(account["username"], account["password"])
        M.select("INBOX", readonly=True)
        since = datetime.strptime(since_iso, "%Y-%m-%d").strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        nums = data[0].split() if (data and data[0]) else []
        for num in nums[:MAX_MESSAGES_PER_SYNC]:
            typ, hdr = M.fetch(num, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            msgid = ""
            if hdr and hdr[0] and isinstance(hdr[0], tuple):
                m = re.search(r'Message-ID:\s*(\S+)',
                              hdr[0][1].decode("utf-8", "ignore"), re.IGNORECASE)
                if m:
                    msgid = m.group(1).strip()
            if not msgid or msgid not in keys:
                continue
            typ, full = M.fetch(num, '(BODY.PEEK[])')
            if full and full[0] and isinstance(full[0], tuple):
                attach_blobs(email_lib.message_from_bytes(full[0][1]))
                fetched += 1
        return fetched, None
    except Exception as e:
        return fetched, f"{type(e).__name__}: {e}"
    finally:
        try:
            if M is not None:
                M.logout()
        except Exception:
            pass


def _rehydrate_pop3(account, since_iso, keys):
    fetched = 0
    conn = None
    try:
        opener = poplib.POP3_SSL if account.get("ssl", True) else poplib.POP3
        conn = opener(account["host"],
                      int(account.get("port") or (995 if account.get("ssl", True) else 110)),
                      timeout=60)
        conn.user(account["username"])
        conn.pass_(account["password"])
        count = conn.stat()[0]
        old_streak = 0
        for num in range(count, max(0, count - MAX_MESSAGES_PER_SYNC), -1):
            try:
                hdr = email_lib.message_from_bytes(
                    b"\r\n".join(_pop3_top_only(conn, num, 0)))
            except poplib.error_proto:
                continue
            d = _msg_local_date(hdr)
            if d is not None:
                if d.isoformat() < since_iso:
                    old_streak += 1
                    if old_streak >= _OLD_STREAK:
                        break
                    continue
                old_streak = 0
            if (hdr.get("Message-ID") or "").strip() not in keys:
                continue
            try:
                attach_blobs(email_lib.message_from_bytes(
                    b"\r\n".join(_pop3_top_only(conn, num, _POP3_TOP_ALL))))
                fetched += 1
            except poplib.error_proto:
                continue
        return fetched, None
    except Exception as e:
        return fetched, f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn is not None:
                conn.quit()
        except Exception:
            pass


def rehydrate_all(cfg, since=None, progress=print):
    """Go back for the attachment COPIES of messages archived before the copy
    store existed (2026-08-10). Same read-only contract as the sync; only
    messages still missing a copy are fully fetched. A mailbox whose pass
    completes cleanly marks its stragglers tried, so the sweep converges even
    when mail has been deleted server-side. No-op once nothing is missing."""
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        since = since or cfg.get("mail_archive_since") or DEFAULT_SINCE
        total = 0
        for account in configured_accounts(cfg):
            keys = _keys_needing_blobs(account.get("id"))
            if not keys:
                continue
            name = account.get("name") or account.get("username")
            proto = (account.get("protocol") or "imap").strip().lower()
            progress(f"[MailArchive] rehydrating {len(keys)} message(s) of "
                     f"attachment copies from {name}…")
            fn = _rehydrate_pop3 if proto == "pop3" else _rehydrate_imap
            fetched, err = fn(account, since, keys)
            total += fetched
            if err:
                progress(f"[MailArchive]   stopped early: {err}")
            else:
                _mark_blob_tried(account.get("id"))
                progress(f"[MailArchive]   {fetched} message(s) re-fetched")
        return total
    finally:
        _sync_lock.release()


def newest_by_account() -> dict:
    """{account_name: newest archived message date (YYYY-MM-DD)} — the
    mail-flow heartbeat mail_health watches."""
    if not os.path.exists(archive_path()):
        return {}
    with _conn() as conn:
        return {r["account_name"]: r["latest"] for r in conn.execute(
            "SELECT account_name, MAX(date_iso) AS latest FROM messages "
            "WHERE date_iso != '' GROUP BY account_name")}


def status() -> dict:
    """Archive totals + per-account sync state, for the /mail page header."""
    if not os.path.exists(archive_path()):
        return {"total": 0, "accounts": [], "by_account": {}, "sync": []}
    with _conn() as conn:
        by_acct = {r["account_name"]: r["n"] for r in conn.execute(
            "SELECT account_name, COUNT(*) AS n FROM messages "
            "GROUP BY account_name ORDER BY account_name")}
        sync = [dict(r) for r in conn.execute(
            "SELECT * FROM sync_state ORDER BY account_name")]
    return {"total": sum(by_acct.values()),
            "accounts": sorted(by_acct.keys()),
            "by_account": by_acct, "sync": sync}
