"""Karen's setup questionnaire (/karen) — extracted from app.py in the
2026-07-29 modular split (GH #6).

Pattern for every routes_*.py module in this split: app.py stays the
composition root and calls register(app, appmod) at its bottom, passing both
the Flask app object and the app MODULE itself. Routes are registered with
add_url_rule under their ORIGINAL endpoint names, so every url_for(), every
template endpoint check (base.html's request.endpoint == 'karen_page'), and
every bookmarked URL keeps working — a literal Blueprint would prefix each
endpoint and force an audit of every reference. Anything the routes need from
app.py is reached through the module object (_M.helper()), a dynamic lookup,
so tests that monkeypatch app-module attributes still take effect here.
"""
from flask import redirect, render_template, request, session, url_for

from database import get_karen_answers, save_karen_answer

_M = None   # the app module (composition root); set by register()


def register(app, appmod):
    global _M
    _M = appmod
    app.add_url_rule("/karen", "karen_page", karen_page)
    app.add_url_rule("/karen/save", "karen_save", karen_save, methods=["POST"])


def _karen_normalize_options(options):
    """Each option is either a plain string (the common case) or a dict with a
    'value' and an optional 'recommended' flag, used to highlight the safer
    choice on a question (e.g. q22's blank-vs-autofill call). Normalize to
    dicts here so the template only ever deals with one shape."""
    out = []
    for opt in options or []:
        if isinstance(opt, dict):
            out.append(dict(value=opt.get("value"), recommended=bool(opt.get("recommended"))))
        else:
            out.append(dict(value=opt, recommended=False))
    return out


def _karen_has_real_answer(answers, question_id):
    saved = answers.get(question_id, {})
    return bool((saved.get("answer") or "").strip() or (saved.get("notes") or "").strip())


def karen_page():
    from karen_questions import SECTIONS, QUESTIONS, DATE_FIXES
    answers = get_karen_answers()
    questions = []
    for i, q in enumerate(QUESTIONS, 1):
        q = dict(q)
        q["display_no"] = i
        q["options"] = _karen_normalize_options(q.get("options"))
        questions.append(q)
    answered = sum(1 for q in questions if _karen_has_real_answer(answers, q["id"]))
    # Already-answered questions (from before this change shipped, or from
    # earlier in this session) don't clutter the view — only show what's left.
    visible = [q for q in questions if not _karen_has_real_answer(answers, q["id"])]
    return render_template("karen.html", sections=SECTIONS, questions=visible,
                           date_fixes=DATE_FIXES, answers=answers,
                           answered=answered, total=len(questions))


def karen_save():
    from karen_questions import QUESTIONS
    user = session.get("user") or "?"
    valid_ids = {q["id"] for q in QUESTIONS}
    qid = request.form.get("qid") or ""
    if qid in valid_ids:
        answer = (request.form.get(f"a_{qid}") or "").strip()
        notes = (request.form.get(f"n_{qid}") or "").strip()
        # Only persist a row when something was actually entered — an empty
        # submit shouldn't leave a misleading "Last saved just now" row behind
        # for a question nobody actually answered.
        if answer or notes:
            save_karen_answer(qid, answer, notes, user)
    return redirect(url_for("karen_page",
                    msg="Thank you. Your response has been sent."))
