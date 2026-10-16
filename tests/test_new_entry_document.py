r"""Report #50 — "This screen should have an option to pull in a scanned file
and choose whether to move it to a new location or leave it where it is. If
it's on the user's local drive, then it should be copied into the correct
location ... OCR completed after and the form filled with that info", and the
bounce that followed the first fix: "i don't see an option on the new entry
page to upload a document."

What these pin, in the order the person meets them:
  * /entry/new offers the control AT THE TOP (the bounce)
  * a file on someone's own computer is COPIED to the file server, never
    referenced where it sits
  * a file already on the server is read where it is, and only accepted when
    filing.py says the path is really on the file server
  * the reader's answer comes back as pre-filled form values
  * "leave it where it is" leaves the bytes, the folder AND the name alone
    (report #90's rule), while "file it into the cabinet" moves it under the
    archive's convention
"""
import io
import json
import os

import app as klr
import database
import filing
import folder_index
import routes_entry
from conftest import make_pdf


def _set_cfg(env, **kv):
    env.cfg.update(kv)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)


def _cabinet(env):
    """A fake file server: <tmp>\\Cabinet with the archive inside it."""
    root = os.path.join(str(env.tmp), "Cabinet")
    archive = os.path.join(root, "Files", "2026", "07. JULY")
    os.makedirs(archive, exist_ok=True)
    _set_cfg(env, archive_folder=os.path.join(root, "Files"), cabinet_root=root,
             server_root=root, server_root_unc=r"\\TESTSRV\Cabinet")
    return root, archive


def _fake_read(monkeypatch, **fields):
    """Stand in for the classifier — these tests are about the plumbing, and
    the real reader costs an API call."""
    result = {"vendor": "ACME UTILITIES", "amount": 147.48, "doc_type": "Bill",
              "entity_code": "TRUST1", "property_codes": ["T27"],
              "invoice_number": "INV-9", "due_date": "2026-07-15",
              "ai_confidence": "High"}
    result.update(fields)
    monkeypatch.setattr(routes_entry, "classify_document", lambda path: dict(result))
    return result


# ── the bounce: the control has to be ON /entry/new ───────────────────────────

def test_new_entry_page_offers_a_document_pick(env, staff_client):
    c, h = staff_client
    html = c.get("/entry/new").get_data(as_text=True)
    assert "Start from a document" in html
    assert '/entry/new/from-document' in html
    assert 'name="upload"' in html          # a file on this computer
    assert 'name="source_path"' in html     # ...or one already on the server
    # "this option should be at the top" — above the fields it fills in.
    assert html.index("Start from a document") < html.index('name="vendor"')


# ── a file on someone's own computer is copied over ───────────────────────────

def test_upload_is_copied_onto_the_file_server_and_read(env, staff_client, monkeypatch):
    c, h = staff_client
    _fake_read(monkeypatch)
    pdf = make_pdf(os.path.join(str(env.tmp), "desk-scan.pdf"))
    with open(pdf, "rb") as fh:
        data = {"upload": (io.BytesIO(fh.read()), "desk-scan.pdf")}
        html = c.post("/entry/new/from-document", data=data, headers=h,
                      content_type="multipart/form-data").get_data(as_text=True)
    staged = os.path.join(env.scan_folder, "Uploads", "New Entry", "desk-scan.pdf")
    assert os.path.isfile(staged), "the bytes must land on the file server"
    # ...and the form comes back filled in from the read.
    assert 'value="ACME UTILITIES"' in html
    assert 'value="147.48"' in html
    assert 'value="INV-9"' in html
    assert "Document pulled in" in html
    # An uploaded file has no home yet, so there is no "leave it" choice.
    assert 'value="leave"' not in html


def test_upload_of_an_unreadable_type_is_refused(env, staff_client):
    c, h = staff_client
    data = {"upload": (io.BytesIO(b"a,b,c"), "ledger.csv")}
    html = c.post("/entry/new/from-document", data=data, headers=h,
                  content_type="multipart/form-data").get_data(as_text=True)
    assert "can&#39;t be read" in html or "can't be read" in html
    assert not os.path.exists(os.path.join(env.scan_folder, "Uploads", "New Entry"))


