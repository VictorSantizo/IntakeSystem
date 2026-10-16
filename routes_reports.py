"""In-app issue reporting ("Report a problem") — extracted from app.py in the
2026-07-29 modular split (GH #6). Same pattern as routes_karen.py (see its
docstring). _REPORT_SHOTS_DIR deliberately stays an app.py attribute, read via
_M, because conftest.py monkeypatches klr._REPORT_SHOTS_DIR per-test.

2026-08-04 redesign (reports #66-#70, spec: VALIDATION_WORKFLOW.md): lifecycle
Open -> In progress -> Dev-resolved -> Verified with a "checked, not yet
resolved" bounce-back, per-report notes, priority bump, topic grouping, AI
one-line summaries, and a token-authed dev API (file-new / add-note /
set-dev-status ONLY) so night-shift and dev sessions write through the
running app instead of touching intake.db over the share. The dev API can
never Verify.
"""
import hmac
import json
import os
import re
import threading
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse

import anthropic
from flask import jsonify, redirect, render_template, request, send_file, session, url_for

from database import (REPORT_CLOSED_STATUSES, add_bug_report, add_child_report,
                      add_report_note,
                      bounce_report, get_active_reports, get_activity_feed,
                      get_bug_reports, get_open_report_count,
                      get_report_notes_map, log_activity,
                      mark_report_dev_resolved, report_url_path,
                      set_bug_report_status, set_report_ai_summary,
                      set_report_parent, set_report_priority, set_report_topic,
                      verify_report)

_M = None   # the app module (composition root); set by register()

# Statuses the dev API may set. Verified (and legacy Resolved) are human-only:
# a machine saying "dev thinks it's fixed" must never look like a person
# saying "I checked and it works".
_DEV_API_STATUSES = ("In progress", "Dev-resolved")


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/report", "submit_report", submit_report, methods=["POST"])
    r("/reports", "reports", adm(reports))
    r("/reports/<int:report_id>/status", "report_status", adm(report_status), methods=["POST"])
    r("/reports/<int:report_id>/note", "report_note", adm(report_note), methods=["POST"])
    r("/reports/<int:report_id>/bounce", "report_bounce", adm(report_bounce), methods=["POST"])
    r("/reports/<int:report_id>/priority", "report_priority", adm(report_priority), methods=["POST"])
    r("/reports/<int:report_id>/topic", "report_topic", adm(report_topic), methods=["POST"])
    r("/reports/<int:report_id>/split", "report_split", adm(report_split), methods=["POST"])
    r("/reports/<int:report_id>/split_suggest", "report_split_suggest",
      adm(report_split_suggest))
    r("/reports/<int:report_id>/group", "report_group", adm(report_group), methods=["POST"])
    r("/reports/ai_refresh", "reports_ai_refresh", adm(reports_ai_refresh), methods=["POST"])
    r("/reports/<int:report_id>/screenshot", "report_screenshot", adm(report_screenshot))
    # Dev API — token-authed (night_shift_token), no login session. app.py
    # exempts these paths from the login and CSRF gates, like the connector
    # heartbeat. File-new + note + dev-status only; nothing else, ever.
    r("/api/dev/report_note", "dev_report_note", dev_report_note, methods=["POST"])
    r("/api/dev/report_status", "dev_report_status", dev_report_status, methods=["POST"])
    r("/api/dev/report_new", "dev_report_new", dev_report_new, methods=["POST"])


def _save_report_screenshot(data_url):
    """Persist the report's page screenshot (a base64 JPEG data-URL from
    html2canvas). Size-capped; returns the saved path or None. Never raises —
    a report must not fail because its screenshot did."""
    try:
        if not (isinstance(data_url, str) and data_url.startswith("data:image/jpeg;base64,")):
            return None
        import base64
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        if not (100 < len(raw) <= 4 * 1024 * 1024) or raw[:3] != b"\xff\xd8\xff":
            return None   # absurd size or not actually a JPEG
        os.makedirs(_M._REPORT_SHOTS_DIR, exist_ok=True)
        path = os.path.join(_M._REPORT_SHOTS_DIR,
                            f"report_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg")
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except Exception as e:
        print(f"[Report] could not save screenshot: {e}")
        return None


