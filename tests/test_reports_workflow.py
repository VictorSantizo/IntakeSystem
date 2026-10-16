"""Issue-report lifecycle: /reports views, notes/bounce/verify actions, the
night-shift dev API, the CLI mapping, the page indicator, and AI summaries.
Spec: VALIDATION_WORKFLOW.md."""
import json
from types import SimpleNamespace

import app as klr
import database
import routes_reports


def _seed(user="Nancy", desc="something broke", page_url="/bills"):
    return database.add_bug_report(user, page_url, "", desc)


def _order_of(resp, *needles):
    """Positions of byte-needles in the response body (asserting presence)."""
    body = resp.data
    pos = []
    for n in needles:
        b = n.encode()
        assert b in body, f"{n!r} missing from page"
        pos.append(body.index(b))
    return pos


def _set_token(env, token):
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    cfg["night_shift_token"] = token
    open(env.cfg_path, "w", encoding="utf-8").write(json.dumps(cfg))


# ── /reports views ────────────────────────────────────────────────────────────

def test_default_view_is_validation_queue_oldest_first(env, admin_client):
    a, _ = admin_client
    r1 = _seed(desc="OLDEST dev-resolved issue")
    r2 = _seed(desc="NEWER dev-resolved issue")
    _seed(desc="plain open issue")
    database.mark_report_dev_resolved(r1, "Dev", "fixed one; check A")
    database.mark_report_dev_resolved(r2, "Dev", "fixed two; check B")
    resp = a.get("/reports")
    p = _order_of(resp, "OLDEST dev-resolved issue", "NEWER dev-resolved issue")
    assert p[0] < p[1], "validation queue must run oldest -> newest"
    assert b"plain open issue" not in resp.data


def test_priority_bump_sorts_first_within_view(env, admin_client):
    a, _ = admin_client
    r1 = _seed(desc="ORDINARY old issue")
    r2 = _seed(desc="BUMPED newer issue")
    database.set_report_priority(r2, 1)
    resp = a.get("/reports?view=active")
    p = _order_of(resp, "BUMPED newer issue", "ORDINARY old issue")
    assert p[0] < p[1], "priority-bumped issues sort first"


def test_filter_by_user_and_legacy_show_params(env, admin_client):
    a, _ = admin_client
    _seed(user="Nancy", desc="NANCY-REPORTED issue")
    _seed(user="Joshua", desc="JOSHUA-REPORTED issue")
    resp = a.get("/reports?view=active&user=Nancy")
    assert b"NANCY-REPORTED issue" in resp.data
    assert b"JOSHUA-REPORTED issue" not in resp.data
    # pre-redesign links keep working
    resp = a.get("/reports?show=open")
    assert resp.status_code == 200 and b"NANCY-REPORTED issue" in resp.data
    assert a.get("/reports?show=all").status_code == 200


def test_note_bounce_verify_actions(env, admin_client):
    a, h = admin_client
    rid = _seed()
    resp = a.post(f"/reports/{rid}/note", data={"text": "seen it too"}, headers=h)
    assert resp.status_code == 302
    assert database.get_report_notes(rid)[-1]["text"] == "seen it too"
    assert database.get_report_notes(rid)[-1]["author"] == "TestAdmin"

    database.mark_report_dev_resolved(rid, "Dev", "did X; check Y")
    resp = a.post(f"/reports/{rid}/bounce", data={"text": "nope, still broken"},
                  headers=h)
    assert resp.status_code == 302
    rep = database.get_bug_reports()[0]
    assert rep["status"] == "In progress" and rep["bounced_at"]

    resp = a.post(f"/reports/{rid}/status", data={"status": "Verified"}, headers=h)
    assert resp.status_code == 302
    assert database.get_bug_reports()[0]["status"] == "Verified"


def test_legacy_resolved_post_maps_to_verified(env, admin_client):
    a, h = admin_client
    rid = _seed()
    a.post(f"/reports/{rid}/status", data={"status": "Resolved"}, headers=h)
    assert database.get_bug_reports()[0]["status"] == "Verified"


