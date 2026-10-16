r"""request_health.py — the app notices when it is starving, and names the cause.

2026-08-11: Klickity accepted connections but served nothing for two and a half
hours. waitress's accept loop was fine (TCP connected in 0.02s), but all 8
worker threads sat blocked, so even a static CSS file never came back. From a
desk it looked like a dead site with no explanation. From inside, waitress
logged "Task queue depth is N" to a console window nobody was watching. When
whatever it was let go at 15:58, every queued request completed at once — a
request that started at 14:09:35 finished at 15:58:24 — which is the signature
of ONE lock held for hours. And by the time anyone looked, the evidence of
which lock was gone.

Diagnosing it afterwards was guesswork: the running code, the deploy history
and the archive all had to be eliminated one at a time, and the answer still
was not certain. That is the thing this module fixes. It does NOT try to
prevent the stall — it makes the stall describe itself:

  * every in-flight request is tracked (start time, method, path);
  * a watchdog thread notices when the workers are saturated AND the oldest
    request has been waiting too long;
  * when that trips it DUMPS EVERY THREAD'S STACK into klickity_klack.log,
    while the threads are still blocked. The blocking call writes itself down.
  * the incident is remembered, so the red banner can say a stall happened
    even though — by definition — no page could render during it.

Nothing here may block a request: the tracker holds a short lock around two
dict operations and never does I/O; the watchdog and the dump run on their own
daemon thread. If this module ever became the thing that hangs, it would be
the worst possible bug, so it stays boring on purpose.
"""
import itertools
import threading
import time
import traceback

# Must match the serve(threads=...) setting in app.py. Kept as a module global
# rather than read from waitress so the tests can drive it.
WORKER_THREADS = 8
SATURATED_AT = 6        # in-flight requests that mean the workers are gone
STALL_SECONDS = 60      # ...and the oldest has waited this long = a stall
DUMP_COOLDOWN = 600     # at most one stack dump per 10 minutes
INCIDENT_TTL = 6 * 3600  # how long the banner keeps reporting a past stall

_lock = threading.Lock()
_inflight = {}                       # token -> (started, method, path)
_seq = itertools.count(1)

_state = {
    "stalled_since": None,   # when the CURRENT stall began (None = healthy)
    "last_dump": 0.0,        # monotonic-ish guard for DUMP_COOLDOWN
    "incident": None,        # the most recent finished/ongoing stall
}


# ── tracking ──────────────────────────────────────────────────────────────────

class Tracked:
    """WSGI middleware: count in-flight requests. Wrap this OUTSIDE the access
    log so it also sees /static and /api/pending_count — those are skipped by
    the log but they still consume a worker thread, and during the 8/11 stall
    the 10-second pollers are the likeliest thing that ate all eight."""

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        token = next(_seq)
        entry = (time.time(), environ.get("REQUEST_METHOD", "?"),
                 environ.get("PATH_INFO", ""))
        with _lock:
            _inflight[token] = entry
        try:
            return self.wsgi(environ, start_response)
        finally:
            with _lock:
                _inflight.pop(token, None)


def snapshot(now=None):
    """What the workers are doing right now. Cheap and lock-short."""
    now = time.time() if now is None else now
    with _lock:
        items = list(_inflight.values())
    ages = sorted(((now - started), method, path)
                  for started, method, path in items)
    return {"in_flight": len(items),
            "oldest_age": ages[-1][0] if ages else 0.0,
            "oldest": ages[-1][1:] if ages else None,
            "requests": list(reversed(ages))[:10]}


def is_stalled(snap=None):
    snap = snapshot() if snap is None else snap
    return (snap["in_flight"] >= SATURATED_AT
            and snap["oldest_age"] >= STALL_SECONDS)


# ── the dump: the whole point of the module ───────────────────────────────────