def submit_report():
    payload = request.get_json(silent=True) or {}
    description = (payload.get("description") or "").strip()
    if not description:
        return jsonify({"ok": False, "error": "Please describe the problem."}), 400
    page_url = payload.get("page_url") or request.referrer or ""
    # Pull a record reference out of the URL if they were on a record/scan.
    m = re.search(r"/(entry|review)/(\d+)", page_url)
    record_ref = f"{m.group(1)} #{m.group(2)}" if m else ""
    # Context blob: browser, JS errors, and (for a record) its recent activity.
    context = {
        "browser": (payload.get("browser") or "")[:500],
        "screen": (payload.get("screen") or "")[:40],
        "js_errors": (payload.get("js_errors") or [])[:10],
    }
    shot_path = _save_report_screenshot(payload.get("screenshot"))
    if shot_path:
        context["screenshot_path"] = shot_path
    if m and m.group(1) == "entry":
        try:
            eid = int(m.group(2))
            recent = [a for a in get_activity_feed(80) if a.get("entry_id") == eid][:5]
            context["recent_activity"] = [
                {"action": a.get("action"), "detail": a.get("detail"),
                 "user": a.get("user"), "when": a.get("created_at")} for a in recent]
        except Exception:
            pass
    rid = add_bug_report(session.get("user", ""), page_url, record_ref,
                         description, json.dumps(context))
    # Fresh reports get their AI one-liner in the background — never on the
    # submit path itself, and only when a real API key is configured.
    try:
        key = _M.load_config().get("claude_api_key", "")
        if key and key != "YOUR_API_KEY_HERE":
            threading.Thread(target=_ai_summarize_reports,
                             kwargs={"limit": 5}, daemon=True).start()
    except Exception:
        pass
    return jsonify({"ok": True, "id": rid})


def _own_sort_key(r):
    """#69 then #66: priority-bumped first, then oldest -> newest."""
    return (-(r.get("priority") or 0), r.get("created_at") or "", r["id"])


def _sorted_reports(items, by_id=None):
    """The list order — with a family kept together (#68/#55).

    A parent and its sub-issues are one thread, so they sort as one: the whole
    family takes the PARENT's place in the list and the children follow it in
    id order. Without that, splitting a report would scatter its pieces across
    a long page and cost exactly the reading Joshua split it to avoid. A report
    with no family sorts as it always did."""
    by_id = by_id or {}

    def key(r):
        root = by_id.get(r.get("parent_id")) or r
        return _own_sort_key(root) + (0 if root["id"] == r["id"] else 1, r["id"])

    return sorted(items, key=key)


def _child_label(parent_id, index):
    """#68a, #68b, ... — Joshua's own notation from the bounce ("68a, 68b,
    etc."). Past 26 sub-issues the letters give out and the number stands in;
    nobody will get there, but a label must never collide."""
    return f"#{parent_id}" + (chr(ord('a') + index) if index < 26 else str(index + 1))


def _matches(report, words, notes_map):
    """Report #73's search: every word present somewhere in the report — its
    own text, its label fields, the page it was filed from, or its note trail.
    Case-insensitive substring, so "email" finds "emailed" and a bare id like
    "55" finds report 55."""
    hay = " ".join(str(report.get(k) or "") for k in
                   ("id", "description", "topic", "ai_summary", "user",
                    "page_url", "record_ref", "status"))
    hay += " " + " ".join(str(n.get("text") or "")
                          for n in notes_map.get(report["id"], []))
    hay = hay.lower()
    return all(w in hay for w in words)


