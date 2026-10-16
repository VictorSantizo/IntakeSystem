r"""request_health.py — the app names its own stall.

2026-08-11: waitress accepted connections for two and a half hours and served
none of them (all 8 workers blocked), then completed everything at once when
whatever held them let go. A request that started at 14:09:35 was logged at
15:58:24. Diagnosing it afterwards took an afternoon of elimination and still
did not name the lock, because the evidence died with the stall.

These pin the thing that fixes that: the watchdog must notice saturation, dump
every thread's stack WHILE the threads are still blocked, and remember the
incident so the banner can report it once pages can render again.
"""
import threading
import time

import pytest

import request_health


@pytest.fixture(autouse=True)
def _clean():
    request_health.reset_for_tests()
    yield
    request_health.reset_for_tests()


def _fake_inflight(count, age, path="/api/pending_count", method="GET"):
    """Put N requests in flight, started `age` seconds ago."""
    started = time.time() - age
    with request_health._lock:
        for i in range(count):
            request_health._inflight[f"t{i}"] = (started, method, path)


# ── tracking ─────────────────────────────────────────────────────────────────

def test_tracked_counts_a_request_and_releases_it():
    seen = {}

    def wsgi(environ, start_response):
        seen["in_flight"] = request_health.snapshot()["in_flight"]
        return [b"ok"]

    app = request_health.Tracked(wsgi)
    app({"REQUEST_METHOD": "GET", "PATH_INFO": "/login"}, lambda *a, **k: None)
    assert seen["in_flight"] == 1, "the request was not counted while running"
    assert request_health.snapshot()["in_flight"] == 0, "not released after"


def test_tracked_releases_even_when_the_request_explodes():
    """A 500 must not leak a slot — leaked slots would fake a stall forever."""
    def wsgi(environ, start_response):
        raise RuntimeError("boom")

    app = request_health.Tracked(wsgi)
    with pytest.raises(RuntimeError):
        app({"REQUEST_METHOD": "GET", "PATH_INFO": "/x"}, lambda *a, **k: None)
    assert request_health.snapshot()["in_flight"] == 0


def test_snapshot_reports_the_oldest_request():
    _fake_inflight(1, age=120, path="/mail/sync", method="POST")
    snap = request_health.snapshot()
    assert snap["in_flight"] == 1
    assert snap["oldest_age"] >= 119
    assert snap["oldest"] == ("POST", "/mail/sync")


# ── the stall test ───────────────────────────────────────────────────────────

def test_a_busy_but_healthy_app_is_not_a_stall():
    """Saturated is not stalled: 8 fast requests are a busy morning."""
    _fake_inflight(8, age=0.5)
    assert not request_health.is_stalled()
    out = []
    request_health.check(out=out.append)
    assert not any("THREAD DUMP" in line for line in out)
    assert request_health.problems() == []


def test_one_slow_request_is_not_a_stall():
    _fake_inflight(1, age=600, path="/reports")
    assert not request_health.is_stalled()
    assert request_health.problems() == []


def test_saturated_and_aging_dumps_every_thread_stack():
    """THE regression: workers gone AND the oldest waiting = dump, now."""
    _fake_inflight(request_health.SATURATED_AT,
                   age=request_health.STALL_SECONDS + 5)
    out = []
    request_health.check(out=out.append)
    text = "\n".join(out)
    assert "THREAD DUMP" in text
    assert "END THREAD DUMP" in text
    # a real stack, not just a header — this thread is in the dump
    assert "test_saturated_and_aging_dumps_every_thread_stack" in text
    assert "--- thread " in text
    # and it says what was blocked
    assert "/api/pending_count" in text


def test_the_dump_does_not_repeat_every_tick():
    """A stall lasting hours must not fill the log with dumps."""
    _fake_inflight(request_health.SATURATED_AT,
                   age=request_health.STALL_SECONDS + 5)
    out = []
    for _ in range(5):
        request_health.check(out=out.append)
    assert "\n".join(out).count("==== THREAD DUMP") == 1


def test_the_incident_survives_so_the_banner_can_report_it():
    """During a total stall no page renders, so the notice that matters is the
    one shown AFTER recovery."""
    _fake_inflight(request_health.SATURATED_AT,
                   age=request_health.STALL_SECONDS + 5)
    request_health.check(out=lambda _m: None)
    live = request_health.problems()
    assert live and "queueing" in live[0]["text"]

    # everything clears; the app serves again
    with request_health._lock:
        request_health._inflight.clear()
    out = []
    request_health.check(out=out.append)
    assert any("recovered" in line for line in out)

    after = request_health.problems()
    assert after, "the stall must still be reported once pages can render"
    assert "stalled" in after[0]["text"]
    assert "klickity_klack.log" in after[0]["text"], "must point at the dump"


def test_an_old_incident_stops_nagging():
    _fake_inflight(request_health.SATURATED_AT,
                   age=request_health.STALL_SECONDS + 5)
    request_health.check(out=lambda _m: None)
    with request_health._lock:
        request_health._inflight.clear()
    request_health.check(out=lambda _m: None)
    assert request_health.problems()
    later = time.time() + request_health.INCIDENT_TTL + 60
    assert request_health.problems(now=later) == []


def test_the_watchdog_never_dies_on_an_error(monkeypatch):
    """A watchdog that raises is worse than none — it would go quiet."""
    monkeypatch.setattr(request_health, "snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    out = []
    t = threading.Thread(target=request_health._loop, args=(0.01, out.append),
                         daemon=True)
    t.start()
    time.sleep(0.1)
    assert any("watchdog error" in line for line in out)
    assert t.is_alive(), "the watchdog thread died on an error"


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_server_wraps_the_tracker_outside_the_access_log():
    """/static and /api/pending_count are skipped by the access log but still
    occupy a worker — the tracker has to see them, so it wraps outside."""
    import os
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8") as f:
        src = f.read()
    assert "request_health.start_watchdog()" in src
    assert "request_health.Tracked(_AccessLog(app.wsgi_app))" in src, (
        "Tracked must wrap the access log, not the other way round")


def test_the_banner_is_wired_into_both_looks():
    import os
    tpl = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "templates", "_chrome_shared.html")
    with open(tpl, encoding="utf-8") as f:
        html = f.read()
    assert "stall_alert" in html          # _chrome_shared is included by BOTH bases
    assert 'id="stall-alert"' in html