def dump_threads(reason, out=print):
    """Every thread's stack, into the log. This is the artifact that makes the
    next stall diagnosable in one minute instead of an afternoon."""
    import sys
    names = {t.ident: t.name for t in threading.enumerate()}
    frames = sys._current_frames()
    out(f"[RequestHealth] ==== THREAD DUMP: {reason} ====")
    for ident, frame in frames.items():
        out(f"[RequestHealth] --- thread {names.get(ident, '?')!r} ({ident}) ---")
        try:
            stack = traceback.format_stack(frame)
        except Exception as e:                       # never raise from a dump
            out(f"[RequestHealth]     <no stack: {type(e).__name__}: {e}>")
            continue
        for chunk in stack:
            for line in chunk.rstrip().splitlines():
                out(f"[RequestHealth]     {line.rstrip()}")
    out("[RequestHealth] ==== END THREAD DUMP ====")


# ── the watchdog ──────────────────────────────────────────────────────────────

def check(now=None, out=print):
    """One watchdog tick. Returns the snapshot it acted on (for the tests)."""
    now = time.time() if now is None else now
    snap = snapshot(now=now)
    if is_stalled(snap):
        if _state["stalled_since"] is None:
            _state["stalled_since"] = now - snap["oldest_age"]
            out(f"[RequestHealth] STALL: {snap['in_flight']} of "
                f"{WORKER_THREADS} workers busy, oldest request "
                f"{snap['oldest_age']:.0f}s old ({snap['oldest']}). "
                f"Dumping thread stacks.")
        _state["incident"] = {
            "began": _state["stalled_since"],
            "seconds": now - _state["stalled_since"],
            "in_flight": snap["in_flight"],
            "oldest": snap["oldest"],
            "ended": None,
        }
        if now - _state["last_dump"] >= DUMP_COOLDOWN:
            _state["last_dump"] = now
            dump_threads(
                f"{snap['in_flight']}/{WORKER_THREADS} workers busy, oldest "
                f"{snap['oldest_age']:.0f}s: {snap['oldest']}", out=out)
    elif _state["stalled_since"] is not None:
        began = _state["stalled_since"]
        _state["stalled_since"] = None
        if _state["incident"]:
            _state["incident"]["ended"] = now
            _state["incident"]["seconds"] = now - began
        out(f"[RequestHealth] recovered after "
            f"{(now - began) / 60:.0f} min — requests are being served again.")
    return snap


def _loop(interval, out=print):
    while True:
        try:
            check(out=out)
        except Exception as e:                        # a watchdog never dies
            out(f"[RequestHealth] watchdog error: {type(e).__name__}: {e}")
        time.sleep(interval)


def start_watchdog(interval=15, out=print):
    t = threading.Thread(target=_loop, args=(interval, out),
                         daemon=True, name="request-health")
    t.start()
    return t


# ── the banner ────────────────────────────────────────────────────────────────

def problems(now=None):
    """Banner rows, same shape mail_health.check returns. A stall is reported
    while it is happening (a page that renders at all is proof the workers are
    not ALL gone) and for INCIDENT_TTL afterwards — during a total stall no
    page can render, so the after-the-fact notice is the one that gets seen."""
    now = time.time() if now is None else now
    inc = _state.get("incident")
    if not inc:
        return []
    ended = inc.get("ended")
    if ended is not None and now - ended > INCIDENT_TTL:
        return []
    mins = max(1, int(inc["seconds"] // 60))
    when = time.strftime("%H:%M", time.localtime(inc["began"]))
    if ended is None:
        text = (f"requests have been queueing since {when} ({mins} min) — "
                f"{inc['in_flight']} of {WORKER_THREADS} workers are blocked")
    else:
        text = (f"the app stalled for {mins} min from {when} and is serving "
                f"again — every request queued until it cleared")
    where = ""
    if inc.get("oldest"):
        where = f" Oldest blocked request: {inc['oldest'][0]} {inc['oldest'][1]}."
    return [{"account": "Klickity",
             "text": text + "." + where +
                     " A thread dump naming the blocked call is in "
                     "klickity_klack.log — search for THREAD DUMP."}]


def reset_for_tests():
    with _lock:
        _inflight.clear()
    _state.update({"stalled_since": None, "last_dump": 0.0, "incident": None})