def reports():
    all_reports = get_bug_reports()
    active = [r for r in all_reports if r.get("status") not in REPORT_CLOSED_STATUSES]

    # Views: validate (default, #68 — Dev-resolved awaiting verification),
    # active (Open / In progress), all, page (indicator click-through).
    # Legacy links: ?show=open -> active, ?show=all -> all.
    view = request.args.get("view") or {"open": "active", "all": "all"}.get(
        request.args.get("show", ""), "validate")
    path = report_url_path(request.args.get("path") or "")
    if path:
        view = "page"
        items = [r for r in active if report_url_path(r.get("page_url")) == path]
    elif view == "all":
        items = list(all_reports)
    elif view == "active":
        items = [r for r in active if r.get("status") != "Dev-resolved"]
    else:
        view = "validate"
        items = [r for r in active if r.get("status") == "Dev-resolved"]

    notes_map = get_report_notes_map()

    f_q = (request.args.get("q") or "").strip()
    f_user = request.args.get("user") or ""
    f_topic = request.args.get("topic") or ""
    f_bumped = request.args.get("bumped") == "1"
    if f_q:
        # #73: Joshua was hunting "all issues related to email display" so he
        # could label them one topic. Search every word he might remember an
        # issue by — its own text, its topic, the AI summary, the page it was
        # filed from, and the note trail (a fix often names the thing the
        # report itself only implied). All words must match, in any field.
        words = f_q.lower().split()
        items = [r for r in items if _matches(r, words, notes_map)]
    if f_user:
        items = [r for r in items if (r.get("user") or "") == f_user]
    if f_topic:
        items = [r for r in items if (r.get("topic") or "") == f_topic]
    if f_bumped:
        items = [r for r in items if r.get("priority")]

    # The card you just acted on stays put for one render, even when the action
    # took it out of this view (#89's bounce: verifying a report in the validate
    # view deleted the very anchor the redirect was aiming at). Re-inserted
    # before the sort so it holds its own place in the list.
    try:
        just_id = int(request.args.get("just") or 0)
    except ValueError:
        just_id = 0
    if just_id and not any(r["id"] == just_id for r in items):
        just = next((r for r in all_reports if r["id"] == just_id), None)
        if just:
            items = items + [just]

    # Families (#68 split / #55 merge). Labels and the parent's roster are
    # computed over ALL reports, not the filtered view: a sub-issue keeps its
    # name and its parent link even when the rest of its family is filtered out.
    by_id = {r["id"]: r for r in all_reports}
    kids_of = {}
    for r in sorted(all_reports, key=lambda x: x["id"]):
        if r.get("parent_id"):
            kids_of.setdefault(r["parent_id"], []).append(r)
    labels = {}
    for pid, kids in kids_of.items():
        for i, k in enumerate(kids):
            labels[k["id"]] = _child_label(pid, i)

    items = _sorted_reports(items, by_id)

    for it in items:
        it["child_label"] = labels.get(it["id"])
        parent = by_id.get(it.get("parent_id"))
        it["parent"] = ({"id": parent["id"], "status": parent.get("status"),
                         "summary": (parent.get("ai_summary") or "").strip()
                         or (parent.get("description") or "")[:70]}
                        if parent else None)
        it["children"] = [{"id": k["id"], "label": labels.get(k["id"]),
                           "status": k.get("status"),
                           "summary": (k.get("ai_summary") or "").strip()
                           or (k.get("description") or "")[:70]}
                          for k in kids_of.get(it["id"], [])]
        try:
            it["context_obj"] = json.loads(it.get("context") or "{}")
        except Exception:
            it["context_obj"] = {}
        ns = notes_map.get(it["id"], [])
        it["notes"] = ns
        dev_notes = [n for n in ns if n.get("kind") in ("dev", "resolution")]
        it["dev_notes"] = dev_notes                       # "has dev touched this" (#66/#68)
        it["resolution"] = next((n for n in reversed(ns)
                                 if n.get("kind") == "resolution"), None)

    n_validate = sum(1 for r in active if r.get("status") == "Dev-resolved")
    return render_template(
        "reports.html", items=items, view=view, path=path, just_id=just_id,
        n_validate=n_validate, n_active=len(active) - n_validate,
        n_all=len(all_reports),
        users=sorted({r.get("user") for r in all_reports if r.get("user")}),
        topics=sorted({(r.get("topic") or "").strip() for r in all_reports
                       if (r.get("topic") or "").strip()}),
        f_q=f_q, f_user=f_user, f_topic=f_topic, f_bumped=f_bumped,
        ai_done=request.args.get("ai_done"),
        open_count=get_open_report_count())


