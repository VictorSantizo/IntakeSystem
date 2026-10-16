"""Karen's landing screen (/home) — "one question, one thing to get done".

Joshua's ask (2026-07-29, GUI_REDESIGN_PROMPT.md): opening the app must not
greet Karen with a wall of words and numbers. Her landing is exactly two
cards — the ONE question the system most needs a human answer on, and the
ONE next action — with the full dashboard a single obvious click away
("Show me everything"). Only Karen's bare "/" lands here (see dashboard()
in app.py); other staff keep the dashboard, and the top bar's Dashboard
link brings Karen back to this screen from anywhere.

No counts anywhere on this screen (Joshua, 2026-07-30): "24 on your list"
is exactly the overwhelm the page exists to avoid. Dates and amounts stay —
they're the substance of a card, not a backlog tally.

Question sources, best first (the prompt's order):
  1. the top unanswered /karen questionnaire items,
  2. a bill near the top of her pay stack that looks ALREADY PAID
     (find_invoice_twins — the FAIR-Plan double-pay lesson),
  3. the oldest review-queue items the AI flagged as needing eyes,
  4. unacknowledged important correspondence from the log.
Task: the next bill in her received-order pay stack (locked rule: KAREN
PAYS BY RECEIVED DATE), else the oldest review-queue item. The cards never
point at the same document.

?three=1 renders three questions and three things instead of one of each —
a comparison view from the 2026-07-30 design session, kept so the two
shapes can be judged side by side on real data. Not linked from anywhere.

Same module pattern as the rest of the 2026-07-29 split (routes_karen.py
has the full rationale): app.py stays the composition root, views register
via add_url_rule, and app helpers are reached through the passed module
object so test monkeypatching still works.
"""
from datetime import date, datetime

from flask import render_template, request, session

from database import (find_invoice_twins, get_important_correspondence,
                      get_karen_answers)

_M = None   # the app module (composition root); set by register()


def register(app, appmod):
    global _M
    _M = appmod
    app.add_url_rule("/home", "home", home)


# How many bills from the top of the pay stack get the already-paid twin
# check. The question only matters for what Karen is about to pay next, and
# her calmest page shouldn't be the most expensive one to render.
_TWIN_SCAN_LIMIT = 5


def _money(amount):
    try:
        return "${:,.2f}".format(float(amount))
    except (TypeError, ValueError):
        return None


def _plain_date(iso):
    """'2026-07-20' -> 'July 20' (no %-d/%#d — strftime day padding isn't
    portable between Windows and the POSIX CI runners)."""
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d")
        return f"{d.strftime('%B')} {d.day}"
    except ValueError:
        return ""


def _open_karen_questions():
    """Unanswered /karen questions in the questionnaire's own order — the
    same order and answered-test as the /karen page itself, so the question
    on the card is the first one Karen sees after clicking through."""
    from karen_questions import QUESTIONS
    from routes_karen import _karen_has_real_answer
    answers = get_karen_answers()
    return [q for q in QUESTIONS if not _karen_has_real_answer(answers, q["id"])]


def _queue_oldest_first():
    """The review queue as [(sid, data)], oldest received first — 'oldest'
    here matches how long a paper has actually been waiting on a human."""
    items = _M._queue_items()
    return sorted(items, key=lambda kv: ((kv[1].get("received_date") or "9999-99-99")[:10],
                                         kv[0]))


