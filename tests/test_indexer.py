"""Full-text document index (Option B / B1). The OCR path needs the server's
Tesseract binary, so these exercise the text-layer path, the FTS search, the
incremental skip, and the Bills-page "matches inside documents" wiring."""
import sqlite3

import app as klr
import database
import indexer

from conftest import make_pdf


def _bill_with_pdf(tmp_path, text, file_hash="h1", **over):
    pdf = make_pdf(str(tmp_path / f"{over.get('vendor', 'doc')}.pdf"), text=text)
    data = {"date_received": "2026-06-08", "doc_type": "Bill", "vendor": "SCE",
            "entity_code": "TRUST1", "property_code": "T131", "status": "Pending",
            "pdf_path": pdf, "file_hash": file_hash}
    data.update(over)
    return database.insert_entry(data), pdf


def test_index_and_search_document_text(env, tmp_path):
    eid, pdf = _bill_with_pdf(tmp_path, "EDISON hydroelectric surcharge account 6573 due")
    assert indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h1"}) == "text"
    # a word that lives only in the document body, in no logged field
    hits = indexer.search("hydroelectric")
    assert len(hits) == 1 and hits[0]["entry_id"] == eid
    assert "hydroelectric" in hits[0]["snippet"].lower()
    # unchanged hash is skipped; a changed hash re-reads
    assert indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h1"}) == "skipped"
    st = indexer.status()
    assert st["total"] >= 1 and st["by_method"].get("text", 0) >= 1


def test_reindex_picks_up_new_content_on_hash_change(env, tmp_path):
    eid, pdf = _bill_with_pdf(tmp_path, "original text alpha")
    indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h1"})
    assert indexer.search("alpha") and not indexer.search("omega")
    make_pdf(pdf, text="replaced text omega")          # same path, new content
    indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h2"})
    assert indexer.search("omega") and not indexer.search("alpha")


def test_search_safe_on_blank_and_operators(env, tmp_path):
    assert indexer.search("") == []
    eid, pdf = _bill_with_pdf(tmp_path, "plain content here")
    indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h1"})
    assert indexer.search('bad" OR "expr') == []       # FTS operators can't crash it


def test_search_error_is_surfaced_not_silent(env, tmp_path, monkeypatch, capsys):
    """2026-07-27 code review: a corrupt/locked/schema-mismatched index used to
    return [] with zero visibility, indistinguishable from "no matches" -- staff
    just think search doesn't work. A caught OperationalError must now print a
    diagnostic (still returning [] so the page never breaks)."""
    eid, pdf = _bill_with_pdf(tmp_path, "plain content here")
    indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h1"})

    class _BoomConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(indexer, "_conn", lambda: _BoomConn())
    assert indexer.search("plain") == []
    assert "search" in capsys.readouterr().out.lower()


def test_missing_file_indexes_as_missing(env, tmp_path):
    gone = str(tmp_path / "gone.pdf")
    eid = database.insert_entry({"date_received": "2026-06-08", "doc_type": "Bill",
        "vendor": "X", "status": "Pending", "pdf_path": gone, "file_hash": "h"})
    assert indexer.index_entry({"id": eid, "pdf_path": gone, "file_hash": "h"}) == "missing"


def test_bills_search_surfaces_inside_documents(env, staff_client, tmp_path):
    c, headers = staff_client
    eid, pdf = _bill_with_pdf(tmp_path, "EDISON hydroelectric surcharge account 6573")
    indexer.index_entry({"id": eid, "pdf_path": pdf, "file_hash": "h1"})
    r = c.get("/bills?q=hydroelectric")
    assert r.status_code == 200
    assert b"Matches inside documents" in r.data and b"hydroelectric" in r.data


def test_ocr_error_is_retried_like_ocr_unavailable(env, tmp_path, monkeypatch):
    """2026-07-25 code review: ocr.py used to swallow a genuine OCR crash and
    report it identically to a truly blank page (both 'none'), which
    index_entry treated as already-successfully-read and skipped forever. A
    crash now reports method 'ocr-error', and index_entry retries it on the
    next run -- same file_hash, not forced -- exactly like it already retries
    a prior 'ocr-unavailable'."""
    pdf = str(tmp_path / "scan.pdf")
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 fake scan")
    eid = database.insert_entry({"date_received": "2026-06-08", "doc_type": "Bill",
        "vendor": "X", "status": "Pending", "pdf_path": pdf, "file_hash": "h1"})

    calls = {"n": 0}
    def fake_extract(path, tesseract_path=""):
        calls["n"] += 1
        return "", "ocr-error"
    monkeypatch.setattr(indexer.ocr, "extract_document_text", fake_extract)

    entry = {"id": eid, "pdf_path": pdf, "file_hash": "h1"}
    assert indexer.index_entry(entry) == "ocr-error"
    assert calls["n"] == 1
    # Same hash, not forced -- must retry (not "skipped"), unlike a genuinely
    # successful read of unchanged content.
    assert indexer.index_entry(entry) == "ocr-error"
    assert calls["n"] == 2

    st = indexer.status()
    assert st["by_method"].get("ocr-error", 0) >= 1
    assert st["pending_ocr"] >= 1


def test_reindex_route_is_admin_only(env, staff_client, admin_client):
    # admin_required bounces non-admins to the dashboard; admins go through and
    # get redirected back to /bills after the background job is kicked.
    c, headers = staff_client
    staff_loc = c.post("/bills/reindex", headers=headers).headers["Location"]
    assert "/bills" not in staff_loc
    a, aheaders = admin_client
    assert "/bills" in a.post("/bills/reindex", headers=aheaders).headers["Location"]
