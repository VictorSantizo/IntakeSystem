"""Karen's /home landing — "one question, one thing to get done"
(GUI_REDESIGN_PROMPT.md, 2026-07-29).

Pins the selection logic (which question wins, which task wins, the empty
states, the two cards never pointing at the same document) and the routing:
Karen's BARE "/" bounces to /home; any query string — and any other staff
member — still gets the full dashboard, so nothing existing moved.
"""
import json
import time

import pytest
from werkzeug.security import generate_password_hash

import app as klr
import database


# ── helpers ────────────────────────────────────────────────────────────────────

def _client(user):
    c = klr.app.test_client()
    with c.session_transaction() as s:
        s["user"] = user
        s["last_active"] = time.time()
        s["csrf"] = "t"
    return c


def _answer_all_karen_questions():
    from karen_questions import QUESTIONS
    for q in QUESTIONS:
        database.save_karen_answer(q["id"], "YES", "", "Karen")


def _bill(vendor="Goleta Water", amount=125.50, received="2026-07-20", **over):
    d = dict(doc_type="Bill", status="Pending", vendor=vendor, amount=amount,
             date_received=received, due_date="2026-08-10")
    d.update(over)
    return database.insert_entry(d)


def _queue_item(sid, **over):
    d = {"mail_type": "Physical Mail", "doc_type": "Bill", "vendor": "SoCal Edison",
         "received_date": "2026-07-18", "pdf_path": "x"}
    d.update(over)
    with klr._queue_lock:
        klr.pending_scans[sid] = d


# ── routing: who lands where ──────────────────────────────────────────────────

def test_karen_bare_root_bounces_to_home(env):
    r = _client("Karen").get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/home")


def test_karen_root_with_args_gets_full_dashboard(env):
    # ?full=1 is home's "Show me everything"; ?month=... is the period picker.
    # Both must render the real dashboard, not bounce back to /home.
    for qs in ("/?full=1", "/?month=2026-07"):
        r = _client("Karen").get(qs)
        assert r.status_code == 200
        assert 'data-kk="nav-dashboard"' in r.get_data(as_text=True)


def test_other_staff_root_is_untouched(env):
    r = _client("TestUser").get("/")
    assert r.status_code == 200
    assert 'data-kk="nav-dashboard"' in r.get_data(as_text=True)


