"""Folder browser + move-anywhere (reports #25/#34/#37/#44/#45, UNC #33/#36):
filing.display_path, the widened destination policy (cabinet root minus
excludes), the /api/folders endpoints, and move_entry_file's attachment-follow
and rename-to-convention behavior."""
import json
import os

import app as klr
import database
import filing
import folder_index
from conftest import make_pdf


def _set_cfg(env, **kv):
    env.cfg.update(kv)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)


def _cabinet(env):
    """A fake file-server root: <tmp>\\Cabinet with Files, Documents,
    IntakeSystem (excluded) inside; archive/filed re-pointed under it."""
    root = os.path.join(str(env.tmp), "Cabinet")
    archive = os.path.join(root, "Files")
    docs = os.path.join(root, "Documents", "BANK STATEMENTS")
    intake = os.path.join(root, "IntakeSystem", "Unfiled Scans")
    os.makedirs(archive, exist_ok=True)
    os.makedirs(docs, exist_ok=True)
    os.makedirs(intake, exist_ok=True)
    _set_cfg(env, archive_folder=archive, cabinet_root=root,
             server_root=root, server_root_unc=r"\\TESTSRV\Cabinet")
    return root


# ── filing.display_path / policy ──────────────────────────────────────────────

def test_display_path_maps_server_root_to_unc(env):
    root = _cabinet(env)
    cfg = klr.load_config()
    p = os.path.join(root, "Files", "2026", "x.pdf")
    assert filing.display_path(p, cfg) == r"\\TESTSRV\Cabinet" + "\\" + os.path.join("Files", "2026", "x.pdf")
    assert filing.display_path(r"C:\somewhere\else.pdf", cfg) == r"C:\somewhere\else.pdf"


def test_validate_allows_cabinet_but_not_excludes(env):
    root = _cabinet(env)
    cfg = klr.load_config()
    docs = os.path.join(root, "Documents", "BANK STATEMENTS")
    assert filing.validate_filed_dir(docs, cfg) == docs
    assert filing.validate_filed_dir(os.path.join(root, "IntakeSystem", "Unfiled Scans"), cfg) is None
    assert filing.validate_filed_dir(os.path.join(root, "_OPS INDEX"), cfg) is None
    # The app's Filed tree stays allowed even though it sits inside IntakeSystem.
    assert filing.validate_filed_dir(env.filed_folder, cfg) == os.path.normpath(env.filed_folder)


def test_resolve_client_dir_reroots_unc_onto_cabinet(env):
    root = _cabinet(env)
    cfg = klr.load_config()
    got = filing.resolve_client_dir(r"\\TESTSRV\Cabinet\Documents\BANK STATEMENTS", cfg)
    assert got == os.path.join(root, "Documents", "BANK STATEMENTS")
    assert filing.resolve_client_dir(r"\\TESTSRV\Cabinet\IntakeSystem\Unfiled Scans", cfg) is None


# ── /api/folders ──────────────────────────────────────────────────────────────

def test_folders_roots_and_children(env, staff_client):
    root = _cabinet(env)
    c, _h = staff_client
    r = c.get("/api/folders")
    roots = {d["name"]: d["path"] for d in r.get_json()["dirs"]}
    assert roots.get("Files (the archive)") == os.path.join(root, "Files")
    assert "Documents" in roots
    assert not any("IntakeSystem" in p for p in roots.values())
    r = c.get("/api/folders", query_string={"path": os.path.join(root, "Documents")})
    d = r.get_json()
    assert [x["name"] for x in d["dirs"]] == ["BANK STATEMENTS"]
    assert d["unc"].startswith(r"\\TESTSRV\Cabinet")
    assert d["crumbs"][-1]["name"] == "Documents"


def test_folders_rejects_off_server_paths(env, staff_client):
    _cabinet(env)
    c, _h = staff_client
    r = c.get("/api/folders", query_string={"path": r"C:\Windows"})
    assert r.status_code == 400


def test_folders_search_finds_by_words(env, staff_client):
    _cabinet(env)
    c, _h = staff_client
    folder_index._build_index()
    r = c.get("/api/folders/search", query_string={"q": "bank statements"})
    res = r.get_json()["results"]
    assert any(x["name"] == "BANK STATEMENTS" for x in res)


# ── move_entry_file: attachments follow, rename option ────────────────────────

def _filed_entry(env, folder, name="07-01-26 TRUST1 ELEC - ACME - $10.00.pdf"):
    os.makedirs(folder, exist_ok=True)
    p = make_pdf(os.path.join(folder, name))
    eid = database.insert_entry({"vendor": "ACME Utilities", "status": "Pending",
                                 "doc_type": "Bill", "bill_type_code": "ELEC",
                                 "entity_code": "TRUST1", "property_code": "T27",
                                 "amount": 10.0, "date_received": "2026-07-01",
                                 "filed_path": p})
    return eid, p


def test_move_takes_same_folder_attachments_along(env, staff_client):
    root = _cabinet(env)
    c, headers = staff_client
    src_dir = os.path.join(root, "Files", "2026", "OLD SPOT")
    eid, p = _filed_entry(env, src_dir)
    copy = make_pdf(os.path.join(src_dir, "a copy.pdf"))
    elsewhere = make_pdf(os.path.join(root, "Files", "away.pdf"))
    database.add_attachment(eid, copy, "Copy", "a copy.pdf", "")
    database.add_attachment(eid, elsewhere, "Copy", "away.pdf", "")
    dest = os.path.join(root, "Documents", "BANK STATEMENTS")
    r = c.post(f"/entry/{eid}/move-file", headers=headers, data={"dest_dir": dest})
    assert r.status_code == 302
    e = database.get_entry(eid)
    assert os.path.dirname(e["filed_path"]) == dest
    atts = {a["original_name"]: a["file_path"] for a in database.get_attachments(eid)}
    assert os.path.dirname(atts["a copy.pdf"]) == dest        # moved along
    assert atts["away.pdf"] == elsewhere                      # other-folder copy untouched
    assert os.path.exists(atts["a copy.pdf"])


def test_move_with_rename_to_convention(env, staff_client):
    root = _cabinet(env)
    c, headers = staff_client
    eid, p = _filed_entry(env, os.path.join(root, "Files", "2026", "OLD SPOT"),
                          name="badly named scan.pdf")
    dest = os.path.join(root, "Documents", "BANK STATEMENTS")
    r = c.post(f"/entry/{eid}/move-file", headers=headers,
               data={"dest_dir": dest, "rename_convention": "1"})
    assert r.status_code == 302
    e = database.get_entry(eid)
    stem = os.path.splitext(os.path.basename(e["filed_path"]))[0]
    assert stem == stem.upper() and "ACME" in stem            # caps: #45
    assert os.path.dirname(e["filed_path"]) == dest
    assert os.path.exists(e["filed_path"])
