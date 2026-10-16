"""rollback-renames CLI (report #90): undo pay-time renames of staff-placed
archive files from the reviewed ROLLBACK_RENAMES.csv. Covers the happy path
(rename + filed_path/pdf_path/attachment re-point + activity + undo ledger),
every runtime skip guard (drift, missing file, occupied restore name,
keep_as_is, non-ROLLBACK action), dry-run inertness, undo round-trip with
per-generation ledgers, intra-run target claims, and case-only renames
(2026-08-06 adversarial-review hardening)."""
import csv
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
import app as klr  # noqa: E402


def _archive_record(env, name, folder="ACCOUNTS PAYABLE - INCOMING BILLS"):
    d = os.path.join(env.tmp, "Files", "2026", folder, "07. JULY")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"%PDF fake " + name.encode("ascii", "replace"))
    eid = database.insert_entry({
        "date_received": "2026-07-13", "doc_type": "Bill", "vendor": "PG&E",
        "amount": 14.41, "status": "Paid", "entity_code": "TRUST3",
        "filed_path": p, "pdf_path": p})
    return eid, p, d


def _csv(env, rows, name="rollback.csv"):
    path = os.path.join(env.tmp, name)
    fields = ["record_id", "current_name", "restore_name", "action", "keep_as_is"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


def test_rollback_happy_path_with_undo(env, capsys):
    renamed = "07-13-26 TRUST3 CK#3028 - PG&E - $14.41 - TB201-14.pdf"
    original = "07-13-26 PG&E - 06-06 TO 07-06-26 - TB201-14.pdf"
    eid, p, d = _archive_record(env, renamed)
    database.add_attachment(eid, p, "Manual Filing", renamed, "T")
    path = _csv(env, [{"record_id": eid, "current_name": renamed,
                       "restore_name": original, "action": "ROLLBACK"}])

    # Dry run: nothing moves.
    assert klr._cli_reports(["rollback-renames", path]) is True
    assert os.path.exists(p) and "would rename" in capsys.readouterr().out

    # Apply.
    klr._cli_reports(["rollback-renames", path, "apply"])
    new_p = os.path.join(d, original)
    assert os.path.exists(new_p) and not os.path.exists(p)
    e = database.get_entry(eid)
    assert e["filed_path"] == new_p and e["pdf_path"] == new_p
    att = database.get_attachments(eid)[0]
    assert att["file_path"] == new_p
    with database.get_conn() as conn:
        acts = [r["action"] for r in conn.execute(
            "SELECT action FROM activity_log WHERE entry_id=?", (eid,))]
    assert "Rename rolled back (report #90)" in acts

    undos = sorted(glob.glob(os.path.join(env.tmp, "ROLLBACK_UNDO_*.csv")))
    assert len(undos) == 1
    rows = list(csv.DictReader(open(undos[0], encoding="utf-8-sig")))
    assert rows[0]["current_name"] == original and rows[0]["restore_name"] == renamed

    # Undo round-trip: feeding the ledger back restores the renamed name — and
    # writes its OWN generation ledger instead of appending to the one it read.
    klr._cli_reports(["rollback-renames", undos[0], "apply"])
    assert os.path.exists(p) and not os.path.exists(new_p)
    assert database.get_entry(eid)["filed_path"] == p
    undos2 = sorted(glob.glob(os.path.join(env.tmp, "ROLLBACK_UNDO_*.csv")))
    assert len(undos2) == 2, "reversal must not append to the ledger it replayed"
    assert len(list(csv.DictReader(open(undos[0], encoding="utf-8-sig")))) == 1


def test_runtime_guards_skip_unsafe_rows(env, capsys):
    renamed = "07-10-26 TRUST2 CK#14592 - MARBORG - $1,285.81 - TL222.pdf"
    original = "07-10-26 MARBORG - JUNE - TL222.pdf"
    eid, p, d = _archive_record(env, renamed)

    # (a) occupied restore name
    with open(os.path.join(d, original), "wb") as f:
        f.write(b"%PDF occupant")
    path = _csv(env, [{"record_id": eid, "current_name": renamed,
                       "restore_name": original, "action": "ROLLBACK"}])
    klr._cli_reports(["rollback-renames", path, "apply"])
    out = capsys.readouterr().out
    assert "occupied" in out and os.path.exists(p)
    os.remove(os.path.join(d, original))

    # (b) drift: record's name changed since the csv was reviewed
    path = _csv(env, [{"record_id": eid, "current_name": "SOMETHING ELSE.pdf",
                       "restore_name": original, "action": "ROLLBACK"}])
    klr._cli_reports(["rollback-renames", path, "apply"])
    assert "changed since review" in capsys.readouterr().out
    assert os.path.exists(p)

    # (c) keep_as_is and non-ROLLBACK rows are untouched
    path = _csv(env, [
        {"record_id": eid, "current_name": renamed, "restore_name": original,
         "action": "ROLLBACK", "keep_as_is": "x"},
        {"record_id": eid, "current_name": renamed, "restore_name": original,
         "action": "skip"},
    ])
    klr._cli_reports(["rollback-renames", path, "apply"])
    assert "Rolled back 0" in capsys.readouterr().out
    assert os.path.exists(p)

    # (d) missing file
    os.remove(p)
    path = _csv(env, [{"record_id": eid, "current_name": renamed,
                       "restore_name": original, "action": "ROLLBACK"}])
    klr._cli_reports(["rollback-renames", path, "apply"])
    assert "not found on disk" in capsys.readouterr().out


def test_action_tolerance_and_visible_ignores(env, capsys):
    """Lowercase 'rollback' still processes; skip/hand-review rows are counted
    and printed, never silently dropped (2026-08-06 review)."""
    renamed = "06-04-26 TRUST1 CK#85542 - CARROLL PLUMBING - T27-29.pdf"
    original = "06-04-26 CARROLL PLUMBING - #107839-  T27-29.pdf"
    eid, p, d = _archive_record(env, renamed)
    path = _csv(env, [
        {"record_id": eid, "current_name": renamed, "restore_name": original,
         "action": "rollback"},
        {"record_id": "77", "current_name": "x.pdf", "restore_name": "y.pdf",
         "action": "hand-review: current file missing"},
    ])
    klr._cli_reports(["rollback-renames", path, "apply"])
    out = capsys.readouterr().out
    assert os.path.exists(os.path.join(d, original))
    assert "left for hand review" in out and "left alone 1" in out


def test_intra_run_target_claim_and_case_only(env, capsys):
    """Two rows restoring to the same name: the second skips even in dry-run
    (claimed-set). A case-only restore is allowed despite Windows' case-blind
    exists()."""
    a_ren = "07-08-26 TRUST1 CK#85569 - MONTECITO WATER DISTRICT - $74.02 - T131.pdf"
    b_ren = "07-08-26 TRUST1 CK#85570 - MONTECITO WATER DISTRICT - $74.02 - T131 (2).pdf"
    shared = "07-08-26 MONTECITO WATER DISTRICT - JUNE - T131.pdf"
    a_id, a_p, d = _archive_record(env, a_ren)
    b_id, b_p, _ = _archive_record(env, b_ren)
    path = _csv(env, [
        {"record_id": a_id, "current_name": a_ren, "restore_name": shared, "action": "ROLLBACK"},
        {"record_id": b_id, "current_name": b_ren, "restore_name": shared, "action": "ROLLBACK"},
    ])
    klr._cli_reports(["rollback-renames", path])          # dry-run sees it too
    out = capsys.readouterr().out
    assert "already claimed by an earlier row" in out and "Would roll back 1" in out

    c_ren = "06-24-26 TRUST1 CK#85555 - SILVESTRE GAMA - $285.00 - T35-5.pdf"
    c_low = "06-24-26 Trust1 CK#85555 - Silvestre Gama - $285.00 - T35-5.pdf"
    c_id, c_p, d2 = _archive_record(env, c_ren)
    path = _csv(env, [{"record_id": c_id, "current_name": c_ren,
                       "restore_name": c_low, "action": "ROLLBACK"}])
    klr._cli_reports(["rollback-renames", path, "apply"])
    assert "occupied" not in capsys.readouterr().out
    assert os.path.basename(database.get_entry(c_id)["filed_path"]) == c_low


def test_bad_csv_and_missing_record(env, capsys):
    klr._cli_reports(["rollback-renames", os.path.join(env.tmp, "nope.csv")])
    assert "No such csv" in capsys.readouterr().out
    path = _csv(env, [{"record_id": "99999", "current_name": "a.pdf",
                       "restore_name": "b.pdf", "action": "ROLLBACK"}])
    klr._cli_reports(["rollback-renames", path, "apply"])
    assert "record missing" in capsys.readouterr().out
