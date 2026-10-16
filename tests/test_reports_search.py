"""Report #73 (Joshua): search the Issue Reports page.

His words: "we need to add a search to this page. i'm trying to look for all
emails related to email display functionality which one of the issues has a
topic of Invoice email..." — then "i was looking for all ISSUES related to
email display to mark them as 'Invoice Email Visibility'".

So the search has to find issues by things OTHER than their own wording: the
topic, the AI summary, the page they were filed from, and the note trail —
because the reason an issue belongs to a group is often only written in a note.
"""
import database


def _report(desc, **kw):
    rid = database.add_bug_report(
        kw.get("user", "Joshua"), kw.get("page_url", ""),
        kw.get("record_ref", ""), desc, kw.get("context", ""))
    if kw.get("topic"):
        database.set_report_topic(rid, kw["topic"])
    if kw.get("note"):
        database.add_report_note(rid, "Dev session", kw["note"], "dev")
    return rid


def _page(client, url):
    return client.get(url).get_data(as_text=True)


def test_search_matches_the_report_text(env, admin_client):
    c, _ = admin_client
    _report("MARKER-HIT I can't see the copy of the email for this invoice")
    _report("MARKER-MISS the fast lane is not working")
    html = _page(c, "/reports?view=all&q=email")
    assert "MARKER-HIT" in html
    assert "MARKER-MISS" not in html


def test_search_reaches_the_note_trail(env, admin_client):
    """The point of #73: an issue belongs to the email group because of what a
    NOTE says, not because its own sentence used the word."""
    c, _ = admin_client
    _report("MARKER-NOTED source panel not visible on entry 813",
            note="Same class as #8 — the source EMAIL metadata was consumed "
                 "at intake and is unrecoverable.")
    _report("MARKER-OTHER the columns on this page are wrong")
    html = _page(c, "/reports?view=all&q=email")
    assert "MARKER-NOTED" in html
    assert "MARKER-OTHER" not in html


def test_search_matches_topic_and_page(env, admin_client):
    c, _ = admin_client
    _report("MARKER-TOPIC something", topic="Invoice Email Visibility")
    _report("MARKER-PAGE something else",
            page_url="http://192.168.10.240:5050/review/1073")
    assert "MARKER-TOPIC" in _page(c, "/reports?view=all&q=invoice")
    assert "MARKER-PAGE" in _page(c, "/reports?view=all&q=review")


def test_every_word_must_match(env, admin_client):
    c, _ = admin_client
    _report("MARKER-BOTH the email panel is missing")
    _report("MARKER-ONE the email arrived fine")
    html = _page(c, "/reports?view=all&q=email+panel")
    assert "MARKER-BOTH" in html
    assert "MARKER-ONE" not in html


def test_search_is_case_insensitive_and_works_in_every_view(env, admin_client):
    c, _ = admin_client
    _report("MARKER-CASE EMAIL display is broken")
    for view in ("all", "active"):
        assert "MARKER-CASE" in _page(c, f"/reports?view={view}&q=email")


def test_search_composes_with_the_reporter_filter(env, admin_client):
    c, _ = admin_client
    _report("MARKER-KAREN email display", user="Karen")
    _report("MARKER-JOSHUA email display", user="Joshua")
    html = _page(c, "/reports?view=all&q=email&user=Karen")
    assert "MARKER-KAREN" in html
    assert "MARKER-JOSHUA" not in html


def test_no_search_term_changes_nothing(env, admin_client):
    c, _ = admin_client
    _report("MARKER-A first thing")
    _report("MARKER-B second thing")
    html = _page(c, "/reports?view=all")
    assert "MARKER-A" in html and "MARKER-B" in html
    assert 'name="q"' in html          # the box is there even with no term