def _back(report_id=None):
    """Return to the reports view (with its filters) the action came from,
    landing on the card that was just acted on.

    Report #89 (Joshua): every button on /reports is a POST-then-redirect, and
    a plain redirect puts you back at the top of a long page — so after each
    note or verify you had to scroll all the way down to find your place again.
    The #report-<id> fragment (the anchor is on the card in reports.html) makes
    the browser land where you were working.

    #89's bounce, 8/6 ("i clicked verified and it scrolled right back up"), was
    right even though the anchor was live: clicking Verified in the validate
    view REMOVES that card from the list, so #report-<id> pointed at a card
    that no longer existed and the browser gave up at the top. `just` keeps the
    card you just acted on in the list for that one render — the anchor
    resolves, you see what you did, and the next page load drops it.
    """
    p = urlparse(request.referrer or "")
    base = (p.path if p.path.startswith("/reports") else url_for("reports"))
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k != "just"] if p.path.startswith("/reports") else []
    if report_id:
        query.append(("just", str(report_id)))
    url = base + (("?" + urlencode(query)) if query else "")
    return redirect(url + (f"#report-{report_id}" if report_id else ""))


def report_status(report_id):
    """Human status changes. 'Verified' = the human check passed; the legacy
    'Resolved' value (old bookmarks/forms) means the same click, so it maps to
    Verified rather than resurrecting the pre-workflow state."""
    status = (request.form.get("status") or "").strip()
    user = session.get("user", "")
    if status in ("Verified", "Resolved"):
        verify_report(report_id, user)
    elif status in ("Open", "In progress"):
        set_bug_report_status(report_id, status)
        add_report_note(report_id, user, f"Status set to {status}.", kind="status")
    return _back(report_id)


def report_note(report_id):
    text = (request.form.get("text") or "").strip()
    if text:
        add_report_note(report_id, session.get("user", ""), text, kind="note")
    return _back(report_id)


def report_bounce(report_id):
    """'Checked, not yet resolved' (#67) — back to In progress with a note."""
    bounce_report(report_id, session.get("user", ""),
                  request.form.get("text") or "")
    return _back(report_id)


def report_priority(report_id):
    set_report_priority(report_id, request.form.get("priority") == "1")
    return _back(report_id)


def report_topic(report_id):
    set_report_topic(report_id, request.form.get("topic") or "")
    return _back(report_id)


# ── Families: split one report up, or group several together ──────────────────
# Joshua, bouncing #68 on 2026-08-04: "reports like this should be separated
# into their own issues. 68a, 68b, etc. as such marking this as not yet
# resolved." And from the other direction, bouncing #55 on 8/5: "There is 3 or
# so issues open for this. we should merge them." Same relationship, both ways
# round — one parent, its sub-issues, verified one at a time but read as one
# thread.

def report_split(report_id):
    """Split a report into sub-issues, one per non-blank line. The parent is
    left exactly as it is (its status is a fact about it, not about its
    children); it just gains a note recording what came out of it."""
    parent = next((r for r in get_bug_reports() if r["id"] == report_id), None)
    if not parent:
        return _back(report_id)
    lines = [ln.strip(" \t-•*") for ln in
             (request.form.get("parts") or "").splitlines()]
    parts = [ln for ln in lines if ln][:20]
    if not parts:
        return _back(report_id)
    user = session.get("user", "")
    existing = sum(1 for r in get_bug_reports() if r.get("parent_id") == report_id)
    made = []
    for i, text in enumerate(parts):
        cid = add_child_report(report_id, parent.get("user") or user,
                               parent.get("page_url"), parent.get("record_ref"),
                               text[:4000], parent.get("context") or "",
                               parent.get("topic") or "", parent.get("priority"))
        made.append(f"{_child_label(report_id, existing + i)} (#{cid})")
    add_report_note(report_id, user,
                    "Split into sub-issues: " + ", ".join(made) +
                    ". Each is verified on its own; they stay grouped here.",
                    kind="note")
    log_activity(None, "Report split", user,
                 f"report #{report_id} -> {len(made)} sub-issue(s)")
    return _back(report_id)


