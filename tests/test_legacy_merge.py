"""Legacy duplicate merge: byte-identical records logged twice (pre-dedup era)
fold into one keeper — payment carried over, blanks filled, copies re-pointed,
conflicting payments refused."""
import os

import app as klr
import database

from test_dedup_triage import _log_custom_bill

H1 = "a" * 64
H2 = "b" * 64


def _twin_bills(env, staff_client, pay_first=False, pay_second=False,
                sig=("2026-06-26", 33.91, "", "ACH")):
    """Two logged records faked into a byte-identical pair (same file_hash)."""
    e1 = _log_custom_bill(env, staff_client, "sce1.pdf",
                          "Southern California Edison", 33.91, "INV-A")
    e2 = _log_custom_bill(env, staff_client, "sce2.pdf",
                          "Southern California Edison (SCE)", 33.91, "INV-B")
    for e, pay in ((e1, pay_first), (e2, pay_second)):
        upd = {"file_hash": H1}
        if pay:
            upd.update(status="Paid", paid_date=sig[0], paid_amount=sig[1],
                       check_number=sig[2], payment_method=sig[3])
        database.update_entry(e["id"], upd)
    return database.get_entry(e1["id"]), database.get_entry(e2["id"])


def test_group_detection_and_keeper_choice(env, staff_client):
    e1, e2 = _twin_bills(env, staff_client, pay_second=True)
    groups = database.get_hash_dupe_groups()
    assert len(groups) == 1 and {r["id"] for r in groups[0]} == {e1["id"], e2["id"]}
    # the paid record wins even though it's newer
    assert klr._pick_keeper(groups[0])["id"] == e2["id"]
    # unpaid pair: the oldest wins
    database.update_entry(e2["id"], {"status": "Pending", "paid_date": None,
                                     "paid_amount": None, "payment_method": None})
    groups = database.get_hash_dupe_groups()
    assert klr._pick_keeper(groups[0])["id"] == e1["id"]


def test_conflict_rules(env, staff_client):
    # identical payment recorded on both copies: NOT a conflict (one payment)
    e1, e2 = _twin_bills(env, staff_client, pay_first=True, pay_second=True)
    assert not klr._merge_conflict([e1, e2])
    # different check numbers: conflict — possible double payment
    database.update_entry(e2["id"], {"check_number": "9999"})
    assert klr._merge_conflict([database.get_entry(e1["id"]),
                                database.get_entry(e2["id"])])


def test_merge_transfers_payment_and_fills_blanks(env, staff_client):
    e1, e2 = _twin_bills(env, staff_client, pay_second=True)
    # keeper (paid e2) has a blank account #; the absorbed copy knows it
    database.update_entry(e1["id"], {"account_number": "12-3456"})
    database.update_entry(e2["id"], {"account_number": ""})
    database.add_attachment(e1["id"], "X:\\somewhere\\old-copy.pdf",
                            "Email", "old-copy.pdf", "t")
    group = database.get_entries_by_hash(H1)
    kid = klr._merge_dupe_group(group, by="TestAdmin")
    assert kid == e2["id"]
    assert database.get_entry(e1["id"]) is None            # absorbed row gone
    keeper = database.get_entry(kid)
    assert keeper["status"] == "Paid"
    assert keeper["account_number"] == "12-3456"           # blank filled
    atts = database.get_attachments(kid)                   # re-pointed + doc kept
    assert any(a["original_name"] == "old-copy.pdf" for a in atts)
    assert any(a["source"] == "Merge" for a in atts)       # e1's filed doc listed
    assert not database.get_hash_dupe_groups()


def test_merge_moves_payment_fields_to_keeper(env, staff_client):
    """A copy can carry payment FIELDS without Paid status (reconciler edge
    states); the surviving keeper absorbs them instead of losing them."""
    e1, e2 = _twin_bills(env, staff_client)
    database.update_entry(e2["id"], {"paid_date": "2026-06-26",
                                     "paid_amount": 33.91,
                                     "payment_method": "ACH"})
    group = database.get_entries_by_hash(H1)
    kid = klr._merge_dupe_group(group)
    assert kid == e1["id"]                       # older record keeps
    k = database.get_entry(kid)
    assert k["paid_date"] == "2026-06-26" and k["paid_amount"] == 33.91
    assert k["payment_method"] == "ACH"


def test_routes_gated_and_merge_all(env, admin_client, staff_client):
    ac, ah = admin_client
    e1, e2 = _twin_bills(env, staff_client, pay_first=True, pay_second=True)
    # a second, conflicted group
    e3 = _log_custom_bill(env, staff_client, "w1.pdf", "Water Co", 50.0, "W-1")
    e4 = _log_custom_bill(env, staff_client, "w2.pdf", "Water Company", 50.0, "W-2")
    database.update_entry(e3["id"], {"file_hash": H2, "status": "Paid",
                                     "paid_date": "2026-06-01", "paid_amount": 50.0,
                                     "check_number": "111"})
    database.update_entry(e4["id"], {"file_hash": H2, "status": "Paid",
                                     "paid_date": "2026-06-02", "paid_amount": 50.0,
                                     "check_number": "222"})
    # non-admin is blocked from the page and the actions
    sc, sh = staff_client
    assert sc.get("/review/legacy_dupes").status_code == 302
    assert sc.post("/review/legacy_dupes/merge_all", headers=sh).status_code == 302
    assert database.get_entry(e1["id"]) is not None        # nothing merged

    r = ac.get("/review/legacy_dupes")
    assert r.status_code == 200 and b"check payments" in r.data

    r = ac.post("/review/legacy_dupes/merge_all", headers=ah)
    assert r.status_code == 302
    assert len(database.get_hash_dupe_groups()) == 1       # conflicted group stays
    assert database.get_hash_dupe_groups()[0][0]["file_hash"] == H2
    # stale hash is a friendly no-op
    r = ac.post("/review/legacy_dupes/merge", headers=ah, data={"hash": H1})
    assert r.status_code == 302