def test_priority_and_topic_routes(env, admin_client):
    a, h = admin_client
    rid = _seed()
    a.post(f"/reports/{rid}/priority", data={"priority": "1"}, headers=h)
    assert database.get_bug_reports()[0]["priority"] == 1
    a.post(f"/reports/{rid}/topic", data={"topic": "Issue Reports page"}, headers=h)
    assert database.get_bug_reports()[0]["topic"] == "Issue Reports page"


def test_dev_status_visible_on_page(env, admin_client):
    a, _ = admin_client
    _seed(desc="UNTOUCHED issue")
    touched = _seed(desc="TOUCHED issue")
    database.add_report_note(touched, "Night Shift", "root cause found", "dev")
    resp = a.get("/reports?view=active")
    assert b"No dev work yet" in resp.data          # the don't-check-it signal
    assert b"root cause found" in resp.data


# ── Night-shift dev API ───────────────────────────────────────────────────────

def test_dev_api_requires_token(env, client):
    rid = _seed()
    # no token configured at all -> closed
    r = client.post("/api/dev/report_note",
                    json={"report_id": rid, "text": "x"})
    assert r.status_code == 403
    _set_token(env, "s3cret-night-token")
    r = client.post("/api/dev/report_note",
                    json={"report_id": rid, "text": "x", "token": "wrong"})
    assert r.status_code == 403
    assert database.get_report_notes(rid) == []


def test_dev_api_add_note_no_login_needed(env, client):
    rid = _seed()
    _set_token(env, "s3cret-night-token")
    r = client.post("/api/dev/report_note",
                    json={"report_id": rid, "text": "shipped a fix candidate",
                          "token": "s3cret-night-token"})
    assert r.status_code == 200 and r.get_json()["ok"]
    note = database.get_report_notes(rid)[-1]
    assert note["author"] == "Night Shift" and note["kind"] == "dev"
    acts = [x for x in database.get_activity_feed(10) if x["action"] == "Report note"]
    assert acts and acts[0]["user"] == "Night Shift"


def test_dev_api_status_transitions_are_scoped(env, client):
    rid = _seed()
    _set_token(env, "tok")
    ok = {"token": "tok", "report_id": rid}
    # Verified (and legacy Resolved) are refused — verification is human-only
    for s in ("Verified", "Resolved", "Open", "junk"):
        r = client.post("/api/dev/report_status", json=dict(ok, status=s, note="n"))
        assert r.status_code == 400, s
    # Dev-resolved without a resolution note is refused (report #70)
    r = client.post("/api/dev/report_status", json=dict(ok, status="Dev-resolved"))
    assert r.status_code == 400
    r = client.post("/api/dev/report_status",
                    json=dict(ok, status="In progress"))
    assert r.status_code == 200
    assert database.get_bug_reports()[0]["status"] == "In progress"
    r = client.post("/api/dev/report_status",
                    json=dict(ok, status="Dev-resolved",
                              note="Rewrote the sort; check oldest-first on /reports"))
    assert r.status_code == 200
    rep = database.get_bug_reports()[0]
    assert rep["status"] == "Dev-resolved"
    assert database.get_report_notes(rid)[-1]["kind"] == "resolution"


def test_dev_api_unknown_report_404(env, client):
    _set_token(env, "tok")
    r = client.post("/api/dev/report_note",
                    json={"token": "tok", "report_id": 999, "text": "x"})
    assert r.status_code == 404


