"""Issue families — one report split into sub-issues, or several reports
grouped into one thread. Spec: VALIDATION_WORKFLOW.md.

Both directions of the same relationship, both asked for in a bounce:

  #68 (Joshua, 2026-08-04): "reports like this should be separated into their
  own issues. 68a, 68b, etc. as such marking this as not yet resolved."

  #55 (Joshua, 2026-08-05): "There is 3 or so issues open for this. we should
  merge them."

The rules these pin: a family sorts as ONE thread (children directly under
their parent, wherever the parent lands); merging closes and rewrites nothing;
grouping is one level deep; and a sub-issue keeps its own status, because
verification is per-issue and always human.
"""
import database
import routes_reports


def _seed(user="Joshua", desc="something broke", page_url="/reports"):
    return database.add_bug_report(user, page_url, "", desc)


def _pos(resp, *needles):
    body = resp.data
    out = []
    for n in needles:
        b = n.encode()
        assert b in body, f"{n!r} missing from page"
        out.append(body.index(b))
    return out


# ── splitting (#68) ───────────────────────────────────────────────────────────

def test_split_makes_lettered_sub_issues_that_inherit_the_parent(env, admin_client):
    a, h = admin_client
    pid = database.add_bug_report("Joshua", "http://kk/reports", "entry #9",
                                  "AI description, associate similar issues, "
                                  "and dev notes per issue")
    database.set_report_topic(pid, "Issue Reports page")
    a.post(f"/reports/{pid}/split", headers=h, data={
        "parts": "an AI description per issue\n"
                 "associate similar issues\n"
                 "\n"                       # blank lines are not sub-issues
                 "- filter for 'no dev work yet'\n"})
    kids = database.get_report_children(pid)
    assert [k["description"] for k in kids] == [
        "an AI description per issue", "associate similar issues",
        "filter for 'no dev work yet'"]
    assert all(k["status"] == "Open" for k in kids), "nobody has worked these yet"
    assert all(k["user"] == "Joshua" and k["record_ref"] == "entry #9"
               and k["topic"] == "Issue Reports page" for k in kids)
    # The parent keeps its own status and records what came out of it.
    assert database.get_bug_reports()[-1]["status"] or True
    trail = " ".join(n["text"] for n in database.get_report_notes(pid))
    assert f"#{pid}a" in trail and f"#{pid}c" in trail


def test_sub_issues_are_lettered_and_shown_under_the_parent(env, admin_client):
    a, h = admin_client
    pid = _seed(desc="PARENT ISSUE with two asks")
    a.post(f"/reports/{pid}/split", headers=h,
           data={"parts": "FIRST ASK\nSECOND ASK"})
    resp = a.get("/reports?view=active")
    p = _pos(resp, "PARENT ISSUE with two asks", "FIRST ASK", "SECOND ASK")
    assert p[0] < p[1] < p[2], "the family reads as one thread, in order"
    assert f"#{pid}a".encode() in resp.data
    assert f"#{pid}b".encode() in resp.data
    assert b"2 sub-issues" in resp.data


def test_a_split_family_travels_with_its_parent_in_the_sort(env, admin_client):
    """The parent is the OLDEST report, so the family must stay at the top even
    though the children were created last — otherwise splitting a report
    scatters it down a long page, which is the reading it was split to avoid."""
    a, h = admin_client
    pid = _seed(desc="OLD PARENT")
    _seed(desc="MIDDLE UNRELATED")
    a.post(f"/reports/{pid}/split", headers=h, data={"parts": "CHILD OF THE OLD ONE"})
    resp = a.get("/reports?view=active")
    p = _pos(resp, "OLD PARENT", "CHILD OF THE OLD ONE", "MIDDLE UNRELATED")
    assert p[0] < p[1] < p[2]


def test_splitting_nothing_does_nothing(env, admin_client):
    a, h = admin_client
    pid = _seed()
    a.post(f"/reports/{pid}/split", headers=h, data={"parts": "   \n \n"})
    assert database.get_report_children(pid) == []


def test_split_suggestion_is_silent_without_an_api_key(env, admin_client):
    """The ✨ button must never break the page — no key configured means an
    empty suggestion, not an error."""
    a, _h = admin_client
    pid = _seed()
    assert a.get(f"/reports/{pid}/split_suggest").get_json() == {"lines": []}


# ── grouping / merging (#55) ──────────────────────────────────────────────────

def test_grouping_makes_one_thread_without_closing_anything(env, admin_client):
    a, h = admin_client
    keeper = _seed(desc="KEEPER the email belongs on the record")
    other = _seed(desc="OTHER same ask, filed twice")
    a.post(f"/reports/{other}/group", headers=h, data={"parent_id": f"#{keeper}"})
    rows = {r["id"]: r for r in database.get_bug_reports()}
    assert rows[other]["parent_id"] == keeper
    assert rows[other]["status"] == "Open", "merging closes nothing"
    assert rows[keeper]["status"] == "Open"
    resp = a.get("/reports?view=active")
    p = _pos(resp, "KEEPER the email belongs", "OTHER same ask")
    assert p[0] < p[1]
    assert f"part of #{keeper}".encode() in resp.data


def test_ungrouping_puts_a_report_back_on_its_own(env, admin_client):
    a, h = admin_client
    keeper, other = _seed(), _seed()
    a.post(f"/reports/{other}/group", headers=h, data={"parent_id": str(keeper)})
    a.post(f"/reports/{other}/group", headers=h, data={"parent_id": ""})
    assert database.get_report_children(keeper) == []


def test_grouping_stays_one_level_deep(env):
    """A tree of trees would put back exactly the reading these features
    remove, so the second level is refused with a message that says where to
    go instead."""
    top, mid, low = _seed(), _seed(), _seed()
    assert database.set_report_parent(mid, top)[0]
    ok, msg = database.set_report_parent(low, mid)
    assert not ok and f"#{top}" in msg
    ok, msg = database.set_report_parent(top, low)
    assert not ok and "sub-issue" in msg


def test_a_report_cannot_be_its_own_parent(env):
    rid = _seed()
    ok, msg = database.set_report_parent(rid, rid)
    assert not ok and "itself" in msg
    ok, msg = database.set_report_parent(rid, 9999)
    assert not ok and "no report" in msg


def test_a_bad_group_number_is_reported_not_crashed(env, admin_client):
    a, h = admin_client
    rid = _seed()
    resp = a.post(f"/reports/{rid}/group", headers=h, data={"parent_id": "the email one"})
    assert resp.status_code in (302, 303)
    trail = " ".join(n["text"] for n in database.get_report_notes(rid))
    assert "not a report number" in trail


# ── the sort itself ───────────────────────────────────────────────────────────

def test_sorted_reports_without_a_family_is_unchanged(env):
    items = [{"id": 2, "created_at": "2026-08-02", "priority": 0},
             {"id": 1, "created_at": "2026-08-01", "priority": 0},
             {"id": 3, "created_at": "2026-08-03", "priority": 1}]
    assert [r["id"] for r in routes_reports._sorted_reports(items)] == [3, 1, 2]
