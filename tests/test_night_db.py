"""night_db is the only sanctioned way to read the live intake.db off-server.

These pin the two things that make it worth having: it cannot be talked into
writing, and it cannot strand an unattended run (report #102).
"""

import os
import sqlite3
import threading
import time

import pytest

import night_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "sample.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE bills (id INTEGER PRIMARY KEY, vendor TEXT, amount REAL)")
    conn.executemany(
        "INSERT INTO bills (vendor, amount) VALUES (?, ?)",
        [("Advanced Community Services", 115.0), ("Midvalley Station", 45.0)],
    )
    conn.commit()
    conn.close()
    return path


def test_reads_rows(db):
    rows = night_db.read("SELECT vendor, amount FROM bills ORDER BY amount", db_path=db)
    assert [r["vendor"] for r in rows] == ["Midvalley Station", "Advanced Community Services"]


def test_params_are_bound(db):
    rows = night_db.read("SELECT vendor FROM bills WHERE amount > ?", (100,), db_path=db)
    assert [r["vendor"] for r in rows] == ["Advanced Community Services"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE bills SET amount = 0",
        "DELETE FROM bills",
        "INSERT INTO bills (vendor) VALUES ('x')",
        "DROP TABLE bills",
        "  update bills set amount = 0",  # leading space + lowercase
        "ATTACH DATABASE 'other.db' AS other",
    ],
)
def test_refuses_anything_that_is_not_a_read(db, sql):
    with pytest.raises(night_db.NotAReadError):
        night_db.read(sql, db_path=db)


def test_connection_is_read_only_even_if_the_guard_were_bypassed(db):
    """Belt and braces: the URI itself forbids writing, not just the SQL check."""
    conn = sqlite3.connect(night_db._read_only_uri(db), uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE bills SET amount = 0")
    conn.close()


def test_the_read_only_uri_is_not_caller_supplied(db):
    # No argument anywhere lets a caller ask for a writable connection.
    assert "mode=ro" in night_db._read_only_uri(db)
    assert "mode=ro" in night_db._read_only_uri("\\\\SERVER\\share\\intake.db")


def test_wal_bytes_reports_zero_when_there_is_no_wal(db):
    assert night_db.wal_bytes(db) == 0


def test_wal_bytes_reports_a_live_wal(db):
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO bills (vendor, amount) VALUES ('Pending Write', 1.0)")
    conn.commit()
    try:
        assert night_db.wal_bytes(db) > 0
    finally:
        conn.close()


def test_a_stalled_read_fails_fast_instead_of_stranding(db, monkeypatch):
    """The whole point: a run must not hang for minutes on a busy database."""

    started = threading.Event()

    def glacial_connect(*args, **kwargs):
        started.set()
        time.sleep(30)  # abandoned; the thread is a daemon

    monkeypatch.setattr(night_db.sqlite3, "connect", glacial_connect)

    began = time.time()
    with pytest.raises(night_db.DatabaseBusy) as caught:
        night_db.read("SELECT 1", db_path=db, timeout=0.5)
    elapsed = time.time() - began

    assert started.is_set()
    assert elapsed < 5, "should have given up promptly, took %.1fs" % elapsed
    # The message has to tell an unattended run what to say in its report.
    assert "wal" in str(caught.value).lower()
    assert "#102" in str(caught.value)


def test_errors_from_the_worker_reach_the_caller(db):
    with pytest.raises(sqlite3.OperationalError):
        night_db.read("SELECT * FROM no_such_table", db_path=db)


def test_defaults_to_the_live_database_next_to_the_module():
    assert night_db.DB_PATH.endswith("intake.db")
    assert os.path.dirname(night_db.DB_PATH) == os.path.dirname(night_db.__file__)
