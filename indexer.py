"""Full-text index of document contents (Option B, phase B1).

A SQLite FTS5 sidecar database (never intake.db) holding the extracted text of
each logged document, so staff can search what's *inside* the PDFs, not just the
logged fields. Text is pulled by ocr.py — the embedded text layer for normal
PDFs, Tesseract OCR for image-only scans.

Kept incremental by the file_hash intake already stores: a document is re-read
only when its hash changes (or a forced reindex). Everything degrades safely —
if Tesseract isn't installed, image-only docs are recorded as 'ocr-unavailable'
and picked up on a later run once it is.

The index lives next to intake.db and is derived from database.DB_PATH, so tests
(which repoint DB_PATH at a temp file) get an isolated index for free.
"""
import os
import sqlite3

import database
import ocr

_UNAVAILABLE = "ocr-unavailable"
# A genuine OCR crash (ocr.py, 2026-07-25 code review) -- retried on the next
# run exactly like _UNAVAILABLE, since neither means "successfully read, done."
_OCR_ERROR = "ocr-error"
_RETRY_METHODS = (_UNAVAILABLE, _OCR_ERROR)


def _index_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "fts_index.db")


def _conn() -> sqlite3.Connection:
    """Open the sidecar DB with its schema ensured. The CREATE ... IF NOT EXISTS
    calls are cheap no-ops once the tables exist, and keep every entry point
    (including tests that repoint DB_PATH at a fresh file) self-contained."""
    conn = sqlite3.connect(_index_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS doc_text "
                 "USING fts5(entry_id UNINDEXED, path UNINDEXED, "
                 "method UNINDEXED, content)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indexed_docs (
            entry_id  INTEGER PRIMARY KEY,
            path      TEXT,
            file_hash TEXT,
            method    TEXT,
            chars     INTEGER,
            indexed_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    return conn


def init_index() -> None:
    """Create the sidecar DB + schema if needed. Safe to call on every startup."""
    _conn().close()


def _doc_path(entry: dict) -> str:
    return (entry.get("filed_path") or entry.get("pdf_path") or "").strip()


def index_entry(entry: dict, tesseract_path: str = "", force: bool = False) -> str:
    """Index one logged record. Returns the method/status:
    'text' | 'ocr' | 'ocr-unavailable' | 'ocr-error' | 'none' | 'missing' | 'skipped'.
    Skips work when the file_hash is unchanged since last index (unless force)."""
    entry_id = entry.get("id")
    if entry_id is None:
        return "none"
    path = _doc_path(entry)
    file_hash = entry.get("file_hash") or ""
    with _conn() as conn:
        prev = conn.execute("SELECT file_hash, method FROM indexed_docs WHERE entry_id=?",
                            (entry_id,)).fetchone()
        # Unchanged and already read successfully → skip. A prior 'ocr-unavailable'
        # is always retried (Tesseract may since have been installed), and so is a
        # prior 'ocr-error' (2026-07-25 code review: a genuine OCR crash isn't
        # "successfully read" either -- it used to be indistinguishable from a
        # truly blank page and so was never retried until a forced reindex).
        if (prev and not force and prev["method"] not in _RETRY_METHODS
                and (prev["file_hash"] or "") == file_hash):
            return "skipped"

        if not path or not os.path.exists(path):
            method, text = "missing", ""
        else:
            text, method = ocr.extract_document_text(path, tesseract_path)

        conn.execute("DELETE FROM doc_text WHERE entry_id=?", (entry_id,))
        if text:
            conn.execute("INSERT INTO doc_text (entry_id, path, method, content) "
                         "VALUES (?,?,?,?)", (entry_id, path, method, text))
        conn.execute(
            "INSERT INTO indexed_docs (entry_id, path, file_hash, method, chars, indexed_at) "
            "VALUES (?,?,?,?,?, datetime('now','localtime')) "
            "ON CONFLICT(entry_id) DO UPDATE SET path=excluded.path, "
            "file_hash=excluded.file_hash, method=excluded.method, "
            "chars=excluded.chars, indexed_at=excluded.indexed_at",
            (entry_id, path, file_hash, method, len(text)))
        conn.commit()
    return method


def reindex(entries=None, tesseract_path: str = "", force: bool = False) -> dict:
    """Index every given record (default: all logged records). Returns a summary
    count per method. Meant to run on a background thread on the server.
    Prints one line per record as it works through them — 2026-07-18: a single
    stuck OCR page went unnoticed for an hour because this ran silently until
    the final summary; now the current record is always visible, and ocr.py's
    per-page timeout means a bad page can no longer block the whole run."""
    init_index()
    if entries is None:
        entries = database.get_all_entries()
    total = len(entries)
    summary = {}
    for i, e in enumerate(entries, 1):
        print(f"[Reindex] {i}/{total}  #{e.get('id')}  {(e.get('vendor') or '').strip()}",
              flush=True)
        m = index_entry(e, tesseract_path=tesseract_path, force=force)
        summary[m] = summary.get(m, 0) + 1
    return summary


def _fts_query(query: str) -> str:
    """Turn user text into a safe FTS5 MATCH expression: a quoted phrase, so
    operators/punctuation in the query can't break the match or error out."""
    return '"' + (query or "").replace('"', '""').strip() + '"'


def search(query: str, limit: int = 50) -> list:
    """Records whose document TEXT matches the query. Returns
    [{entry_id, method, snippet}], best match first. Empty on blank query or if
    the index doesn't exist yet."""
    q = (query or "").strip()
    if not q or not os.path.exists(_index_path()):
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT entry_id, method, "
                "snippet(doc_text, 3, '[', ']', ' … ', 12) AS snippet "
                "FROM doc_text WHERE doc_text MATCH ? ORDER BY rank LIMIT ?",
                (_fts_query(q), limit)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        # A corrupt/locked/schema-mismatched index must not look identical to
        # "no matches" -- staff would just think search doesn't work (2026-07-27
        # code review). Printed (not raised) so a bad index never breaks the page.
        print(f"[Indexer] search({q!r}) failed: {e} -- suggest a Reindex")
        return []


def status() -> dict:
    """How much is indexed, broken down by method — for the Reindex UI."""
    if not os.path.exists(_index_path()):
        return {"total": 0, "by_method": {}, "pending_ocr": 0, "last_indexed": None}
    with _conn() as conn:
        by_method = {r["method"]: r["n"] for r in conn.execute(
            "SELECT method, COUNT(*) AS n FROM indexed_docs GROUP BY method")}
        total = sum(by_method.values())
        last = conn.execute("SELECT MAX(indexed_at) AS t FROM indexed_docs").fetchone()["t"]
    return {"total": total, "by_method": by_method,
            "pending_ocr": sum(by_method.get(m, 0) for m in _RETRY_METHODS),
            "last_indexed": last}