# ── a file already on the file server ─────────────────────────────────────────

def test_server_side_document_is_read_where_it_sits(env, staff_client, monkeypatch):
    c, h = staff_client
    _root, archive = _cabinet(env)
    _fake_read(monkeypatch)
    pdf = make_pdf(os.path.join(archive, "07-01-26 ACME - JULY.pdf"))
    html = c.post("/entry/new/from-document", headers=h,
                  data={"source_path": pdf}).get_data(as_text=True)
    assert os.path.isfile(pdf), "reading a document must not move it"
    assert 'value="ACME UTILITIES"' in html
    # Both placements are offered, and leaving it alone is the default.
    assert 'value="leave"' in html and 'value="cabinet"' in html


def test_a_path_off_the_file_server_is_refused(env, staff_client):
    c, h = staff_client
    _cabinet(env)
    outside = make_pdf(os.path.join(str(env.tmp), "not-on-the-server.pdf"))
    html = c.post("/entry/new/from-document", headers=h,
                  data={"source_path": outside}).get_data(as_text=True)
    assert "isn&#39;t on the file server" in html or "isn't on the file server" in html


def test_resolve_client_file_reroots_a_pasted_unc_path(env):
    _root, archive = _cabinet(env)
    cfg = klr.load_config()
    pdf = make_pdf(os.path.join(archive, "07-02-26 ACME.pdf"))
    pasted = r"\\TESTSRV\Cabinet\Files\2026\07. JULY\07-02-26 ACME.pdf"
    assert filing.resolve_client_file(pasted, cfg) == os.path.normpath(pdf)
    assert filing.resolve_client_file(
        r"\\TESTSRV\Cabinet\Files\2026\07. JULY\nope.pdf", cfg) is None
    assert filing.resolve_client_file(r"C:\Users\Someone\bill.pdf", cfg) is None


# ── the two placements, at save time ──────────────────────────────────────────

def _log_it(c, h, path, placement, **extra):
    data = {"date_received": "2026-07-01", "doc_type": "Bill",
            "vendor": "ACME UTILITIES", "entity_code": "TRUST1",
            "property_codes": "T27", "amount": "147.48", "date_ok": "1",
            "source_path": path, "placement": placement}
    data.update(extra)
    return c.post("/entry/new", data=data, headers=h, follow_redirects=False)


def test_leave_it_where_it_is_touches_nothing(env, staff_client):
    c, h = staff_client
    _root, archive = _cabinet(env)
    name = "07-01-26 ACME UTILITIES - JULY - INV# 9 - T27.pdf"
    pdf = make_pdf(os.path.join(archive, name))
    before = open(pdf, "rb").read()
    _log_it(c, h, pdf, "leave")
    rows = database.get_all_entries()
    assert len(rows) == 1
    assert os.path.isfile(pdf), "the staff-filed document must stay put"
    assert os.path.basename(pdf) == name, "and must not be renamed (#90)"
    assert open(pdf, "rb").read() == before
    assert os.path.normpath(rows[0]["filed_path"]) == os.path.normpath(pdf)
    assert rows[0]["file_hash"], "the bytes are hashed so dedup can see it later"


def test_file_it_into_the_cabinet_moves_and_renames(env, staff_client):
    c, h = staff_client
    _root, archive = _cabinet(env)
    pdf = make_pdf(os.path.join(archive, "scan0001.pdf"))
    _log_it(c, h, pdf, "cabinet")
    rows = database.get_all_entries()
    assert len(rows) == 1
    filed = rows[0]["filed_path"]
    assert filed and os.path.isfile(filed)
    assert not os.path.exists(pdf), "'move it' means the original does move"
    stem = os.path.splitext(os.path.basename(filed))[0]
    assert stem == stem.upper(), "server filenames are ALL CAPS (#45)"
    assert "ACME UTILITIES" in stem, "and follow the archive's convention"