def test_dev_api_file_new_report(env, client):
    """Dev sessions can FILE a report (the same verb as the staff widget —
    always lands Open, never any other status). Token-gated like the rest."""
    _set_token(env, "tok")
    r = client.post("/api/dev/report_new", json={"description": "x"})
    assert r.status_code == 403                       # no token
    r = client.post("/api/dev/report_new", json={"token": "tok",
                                                 "description": "   "})
    assert r.status_code == 400                       # blank description
    r = client.post("/api/dev/report_new",
                    json={"token": "tok",
                          "description": "History entries all read 'Klick Klack'",
                          "page_url": "http://192.168.10.240:5050/bills?view=due"})
    assert r.status_code == 200
    rid = r.get_json()["id"]
    rep = next(x for x in database.get_bug_reports() if x["id"] == rid)
    assert rep["status"] == "Open"
    assert rep["user"] == "Night Shift"               # default actor
    assert rep["page_url"].endswith("/bills?view=due")
    # record refs derive from the url exactly like the staff widget's do
    r2 = client.post("/api/dev/report_new",
                     json={"token": "tok", "description": "d",
                           "actor": "Dev session", "page_url": "/entry/417/edit"})
    rep2 = next(x for x in database.get_bug_reports()
                if x["id"] == r2.get_json()["id"])
    assert rep2["record_ref"] == "entry #417" and rep2["user"] == "Dev session"
    acts = [x for x in database.get_activity_feed(10) if x["action"] == "Report filed"]
    assert acts and acts[0]["user"] == "Dev session"


# ── Server-console CLI mapping ────────────────────────────────────────────────

def test_cli_resolve_report_maps_to_dev_resolved(env, capsys):
    rid = _seed()
    assert klr._cli_reports(["resolve-report", str(rid),
                             "--note", "Fixed the widget;", "check", "/bills"]) is True
    out = capsys.readouterr().out
    assert "Dev-resolved" in out and "verification" in out.lower()
    rep = database.get_bug_reports()[0]
    assert rep["status"] == "Dev-resolved"
    note = database.get_report_notes(rid)[-1]
    assert note["kind"] == "resolution" and "check /bills" in note["text"]
    # without --note a placeholder resolution note still lands
    r2 = _seed(desc="second")
    klr._cli_reports(["resolve-report", str(r2)])
    assert database.get_report_notes(r2)[-1]["kind"] == "resolution"


# ── Page indicator ────────────────────────────────────────────────────────────

def test_page_indicator_shows_for_admin_on_affected_page(env, admin_client, staff_client):
    a, _ = admin_client
    s, _ = staff_client
    _seed(page_url="http://192.168.10.240:5050/log?foo=1")
    assert b"about this page" in a.get("/log").data
    assert b"about this page" not in a.get("/bills").data
    assert b"about this page" not in s.get("/log").data       # staff: quiet


def test_page_indicator_stays_off_reports_page_itself(env, admin_client):
    a, _ = admin_client
    _seed(page_url="/reports")
    assert b"about this page" not in a.get("/reports?view=active").data


# ── AI summaries ──────────────────────────────────────────────────────────────

class _FakeAnthropic:
    def __init__(self, **kw):
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(
            text='{"summary": "One-line AI summary.", "topic": "Suggested topic"}')])


def test_ai_refresh_fills_summary_and_only_blank_topics(env, admin_client, monkeypatch):
    monkeypatch.setattr(routes_reports.anthropic, "Anthropic", _FakeAnthropic)
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    cfg["claude_api_key"] = "sk-test-not-placeholder"
    open(env.cfg_path, "w", encoding="utf-8").write(json.dumps(cfg))
    r1 = _seed(desc="needs a summary")
    r2 = _seed(desc="already grouped")
    database.set_report_topic(r2, "Hand-picked topic")
    a, h = admin_client
    resp = a.post("/reports/ai_refresh", headers=h)
    assert resp.status_code == 302
    reps = {r["id"]: r for r in database.get_bug_reports()}
    assert reps[r1]["ai_summary"] == "One-line AI summary."
    assert reps[r2]["ai_summary"] == "One-line AI summary."
    assert reps[r1]["topic"] == "Suggested topic"
    assert reps[r2]["topic"] == "Hand-picked topic"        # never overwritten


def test_ai_refresh_degrades_when_unconfigured(env, admin_client):
    _seed()
    a, h = admin_client
    resp = a.post("/reports/ai_refresh", headers=h)        # placeholder API key
    assert resp.status_code == 302
    assert not (database.get_bug_reports()[0]["ai_summary"] or "")
    assert a.get("/reports").status_code == 200