def report_split_suggest(report_id):
    """✨ Suggest the split — read the report and propose one line per separate
    ask, for the person to edit before anything is created. Nothing is written
    here. Returns {"lines": [...]}; an empty list whenever the AI is
    unavailable, so the button can never break the page (#68's 'AI description
    would be helpful', applied to its own bounce)."""
    rep = next((r for r in get_bug_reports() if r["id"] == report_id), None)
    if not rep:
        return jsonify({"lines": []})
    cfg = _M.load_config()
    key = cfg.get("claude_api_key", "")
    if not key or key == "YOUR_API_KEY_HERE":
        return jsonify({"lines": []})
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content":
                       "Staff filed one problem report about an internal "
                       "property-management app, and it contains several "
                       "separate asks. Reply with ONLY JSON: "
                       '{"parts": ["<one ask, in the reporter\'s own words '
                       'where possible, max 25 words>", ...]}. One entry per '
                       "thing that could be finished on its own. If it is "
                       'really only one ask, return {"parts": []}.\n'
                       f"Report: {(rep.get('description') or '')[:3000]}"}],
            timeout=25.0)
        raw = "".join(getattr(b, "text", "") for b in msg.content)
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        parts = [str(p).strip()[:400] for p in (data.get("parts") or []) if str(p).strip()]
        return jsonify({"lines": parts[:20]})
    except Exception as e:
        print(f"[Reports] split suggestion failed ({type(e).__name__}): {e}")
        return jsonify({"lines": []})


def report_group(report_id):
    """Merge this report into another one's thread, or take it back out
    (#55: "There is 3 or so issues open for this. we should merge them").
    Nothing is closed and nothing is rewritten — the reports stay separate
    records, they just travel together and sort together."""
    raw = (request.form.get("parent_id") or "").strip().lstrip("#")
    user = session.get("user", "")
    if not raw:
        set_report_parent(report_id, None)
        add_report_note(report_id, user, "Taken out of its issue group.", kind="note")
        return _back(report_id)
    try:
        parent_id = int(raw)
    except ValueError:
        add_report_note(report_id, user,
                        f'Could not group: "{raw[:40]}" is not a report number.',
                        kind="note")
        return _back(report_id)
    ok, message = set_report_parent(report_id, parent_id)
    add_report_note(report_id, user,
                    (f"Grouped under #{parent_id} — same issue, one thread."
                     if ok else f"Could not group: {message}"), kind="note")
    if ok:
        add_report_note(parent_id, user,
                        f"#{report_id} grouped under this one as a sub-issue.",
                        kind="note")
        log_activity(None, "Reports grouped", user,
                     f"report #{report_id} -> under #{parent_id}")
    return _back(report_id)


# ── AI one-line summaries (#68) ───────────────────────────────────────────────

def _ai_summarize_reports(limit=25):
    """Fill ai_summary (and topic, only where blank) for active reports that
    lack one. Batch — triggered by the admin button or, small, after a fresh
    report; NEVER on page load. Any failure stops the batch quietly: the page
    must render identically with zero summaries."""
    cfg = _M.load_config()
    key = cfg.get("claude_api_key", "")
    if not key or key == "YOUR_API_KEY_HERE":
        return 0
    todo = [r for r in get_active_reports()
            if not (r.get("ai_summary") or "").strip()][:limit]
    if not todo:
        return 0
    topics = sorted({(r.get("topic") or "").strip() for r in get_bug_reports()
                     if (r.get("topic") or "").strip()})
    done = 0
    try:
        client = anthropic.Anthropic(api_key=key)
        for r in todo:
            prompt = (
                "Staff filed this problem report about an internal property-"
                "management intake app. Reply with ONLY JSON: "
                '{"summary": "<one plain-words line, max 20 words: what is '
                'wrong or wanted>", "topic": "<2-4 word group label>"}\n'
                f"Reuse one of these topic labels when one fits: {topics}\n"
                f"Report (from {r.get('user') or 'staff'} on page "
                f"{r.get('page_url') or '?'}): {(r.get('description') or '')[:1500]}")
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=300,
                messages=[{"role": "user", "content": prompt}], timeout=20.0)
            raw = "".join(getattr(b, "text", "") for b in msg.content)
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            summary = str(data.get("summary") or "").strip()[:200]
            if summary:
                set_report_ai_summary(r["id"], summary)
                done += 1
            topic = str(data.get("topic") or "").strip()[:60]
            if topic and not (r.get("topic") or "").strip():
                set_report_topic(r["id"], topic)
    except Exception as e:
        print(f"[Reports] AI summaries stopped after {done}/{len(todo)} "
              f"({type(e).__name__}): {e}")
    return done


def reports_ai_refresh():
    done = _ai_summarize_reports()
    return redirect(url_for("reports") + f"?ai_done={done}")


