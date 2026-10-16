"""The Files view (/files): the real cabinet tree with record chips.

Read-only browse of the actual folder structure, the database overlaid:
registered files carry their record chip, attachment copies say so, and
everything else is honestly "on disk only". Serving stays inside the cabinet
(containment via filing.resolve_client_dir — same rule as every path)."""
import json
import os

import app as klr
import database
from conftest import make_pdf


def _cabinet(env):
    """A small archive: 2024 statements tree + a loose 2026 bill."""
    archive = os.path.join(str(env.tmp), "Files")
    tree = os.path.join(archive, "2024", "PP", "CASH FLOW STATEMENTS",
                        "PP16 - 105 REAL ROAD", "1. JANUARY")
    os.makedirs(tree, exist_ok=True)
    env.cfg.update({"archive_folder": archive})
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    p_filed = make_pdf(os.path.join(tree, "PP16 CASH FLOW - JAN 2024.pdf"))
    p_loose = make_pdf(os.path.join(tree, "PP16 CASH FLOW - FEB 2024.pdf"))
    return archive, tree, p_filed, p_loose


def test_roots_page_lists_the_archive(env, staff_client):
    _cabinet(env)
    c, _h = staff_client
    html = c.get("/files").get_data(as_text=True)
    assert "Files (the archive)" in html


def test_browse_shows_chips_and_on_disk_only(env, staff_client):
    archive, tree, p_filed, p_loose = _cabinet(env)
    rid = database.insert_entry({
        "date_received": "2024-01-31", "status": "Filed",
        "doc_type": "Statement", "vendor": "Cash Flow", "entity_code": "PP",
        "pdf_path": p_filed, "filed_path": p_filed})
    rec_no = database.get_entry(rid)["record_no"]
    c, _h = staff_client
    html = c.get(f"/files?p={tree}").get_data(as_text=True)
    assert "PP16 CASH FLOW - JAN 2024.pdf" in html
    assert rec_no in html and "s-filed" in html          # the record chip
    assert "on disk only" in html                        # the loose file
    assert f"/entry/{rid}/view" in html
    assert "PP16 - 105 REAL ROAD" in html                # breadcrumb trail


def test_browse_shows_copy_chip_for_attachments(env, staff_client):
    archive, tree, p_filed, p_loose = _cabinet(env)
    rid = database.insert_entry({
        "date_received": "2024-01-31", "status": "Filed",
        "doc_type": "Statement", "vendor": "Cash Flow",
        "pdf_path": p_filed, "filed_path": p_filed})
    database.add_attachment(rid, p_loose, "Hand-filed",
                            os.path.basename(p_loose), "")
    c, _h = staff_client
    html = c.get(f"/files?p={tree}").get_data(as_text=True)
    assert "copy of" in html


def test_folder_search_matches_all_words(env, staff_client):
    archive, tree, _pf, _pl = _cabinet(env)
    c, _h = staff_client
    html = c.get(f"/files?p={archive}&q=cash+jan").get_data(as_text=True)
    assert "JAN 2024.pdf" in html
    assert "FEB 2024.pdf" not in html


def test_open_serves_pdfs_and_stays_inside_the_cabinet(env, staff_client):
    archive, tree, p_filed, _pl = _cabinet(env)
    c, _h = staff_client
    r = c.get(f"/files/open?p={p_filed}")
    assert r.status_code == 200
    assert r.data.startswith(b"%PDF")
    outside = os.path.join(str(env.tmp), "outside.txt")
    with open(outside, "w") as f:
        f.write("secret")
    assert c.get(f"/files/open?p={outside}").status_code == 404


def test_files_is_in_the_nav_after_the_log(env, staff_client):
    _cabinet(env)
    c, _h = staff_client
    nav = c.get("/log").get_data(as_text=True).split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert nav.index('data-kk="nav-log"') < nav.index('data-kk="nav-files"') \
        < nav.index('data-kk="nav-bills"')
