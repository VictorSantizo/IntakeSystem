r"""
folder_index.py – folder browser + search over the file server, for the
"where does this document go" pickers (reports #25/#34/#37/#44).

Staff had to PASTE folder paths from File Explorer into free-text fields —
which broke across desks (T:/Z:/F: drive-letter drift, reports #33/#36) and
gave no way to look around. This serves:

  GET /api/folders?path=<dir>   children of a folder (live os.scandir — always
                                fresh), with breadcrumbs; no path = top-level
                                roots (the archive, the Filed tree, the rest of
                                the cabinet root minus excluded subtrees)
                                &files=1 also lists the DOCUMENTS in the folder,
                                for the file picker (#50) — folders alone can't
                                answer "start this record from that scan"
  GET /api/folders/search?q=    typeahead over a cached index of every folder
                                name on the file server (background-built,
                                refreshed every 15 min; {"building": true}
                                while the first build runs)

Every path is validated through filing.resolve_client_dir/validate_filed_dir —
the picker can only ever land somewhere a document is allowed to be filed.
Responses carry both the server path and the UNC display form (house rule:
staff always see \\LACKSSERVER2021\File-Cabinet\...).
"""
import os
import threading
import time

from flask import jsonify, request

import filing

_M = None

_IDX_TTL_SECS = 900

# What the file picker will show and hand to a form: the document formats the
# classifier can actually read. Anything else in the folder stays invisible —
# offering a .xlsx to "start a record from this document" only wastes a click.
_DOC_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".docx", ".doc")
_idx_lock = threading.Lock()
_idx = {"at": 0.0, "dirs": []}     # [(lowercase path, real path), ...]
_building = False


def _browse_roots(cfg):
    """[(label, path)] top-level picker entries. The archive and Filed trees
    get friendly labels; every other allowed folder under the cabinet root
    appears under its own name (Documents, RENT ROLLS, ...)."""
    out = []
    archive = (cfg.get("archive_folder") or "").strip()
    filed = (cfg.get("filed_folder") or "").strip()
    if archive and os.path.isdir(archive):
        out.append(("Files (the archive)", archive))
    if filed and os.path.isdir(filed):
        out.append(("Filed (Klick Klack's tree)", filed))
    croot = filing.cabinet_root(cfg)
    seen = {os.path.normcase(p) for _, p in out}
    try:
        names = sorted(os.listdir(croot), key=str.lower) if os.path.isdir(croot) else []
    except OSError:
        names = []
    for name in names:
        p = os.path.join(croot, name)
        if os.path.normcase(p) in seen or name.startswith(("_", "~", ".")):
            continue
        if not os.path.isdir(p) or not filing.validate_filed_dir(p, cfg):
            continue
        out.append((name, p))
    return out


def _crumbs(path, cfg):
    """Breadcrumb chain from the outermost still-valid ancestor down to path."""
    chain = []
    cur = os.path.normpath(path)
    while cur and filing.validate_filed_dir(cur, cfg):
        chain.append({"name": os.path.basename(cur) or cur, "path": cur})
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return list(reversed(chain))


def _entry(path, cfg, name=None):
    return {"name": name or os.path.basename(path) or path,
            "path": path, "unc": filing.display_path(path, cfg)}


def api_folders():
    cfg = _M.load_config()
    raw = (request.args.get("path") or "").strip()
    want_files = request.args.get("files") in ("1", "true", "yes")
    if not raw:
        # The roots listing is folders by construction — a document never sits
        # loose at the top of the file server.
        out = {"path": None, "crumbs": [],
               "dirs": [_entry(p, cfg, name=lbl) for lbl, p in _browse_roots(cfg)]}
        if want_files:
            out["files"] = []
        return jsonify(out)
    resolved = filing.resolve_client_dir(raw, cfg)
    if not resolved or not os.path.isdir(resolved):
        return jsonify({"error": "That folder isn't on the file server."}), 400
    dirs, files = [], []
    try:
        with os.scandir(resolved) as it:
            for d in it:
                if d.name.startswith(("_", "~", ".")):
                    continue
                if d.is_dir():
                    if len(dirs) < 400 and filing.validate_filed_dir(d.path, cfg):
                        dirs.append(_entry(d.path, cfg))
                elif want_files and len(files) < 400 and \
                        d.name.lower().endswith(_DOC_EXTS):
                    files.append(_entry(d.path, cfg))
                if len(dirs) >= 400 and (not want_files or len(files) >= 400):
                    break
    except OSError as e:
        return jsonify({"error": f"Could not read that folder: {e}"}), 400
    dirs.sort(key=lambda x: x["name"].lower())
    out = {"path": resolved,
           "unc": filing.display_path(resolved, cfg),
           "crumbs": _crumbs(resolved, cfg),
           "dirs": dirs}
    if want_files:
        files.sort(key=lambda x: x["name"].lower())
        out["files"] = files
    return jsonify(out)


# ── search index ──────────────────────────────────────────────────────────────

def _build_index():
    global _building
    try:
        cfg = _M.load_config()
        dirs = []
        for _lbl, root in _browse_roots(cfg):
            for dirpath, dirnames, _files in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(("_", "~", "."))]
                for d in dirnames:
                    p = os.path.join(dirpath, d)
                    dirs.append((p.lower(), p))
                if len(dirs) > 120_000:      # runaway guard; the tree is ~30-60k dirs
                    break
        with _idx_lock:
            _idx["dirs"] = dirs
            _idx["at"] = time.monotonic()
        print(f"[Folders] Indexed {len(dirs)} folder(s) for search.")
    except Exception as e:
        print(f"[Folders] index build failed: {e}")
    finally:
        _building = False


def _ensure_index() -> bool:
    """True if the index is usable now; kicks off a background (re)build when
    stale. Search stays responsive on the possibly-stale copy meanwhile."""
    global _building
    with _idx_lock:
        fresh = _idx["at"] and time.monotonic() - _idx["at"] < _IDX_TTL_SECS
        have = bool(_idx["dirs"])
        if not fresh and not _building:
            _building = True
            threading.Thread(target=_build_index, daemon=True).start()
    return have


def api_folders_search():
    cfg = _M.load_config()
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify({"results": [], "building": False})
    have = _ensure_index()
    if not have:
        return jsonify({"results": [], "building": True})
    words = q.split()
    with _idx_lock:
        snapshot = _idx["dirs"]
    hits = []
    for low, real in snapshot:
        if all(w in low for w in words):
            base = os.path.basename(low)
            rank = (0 if base.startswith(words[0]) else
                    (1 if words[-1] in base else 2), len(low))
            hits.append((rank, real))
            if len(hits) >= 400:
                break
    hits.sort(key=lambda h: h[0])
    return jsonify({"results": [_entry(p, cfg) for _r, p in hits[:25]],
                    "building": False})


def register(flask_app, app_module):
    global _M
    _M = app_module
    flask_app.add_url_rule("/api/folders", "api_folders", api_folders, methods=["GET"])
    flask_app.add_url_rule("/api/folders/search", "api_folders_search",
                           api_folders_search, methods=["GET"])
