"""Issue-report lifecycle, data layer (VALIDATION_WORKFLOW.md).

States: Open -> In progress -> Dev-resolved (awaiting verification) -> Verified,
plus the bounce-back ("checked, not yet resolved" -> In progress). Legacy
'Resolved' rows read as closed history; nothing auto-flips on migration.
"""
import database


def _add(user="Nancy", desc="something broke", status=None, page_url="/bills"):
    rid = database.add_bug_report(user, page_url, "", desc)
    if status:
        database.set_bug_report_status(rid, status)
    return rid


def test_migration_upgrades_legacy_schema(env):
    """A pre-workflow bug_reports table gains the new columns on init_db and
    existing rows keep their status exactly — Open stays Open."""
    with database.get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS report_notes")
        conn.execute("DROP TABLE bug_reports")
        conn.execute("""CREATE TABLE bug_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, page_url TEXT,
            record_ref TEXT, description TEXT, context TEXT,
            status TEXT DEFAULT 'Open',
            created_at TEXT DEFAULT (datetime('now','localtime')))""")
        conn.execute("INSERT INTO bug_reports (user, description) "
                     "VALUES ('Nancy', 'legacy open row')")
        conn.execute("INSERT INTO bug_reports (user, description, status) "
                     "VALUES ('Karen', 'legacy closed row', 'Resolved')")
        conn.commit()
    database.init_db()
    by_desc = {r["description"]: r for r in database.get_bug_reports()}
    assert by_desc["legacy open row"]["status"] == "Open"
    assert by_desc["legacy closed row"]["status"] == "Resolved"
    assert by_desc["legacy open row"]["priority"] == 0
    assert not (by_desc["legacy open row"]["ai_summary"] or "")
    assert not (by_desc["legacy open row"]["bounced_at"] or "")
    # report_notes exists again and accepts rows
    database.add_report_note(by_desc["legacy open row"]["id"], "Dev", "note", "dev")


def test_notes_roundtrip_oldest_first(env):
    rid = _add()
    other = _add(desc="unrelated")
    database.add_report_note(rid, "Night Shift", "started looking", "dev")
    database.add_report_note(rid, "Joshua", "also happens on Chrome")
    notes = database.get_report_notes(rid)
    assert [n["text"] for n in notes] == ["started looking", "also happens on Chrome"]
    assert notes[0]["kind"] == "dev" and notes[1]["kind"] == "note"
    assert notes[0]["author"] == "Night Shift"
    assert database.get_report_notes(other) == []
    # batch map for the page
    m = database.get_report_notes_map()
    assert len(m[rid]) == 2 and other not in m


def test_dev_resolved_requires_a_resolution_note(env):
    rid = _add()
    try:
        database.mark_report_dev_resolved(rid, "Night Shift", "   ")
        assert False, "blank resolution note must be refused"
    except ValueError:
        pass
    assert database.get_bug_reports()[0]["status"] == "Open"
    database.mark_report_dev_resolved(rid, "Night Shift",
                                      "Fixed the sort; check /bills orders by due date.")
    rep = database.get_bug_reports()[0]
    assert rep["status"] == "Dev-resolved"
    assert not (rep["bounced_at"] or "")
    notes = database.get_report_notes(rid)
    assert notes[-1]["kind"] == "resolution"
    assert "check /bills" in notes[-1]["text"]


def test_bounce_back_returns_to_in_progress(env):
    rid = _add()
    database.mark_report_dev_resolved(rid, "Night Shift", "Did the thing.")
    database.bounce_report(rid, "Joshua", "Still broken on the All tab.")
    rep = database.get_bug_reports()[0]
    assert rep["status"] == "In progress"
    assert rep["bounced_at"]                      # night shift works these first
    notes = database.get_report_notes(rid)
    assert notes[-1]["kind"] == "bounce" and "All tab" in notes[-1]["text"]
    # dev-resolving again clears the bounce flag
    database.mark_report_dev_resolved(rid, "Night Shift", "Second try; check All tab.")
    assert not (database.get_bug_reports()[0]["bounced_at"] or "")


def test_verify_and_priority_and_topic(env):
    rid = _add()
    database.set_report_priority(rid, 1)
    database.set_report_topic(rid, "Issue Reports page")
    database.verify_report(rid, "Joshua")
    rep = database.get_bug_reports()[0]
    assert rep["status"] == "Verified"
    assert rep["priority"] == 1
    assert rep["topic"] == "Issue Reports page"


def test_open_count_counts_everything_not_closed(env):
    _add(status=None)                    # Open
    _add(status="In progress")
    r3 = _add()
    database.mark_report_dev_resolved(r3, "Dev", "done; check it")
    _add(status="Verified")
    _add(status="Resolved")              # legacy closed
    assert database.get_open_report_count() == 3
    active_ids = {r["id"] for r in database.get_active_reports()}
    assert len(active_ids) == 3


def test_open_report_paths_ignore_host_and_query(env):
    a = _add(page_url="http://192.168.10.240:5050/reports?show=open")
    b = _add(page_url="/log")
    closed = _add(page_url="/log", status="Verified")
    paths = database.get_open_report_paths()
    assert a in paths["/reports"]
    assert b in paths["/log"] and closed not in paths["/log"]
    assert "" not in paths
