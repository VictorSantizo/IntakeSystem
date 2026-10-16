"""Queue mechanics, file-safety guarantees, repair, backups, model sync."""
import os
import json
import time

import pytest

import app as klr
import database
import property_model
import watcher
from conftest import make_pdf


# ── queue claim/release ────────────────────────────────────────────────────────

def test_claim_is_exclusive(env):
    klr.pending_scans[7] = {"pdf_path": "x"}
    assert klr._claim_scan(7) is not None
    assert klr._claim_scan(7) is None            # second claim loses
    klr._release_scan(7)
    assert klr._claim_scan(7) is not None        # released -> claimable again


def test_queue_add_assigns_unique_ids(env):
    a = klr._queue_add({"pdf_path": "a"})
    b = klr._queue_add({"pdf_path": "b"})
    assert a != b and set(dict(klr._queue_items())) == {a, b}


def test_restore_strips_processing_flag(env):
    database.save_pending_scan(3, json.dumps({"pdf_path": "x", "_processing": True}))
    klr.pending_scans.clear()
    klr._load_pending_from_db()
    assert klr.pending_scans[3].get("_processing") is None


# ── never-delete guarantees ────────────────────────────────────────────────────

def test_move_aside_is_collision_safe(env, tmp_path):
    p1 = tmp_path / "doc.pdf"; p1.write_text("a")
    d1 = klr._move_aside(str(p1), "_Blank")
    p2 = tmp_path / "doc.pdf"; p2.write_text("b")
    d2 = klr._move_aside(str(p2), "_Blank")
    assert os.path.exists(d1) and os.path.exists(d2) and d1 != d2


def test_skip_moves_file_out_of_watched_folder(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "skipme.pdf"; f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f)})
    r = c.post(f"/review/{sid}/skip", headers=headers)
    assert r.status_code == 302
    assert not f.exists()
    assert (tmp_path / "_Skipped" / "skipme.pdf").exists()
    assert sid not in klr.pending_scans


def test_blank_filter_never_flags_sentinels():
    def is_blank(extracted):   # mirrors the enqueue_scan condition
        has_text_layer = bool(extracted) and not extracted.startswith("[")
        return has_text_layer and len([w for w in extracted.split() if len(w) > 1]) < 15
    assert not is_blank("[scanned PDF – read via vision]")
    assert not is_blank("[image document]")
    assert not is_blank("")
    assert is_blank("page 2 of 2")


# ── filed-path self-heal ───────────────────────────────────────────────────────

def test_repair_filed_paths_relinks_renamed_docs(env):
    filed_dir = os.path.join(env.filed_folder, "TRUST1", "T27", "2026")
    os.makedirs(filed_dir)
    eid = database.insert_entry({
        "date_received": "2026-06-27", "entity_code": "TRUST1",
        "bill_type_code": "ELEC", "vendor": "Frontier", "amount": 147.48,
        "property_code": "T27", "doc_type": "Bill", "status": "Pending",
        "filed_path": os.path.join(filed_dir, "old-name-that-is-gone.pdf"),
    })
    # the doc on disk carries the CONVENTION name, not the recorded one
    make_pdf(os.path.join(filed_dir, "06-27-26 TRUST1 ELEC - Frontier - $147.48 - T27.pdf"))
    klr._repair_filed_paths()
    entry = database.get_entry(eid)
    assert os.path.exists(entry["filed_path"])
    assert entry["filed_path"].endswith("Frontier - $147.48 - T27.pdf")


def test_repair_matches_by_id_suffix(env):
    filed_dir = os.path.join(env.filed_folder, "PP", "PP16", "2026")
    os.makedirs(filed_dir)
    eid = database.insert_entry({
        "date_received": "2026-06-27", "entity_code": "PP", "vendor": "Karen Lacks & Co.",
        "doc_type": "Bill", "status": "Pending",
        "filed_path": os.path.join(filed_dir, "gone.pdf"),
    })
    make_pdf(os.path.join(filed_dir, f"whatever renamed to ({eid}).pdf"))
    klr._repair_filed_paths()
    assert database.get_entry(eid)["filed_path"].endswith(f"({eid}).pdf")


# ── backups ────────────────────────────────────────────────────────────────────

