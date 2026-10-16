"""Pass-to-teammate: hand a record to a named staff member with a note (the
office's sticky-note habit, in-app). Recipient sees a nav badge + a dashboard
"Passed to you" list; Done clears it; everything lands in the activity log."""
import app as klr
import database

from conftest import _login_session


def _bill(**over):
    base = {"date_received": "2026-06-11", "doc_type": "Bill",
            "vendor": "GOLETA WATER", "entity_code": "TRUST1",
            "property_code": "TL222", "status": "Pending", "amount": 1507.60}
    base.update(over)
    return database.insert_entry(base)


def test_pass_and_done_roundtrip(env, staff_client):
    c, headers = staff_client                       # TestUser
    eid = _bill()
    r = c.post(f"/entry/{eid}/pass", headers=headers,
               data={"to_user": "TestAdmin", "note": "mark this paid per receipt"})
    assert r.status_code == 302
    passes = database.get_passes_for_user("TestAdmin")
    assert len(passes) == 1
    p = passes[0]
    assert p["entry_id"] == eid and p["from_user"] == "TestUser"
    assert p["note"] == "mark this paid per receipt"
    assert database.open_pass_count("TestAdmin") == 1
    # activity trail on the record
    acts = [a for a in database.get_activity_feed(200) if a.get("entry_id") == eid]
    assert any("Passed to TestAdmin" in a["action"] for a in acts)
    # done clears it (idempotently)
    assert database.mark_pass_done(p["id"], "TestAdmin") is True
    assert database.mark_pass_done(p["id"], "TestAdmin") is False
    assert database.open_pass_count("TestAdmin") == 0


def test_pass_rejects_unknown_recipient(env, staff_client):
    c, headers = staff_client
    eid = _bill()
    c.post(f"/entry/{eid}/pass", headers=headers,
           data={"to_user": "Nobody Real", "note": "x"})
    assert database.get_passes_for_entry(eid) == []


def test_recipient_sees_badge_dashboard_and_record_note(env, staff_client):
    c, headers = staff_client
    eid = _bill()
    c.post(f"/entry/{eid}/pass", headers=headers,
           data={"to_user": "TestAdmin", "note": "please handle the Goleta pair"})
    # recipient logs in: nav badge + dashboard box + sticky note on the record
    a = klr.app.test_client()
    _login_session(a, "TestAdmin")
    dash = a.get("/").get_data(as_text=True)
    assert "Passed to you" in dash and "GOLETA WATER" in dash
    assert "please handle the Goleta pair" in dash
    assert "For you" in dash                        # nav badge
    rec = a.get(f"/entry/{eid}/view").get_data(as_text=True)
    assert "passed this to" in rec and "TestAdmin" in rec
    # sender's own dashboard does NOT list it
    assert "Passed to you" not in c.get("/").get_data(as_text=True)


def test_done_from_dashboard_clears_everywhere(env, staff_client):
    c, headers = staff_client
    eid = _bill()
    c.post(f"/entry/{eid}/pass", headers=headers, data={"to_user": "TestAdmin"})
    pid = database.get_passes_for_user("TestAdmin")[0]["id"]
    a = klr.app.test_client()
    aheaders = _login_session(a, "TestAdmin")
    assert a.post(f"/pass/{pid}/done", headers=aheaders).status_code == 302
    assert database.open_pass_count("TestAdmin") == 0
    assert "Passed to you" not in a.get("/").get_data(as_text=True)
    # record keeps the history row as Done (no open sticky note shown)
    assert database.get_passes_for_entry(eid)[0]["status"] == "Done"
    assert "passed this to" not in a.get(f"/entry/{eid}/view").get_data(as_text=True)


def test_pass_form_offers_everyone_but_self(env, staff_client):
    c, headers = staff_client                       # TestUser
    eid = _bill()
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    assert '<option value="TestAdmin">' in html
    assert '<option value="TestUser">' not in html