def test_home_reachable_by_url_but_login_gated(env):
    assert _client("TestUser").get("/home").status_code == 200
    r = klr.app.test_client().get("/home")   # anonymous
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_karen_login_lands_on_home(env):
    """Definition of done: OPENING the app shows the two-card screen."""
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    cfg["staff"].append({"name": "Karen",
                         "password": generate_password_hash("karenpw", method="pbkdf2:sha256")})
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    c = klr.app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "t"
    r = c.post("/login", data={"name": "Karen", "password": "karenpw", "csrf_token": "t"},
               follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "One question for you" in html or 'id="all-clear"' in html
    assert "Show me everything" in html


# ── the Classy/Colorful switch on the standalone landing ─────────────────────
# /home has no base.html chrome, so the switch include must carry its own
# csrf_token — Karen's 2026-08-11 report: the "always visible" switch wasn't
# visible on the one page she actually lands on.

def test_home_shows_the_skin_switch_with_baked_in_csrf(env):
    html = _client("Karen").get("/home").get_data(as_text=True)
    switch = html.split('class="skin-flip"', 1)[1].split("</form>", 1)[0]
    assert 'data-kk="skin-switch"' in switch
    assert 'name="csrf_token"' in switch       # no chrome JS here to inject it
    assert 'aria-checked="false"' in switch    # Karen is on Colorful
    assert 'value="new"' in switch             # flipping goes to Classy


def test_flipping_from_home_lands_on_the_classy_dashboard(env):
    # Exactly what the /home form submits: form-field token, no X-CSRF header.
    c = _client("Karen")
    r = c.post("/account/skin", data={"skin": "new", "csrf_token": "t"})
    assert r.status_code == 302
    assert "full=1" in r.headers["Location"]   # past the /home bounce
    follow = c.get(r.headers["Location"].split("http://localhost", 1)[-1])
    html = follow.get_data(as_text=True)
    assert follow.status_code == 200
    assert "newlook.css" in html               # the NEW dashboard, visibly
    switch = html.split('class="skin-flip"', 1)[1].split("</form>", 1)[0]
    assert 'aria-checked="true"' in switch     # and the switch now reads ON


# ── the question card: best source first ─────────────────────────────────────

def test_karen_questionnaire_wins_over_everything(env):
    _bill()
    _queue_item(901, important=True, important_reason="Shut-off notice")
    html = _client("Karen").get("/home").get_data(as_text=True)
    from karen_questions import QUESTIONS
    assert "BRITT bank account" in html          # QUESTIONS[0], still unanswered
    assert QUESTIONS[0]["id"] == "q1"            # guard: the fragment above is q1's
    assert '/karen' in html
    assert "Shut-off notice" not in html         # queue item did NOT take the card


def test_answered_questions_are_skipped(env):
    database.save_karen_answer("q1", "YES", "", "Karen")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert "BRITT bank account" not in html
    assert "personal spending" in html           # q2 is next in line


def test_already_paid_twin_question_and_task_avoids_it(env):
    _answer_all_karen_questions()
    paid = _bill(vendor="FAIR Plan", amount=1026.00, received="2026-06-15",
                 status="Paid", invoice_number="INV-777")
    twin = _bill(vendor="CA FAIR Plan", amount=1026.00, received="2026-07-18",
                 invoice_number="INV-777")
    other = _bill(vendor="Frontier", amount=88.00, received="2026-07-20",
                  invoice_number="F-123")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert "already marked paid" in html
    assert f"/entry/{twin}/view" in html                       # question points here
    assert f"/entry/{other}/view?ctx=catchup" in html          # task moved past it
    assert f"/entry/{twin}/view?ctx=catchup" not in html       # never both cards
    assert f"/entry/{paid}/view" not in html


def test_flagged_queue_item_question(env):
    _answer_all_karen_questions()
    bill = _bill()
    _queue_item(901, important=True, important_reason="Shut-off notice for TB2")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert "Shut-off notice for TB2" in html
    assert "/review/901" in html
    assert f"/entry/{bill}/view?ctx=catchup" in html           # task still the bill


def test_logged_important_correspondence_question(env):
    _answer_all_karen_questions()
    bill = _bill()
    legal = database.insert_entry(dict(
        date_received="2026-07-19", doc_type="Legal", status="Pending",
        vendor="Wolfe and Associates", important_reason="Attorney letter"))
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert f"/entry/{legal}/view" in html
    assert "Attorney letter" in html
    assert f"/entry/{bill}/view?ctx=catchup" in html


# ── the task card ─────────────────────────────────────────────────────────────

def test_task_is_oldest_received_not_soonest_due(env):
    """KAREN PAYS BY RECEIVED DATE — locked rule, matches /catchup default."""
    _answer_all_karen_questions()
    older = _bill(vendor="Older Vendor", amount=10.0, received="2026-07-10",
                  due_date="2026-09-01")
    newer = _bill(vendor="Newer Vendor", amount=20.0, received="2026-07-25",
                  due_date="2026-07-30")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert f"/entry/{older}/view?ctx=catchup" in html
    assert f"/entry/{newer}/view?ctx=catchup" not in html
    assert 'id="question-empty"' in html         # all sources empty → quiet strip


def test_task_falls_back_to_oldest_queue_item(env):
    _answer_all_karen_questions()
    _queue_item(1, received_date="2026-07-18", vendor="Newer Scan")
    _queue_item(2, received_date="2026-07-10", vendor="Older Scan")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert "/review/2" in html
    assert "Older Scan" in html
    assert 'id="question-empty"' in html


def test_question_and_task_never_the_same_item(env):
    _answer_all_karen_questions()
    _queue_item(7, important=True, important_reason="Flagged by the AI")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert "/review/7" in html                   # it IS the question…
    assert 'id="task-empty"' in html             # …so the task card stands down


def test_no_counts_on_the_calm_screen(env):
    """Joshua, 2026-07-30: no backlog tallies ("24 on your list", "513 more
    wait behind it") — counts ARE the overwhelm this page replaces. Dates and
    the bill amount are allowed; counting words are not."""
    import re
    for i in range(6):
        _bill(vendor=f"Vendor {chr(65 + i)}", amount=None,
              received=f"2026-07-1{i}", invoice_number=f"V-{i}")
        _queue_item(100 + i, received_date=f"2026-07-1{i}")
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert "on your list" not in html
    assert re.search(r"\d+ more", html) is None
    assert re.search(r"\d+ in the queue", html) is None


def test_three_compare_view(env):
    """?three=1 — the 2026-07-30 comparison shape: three questions, three
    things. The default stays one-and-one."""
    for i in range(4):
        _bill(vendor=f"Vendor {chr(65 + i)}", amount=50.0 + i,
              received=f"2026-07-1{i}", invoice_number=f"C-{i}")
    html = _client("Karen").get("/home?three=1").get_data(as_text=True)
    assert "BRITT bank account" in html          # karen q1
    assert "personal spending" in html           # karen q2
    assert html.count('class="card qcard"') == 3   # three question cards
    assert html.count("ctx=catchup") == 3        # three pay tasks
    assert "Questions for you" in html and "Things to get done" in html
    # default view is untouched: one of each
    html1 = _client("Karen").get("/home").get_data(as_text=True)
    assert html1.count("ctx=catchup") == 1
    assert "One question for you" in html1


def test_all_clear_celebration(env):
    _answer_all_karen_questions()
    html = _client("Karen").get("/home").get_data(as_text=True)
    assert 'id="all-clear"' in html
    assert "All caught up" in html
    assert "Show me everything" in html          # the way out is always there
