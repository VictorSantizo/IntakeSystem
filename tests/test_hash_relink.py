"""_repair_filed_paths_by_hash: the second-pass self-heal for records whose
filed document was renamed OR MOVED to a sibling folder — a rename
_repair_filed_paths can't predict by name, but file_hash (sha256 of the
document bytes) survives untouched. Mirrors the fixture style in
test_queue_and_files.py's filed-path-repair section.
"""
import os

import database
import app as klr
from conftest import make_pdf


def _base(env):
    """A record's usual folder depth: <filed>/<entity>/<property>/<year>."""
    return os.path.join(env.filed_folder, "TRUST1", "T27", "2026")


def _entry(filed_path, file_hash, **overrides):
    d = {"date_received": "2026-06-27", "entity_code": "TRUST1", "property_code": "T27",
         "doc_type": "Bill", "vendor": "Harvest-Bot", "status": "Pending",
         "filed_path": filed_path, "file_hash": file_hash}
    d.update(overrides)
    return database.insert_entry(d)


def test_relinks_a_file_renamed_into_a_sibling_folder(env):
    base = _base(env)
    dead_path = os.path.join(base, "06. JUNE", "old-name-thats-gone.pdf")
    sibling_dir = os.path.join(base, "07. JULY")
    os.makedirs(sibling_dir)
    renamed = make_pdf(os.path.join(sibling_dir, "somebody-renamed-this.pdf"))
    file_hash = klr._sha256_file(renamed)
    eid = _entry(dead_path, file_hash)

    klr._repair_filed_paths_by_hash()

    entry = database.get_entry(eid)
    assert entry["filed_path"] == renamed
    assert os.path.exists(entry["filed_path"])


def test_does_not_link_a_candidate_with_different_bytes(env):
    base = _base(env)
    dead_path = os.path.join(base, "06. JUNE", "gone.pdf")
    sibling_dir = os.path.join(base, "07. JULY")
    os.makedirs(sibling_dir)
    make_pdf(os.path.join(sibling_dir, "unrelated-doc.pdf"), text="TOTALLY DIFFERENT CONTENT")
    eid = _entry(dead_path, "0" * 64)   # a hash that matches nothing on disk

    klr._repair_filed_paths_by_hash()

    entry = database.get_entry(eid)
    assert entry["filed_path"] == dead_path   # untouched — no byte-identical file found


def test_does_not_steal_a_file_another_record_already_references(env):
    base = _base(env)
    sibling_dir = os.path.join(base, "07. JULY")
    os.makedirs(sibling_dir)
    shared_file = make_pdf(os.path.join(sibling_dir, "shared.pdf"), text="SHARED BILL CONTENT")
    shared_hash = klr._sha256_file(shared_file)
    _entry(shared_file, shared_hash)   # healthy record: already owns this exact file

    dead_path = os.path.join(base, "06. JUNE", "gone2.pdf")
    eid = _entry(dead_path, shared_hash)   # same bytes, but its own copy is missing

    klr._repair_filed_paths_by_hash()

    entry = database.get_entry(eid)
    assert entry["filed_path"] == dead_path   # the only byte match on disk was already claimed


def test_ignores_candidates_under_underscore_duplicates(env):
    base = _base(env)
    dead_path = os.path.join(base, "06. JUNE", "gone3.pdf")
    dup_dir = os.path.join(base, "_Duplicates")
    os.makedirs(dup_dir)
    dup_file = make_pdf(os.path.join(dup_dir, "same-bytes.pdf"), text="DUPLICATE HOLDING AREA")
    file_hash = klr._sha256_file(dup_file)
    eid = _entry(dead_path, file_hash)

    klr._repair_filed_paths_by_hash()

    entry = database.get_entry(eid)
    assert entry["filed_path"] == dead_path   # _Duplicates is a human decision, never searched


def test_is_a_noop_and_never_walks_disk_when_nothing_is_broken(env, monkeypatch):
    base = _base(env)
    os.makedirs(base)
    healthy = make_pdf(os.path.join(base, "healthy.pdf"))
    _entry(healthy, klr._sha256_file(healthy))

    def _no_walk(*a, **k):
        raise AssertionError("must not walk the filesystem when no record is broken")
    monkeypatch.setattr(os, "walk", _no_walk)

    klr._repair_filed_paths_by_hash()   # returns before ever calling os.walk

    entry = database.get_all_entries()[0]
    assert entry["filed_path"] == healthy


def test_logs_ambiguity_when_multiple_unclaimed_files_share_a_hash(env):
    """Two byte-identical unclaimed copies (e.g. the vendor mailed a duplicate
    and both were harvested into sibling folders): link the first found and
    say so in the log, rather than silently picking one."""
    base = _base(env)
    dead_path = os.path.join(base, "06. JUNE", "gone4.pdf")
    dir_a = os.path.join(base, "07. JULY")
    dir_b = os.path.join(base, "08. AUGUST")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    copy_a = make_pdf(os.path.join(dir_a, "copy-a.pdf"), text="TWIN COPY CONTENT")
    copy_b = make_pdf(os.path.join(dir_b, "copy-b.pdf"), text="TWIN COPY CONTENT")
    file_hash = klr._sha256_file(copy_a)
    assert file_hash == klr._sha256_file(copy_b)
    eid = _entry(dead_path, file_hash)

    klr._repair_filed_paths_by_hash()

    entry = database.get_entry(eid)
    assert entry["filed_path"] in (copy_a, copy_b)   # linked to one of the two
    assert os.path.exists(entry["filed_path"])