def test_a_chosen_destination_folder_is_honoured(env, staff_client):
    c, h = staff_client
    _root, archive = _cabinet(env)
    dest = os.path.join(str(env.tmp), "Cabinet", "Files", "2026", "08. AUGUST")
    os.makedirs(dest)
    pdf = make_pdf(os.path.join(archive, "scan0002.pdf"))
    _log_it(c, h, pdf, "cabinet", filed_dir=dest)
    filed = database.get_all_entries()[0]["filed_path"]
    assert os.path.normpath(os.path.dirname(filed)) == os.path.normpath(dest)


def test_a_forged_source_path_is_ignored_at_save_time(env, staff_client):
    """The hidden field is a convenience, not a permission: a path outside the
    file server is dropped and the record is logged without a document rather
    than reaching for a file it should not touch."""
    c, h = staff_client
    _cabinet(env)
    outside = make_pdf(os.path.join(str(env.tmp), "elsewhere.pdf"))
    _log_it(c, h, outside, "leave")
    row = database.get_all_entries()[0]
    assert not row["filed_path"]
    assert os.path.isfile(outside), "and it certainly must not be moved"


# ── the received date follows the house rule (#107) ───────────────────────────

def test_received_date_falls_back_to_the_filename_and_its_year_folder(env, staff_client,
                                                                     monkeypatch):
    """No stamp read off the document — so the filename supplies month and day
    and the year tree supplies the year, exactly as the cabinet watcher does."""
    c, h = staff_client
    _root, archive = _cabinet(env)
    _fake_read(monkeypatch, received_date=None)
    pdf = make_pdf(os.path.join(archive, "07-01-23 PEYTON_SCAPES - JUNE - INV# 621.pdf"))
    html = c.post("/entry/new/from-document", headers=h,
                  data={"source_path": pdf}).get_data(as_text=True)
    assert 'value="2026-07-01"' in html
    assert "year is a typo" in html


def test_a_stamp_read_off_the_document_wins(env, staff_client, monkeypatch):
    c, h = staff_client
    _root, archive = _cabinet(env)
    _fake_read(monkeypatch, received_date="2026-07-09")
    pdf = make_pdf(os.path.join(archive, "07-01-26 ACME.pdf"))
    html = c.post("/entry/new/from-document", headers=h,
                  data={"source_path": pdf}).get_data(as_text=True)
    assert 'value="2026-07-09"' in html


# ── the byte-level duplicate warning, before anyone types ─────────────────────

def test_a_document_already_on_file_is_called_out(env, staff_client, monkeypatch):
    c, h = staff_client
    _root, archive = _cabinet(env)
    _fake_read(monkeypatch)
    pdf = make_pdf(os.path.join(archive, "07-03-26 ACME.pdf"))
    database.insert_entry({"date_received": "2026-07-03", "vendor": "ACME UTILITIES",
                           "doc_type": "Bill", "status": "Pending",
                           "file_hash": klr._sha256_file(pdf)})
    html = c.post("/entry/new/from-document", headers=h,
                  data={"source_path": pdf}).get_data(as_text=True)
    assert "already logged as" in html


# ── the picker's data source ──────────────────────────────────────────────────

def test_api_folders_lists_documents_only_when_asked(env, staff_client):
    c, h = staff_client
    _root, archive = _cabinet(env)
    make_pdf(os.path.join(archive, "a bill.pdf"))
    with open(os.path.join(archive, "notes.xlsx"), "w") as fh:
        fh.write("x")
    plain = c.get("/api/folders", query_string={"path": archive}).get_json()
    assert "files" not in plain
    listed = c.get("/api/folders", query_string={"path": archive,
                                                 "files": "1"}).get_json()
    assert [x["name"] for x in listed["files"]] == ["a bill.pdf"]
    assert listed["files"][0]["unc"].startswith(r"\\TESTSRV\Cabinet")
