"""Regression for the 2026-07-25 code review's #17 finding: Settings/staff
mutations (entities, properties, bill types, mail accounts, staff) made no
audit trail at all -- an admin could rename/delete/repoint config with no
record of who or when. Fix: each mutation route now calls log_activity(None,
...) with the actor and a plain-language detail string. entry_id is None
since these aren't tied to one intake record (the established pattern, e.g.
"Bulk AI fill"). Passwords themselves must never be logged -- only that a
reset/toggle happened and for whom.
"""
import app as klr
import database


def _feed(limit=30):
    return database.get_activity_feed(limit)


def _actions(feed):
    return [row["action"] for row in feed]


def test_entity_add_update_delete_logged(env, admin_client):
    c, headers = admin_client
    c.post("/settings/entities/add", headers=headers, data={"code": "ZZ", "name": "Zed Zed"})
    c.post("/settings/entities/ZZ/update", headers=headers, data={"code": "ZZ2", "name": "Zed Zed Renamed"})
    c.post("/settings/entities/ZZ2/delete", headers=headers)

    feed = _feed()
    assert _actions(feed)[:3][::-1] == ["Entity added", "Entity renamed", "Entity deleted"]
    added = next(r for r in feed if r["action"] == "Entity added")
    assert added["user"] == "TestAdmin"
    assert "ZZ" in added["detail"] and "Zed Zed" in added["detail"]

    deleted = next(r for r in feed if r["action"] == "Entity deleted")
    assert deleted["user"] == "TestAdmin"
    assert "ZZ2" in deleted["detail"]


def test_property_add_update_delete_logged(env, admin_client):
    c, headers = admin_client
    c.post("/settings/properties/add", headers=headers, data={"code": "P900", "address": "900 Test St"})
    c.post("/settings/properties/P900/update", headers=headers, data={"address": "901 Test St"})
    c.post("/settings/properties/P900/delete", headers=headers)

    feed = _feed()
    added = next(r for r in feed if r["action"] == "Property added")
    assert added["user"] == "TestAdmin" and "P900" in added["detail"]
    updated = next(r for r in feed if r["action"] == "Property updated")
    assert "P900" in updated["detail"]
    deleted = next(r for r in feed if r["action"] == "Property deleted")
    assert "P900" in deleted["detail"]


def test_bill_type_add_rename_delete_logged(env, admin_client):
    c, headers = admin_client
    c.post("/settings/bill_types/add", headers=headers,
           data={"code": "ZBT", "description": "Zed Bill Type"})
    c.post("/settings/bill_types/ZBT/update", headers=headers,
           data={"code": "ZBT2", "description": "Zed Bill Type", "category": "Bill"})
    c.post("/settings/bill_types/ZBT2/delete", headers=headers)

    feed = _feed()
    added = next(r for r in feed if r["action"] == "Bill type added")
    assert "ZBT" in added["detail"]
    renamed = next(r for r in feed if r["action"] == "Bill type renamed")
    assert "ZBT" in renamed["detail"] and "ZBT2" in renamed["detail"]
    deleted = next(r for r in feed if r["action"] == "Bill type deleted")
    assert "ZBT2" in deleted["detail"]


def test_mail_account_add_update_delete_logged(env, admin_client):
    c, headers = admin_client
    c.post("/settings/mail/add", headers=headers,
           data={"host": "imap.example.com", "username": "zed@example.com", "password": "secret1"})
    acct_id = klr.load_config()["mail_accounts"][-1]["id"]
    c.post(f"/settings/mail/{acct_id}/update", headers=headers,
           data={"host": "imap.example.com", "username": "zed@example.com", "name": "Zed Mailbox"})
    c.post(f"/settings/mail/{acct_id}/delete", headers=headers)

    feed = _feed()
    added = next(r for r in feed if r["action"] == "Mailbox added")
    assert "zed@example.com" in added["detail"]
    updated = next(r for r in feed if r["action"] == "Mailbox updated")
    assert "Zed Mailbox" in updated["detail"]
    deleted = next(r for r in feed if r["action"] == "Mailbox deleted")
    assert deleted["user"] == "TestAdmin"
    # never log the password
    assert not any("secret1" in (r["detail"] or "") for r in feed)


def test_staff_add_password_reset_toggle_admin_remove_logged(env, admin_client):
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers, data={"name": "ZedStaffer", "password": "abcd1234"})
    c.post("/settings/staff/password", headers=headers, data={"name": "ZedStaffer", "password": "newpass99"})
    c.post("/settings/staff/toggle_admin", headers=headers, data={"name": "ZedStaffer"})
    c.post("/settings/staff/remove", headers=headers, data={"name": "ZedStaffer"})

    feed = _feed()
    added = next(r for r in feed if r["action"] == "Staff added")
    assert "ZedStaffer" in added["detail"]
    reset = next(r for r in feed if r["action"] == "Staff password reset")
    assert reset["detail"] == "ZedStaffer"
    toggled = next(r for r in feed if r["action"] == "Staff admin toggled")
    assert "ZedStaffer" in toggled["detail"]

    # no password material anywhere in the feed
    assert not any("abcd1234" in (r["detail"] or "") for r in feed)
    assert not any("newpass99" in (r["detail"] or "") for r in feed)


def test_non_admin_mutation_attempt_not_logged(env, staff_client):
    """A non-admin can't even reach these routes (admin_required), so no
    activity should be recorded for a rejected attempt."""
    c, headers = staff_client
    c.post("/settings/entities/add", headers=headers, data={"code": "NOPE", "name": "Should Not Land"})
    feed = _feed()
    assert not any("NOPE" in (r["detail"] or "") for r in feed)
