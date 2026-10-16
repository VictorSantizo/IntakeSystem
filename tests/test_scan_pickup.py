"""Scan pickup reliability (reports #42/#48): over-long OCR filenames must not
be silently dropped, renamed-into-place files must be ingested, and files the
pipeline still missed must be visible on /scan instead of invisible."""
import os
import time
import types

from conftest import make_pdf

import app as klr
import longpaths
import scanflow
import watcher


def _stub_classifier(monkeypatch, doc_type="Bill"):
    monkeypatch.setattr(klr, "classify_document",
                        lambda path: {"doc_type": doc_type, "vendor": "ACME",
                                      "confidence": 0.4})
    monkeypatch.setattr(klr, "split_if_needed", lambda path, key: [path])


def _long_name_pdf(folder, target_len=300):
    """Create a PDF whose FULL path is over MAX_PATH, like ScanSnap's
    name-by-recognized-text files. Written via the extended-length prefix
    because plain open() can't reach it either."""
    room = target_len - len(str(folder)) - 1 - 4
    name = ("Assessment Type 2026 Market Value " * 20)[:room].strip() + ".pdf"
    path = os.path.join(str(folder), name)
    tmp = os.path.join(str(folder), "short_stage.pdf")
    make_pdf(tmp)
    os.rename(longpaths.extended(tmp), longpaths.extended(path))
    assert len(path) > 260
    return path


# ── longpaths helpers ─────────────────────────────────────────────────────────

def test_longpaths_probes_survive_max_path(tmp_path):
    p = _long_name_pdf(tmp_path)
    assert longpaths.isfile(p)
    assert longpaths.exists(p)
    assert longpaths.getsize(p) > 0
    assert longpaths.getmtime(p) > 0


def test_rescue_long_name_shortens_in_place(tmp_path):
    p = _long_name_pdf(tmp_path)
    out = longpaths.rescue_long_name(p)
    assert out != p
    assert len(out) <= longpaths.SAFE_PATH_LEN
    assert os.path.isfile(out)                  # plain os.path works again
    assert os.path.dirname(out) == str(tmp_path)
    assert out.lower().endswith(".pdf")


def test_rescue_leaves_normal_names_alone(tmp_path):
    p = make_pdf(tmp_path / "normal.pdf")
    assert longpaths.rescue_long_name(p) == p


def test_rescue_does_not_clobber_existing(tmp_path):
    p1 = _long_name_pdf(tmp_path)
    out1 = longpaths.rescue_long_name(p1)
    p2 = _long_name_pdf(tmp_path)
    out2 = longpaths.rescue_long_name(p2)
    assert out2 != out1
    assert os.path.isfile(out1) and os.path.isfile(out2)


# ── watcher: renamed-into-place files ─────────────────────────────────────────

def _handler(folder, hits):
    return watcher.ScanHandler(lambda p: hits.append(p), str(folder))


def test_on_moved_ingests_rename_into_watched_folder(tmp_path):
    hits = []
    h = _handler(tmp_path, hits)
    dest = make_pdf(tmp_path / "Banc of California.pdf")
    h.on_moved(types.SimpleNamespace(is_directory=False,
                                     src_path=str(tmp_path / "garbage-ocr-name.pdf"),
                                     dest_path=dest))
    assert hits == [dest]


def test_on_moved_ignores_exit_to_subfolder(tmp_path):
    """A file moved INTO _Blank/_Duplicates is leaving the flow, not arriving."""
    hits = []
    h = _handler(tmp_path, hits)
    sub = tmp_path / "_Duplicates"
    sub.mkdir()
    dest = make_pdf(sub / "dupe.pdf")
    h.on_moved(types.SimpleNamespace(is_directory=False,
                                     src_path=str(tmp_path / "dupe.pdf"),
                                     dest_path=dest))
    assert hits == []


def test_on_moved_ignores_unsupported_and_dirs(tmp_path):
    hits = []
    h = _handler(tmp_path, hits)
    h.on_moved(types.SimpleNamespace(is_directory=True,
                                     src_path=str(tmp_path / "a"),
                                     dest_path=str(tmp_path / "b")))
    h.on_moved(types.SimpleNamespace(is_directory=False,
                                     src_path=str(tmp_path / "x.tmp"),
                                     dest_path=str(tmp_path / "y.tmp")))
    assert hits == []


def test_on_created_still_ingests(tmp_path):
    hits = []
    h = _handler(tmp_path, hits)
    p = make_pdf(tmp_path / "fresh.pdf")
    h.on_created(types.SimpleNamespace(is_directory=False, src_path=p))
    assert hits == [p]


# ── enqueue + sweep: long-named files reach the queue ─────────────────────────

def test_enqueue_rescues_long_name(env, monkeypatch):
    _stub_classifier(monkeypatch)
    p = _long_name_pdf(env.scan_folder)
    klr.enqueue_scan(p, "Physical Mail")
    queue = list(klr.pending_scans.values())
    assert len(queue) == 1
    assert len(queue[0]["pdf_path"]) < 260
    assert os.path.isfile(queue[0]["pdf_path"])


def test_sweep_picks_up_long_named_file(env, monkeypatch):
    _stub_classifier(monkeypatch)
    p = _long_name_pdf(env.scan_folder)
    old = time.time() - 600
    os.utime(longpaths.extended(p), (old, old))
    klr._sweep_unprocessed(env.scan_folder, "")
    assert len(klr.pending_scans) == 1


def test_sweep_skips_fresh_files(env, monkeypatch):
    """A file younger than the min age is probably mid-ingest by the watcher."""
    _stub_classifier(monkeypatch)
    make_pdf(os.path.join(env.scan_folder, "just-landed.pdf"))
    klr._sweep_unprocessed(env.scan_folder, "")
    assert len(klr.pending_scans) == 0


def test_sweep_backs_off_poison_files(env, monkeypatch):
    monkeypatch.setattr(klr, "split_if_needed", lambda path, key: [path])

    def boom(path):
        raise RuntimeError("poison")
    monkeypatch.setattr(klr, "classify_document", boom)
    p = make_pdf(os.path.join(env.scan_folder, "poison.pdf"))
    old = time.time() - 600
    os.utime(p, (old, old))
    klr._sweep_failures.clear()
    for _ in range(klr._SWEEP_MAX_TRIES + 2):
        klr._sweep_unprocessed(env.scan_folder, "")
    assert klr._sweep_failures.get(p) == klr._SWEEP_MAX_TRIES
    assert len(klr.pending_scans) == 0


# ── /scan surfacing of stranded files ─────────────────────────────────────────

def test_scan_status_reports_stranded(staff_client):
    c, headers = staff_client
    cfg_folder = klr.load_config()["scan_folder"]
    p = make_pdf(os.path.join(cfg_folder, "forgotten.pdf"))
    old = time.time() - 600
    os.utime(p, (old, old))
    r = c.get("/api/scan/status")
    stranded = r.get_json()["stranded"]
    assert [f["name"] for f in stranded] == ["forgotten.pdf"]
    assert stranded[0]["age_minutes"] >= 9


def test_scan_status_ignores_fresh_and_queued(staff_client, monkeypatch):
    c, headers = staff_client
    cfg_folder = klr.load_config()["scan_folder"]
    fresh = make_pdf(os.path.join(cfg_folder, "fresh.pdf"))          # too young
    queued = make_pdf(os.path.join(cfg_folder, "queued.pdf"))        # known to queue
    old = time.time() - 600
    os.utime(queued, (old, old))
    klr.pending_scans[1] = {"pdf_path": queued}
    r = c.get("/api/scan/status")
    assert r.get_json()["stranded"] == []