def _pick_questions(stack, queue, limit=1):
    """Up to `limit` question cards, best source first. Each dict carries:
    text, url, button, note (+ entry_id/sid when the question is about a
    specific document, so the task cards can avoid it)."""
    out = []
    open_qs = _open_karen_questions()
    for q in open_qs[:limit]:
        out.append(dict(kind="karen", text=q["text"], url="/karen", button="Answer it",
                        note=("" if out else
                              "Last one on your list!" if len(open_qs) == 1
                              else "One at a time is fine.")))
    for bill in stack[:_TWIN_SCAN_LIMIT]:
        if len(out) >= limit:
            break
        paid_twins = [t for t in find_invoice_twins(bill)
                      if (t.get("status") or "") in ("Paid", "Filed")]
        if paid_twins:
            m = _money(bill.get("amount"))
            out.append(dict(kind="twin", entry_id=bill["id"],
                            text=(f"Was this already paid? The bill from "
                                  f"{bill.get('vendor') or 'an unknown vendor'}"
                                  + (f" for {m}" if m else "")
                                  + " looks like one that was already marked paid."),
                            url=f"/entry/{bill['id']}/view", button="Take a look",
                            note="Worth a check before it gets paid twice."))
    for sid, d in queue:
        if len(out) >= limit:
            break
        if d.get("important"):
            why = (d.get("important_reason") or d.get("notice_summary")
                   or "It was flagged as needing your eyes.")
            out.append(dict(kind="queue", sid=sid,
                            text=(f"A document from {d.get('vendor') or 'an unknown sender'} "
                                  f"needs your eyes: {why}"),
                            url=f"/review/{sid}", button="Look at it",
                            note="It's waiting in the review queue."))
    used_ids = {c.get("entry_id") for c in out}
    for row in get_important_correspondence(limit=limit):
        if len(out) >= limit:
            break
        if row["id"] in used_ids:
            continue
        why = (row.get("important_reason") or row.get("notice_summary")
               or row.get("doc_type") or "")
        out.append(dict(kind="logged", entry_id=row["id"],
                        text=(f"{_M._doc_label(row)} needs your eyes"
                              + (f": {why}" if why else ".")),
                        url=f"/entry/{row['id']}/view", button="Read it",
                        note="It stays here until someone acknowledges it."))
    return out


def _pick_tasks(stack, queue, skip_entry_ids, skip_sids, limit=1):
    """Up to `limit` next-action cards. skip_* keep the tasks from pointing
    at any document a question card is already about."""
    out = []
    for bill in stack:
        if len(out) >= limit:
            break
        if bill["id"] in skip_entry_ids:
            continue
        m = _money(bill.get("amount"))
        others = any(b["id"] != bill["id"] and b["id"] not in skip_entry_ids
                     for b in stack)
        received = _plain_date(bill.get("date_received"))
        first = not out
        out.append(dict(kind="pay", entry_id=bill["id"],
                        text=("Pay this bill: "
                              + (bill.get("vendor") or "open it to see who from")
                              + (f" — {m}" if m else "")),
                        url=f"/entry/{bill['id']}/view?ctx=catchup", button="Open the bill",
                        note=((f"Received {received} — " if received else "")
                              + ("it's next in your stack."
                                 + ("" if others else " It's the last one!"))
                              if first else
                              (f"Received {received} — after that." if received
                               else "After that."))))
    for sid, d in queue:
        if len(out) >= limit:
            break
        if sid in skip_sids:
            continue
        received = _plain_date(d.get("received_date"))
        first = not out
        out.append(dict(kind="review", sid=sid,
                        text=("Check in the next document: "
                              + (d.get("vendor") or "a new scan")),
                        url=f"/review/{sid}", button="Review it",
                        note=((f"Waiting since {received} — " if received else "")
                              + ("it's next in line." if first else "after that."))))
    return out


def home():
    stack = _M._catchup_received_rows()
    queue = _queue_oldest_first()
    limit = 3 if request.args.get("three") else 1
    questions = _pick_questions(stack, queue, limit)
    tasks = _pick_tasks(stack, queue,
                        skip_entry_ids={c.get("entry_id") for c in questions
                                        if c.get("entry_id")},
                        skip_sids={c.get("sid") for c in questions if c.get("sid")},
                        limit=limit)
    hour = datetime.now().hour
    greeting = ("Good morning" if hour < 12
                else "Good afternoon" if hour < 17 else "Good evening")
    today = date.today()
    return render_template("home.html",
                           user=session.get("user") or "",
                           greeting=greeting,
                           today=f"{today.strftime('%A, %B')} {today.day}",
                           questions=questions, tasks=tasks)
