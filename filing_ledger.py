r"""
filing_ledger.py – "that move was us" registry for the live cabinet watcher.

cabinet_watch.py watches Karen's archive trees for files that PEOPLE add or
move by hand. But the app itself files, re-files, attaches and archives
documents into those same trees all day — every one of those would look like a
fresh hand-filed arrival and get double-registered.

So every app code path that is about to move/copy/rename a document announces
the DESTINATION here first (one line at each choke point in app.py), and the
watcher drops arrivals that match a recent announcement. Entries expire after a
few minutes — long enough for a slow SMB move to complete and its filesystem
event to fire, short enough that a person later hand-moving the same file is
seen again.

In-process only (module-level state): the app and the watcher live in the same
Flask process on LACKSSERVER2021, which is also the only writer of intake.db.
"""
import os
import threading
import time

TTL_SECS = 300

_lock = threading.Lock()
_announced = {}   # normcase(normpath(path)) -> time.monotonic() of the announce


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


def announce(dest_path: str):
    """Record that the app is about to place a file at dest_path."""
    if not dest_path:
        return
    now = time.monotonic()
    with _lock:
        if len(_announced) > 5000:   # bound memory on a long-running server
            cutoff = now - TTL_SECS
            for k in [k for k, t in _announced.items() if t < cutoff]:
                del _announced[k]
        _announced[_key(dest_path)] = now


def is_app_move(path: str) -> bool:
    """True if `path` was announced within the TTL — i.e. this filesystem
    event is the app's own doing, not a hand-filed arrival."""
    with _lock:
        t = _announced.get(_key(path))
    return t is not None and (time.monotonic() - t) <= TTL_SECS
