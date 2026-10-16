"""Removing duplicate 'Copies on file' (2026-07-07): byte-identical duplicates
get attached to a record rather than re-queued, and after heavy testing one
record can carry dozens. Staff can now prune them from the record view — per
copy or all at once — with the files set aside (recoverable), never deleted,
and the primary document + record untouched.
"""
import os

import database
import app as klr


def _record_with_copies(tmp_path, n=3):
    entry_id = database.insert_entry({
        "date_received": "2026-07-07", "doc_type": "Correspondence",
        "vendor": "Southern California Edison Company (SCE)", "status": "Pending"})
    paths = []
    for i in range(n):
        d = tmp_path / "Filed"
        d.mkdir(exist_ok=True)
        p = d / f"copy{i}.pdf"
        p.write_text("identical bytes")
        database.add_attachment(entry_id, str(p), "Physical Mail", f"scan{i}.pdf", "Joshua")
        paths.append(p)
    return entry_id, paths


# ------------------------------------------------------------- DB helpers

def test_delete_attachment_returns_row_and_removes_it(env, tmp_path):
    entry_id, _ = _record_with_copies(tmp_path, n=2)
    atts = database.get_attachments(entry_id)
    removed = database.delete_attachment(atts[0]["id"])
    assert removed["id"] == atts[0]["id"]
    remaining = [a["id"] for a in database.get_attachments(entry_id)]
    assert atts[0]["id"] not in remaining and atts[1]["id"] in remaining
    assert database.delete_attachment(atts[0]["id"]) is None   # already gone


def test_delete_attachments_returns_all_rows(env, tmp_path):
    entry_id, _ = _record_with_copies(tmp_path, n=4)
    removed = database.delete_attachments(entry_id)
    assert len(removed) == 4
    assert database.get_attachments(entry_id) == []


# ------------------------------------------------------------- routes

def test_remove_one_copy_sets_file_aside_and_keeps_others(env, staff_client, tmp_path):
    c, headers = staff_client
    entry_id, paths = _record_with_copies(tmp_path, n=3)
    atts = database.get_attachments(entry_id)
    r = c.post(f"/attachment/{atts[0]['id']}/remove", headers=headers)
    assert r.status_code in (302, 303)
    assert len(database.get_attachments(entry_id)) == 2         # one removed
    assert not paths[0].exists()                                # file moved out
    assert (tmp_path / "Filed" / "_Removed Copies" / "copy0.pdf").exists()
    assert database.get_entry(entry_id) is not None             # record untouched


def test_remove_all_copies_clears_list_and_recovers_files(env, staff_client, tmp_path):
    c, headers = staff_client
    entry_id, paths = _record_with_copies(tmp_path, n=5)
    r = c.post(f"/entry/{entry_id}/copies/remove_all", headers=headers)
    assert r.status_code in (302, 303)
    assert database.get_attachments(entry_id) == []
    assert all(not p.exists() for p in paths)
    recovered = tmp_path / "Filed" / "_Removed Copies"
    assert len(list(recovered.glob("*.pdf"))) == 5
    assert database.get_entry(entry_id) is not None


def test_remove_missing_file_still_clears_the_row(env, staff_client, tmp_path):
    c, headers = staff_client
    entry_id, paths = _record_with_copies(tmp_path, n=1)
    paths[0].unlink()                                           # file already gone
    atts = database.get_attachments(entry_id)
    r = c.post(f"/attachment/{atts[0]['id']}/remove", headers=headers)
    assert r.status_code in (302, 303)
    assert database.get_attachments(entry_id) == []            # row still cleared


def test_remove_all_is_activity_logged(env, staff_client, tmp_path):
    c, headers = staff_client
    entry_id, _ = _record_with_copies(tmp_path, n=3)
    c.post(f"/entry/{entry_id}/copies/remove_all", headers=headers)
    feed = " ".join(a.get("action", "") + a.get("detail", "")
                    for a in database.get_activity_feed(50))
    assert "Duplicate copies removed" in feed and "3 copies" in feed


# --------------------------------------------------------- record view UI

def test_record_view_shows_remove_controls(env, staff_client, tmp_path):
    c, headers = staff_client
    entry_id, _ = _record_with_copies(tmp_path, n=2)
    html = c.get(f"/entry/{entry_id}/view").get_data(as_text=True)
    assert "Remove all copies" in html
    assert f"/entry/{entry_id}/copies/remove_all" in html
    assert "/remove" in html


def test_record_view_no_remove_controls_without_copies(env, staff_client):
    c, headers = staff_client
    entry_id = database.insert_entry({"date_received": "2026-07-07", "doc_type": "Bill",
                                      "vendor": "Solo", "status": "Pending"})
    html = c.get(f"/entry/{entry_id}/view").get_data(as_text=True)
    assert "Remove all copies" not in html


def test_delete_entry_retires_attachments_instead_of_orphaning(env, staff_client, tmp_path):
    # 2026-07-26 review finding: delete_entry_route deleted the attachment ROWS
    # but never set their files aside, unlike every other delete path in the
    # app -- the files became untracked, recoverable-nowhere junk on disk.
    c, headers = staff_client
    entry_id, paths = _record_with_copies(tmp_path, n=2)
    r = c.post(f"/entry/{entry_id}/delete", headers=headers)
    assert r.status_code in (302, 303)
    assert database.get_entry(entry_id) is None
    assert all(not p.exists() for p in paths)                  # moved, not left behind
    recovered = tmp_path / "Filed" / "_Removed Copies"
    assert len(list(recovered.glob("*.pdf"))) == 2
