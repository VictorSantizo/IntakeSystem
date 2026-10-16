"""Read-only access to the live intake.db from a machine that is NOT the server.

The house rule is that `intake.db` is never written from a workstation: the
database lives on the server, and SQLite's WAL mode needs real shared memory,
which does not exist across an SMB share. Writing it from a mapped drive
corrupts it.

Until now that rule was enforced by whoever typed the query remembering to put
`?mode=ro` in the connection string by hand, every single time. This module
makes it structural instead: there is no way to ask it for a writable
connection, and it refuses any statement that is not a read.

It also bounds how long a read may take. Measured 2026-08-06 (report #102):
when the server app is mid-write the WAL is non-empty, and a reader on a
workstation has to rebuild the WAL index over SMB while the writer holds the
lock -- reads that normally take 0.1s stalled for two and five minutes. A
`busy_timeout` does not help, because the stall is in opening the database, not
in executing the statement. So the work runs on a daemon thread and is
abandoned if it overruns, which turns a stranded unattended run into a clear,
fast "the app is busy" that the caller can report and move on from.

    from night_db import read, wal_bytes

    rows = read("SELECT id, vendor FROM intake_log WHERE amount > ?", (100,))

Import it from a script that lives in this folder. The bundled interpreter
pins sys.path with `python/python314._pth`, so cwd and PYTHONPATH are both
ignored; only the directory of the running script gets added.

A non-empty WAL on its own does NOT mean a read will stall -- measured here at
922 KB with reads still landing in 0.03s. It takes an *active* writer. So
`wal_bytes()` is for explaining a stall after the fact, not for predicting one;
the timeout is what actually protects an unattended run.

Anything that needs to *write*, or to read reliably while staff are working,
belongs on the server, not here.
"""

import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "intake.db")

# Generous enough for any real query against this database (a full scan measures
# ~0.1s), short enough that an unattended run reports and moves on.
DEFAULT_TIMEOUT = 20.0

_READ_PREFIXES = ("select", "with", "pragma", "explain")


class DatabaseBusy(RuntimeError):
    """The read could not finish in time -- the server app is probably writing."""


class NotAReadError(ValueError):
    """Refused: this module only reads."""


def wal_bytes(db_path=DB_PATH):
    """Size of the -wal file. Non-zero means the app has uncheckpointed writes."""
    try:
        return os.path.getsize(db_path + "-wal")
    except OSError:
        return 0


def _check_is_read(sql):
    stripped = sql.lstrip().lstrip("(").lstrip()
    if not stripped.lower().startswith(_READ_PREFIXES):
        raise NotAReadError(
            "night_db only reads; refused: %r. Writes to intake.db must happen "
            "on the server, never from a workstation." % sql.strip()[:60]
        )


def _read_only_uri(db_path):
    # Forward slashes and the file: scheme so this works for UNC and mapped paths
    # alike. mode=ro is not optional and not caller-supplied -- that is the point.
    return "file:%s?mode=ro" % str(db_path).replace("\\", "/")


def read(sql, params=(), db_path=DB_PATH, timeout=DEFAULT_TIMEOUT):
    """Run one read against the live database and return a list of sqlite3.Row.

    Raises NotAReadError if `sql` is not a read, and DatabaseBusy if the whole
    thing (open included) does not finish within `timeout` seconds.
    """
    _check_is_read(sql)

    result = {}

    def work():
        try:
            conn = sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=timeout)
            conn.row_factory = sqlite3.Row
            try:
                result["rows"] = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except BaseException as exc:  # handed back to the calling thread below
            result["error"] = exc

    # Daemon, so that if it is still stuck inside SQLite when we give up, an
    # abandoned thread cannot keep the interpreter alive.
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        raise DatabaseBusy(
            "Read of %s did not finish in %.0fs and was abandoned. The -wal file "
            "is %d KB, so the server app is most likely mid-write; a workstation "
            "cannot read a WAL database over SMB while that is happening "
            "(report #102). Try again when the app is idle, or get the data from "
            "the server." % (db_path, timeout, wal_bytes(db_path) // 1024)
        )
    if "error" in result:
        raise result["error"]
    return result["rows"]
