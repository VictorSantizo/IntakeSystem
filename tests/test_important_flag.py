"""Manual "flag as important" (follow-on to the 2026-07-07 dashboard landing):
before this, "important" only meant doc_type Legal/Correspondence — there was
no way to flag an arbitrary bill or record as needing eyes. Now any record can
be flagged/unflagged; flagged records join the dashboard's needs-attention
list regardless of doc_type, until filed or unflagged.
"""
import database
import app as klr


def _bill(**overrides):
    e = {"date_received": "2026-07-07", "doc_type": "Bill", "vendor": "Frontier",
         "amount": 99.0, "status": "Pending", "processed_by": "TestUser"}
    e.update(overrides)
    return database.insert_entry(e)


# ── Query scope ────────────────────────────────────────────────────────────────

def test_flagged_bill_joins_important_list(env):
    bill_id = _bill()
    assert bill_id not in [r["id"] for r in database.get_important_correspondence()]
    database.update_entry(bill_id, {"important": 1})
    assert bill_id in [r["id"] for r in database.get_important_correspondence()]


def test_unflagging_removes_from_important_list(env):
    bill_id = _bill(important=1)
    assert bill_id in [r["id"] for r in database.get_important_correspondence()]
    database.update_entry(bill_id, {"important": 0})
    assert bill_id not in [r["id"] for r in database.get_important_correspondence()]


def test_filed_flagged_record_no_longer_needs_attention(env):
    bill_id = _bill(important=1, status="Filed")
    assert bill_id not in [r["id"] for r in database.get_important_correspondence()]


def test_doc_type_importance_unaffected_by_flag_column(env):
    legal_id = database.insert_entry({"date_received": "2026-07-07", "doc_type": "Legal",
                                      "vendor": "Smith & Associates", "status": "Pending"})
    assert legal_id in [r["id"] for r in database.get_important_correspondence()]


def test_log_filter_by_important(env):
    _bill(vendor="Plain Bill")
    _bill(vendor="Flagged Bill", important=1)
    flagged = database.get_all_entries({"important": "1"})
    assert [e["vendor"] for e in flagged] == ["Flagged Bill"]


# ── Toggle route ───────────────────────────────────────────────────────────────

def test_toggle_route_flags_then_unflags(env, staff_client):
    c, headers = staff_client
    bill_id = _bill()
    r = c.post(f"/entry/{bill_id}/important", headers=headers, follow_redirects=False)
    assert r.status_code == 302 and f"/entry/{bill_id}/view" in r.headers["Location"]
    assert database.get_entry(bill_id)["important"] == 1
    c.post(f"/entry/{bill_id}/important", headers=headers)
    assert database.get_entry(bill_id)["important"] == 0


def test_toggle_logs_activity_with_user(env, staff_client):
    c, headers = staff_client
    bill_id = _bill()
    c.post(f"/entry/{bill_id}/important", headers=headers)
    acts = database.get_activity_feed(5)
    assert acts[0]["action"] == "Flagged important"
    assert acts[0]["user"] == "TestUser"
    assert acts[0]["entry_id"] == bill_id
    c.post(f"/entry/{bill_id}/important", headers=headers)
    assert database.get_activity_feed(5)[0]["action"] == "Important flag removed"


def test_toggle_preserves_nav_context(env, staff_client):
    c, headers = staff_client
    bill_id = _bill()
    r = c.post(f"/entry/{bill_id}/important?ctx=log&doc_type=Bill", headers=headers,
               follow_redirects=False)
    assert "ctx=log" in r.headers["Location"]
    assert "doc_type=Bill" in r.headers["Location"]


def test_toggle_missing_entry_redirects_to_log(env, staff_client):
    c, headers = staff_client
    r = c.post("/entry/99999/important", headers=headers, follow_redirects=False)
    assert r.status_code == 302 and "/log" in r.headers["Location"]


# ── UI surfacing ───────────────────────────────────────────────────────────────

def test_entry_view_shows_flag_button_and_badge(env, staff_client):
    c, headers = staff_client
    bill_id = _bill()
    html = c.get(f"/entry/{bill_id}/view").get_data(as_text=True)
    assert "Flag important" in html
    assert "Important</span>" not in html                    # no badge while unflagged
    c.post(f"/entry/{bill_id}/important", headers=headers)
    html = c.get(f"/entry/{bill_id}/view").get_data(as_text=True)
    assert "Flagged &mdash; remove" in html or "Flagged —" in html
    assert "Important" in html


def test_dashboard_shows_flagged_bill_with_marker(env, staff_client):
    c, headers = staff_client
    _bill(vendor="Disputed Roofing Invoice", important=1)
    html = c.get("/").get_data(as_text=True)
    assert "legal or important document" in html
    assert "Disputed Roofing Invoice" in html
    assert "important=1" in html                             # the "Flagged →" quick link


def test_log_page_flag_indicator_and_filter_link(env, staff_client):
    c, headers = staff_client
    _bill(vendor="Plain Bill")
    _bill(vendor="Flagged Bill", important=1)
    r = c.get("/log?important=1")
    assert r.status_code == 200
    assert b"Flagged Bill" in r.data and b"Plain Bill" not in r.data
    # re-submitting the filter form keeps the flag filter (hidden input present)
    assert b'name="important" value="1"' in r.data