def test_daily_backup_writes_prunes_and_skips(env, tmp_path):
    database.insert_entry({"date_received": "2026-07-06", "doc_type": "Bill",
                           "status": "Pending"})
    bdir = tmp_path / "auto"; bdir.mkdir()
    (bdir / "intake_20200101.db").write_text("old")
    (bdir / "unrelated.db").write_text("keep")
    dest = klr._write_daily_backup(str(bdir))
    assert dest and os.path.exists(dest)
    assert not (bdir / "intake_20200101.db").exists()      # pruned (past retention)
    assert (bdir / "unrelated.db").exists()                # foreign file untouched
    assert klr._write_daily_backup(str(bdir)) is None      # second run same day: no-op


def test_daily_backup_config_copy_carries_no_secrets(env, tmp_path):
    """2026-07-27 review GH #12 (Joshua's call, 07-29): the daily config backup
    exists so entities/properties/mailbox settings survive a wipe -- the API
    key, mailbox passwords, and staff password hashes must NOT ride along into
    a second location on disk. Secrets are redacted; structure is kept."""
    import json
    cfg = klr.load_config()
    cfg["claude_api_key"] = "sk-ant-VERYSECRET123"
    cfg["connector_token"] = "tok-VERYSECRET456"
    cfg["night_shift_token"] = "night-VERYSECRET321"
    cfg["mail_accounts"] = [{"id": 1, "name": "Karen", "protocol": "imap",
                             "host": "imap.example.com", "port": 993, "ssl": True,
                             "username": "k@example.com",
                             "password": "mail-VERYSECRET789", "enabled": True}]
    cfg["staff"] = [{"name": "Karen", "password": "pbkdf2:sha256:VERYSECREThash",
                     "admin": True}]
    klr.save_config(cfg)

    bdir = tmp_path / "auto"; bdir.mkdir()
    dest = klr._write_daily_backup(str(bdir))
    assert dest
    copies = list(bdir.glob("config_*.json"))
    assert len(copies) == 1
    text = copies[0].read_text(encoding="utf-8")
    assert "VERYSECRET" not in text
    backed = json.loads(text)
    # Non-secret structure survives for a real restore.
    assert backed["mail_accounts"][0]["host"] == "imap.example.com"
    assert backed["staff"][0]["name"] == "Karen"
    assert backed["entities"] == cfg["entities"]
    # And the LIVE config is untouched -- only the backup copy is redacted.
    assert klr.load_config()["claude_api_key"] == "sk-ant-VERYSECRET123"


# ── property model sync ────────────────────────────────────────────────────────

def test_entity_property_sync_roundtrip(env):
    property_model.sync_entity_upsert("TRUST9", "Nine")
    property_model.sync_property_upsert("T99", "9 Test St", "TRUST9")
    assert property_model.entity_unit_weights(["T99"]) == {"TRUST9": 1}
    property_model.sync_entity_rename("TRUST9", "TRUST9X", "Nine Renamed")
    assert property_model.entity_unit_weights(["T99"]) == {"TRUST9X": 1}
    property_model.update_property_units("T99", 42)
    property_model.sync_property_upsert("T99", "9 Test St v2", "TRUST9X")
    assert property_model.entity_unit_weights(["T99"]) == {"TRUST9X": 42}  # curated count kept


# ── watcher ────────────────────────────────────────────────────────────────────

def test_wait_until_stable(tmp_path):
    f = tmp_path / "scan.pdf"
    f.write_text("data")
    assert watcher._wait_until_stable(str(f), timeout=5) is True
    assert watcher._wait_until_stable(str(tmp_path / "missing.pdf"), timeout=2) is False


# ── dedup hashing ──────────────────────────────────────────────────────────────

def test_sha256_and_hash_lookup(env, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"same bytes")
    h = klr._sha256_file(str(f))
    assert h == klr._sha256_file(str(f))
    eid = database.insert_entry({"date_received": "2026-07-06", "doc_type": "Bill",
                                 "status": "Pending", "file_hash": h})
    owners = database.get_entries_by_hash(h)
    assert [o["id"] for o in owners] == [eid]
    assert database.get_entries_by_hash("") == []