# ── Dev API (night shift) ─────────────────────────────────────────────────────

def _dev_auth():
    """(payload, error_response). Token-gated like the connector heartbeat:
    night_shift_token in config.json, compared constant-time. No token
    configured = the API is off."""
    payload = request.get_json(silent=True) or {}
    expected = (_M.load_config().get("night_shift_token") or "").strip()
    sent = str(payload.get("token") or "")
    if not expected or not hmac.compare_digest(expected, sent):
        return None, (jsonify({"error": "bad token"}), 403)
    return payload, None


def _dev_payload_report(payload):
    """(report_id, actor, error_response) for a dev-API payload."""
    try:
        rid = int(payload.get("report_id"))
    except (TypeError, ValueError):
        return None, "", (jsonify({"error": "report_id required"}), 400)
    if not any(r["id"] == rid for r in get_bug_reports()):
        return None, "", (jsonify({"error": f"no report #{rid}"}), 404)
    actor = (str(payload.get("actor") or "").strip() or "Night Shift")[:40]
    return rid, actor, None


def dev_report_note():
    payload, err = _dev_auth()
    if err:
        return err
    rid, actor, err = _dev_payload_report(payload)
    if err:
        return err
    text = (str(payload.get("text") or "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    add_report_note(rid, actor, text[:4000], kind="dev")
    log_activity(None, "Report note", actor, f"dev note on report #{rid}")
    return jsonify({"ok": True})


def dev_report_status():
    payload, err = _dev_auth()
    if err:
        return err
    rid, actor, err = _dev_payload_report(payload)
    if err:
        return err
    status = (str(payload.get("status") or "")).strip()
    if status not in _DEV_API_STATUSES:
        return jsonify({"error": "status must be one of "
                        f"{list(_DEV_API_STATUSES)} — Verified/closed states "
                        "are set by a person in the app, never here "
                        "(VALIDATION_WORKFLOW.md)"}), 400
    note = (str(payload.get("note") or "")).strip()
    if status == "Dev-resolved":
        if not note:
            return jsonify({"error": "Dev-resolved requires a resolution note: "
                            "what was done and what to check (report #70)"}), 400
        mark_report_dev_resolved(rid, actor, note[:4000])
    else:
        set_bug_report_status(rid, "In progress")
        if note:
            add_report_note(rid, actor, note[:4000], kind="dev")
    log_activity(None, "Report dev-status", actor, f"report #{rid} -> {status}")
    return jsonify({"ok": True})


def dev_report_new():
    """File a NEW report from a token-authed dev session (Joshua 2026-08-04:
    issues asked for in chat must be tracked like widget reports). Same verb
    as the staff widget and no more: the report always lands Open — this
    endpoint cannot set any status, so creation stays the lowest-privilege
    action in the API."""
    payload, err = _dev_auth()
    if err:
        return err
    description = (str(payload.get("description") or "")).strip()
    if not description:
        return jsonify({"error": "description required"}), 400
    actor = (str(payload.get("actor") or "").strip() or "Night Shift")[:40]
    page_url = (str(payload.get("page_url") or "")).strip()[:500]
    m = re.search(r"/(entry|review)/(\d+)", page_url)
    record_ref = f"{m.group(1)} #{m.group(2)}" if m else ""
    rid = add_bug_report(actor, page_url, record_ref, description[:4000],
                         json.dumps({"source": "dev-api"}))
    log_activity(None, "Report filed", actor, f"report #{rid} via dev API")
    return jsonify({"ok": True, "id": rid})


def report_screenshot(report_id):
    rep = next((r for r in get_bug_reports() if r["id"] == report_id), None)
    if not rep:
        return "Not found", 404
    try:
        path = json.loads(rep.get("context") or "{}").get("screenshot_path") or ""
    except Exception:
        path = ""
    # Serve only files that live inside the Bug Reports folder — the path comes
    # from our own DB, but never send_file anything a corrupted row points at.
    if not (path and os.path.exists(path)
            and os.path.normcase(os.path.abspath(path)).startswith(
                os.path.normcase(_M._REPORT_SHOTS_DIR + os.sep))):
        return "No screenshot for this report", 404
    return send_file(path, mimetype="image/jpeg")
