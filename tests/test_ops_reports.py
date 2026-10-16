"""Ops Reports: admin gate, allow-list-only serving, staleness flag,
traversal rejection, held-for-a-human count, txt-in-<pre> rendering."""
import os
import time

import routes_ops_reports as ops


def _write(name, content="<html><body>report body</body></html>", age_hours=0):
    """Drop an artifact into the (test-isolated) ops dir, optionally aged."""
    path = os.path.join(ops.DEFAULT_OPS_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if age_hours:
        t = time.time() - age_hours * 3600
        os.utime(path, (t, t))
    return path


def _write_dailies(age_hours=0):
    _write("HARVEST.html", '<div class="kl">Held for a human</div>'
                           '<div class="kv">7</div>', age_hours)
    _write("RECONCILIATION.html", age_hours=age_hours)
    _write("BIG_PICTURE.html", age_hours=age_hours)


def test_list_and_raw_are_admin_only(env, staff_client, admin_client):
    _write_dailies()
    c, _ = staff_client
    for url in ("/ops-reports", "/ops-reports/view/HARVEST.html",
                "/ops-reports/raw/HARVEST.html"):
        r = c.get(url)
        assert r.status_code == 302, url   # admin_required bounces staff away
    a, _ = admin_client
    assert a.get("/ops-reports").status_code == 200


def test_list_fresh_dailies_no_alarm_and_held_count(env, admin_client):
    _write_dailies()
    a, _ = admin_client
    html = a.get("/ops-reports").data.decode()
    for title in ("Harvest", "Payment Reconciliation", "The Big Picture"):
        assert title in html
    assert "looks like it FAILED" not in html
    assert "7 held for a human" in html
    # one-shot artifacts not on disk are listed quietly, never flagged
    assert "ROLLBACK_RENAMES.html" in html and "MISSING" not in html


def test_stale_daily_is_loud(env, admin_client):
    _write_dailies(age_hours=30)   # > 26h: the 6 AM task must have failed
    a, _ = admin_client
    html = a.get("/ops-reports").data.decode()
    assert "looks like it FAILED" in html
    assert html.count("STALE") >= 3   # all three dailies flagged
    # 25h old is within the slack window - no alarm
    _write_dailies(age_hours=25)
    html = a.get("/ops-reports").data.decode()
    assert "looks like it FAILED" not in html


def test_missing_daily_counts_as_failure(env, admin_client):
    _write("HARVEST.html")   # the other two dailies never generated
    a, _ = admin_client
    html = a.get("/ops-reports").data.decode()
    assert "looks like it FAILED" in html and "never generated" in html


def test_raw_serves_only_registry_names(env, admin_client):
    _write_dailies()
    secret = _write("secret_notes.txt", "do not serve me")
    assert os.path.exists(secret)   # present in the dir, absent from REGISTRY
    a, _ = admin_client
    r = a.get("/ops-reports/raw/HARVEST.html")
    assert r.status_code == 200
    assert b"Held for a human" in r.data
    assert r.mimetype == "text/html"
    assert a.get("/ops-reports/raw/secret_notes.txt").status_code == 404
    assert a.get("/ops-reports/view/secret_notes.txt").status_code == 404


def test_raw_rejects_traversal(env, admin_client):
    _write_dailies()
    a, _ = admin_client
    for evil in ("..%2Fconfig.json", "..%5Cconfig.json",
                 "%2e%2e%2fconfig.json", "....//HARVEST.html",
                 "..\\..\\config.json"):
        r = a.get(f"/ops-reports/raw/{evil}")
        assert r.status_code == 404, evil
    # and the registry itself can't be tricked by a nearly-right name
    assert a.get("/ops-reports/raw/harvest.html").status_code == 404


def test_view_html_iframes_raw(env, admin_client):
    _write_dailies()
    a, _ = admin_client
    html = a.get("/ops-reports/view/BIG_PICTURE.html").data.decode()
    assert '<iframe src="/ops-reports/raw/BIG_PICTURE.html"' in html


def test_view_log_renders_escaped_pre(env, admin_client):
    _write("bigpicture_daily.log", "run ok <b>not markup</b> & done")
    a, _ = admin_client
    r = a.get("/ops-reports/view/bigpicture_daily.log")
    html = r.data.decode()
    assert "<pre" in html
    assert "&lt;b&gt;not markup&lt;/b&gt; &amp; done" in html
    raw = a.get("/ops-reports/raw/bigpicture_daily.log")
    assert raw.mimetype == "text/plain"
    assert b"<b>not markup</b>" in raw.data


def test_view_missing_file_404(env, admin_client):
    a, _ = admin_client   # registry name, but nothing on disk
    assert a.get("/ops-reports/view/HARVEST_BACKFILL.html").status_code == 404
    assert a.get("/ops-reports/raw/HARVEST_BACKFILL.html").status_code == 404
