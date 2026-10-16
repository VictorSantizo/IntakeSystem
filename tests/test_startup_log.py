"""Startup log: every app start appends a line to STARTUP_LOG.txt with the
timestamp and the code version on disk, so a restart is verifiable instead of
guessed at."""
import app as klr


def test_startup_log_appends_lines(tmp_path, monkeypatch):
    path = tmp_path / "STARTUP_LOG.txt"
    monkeypatch.setattr(klr, "_STARTUP_LOG_PATH", str(path))
    klr._log_startup()
    klr._log_startup()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all("app started" in ln and "code " in ln for ln in lines)


def test_startup_log_trims_to_cap(tmp_path, monkeypatch):
    path = tmp_path / "STARTUP_LOG.txt"
    monkeypatch.setattr(klr, "_STARTUP_LOG_PATH", str(path))
    monkeypatch.setattr(klr, "_STARTUP_LOG_KEEP", 5)
    for _ in range(8):
        klr._log_startup()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5


def test_code_version_shape():
    """Reads the repo's .git directly (7-hex short hash), or '' where there is
    no .git (e.g. a bare deployment) — either is acceptable, a crash is not."""
    v = klr._code_version()
    assert v == "" or (len(v) == 7 and all(c in "0123456789abcdef" for c in v))
