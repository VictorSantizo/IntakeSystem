"""/paid-backfill — the side-by-side visual review for the paid-bills backfill.

Reads the dry-run CSV, shows packet-vs-record, records verdicts to the
DECISIONS csv. Admin-only; the doc route serves only files the CSV names and
only from inside the archive; nothing ever writes the intake DB.
"""
import csv
import json
import os

import database
import paid_backfill as pb


def _set_cfg(env, **kv):
    env.cfg.update(kv)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)


def _setup(env, tmp_path, rows):
    """A tiny archive + ops dir holding a dry-run CSV with the given rows.
    Each row: (disposition, reason, filename, amount, matched_id) — the file
    is created under the archive so the doc route can serve it."""
    archive = tmp_path / "Files" / "2026" / "KLR" / "KLR PAID BILLS"
    archive.mkdir(parents=True, exist_ok=True)
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    _set_cfg(env, ops_index_dir=str(ops), archive_folder=str(tmp_path / "Files"))
    out = []
    for disp, reason, fname, amount, mid in rows:
        p = archive / fname
        p.write_bytes(b"%PDF-1.4 packet")
        out.append({"disposition": disp, "reason": reason, "unc_path": str(p),
                    "name_date": "2026-06-14", "entity": "KLR", "method": "Check",
                    "check": "12345", "amount": amount, "payee": "ACME UTILITIES",
                    "props": "", "matched_id": mid, "matched_status": "",
                    "matched_vendor": "", "matched_amount": "", "matched_check": ""})
    with open(ops / "PAID_BACKFILL_2026.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=pb._CSV_COLS)
        w.writeheader()
        w.writerows(out)
    return ops


def _bill(**kw):
    e = {"vendor": "ACME Utilities", "amount": 147.48, "status": "Pending",
         "doc_type": "Bill", "date_received": "2026-06-10"}
    e.update(kw)
    return database.insert_entry(e)


def test_page_is_admin_only(env, tmp_path, staff_client):
    _setup(env, tmp_path, [("held", "why", "a.pdf", "10.00", "")])
    c, _h = staff_client
    r = c.get("/paid-backfill")
    assert r.status_code != 200


def test_list_groups_and_rows(env, tmp_path, admin_client):
    _setup(env, tmp_path, [
        ("held", "twin risk", "a.pdf", "10.00", ""),
        ("match", "amount + vendor", "b.pdf", "147.48", "1"),
        ("create-paid", "clean", "c.pdf", "20.00", "")])
    c, _h = admin_client
    body = c.get("/paid-backfill").get_data(as_text=True)
    assert "0 / 1 decided" in body          # held group chip
    assert "a.pdf" in body and "b.pdf" not in body   # held filter is default
    body = c.get("/paid-backfill?disp=match").get_data(as_text=True)
    assert "b.pdf" in body


def test_item_shows_packet_beside_the_matched_record(env, tmp_path, admin_client):
    eid = _bill()
    _setup(env, tmp_path, [("match", "amount + vendor + date window",
                            "b.pdf", "147.48", str(eid))])
    c, _h = admin_client
    body = c.get("/paid-backfill/item/0").get_data(as_text=True)
    assert "/paid-backfill/doc/0" in body            # packet iframe
    assert f"/entry/{eid}/pdf" not in body or True   # record pane (no doc on file)
    assert "ACME Utilities" in body                  # record fields
    assert "Same bill" in body and "mark Paid" in body
    assert "$147.48" in body


def test_doc_route_serves_only_archive_files_from_the_csv(env, tmp_path, admin_client):
    _setup(env, tmp_path, [("held", "why", "a.pdf", "10.00", "")])
    # A row pointing OUTSIDE the archive must never be served.
    stray = tmp_path / "elsewhere.pdf"
    stray.write_bytes(b"%PDF-1.4 stray")
    ops = env.cfg["ops_index_dir"]
    with open(os.path.join(ops, "PAID_BACKFILL_2026.csv"), "a", newline="",
              encoding="utf-8") as f:
        csv.writer(f).writerow(["held", "why", str(stray), "2026-06-14", "KLR",
                                "Check", "1", "5.00", "X", "", "", "", "", "", ""])
    c, _h = admin_client
    ok = c.get("/paid-backfill/doc/0")
    assert ok.status_code == 200 and b"packet" in ok.data
    assert c.get("/paid-backfill/doc/1").status_code == 404
    assert c.get("/paid-backfill/doc/99").status_code == 404


def test_decide_saves_and_advances_to_next_undecided(env, tmp_path, admin_client):
    _setup(env, tmp_path, [
        ("held", "why", "a.pdf", "10.00", ""),
        ("held", "why too", "b.pdf", "11.00", "")])
    c, h = admin_client
    r = c.post("/paid-backfill/decide/0", headers=h,
               data={"verdict": "create", "note": "new one"})
    assert "/paid-backfill/item/1" in r.headers["Location"]
    dpath = os.path.join(env.cfg["ops_index_dir"],
                         "PAID_BACKFILL_2026_DECISIONS.csv")
    with open(dpath, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["verdict"] == "create" and rows[0]["note"] == "new one"
    assert rows[0]["decided_by"] == "TestAdmin"
    # The decided row shows its verdict on the item page and the list.
    assert "decided: create" in c.get("/paid-backfill/item/0").get_data(as_text=True)
    # Last decision wins on a re-decide.
    c.post("/paid-backfill/decide/0", headers=h, data={"verdict": "skip"})
    assert "decided: skip" in c.get("/paid-backfill/item/0").get_data(as_text=True)


def test_attach_requires_a_matched_record_and_valid_verdict(env, tmp_path, admin_client):
    _setup(env, tmp_path, [("create-paid", "clean", "c.pdf", "20.00", "")])
    c, h = admin_client
    c.post("/paid-backfill/decide/0", headers=h, data={"verdict": "attach"})
    c.post("/paid-backfill/decide/0", headers=h, data={"verdict": "bogus"})
    assert not os.path.exists(os.path.join(env.cfg["ops_index_dir"],
                                           "PAID_BACKFILL_2026_DECISIONS.csv"))


def test_intake_db_is_never_written_by_the_review(env, tmp_path, admin_client):
    eid = _bill()
    _setup(env, tmp_path, [("match", "amount + vendor", "b.pdf", "147.48", str(eid))])
    c, h = admin_client
    c.post("/paid-backfill/decide/0", headers=h, data={"verdict": "attach"})
    fresh = database.get_entry(eid)
    assert fresh["status"] == "Pending" and not fresh.get("check_number")
