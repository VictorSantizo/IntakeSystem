"""Move Document (reports #25/#37/#46): in-app file move that keeps the record
and its document together, cross-desk folder resolution (report #36), and the
on-view relink that heals a hand-moved file the moment the record is opened."""
import os

import database
import filing
import app as klr
from conftest import make_pdf


def _entry(filed_path, file_hash="", **overrides):
    d = {"date_received": "2026-07-18", "entity_code": "TRUST1", "property_code": "T27",
         "doc_type": "Bill", "vendor": "Banc Test", "status": "Pending",
         "filed_path": filed_path, "file_hash": file_hash}
    d.update(overrides)
    return database.insert_entry(d)


# ── filing.resolve_client_dir ─────────────────────────────────────────────────

def _cfg_with_archive(tmp_path):
    archive = tmp_path / "File-Cabinet" / "Files"
    dest = archive / "2026" / "ACCOUNTS PAYABLE - INCOMING BILLS" / "07. JULY"
    dest.mkdir(parents=True)
    filed = tmp_path / "Filed"
    filed.mkdir()
    return ({"archive_folder": str(archive), "filed_folder": str(filed)},
            str(dest))


def test_resolve_accepts_server_side_path(tmp_path):
    cfg, dest = _cfg_with_archive(tmp_path)
    assert filing.resolve_client_dir(dest, cfg) == os.path.normpath(dest)


def test_resolve_reroots_other_desks_drive_letter(tmp_path):
    """Joshua's desk shows the archive as F:\\Files\\..., Nancy's as Z:\\...;
    the server's own root is elsewhere (report #36)."""
    cfg, dest = _cfg_with_archive(tmp_path)
    typed = "Q:\\Files\\2026\\ACCOUNTS PAYABLE - INCOMING BILLS\\07. JULY"
    assert filing.resolve_client_dir(typed, cfg) == os.path.normpath(dest)


def test_resolve_reroots_unc_share(tmp_path):
    cfg, dest = _cfg_with_archive(tmp_path)
    typed = "\\\\LACKSSERVER2021\\File-Cabinet\\Files\\2026\\ACCOUNTS PAYABLE - INCOMING BILLS\\07. JULY"
    assert filing.resolve_client_dir(typed, cfg) == os.path.normpath(dest)


def test_resolve_rejects_nonexistent_rerooted_folder(tmp_path):
    """A re-rooted guess must never invent a directory tree."""
    cfg, _dest = _cfg_with_archive(tmp_path)
    assert filing.resolve_client_dir("Q:\\Files\\2031\\NOPE", cfg) is None


def test_resolve_rejects_paths_outside_roots(tmp_path):
    cfg, _dest = _cfg_with_archive(tmp_path)
    assert filing.resolve_client_dir(str(tmp_path / "elsewhere"), cfg) is None
    assert filing.resolve_client_dir("", cfg) is None


# ── POST /entry/<id>/move-file ────────────────────────────────────────────────

def test_move_file_moves_and_updates_record(staff_client, env):
    c, headers = staff_client
    src_dir = os.path.join(env.filed_folder, "TRUST1", "T27", "2026")
    os.makedirs(src_dir)
    src = make_pdf(os.path.join(src_dir, "07-18-26 BANC TEST.pdf"))
    eid = _entry(src)
    dest_dir = os.path.join(env.filed_folder, "TRUST1", "T27", "2027")
    r = c.post(f"/entry/{eid}/move-file", data={"dest_dir": dest_dir},
               headers=headers)
    assert r.status_code == 302
    moved = os.path.join(dest_dir, "07-18-26 BANC TEST.pdf")
    assert os.path.isfile(moved) and not os.path.exists(src)
    assert database.get_entry(eid)["filed_path"] == moved
    acts = database.get_activity_feed()
    assert any(a["action"] == "Document moved" and a["entry_id"] == eid for a in acts)


def test_move_file_rejects_outside_roots(staff_client, env):
    c, headers = staff_client
    src_dir = os.path.join(env.filed_folder, "TRUST1")
    os.makedirs(src_dir)
    src = make_pdf(os.path.join(src_dir, "doc.pdf"))
    eid = _entry(src)
    outside = str(env.tmp / "not-allowed")
    r = c.post(f"/entry/{eid}/move-file", data={"dest_dir": outside},
               headers=headers, follow_redirects=False)
    assert r.status_code == 302
    assert os.path.isfile(src)
    assert database.get_entry(eid)["filed_path"] == src


def test_move_file_never_overwrites(staff_client, env):
    c, headers = staff_client
    src_dir = os.path.join(env.filed_folder, "A")
    dest_dir = os.path.join(env.filed_folder, "B")
    os.makedirs(src_dir)
    os.makedirs(dest_dir)
    src = make_pdf(os.path.join(src_dir, "same-name.pdf"))
    blocker = make_pdf(os.path.join(dest_dir, "same-name.pdf"), text="different bytes")
    eid = _entry(src)
    c.post(f"/entry/{eid}/move-file", data={"dest_dir": dest_dir}, headers=headers)
    assert os.path.isfile(src)                       # untouched
    assert database.get_entry(eid)["filed_path"] == src
    assert os.path.getsize(blocker) > 0


# ── heal-on-view ──────────────────────────────────────────────────────────────

def test_view_heals_hand_moved_file(staff_client, env):
    """Record #854's case: the file was hand-moved (and renamed) into a sibling
    folder; opening the record must relink it immediately, not at next restart."""
    c, headers = staff_client
    base = os.path.join(env.filed_folder, "TRUST1", "T27", "2026")
    new_dir = os.path.join(base, "07. JULY")
    os.makedirs(new_dir)
    real = make_pdf(os.path.join(new_dir, "07-18-26 BANC TEST - MOVED.PDF"))
    dead = os.path.join(base, "06. JUNE", "old-name.pdf")
    eid = _entry(dead, file_hash=klr._sha256_file(real))
    r = c.get(f"/entry/{eid}/view")
    assert r.status_code == 200
    assert database.get_entry(eid)["filed_path"] == real
    assert b"No scanned file is available" not in r.data


def test_view_does_not_steal_another_records_file(staff_client, env):
    c, headers = staff_client
    base = os.path.join(env.filed_folder, "TRUST1", "T27", "2026")
    os.makedirs(base)
    owned = make_pdf(os.path.join(base, "owned.pdf"))
    h = klr._sha256_file(owned)
    _owner = _entry(owned, file_hash=h)
    dead = os.path.join(base, "gone.pdf")
    eid = _entry(dead, file_hash=h)      # same bytes, but the file is claimed
    c.get(f"/entry/{eid}/view")
    assert database.get_entry(eid)["filed_path"] == dead   # unchanged
