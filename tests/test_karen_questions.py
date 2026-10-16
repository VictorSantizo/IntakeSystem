"""Karen's setup questionnaire (/karen): the page renders every still-open
question with a notes box and its own Submit button, submitting one question
saves just that question and removes it from the list, blank submits don't
leave a stray DB row, and already-answered questions (from before or from
earlier in the session) stay off the list. Content comes from
karen_questions.py."""

import database
from karen_questions import QUESTIONS, SECTIONS, DATE_FIXES


def _opt_values(q):
    """Options are either plain strings or {'value':..., 'recommended':...}
    dicts (the latter used to highlight a recommended choice, e.g. q22)."""
    return [opt["value"] if isinstance(opt, dict) else opt for opt in q["options"]]


def test_page_renders_every_open_question_with_notes(staff_client):
    c, _ = staff_client
    r = c.get("/karen")
    assert r.status_code == 200
    # Jinja autoescapes apostrophes/quotes ("Syed's" -> "Syed&#39;s") — normalize
    # so assertions can use the natural text.
    html = (r.get_data(as_text=True)
            .replace("&#39;", "'").replace("&#34;", '"').replace("&amp;", "&"))
    for skey, stitle, _intro in SECTIONS:
        assert f"{skey}. {stitle}" in html
    for q in QUESTIONS:
        assert f'name="n_{q["id"]}"' in html          # a notes box on EVERY open item
        for val in _opt_values(q):
            assert f'value="{val}"' in html
    # the section-C glance table is shown
    for wb, _entry, was, now in DATE_FIXES:
        assert wb in html and was in html and now in html
    assert f"0 of {len(QUESTIONS)} answered" in html


def test_q22_ambiguous_entity_question_recommends_blank(staff_client):
    c, _ = staff_client
    html = c.get("/karen").get_data(as_text=True)
    q22 = next(q for q in QUESTIONS if q["id"] == "q22")
    assert q22["text"] in html.replace("&#39;", "'").replace("&mdash;", "—")
    assert "(recommended)" in html
    # the safer, human-review option is the one flagged recommended
    assert q22["options"][0]["recommended"] is True
    assert "LEAVE IT BLANK" in q22["options"][0]["value"]


def test_submit_saves_and_confirms(staff_client):
    c, headers = staff_client
    r = c.post("/karen/save", data={"qid": "q1", "a_q1": "YES"}, headers=headers, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Thank you. Your response has been sent." in html

    saved = database.get_karen_answers()
    assert saved["q1"]["answer"] == "YES"
    assert saved["q1"]["updated_by"] == "TestUser"


def test_submit_removes_question_from_view(staff_client):
    c, headers = staff_client
    html_before = c.get("/karen").get_data(as_text=True)
    assert 'name="a_q1"' in html_before

    c.post("/karen/save", data={"qid": "q1", "a_q1": "YES"}, headers=headers)

    html_after = c.get("/karen").get_data(as_text=True)
    assert 'name="a_q1"' not in html_after
    # other questions are unaffected and still shown
    assert 'name="a_q2"' in html_after
    assert f"1 of {len(QUESTIONS)} answered" in html_after


def test_previously_answered_question_hidden_on_load(staff_client):
    c, _ = staff_client
    # Simulate an answer that landed before this change shipped — a direct
    # DB write, not a fresh submit through the new per-question form.
    database.save_karen_answer("q6", "OWNER", "", "SomeoneElse")
    html = c.get("/karen").get_data(as_text=True)
    assert 'name="a_q6"' not in html
    assert f"1 of {len(QUESTIONS)} answered" in html


def test_notes_only_answer_counts_and_hides(staff_client):
    c, headers = staff_client
    c.post("/karen/save", data={"qid": "q17", "n_q17": "Karen, Thursday afternoon"}, headers=headers)
    saved = database.get_karen_answers()
    assert saved["q17"]["notes"] == "Karen, Thursday afternoon"
    html = c.get("/karen").get_data(as_text=True)
    assert 'name="a_q17"' not in html and 'name="n_q17"' not in html


def test_blank_submit_does_not_create_stray_row(staff_client):
    c, headers = staff_client
    c.post("/karen/save", data={"qid": "q4"}, headers=headers)  # no answer, no notes
    saved = database.get_karen_answers()
    assert "q4" not in saved
    # and it's still shown, since nothing was actually recorded
    html = c.get("/karen").get_data(as_text=True)
    assert 'name="a_q4"' in html


def test_resubmit_updates_without_losing_others(staff_client):
    c, headers = staff_client
    c.post("/karen/save", data={"qid": "q1", "a_q1": "YES"}, headers=headers)
    c.post("/karen/save", data={"qid": "q2", "a_q2": "YES"}, headers=headers)
    # q1 is answered again directly (e.g. she changes her mind) — still just
    # that one question's row gets touched
    c.post("/karen/save", data={"qid": "q1", "a_q1": "NO", "n_q1": "changed my mind"}, headers=headers)
    saved = database.get_karen_answers()
    assert saved["q1"]["answer"] == "NO"
    assert saved["q1"]["notes"] == "changed my mind"
    assert saved["q2"]["answer"] == "YES"


def test_unknown_question_id_ignored(staff_client):
    c, headers = staff_client
    r = c.post("/karen/save", data={"qid": "not-a-real-id", "a_not-a-real-id": "YES"}, headers=headers)
    assert r.status_code == 302
    assert "not-a-real-id" not in database.get_karen_answers()


def test_requires_login(client):
    r = client.get("/karen")
    assert r.status_code in (301, 302)
    assert "/login" in r.headers.get("Location", "")
