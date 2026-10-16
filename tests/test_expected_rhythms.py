"""Mined payment rhythms wired into the Expected Bills radar.

The /expected page grows a second suggestion source: vendor rhythms measured
by payment_rules over the PAID_BACKFILL datasets, with per-row and adopt-all
flows. The table's contract holds — seeded by a person's click, never
auto-populated — and adopted archive spellings must still match the AI-read
vendor names (the upgraded _vendor_match).
"""
import csv
import datetime
import json
import os

import app as klr
import database
import paid_backfill as pb


def _reset_cache():
    klr._rhythm_sugg_cache.update({"t": 0.0, "rows": None})


def _seed_dataset(env, tmp_path, payee="SO.CALIF. EDISON", months=(1, 2, 3, 4, 5, 6),
                  entity="TRUST2", amount="147.48"):
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    env.cfg["ops_index_dir"] = str(ops)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    rows = []
    for m in months:
        rows.append({"disposition": "create-paid", "reason": "clean",
                     "unc_path": f"\\\\X\\{payee}-{m}.pdf",
                     "name_date": f"2026-{m:02d}-10", "entity": entity,
                     "method": "Check", "check": str(9000 + m), "amount": amount,
                     "payee": payee, "props": "", "matched_id": "",
                     "matched_status": "", "matched_vendor": "",
                     "matched_amount": "", "matched_check": ""})
    with open(ops / "PAID_BACKFILL_2026.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=pb._CSV_COLS)
        w.writeheader()
        w.writerows(rows)
    _reset_cache()


# ── the upgraded vendor matcher ───────────────────────────────────────────────

def test_vendor_match_handles_archive_spellings():
    assert klr._vendor_match("SO.CALIF. EDISON",
                             "Southern California Edison Company (SCE)")
    assert klr._vendor_match("RONALD L. WOLFE", "Ronald L. Wolfe & Associates")
    # One generic shared token is NOT a match (two different electricians).
    assert not klr._vendor_match("LADOUCE ELECTRIC", "ACME ELECTRIC SUPPLY")
    # The original behaviors survive.
    assert klr._vendor_match("socal gas", "SoCal Gas - Southern California Gas Company")
    assert not klr._vendor_match("SoCal Gas", "Frontier")
    assert not klr._vendor_match("", "Frontier")


# ── suggestions on the page ───────────────────────────────────────────────────

def test_rhythm_suggestions_render_with_cadence_and_next_due(env, tmp_path, admin_client):
    _seed_dataset(env, tmp_path)
    c, _h = admin_client
    body = c.get("/expected").get_data(as_text=True)
    assert "Mined payment rhythms" in body
    assert "SO.CALIF. EDISON" in body
    assert "monthly" in body and "$147.48" in body
    assert "Adopt all 1" in body


def test_suggestion_canonicalizes_to_the_intake_spelling(env, tmp_path, admin_client):
    database.insert_entry({"vendor": "Southern California Edison",
                           "doc_type": "Bill", "status": "Paid",
                           "date_received": "2026-06-01"})
    _seed_dataset(env, tmp_path)
    c, _h = admin_client
    body = c.get("/expected").get_data(as_text=True)
    assert "Southern California Edison" in body     # the log's spelling wins


def test_already_tracked_vendor_is_not_resuggested(env, tmp_path, admin_client):
    database.add_expected_bill("Southern California Edison", cadence="monthly")
    _seed_dataset(env, tmp_path)                    # same vendor, archive spelling
    c, _h = admin_client
    body = c.get("/expected").get_data(as_text=True)
    assert "Mined payment rhythms" not in body


# ── adopting ──────────────────────────────────────────────────────────────────

def test_adopt_all_creates_radar_rows_once(env, tmp_path, admin_client):
    _seed_dataset(env, tmp_path)
    c, h = admin_client
    r = c.post("/expected/adopt_rhythms", headers=h)
    assert "Adopted+1" in r.headers["Location"]
    rows = database.get_expected_bills()
    assert len(rows) == 1
    row = rows[0]
    assert row["vendor"] == "SO.CALIF. EDISON"
    assert row["cadence"] == "monthly"
    assert row["typical_amount"] == 147.48
    assert row["entity_code"] == "TRUST2"
    assert row["source"] == "suggested"
    assert "payment-rules miner" in row["notes"]
    # Idempotent: a second click adopts nothing new.
    _reset_cache()
    r = c.post("/expected/adopt_rhythms", headers=h)
    assert "Adopted+0" in r.headers["Location"]
    assert len(database.get_expected_bills()) == 1


def test_adopt_needs_admin(env, tmp_path, staff_client):
    _seed_dataset(env, tmp_path)
    c, h = staff_client
    c.post("/expected/adopt_rhythms", headers=h)
    assert database.get_expected_bills() == []


def test_adopted_archive_spelling_still_catches_the_real_bill(env, tmp_path, admin_client):
    """The whole point: an adopted 'SO.CALIF. EDISON' radar row must read a
    logged 'Southern California Edison Company' bill as ARRIVED."""
    _seed_dataset(env, tmp_path)
    c, h = admin_client
    c.post("/expected/adopt_rhythms", headers=h)
    database.insert_entry({"vendor": "Southern California Edison Company",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": datetime.date.today().isoformat()})
    statuses = klr._expected_statuses()
    assert statuses[0]["state"] == "arrived"


def test_no_datasets_means_no_rhythm_card(env, tmp_path, admin_client):
    env.cfg["ops_index_dir"] = str(tmp_path / "empty-ops")
    os.makedirs(env.cfg["ops_index_dir"], exist_ok=True)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    _reset_cache()
    c, _h = admin_client
    body = c.get("/expected").get_data(as_text=True)
    assert "Mined payment rhythms" not in body
