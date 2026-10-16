"""refile-intruders CLI (08. AUGUST incident): re-date + re-file the app-made
intake-shape intruders from a reviewed REFILE_INTRUDERS.csv. Mirrors the
rollback-renames discipline: approval-list csv, per-row runtime revalidation,
crash-safe undo ledger, db-failure move-back. Covers the happy path (move +
re-date + filed_path/pdf_path/attachment re-point + activity + undo round
trip), dry-run inertness, the date-only update, and every skip guard."""
import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
import app as klr  # noqa: E402

INTRUDER = "08-05-26 KLR REPAIR - CARROLL PLUMBING & MAINT, INC - $698.92 - KLR-OF.pdf"
PROPOSED = "06-24-26 CARROLL PLUMBING & MAINT, INC - INV# 107937 - KLR-OF.pdf"


def _use_archive(env):
    archive = os.path.join(str(env.tmp), "Files")
    os.makedirs(archive, exist_ok=True)
    env.cfg["archive_folder"] = archive
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    return archive


def _intruder_record(env, archive, month="08. AUGUST", name=INTRUDER):
    d = os.path.join(archive, "2026", "ACCOUNTS PAYABLE - INCOMING BILLS", month)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"%PDF fake " + name.encode("ascii", "replace"))
    eid = database.insert_entry({
        "date_received": "2026-08-05", "scanned_date": "2026-06-26",
        "doc_type": "Bill", "vendor": "Carroll Plumbing & Maint, Inc",
        "amount": 698.92, "status": "Pending", "entity_code": "KLR",
        "invoice_number": "107937", "property_code": "KLR-OF",
        "filed_path": p, "pdf_path": p})
    return eid, p, d


def _csv(env, rows, name="refile.csv"):
    path = os.path.join(env.tmp, name)
    fields = ["record_id", "current_name", "new_dir", "new_date",
              "proposed_name", "action", "keep_as_is"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


def _june(archive):
    return os.path.join(archive, "2026", "ACCOUNTS PAYABLE - INCOMING BILLS",
                        "06. JUNE")


def test_refile_happy_path_with_undo(env, capsys):
    archive = _use_archive(env)
    eid, p, _d = _intruder_record(env, archive)
    database.add_attachment(eid, p, "Manual Filing", INTRUDER, "T")
    june = _june(archive)          # does not exist yet — created at apply
    path = _csv(env, [{"record_id": eid, "current_name": INTRUDER,
                       "new_dir": june, "new_date": "2026-06-24",
                       "proposed_name": PROPOSED, "action": "REFILE"}])

    # Dry run: nothing moves, nothing re-dates.
    assert klr._cli_reports(["refile-intruders", path]) is True
    out = capsys.readouterr().out
    assert "would move" in out and "re-date" in out
    assert os.path.exists(p)
    assert database.get_entry(eid)["date_received"] == "2026-08-05"

    # Apply.
    klr._cli_reports(["refile-intruders", path, "apply"])
    new_p = os.path.join(june, PROPOSED)
    assert os.path.exists(new_p) and not os.path.exists(p)
    e = database.get_entry(eid)
    assert e["filed_path"] == new_p and e["pdf_path"] == new_p
    assert e["date_received"] == "2026-06-24"
    assert database.get_attachments(eid)[0]["file_path"] == new_p
    with database.get_conn() as conn:
        acts = [r["action"] for r in conn.execute(
            "SELECT action FROM activity_log WHERE entry_id=?", (eid,))]
    assert "Re-filed per folder standards (08. AUGUST incident)" in acts

    # Undo round-trip: the ledger restores name, folder, AND the old date.
    undos = sorted(glob.glob(os.path.join(env.tmp, "REFILE_UNDO_*.csv")))
    assert len(undos) == 1
    rows = list(csv.DictReader(open(undos[0], encoding="utf-8-sig")))
    assert rows[0]["proposed_name"] == INTRUDER
    assert rows[0]["new_date"] == "2026-08-05"
    klr._cli_reports(["refile-intruders", undos[0], "apply"])
    e = database.get_entry(eid)
    assert e["filed_path"] == p and os.path.exists(p) and not os.path.exists(new_p)
    assert e["date_received"] == "2026-08-05"


def test_refile_date_only_update_moves_nothing(env, capsys):
    archive = _use_archive(env)
    eid, p, d = _intruder_record(env, archive)
    path = _csv(env, [{"record_id": eid, "current_name": INTRUDER,
                       "new_dir": d, "new_date": "2026-06-24",
                       "proposed_name": INTRUDER, "action": "REFILE"}])
    klr._cli_reports(["refile-intruders", path, "apply"])
    e = database.get_entry(eid)
    assert e["date_received"] == "2026-06-24"
    assert e["filed_path"] == p and os.path.exists(p)
    assert "updated" in capsys.readouterr().out


def test_refile_skip_guards(env, capsys):
    archive = _use_archive(env)
    eid, p, d = _intruder_record(env, archive)
    june = _june(archive)
    os.makedirs(june, exist_ok=True)

    # Occupied target.
    with open(os.path.join(june, PROPOSED), "wb") as f:
        f.write(b"%PDF other")
    path = _csv(env, [{"record_id": eid, "current_name": INTRUDER,
                       "new_dir": june, "new_date": "2026-06-24",
                       "proposed_name": PROPOSED, "action": "REFILE"}])
    klr._cli_reports(["refile-intruders", path, "apply"])
    assert "occupied" in capsys.readouterr().out
    os.remove(os.path.join(june, PROPOSED))

    # Future / garbage dates, bad destination, drifted record, hand-review.
    bad = [
        {"record_id": eid, "current_name": INTRUDER, "new_dir": june,
         "new_date": "2099-01-01", "proposed_name": PROPOSED, "action": "REFILE"},
        {"record_id": eid, "current_name": INTRUDER, "new_dir": june,
         "new_date": "junk", "proposed_name": PROPOSED, "action": "REFILE"},
        {"record_id": eid, "current_name": INTRUDER,
         "new_dir": os.path.join(str(env.tmp), "outside"),
         "new_date": "2026-06-24", "proposed_name": PROPOSED, "action": "REFILE"},
        {"record_id": eid, "current_name": "not the current name.pdf",
         "new_dir": june, "new_date": "2026-06-24",
         "proposed_name": PROPOSED, "action": "REFILE"},
        {"record_id": eid, "current_name": INTRUDER, "new_dir": june,
         "new_date": "2026-06-24", "proposed_name": "../evil.pdf",
         "action": "REFILE"},
        {"record_id": eid, "current_name": INTRUDER, "new_dir": june,
         "new_date": "2026-06-24", "proposed_name": PROPOSED,
         "action": "hand-review: lookalike"},
        {"record_id": eid, "current_name": INTRUDER, "new_dir": june,
         "new_date": "2026-06-24", "proposed_name": PROPOSED,
         "action": "REFILE", "keep_as_is": "x"},
    ]
    klr._cli_reports(["refile-intruders", _csv(env, bad, "bad.csv"), "apply"])
    out = capsys.readouterr().out
    assert "future" in out and "isn't ISO" in out
    assert "allowed trees" in out and "changed since review" in out
    assert "failed validation" in out
    assert "hand review" in out and "kept as-is" in out
    # Nothing moved or re-dated by any of them.
    e = database.get_entry(eid)
    assert e["filed_path"] == p and e["date_received"] == "2026-08-05"
    assert not glob.glob(os.path.join(env.tmp, "REFILE_UNDO_*.csv"))
