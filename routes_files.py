r"""routes_files.py — the Files view: the real cabinet tree, in the app.

Klick Klack has grown into the office's document manager (Joshua, 2026-08-05:
"a place where the staff can go to find documents"), but until now the app
only showed documents the DATABASE knew, arranged by the database's own ideas
(entity drawers, month tabs). This page is the inverse: it renders the actual
folder structure staff have used for 13 years — read straight off the file
server, live — and overlays what the app knows about each file:

    registered   -> a chip with the record's status / doc type / number,
                    linking to the record view (joined on filed_path/pdf_path)
    a copy       -> "copy of <record>" via the attachments table
    unregistered -> "on disk only" — visible even though no record exists,
                    which doubles as the archive-backfill to-do list

Read-only by design: no move, no rename, no delete — browsing must never be
able to reorganize the cabinet. The pieces it composes already existed:
folder_index's browse roots + crumbs (reports #25/#34/#37/#44), filing's
resolve/validate/display_path (the UNC house rule, #33/#36), and
desk_commands' open_explorer (report #22) for the "show me the real folder"
jump. /files/open streams a file for viewing, with the same containment
validation as every other path that touches the cabinet.
"""
import os
import time

from flask import abort, request, send_file

import filing
import folder_index
from database import get_attachments_for_paths, get_entries_for_paths
from routes_newlook import render_skinned

_M = None

_LIST_CAP = 500          # entries shown per folder before "…and N more"
_COUNT_CAP = 200         # per-subfolder child counts stop counting here
_COUNT_SKIP_ABOVE = 150  # a folder with more subfolders than this skips counts
                         # (one scandir per subfolder over SMB adds up)
_SEARCH_RESULT_CAP = 200
_SEARCH_DIR_CAP = 3000   # walked directories per search, runaway guard


def register(app, appmod):
    global _M
    _M = appmod
    app.add_url_rule("/files", "files_view", files_view)
    app.add_url_rule("/files/open", "files_open", files_open)


def _hidden(name: str) -> bool:
    return name.startswith(("_", "~", "."))


def _human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}"
    return ""


def _records_overlay(paths, cfg):
    """{normcase(path): chip dict} for every path the database knows —
    registered records first, attachment copies second (a path that is both
    stays a record)."""
    overlay = {}
    for e in get_entries_for_paths(paths):
        chip = {"kind": "record", "id": e["id"], "record_no": e.get("record_no") or "",
                "status": e.get("status") or "", "doc_type": e.get("doc_type") or "",
                "amount": e.get("amount")}
        for key in ("filed_path", "pdf_path"):
            p = e.get(key) or ""
            if p:
                overlay.setdefault(os.path.normcase(os.path.normpath(p)), chip)
    for a in get_attachments_for_paths(paths):
        p = os.path.normcase(os.path.normpath(a.get("file_path") or ""))
        overlay.setdefault(p, {"kind": "copy", "id": a["entry_id"],
                               "record_no": a.get("record_no") or "",
                               "status": a.get("status") or "",
                               "doc_type": a.get("doc_type") or "", "amount": None})
    return overlay


def _file_row(path, name, st):
    return {"name": name, "path": path,
            "size": _human_size(getattr(st, "st_size", None)),
            "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
                     if getattr(st, "st_mtime", None) else "",
            "is_pdf": name.lower().endswith(".pdf")}


def _list_folder(resolved):
    """(dirs, files, more) — one live scandir, capped, app holding areas and
    temp litter hidden (same exclusions the pickers use)."""
    dirs, files, more = [], [], 0
    with os.scandir(resolved) as it:
        for entry in it:
            if _hidden(entry.name):
                continue
            if len(dirs) + len(files) >= _LIST_CAP:
                more += 1
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": entry.path, "count": None})
                else:
                    files.append(_file_row(entry.path, entry.name, entry.stat()))
            except OSError:
                continue
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    if len(dirs) <= _COUNT_SKIP_ABOVE:
        for d in dirs:
            try:
                with os.scandir(d["path"]) as it:
                    n = 0
                    for entry in it:
                        if _hidden(entry.name):
                            continue
                        n += 1
                        if n >= _COUNT_CAP:
                            break
                    d["count"] = f"{_COUNT_CAP}+" if n >= _COUNT_CAP else n
            except OSError:
                d["count"] = None
    return dirs, files, more


def _search_folder(resolved, q):
    """Filename search under the current folder — every word must appear.
    Capped and honest about it: the caller shows 'first N' when truncated."""
    words = [w for w in q.lower().split() if w]
    hits, visited, truncated = [], 0, False
    for dirpath, dirnames, filenames in os.walk(resolved):
        dirnames[:] = [d for d in dirnames if not _hidden(d)]
        visited += 1
        if visited > _SEARCH_DIR_CAP:
            truncated = True
            break
        for name in filenames:
            if _hidden(name):
                continue
            low = name.lower()
            if all(w in low for w in words):
                p = os.path.join(dirpath, name)
                try:
                    hits.append(_file_row(p, name, os.stat(p)))
                except OSError:
                    continue
                if len(hits) >= _SEARCH_RESULT_CAP:
                    truncated = True
                    break
        if len(hits) >= _SEARCH_RESULT_CAP:
            break
    return hits, truncated


def files_view():
    cfg = _M.load_config()
    raw = (request.args.get("p") or "").strip()
    q = (request.args.get("q") or "").strip()

    if not raw:
        roots = [{"name": lbl, "path": p, "count": None}
                 for lbl, p in folder_index._browse_roots(cfg)]
        return render_skinned("files.html", crumbs=[], dirs=roots, files=[],
                              here=None, here_unc="", q="", more=0,
                              truncated=False, at_roots=True)

    resolved = filing.resolve_client_dir(raw, cfg)
    if not resolved or not os.path.isdir(resolved):
        abort(404)

    if q:
        files, truncated = _search_folder(resolved, q)
        dirs, more = [], 0
    else:
        dirs, files, more = _list_folder(resolved)
        truncated = False

    overlay = _records_overlay([f["path"] for f in files], cfg)
    for f in files:
        f["chip"] = overlay.get(os.path.normcase(os.path.normpath(f["path"])))

    return render_skinned("files.html",
                          crumbs=folder_index._crumbs(resolved, cfg),
                          dirs=dirs, files=files, here=resolved,
                          here_unc=filing.display_path(resolved, cfg),
                          q=q, more=more, truncated=truncated, at_roots=False)


def files_open():
    """Stream a cabinet file for viewing (PDFs render in the browser). The
    parent folder must resolve inside the cabinet — same containment rule as
    everything else that touches these paths — and nothing is ever written."""
    cfg = _M.load_config()
    raw = (request.args.get("p") or "").strip()
    if not raw:
        abort(404)
    parent = filing.resolve_client_dir(os.path.dirname(raw), cfg)
    if not parent:
        abort(404)
    path = os.path.join(parent, os.path.basename(raw))
    if _hidden(os.path.basename(path)) or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=False, conditional=True)
