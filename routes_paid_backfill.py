r"""
The paid-backfill visual review — /paid-backfill (admin only).

Joshua (2026-08-06): "a side-by-side comparison kind of like the duplicate
side-by-side document tool, that way I don't just look through a CSV but I can
go through the documents visually." Reads the dry-run artifact
PAID_BACKFILL_<year>.csv (written by paid_backfill.py into _OPS INDEX), shows
each archive packet NEXT TO the record it would attach to (the compare.html
layout), and records a verdict per packet into
PAID_BACKFILL_<year>_DECISIONS.csv beside it.

Verdicts are the phase-2 apply's instructions — attach (to the cited record,
marking it Paid if Pending), create (its own already-Paid record), skip. The
DECISIONS file is append-only, last verdict per packet wins, so a change of
mind is one more click. NOTHING here writes the intake DB or touches archive
files: the only write is that CSV, next to the artifact it annotates.

Serving safety (same philosophy as routes_ops_reports): the document route
serves a row of the loaded CSV by INDEX — never a caller-supplied path — and
the row's UNC path must resolve (server_root_unc → server_root) to an existing
file inside the archive or cabinet root before a byte leaves the server.
"""
import csv
import os
import re
import threading
from datetime import datetime

from flask import (abort, redirect, render_template, request, send_file,
                   session, url_for)

import database
import filing

_M = None   # the app module (composition root); set by register()

_DEFAULT_OPS_DIR = r"F:\File-Cabinet\_OPS INDEX"
_CSV_RE = re.compile(r"^PAID_BACKFILL_(\d{4})\.csv$")
_VERDICTS = ("attach", "create", "skip")
# Dispositions a verdict makes sense for; the rest are informational.
_DECIDABLE = ("held", "match", "create-paid", "needs-ocr")
_DISP_ORDER = ("held", "match", "create-paid", "needs-ocr", "linked-by-hash",
               "known", "skip-movement", "skip-summary")

_decisions_lock = threading.Lock()


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/paid-backfill", "paid_backfill_list", adm(backfill_list))
    r("/paid-backfill/item/<int:idx>", "paid_backfill_item", adm(backfill_item))
    r("/paid-backfill/doc/<int:idx>", "paid_backfill_doc", adm(backfill_doc))
    r("/paid-backfill/decide/<int:idx>", "paid_backfill_decide",
      adm(backfill_decide), methods=["POST"])


def _ops_dir() -> str:
    try:
        override = _M.load_config().get("ops_index_dir")
    except Exception:
        override = None
    return override or _DEFAULT_OPS_DIR


def _years() -> list:
    try:
        names = os.listdir(_ops_dir())
    except OSError:
        return []
    return sorted((int(m.group(1)) for n in names if (m := _CSV_RE.match(n))),
                  reverse=True)


def _year_from_request():
    years = _years()
    try:
        y = int(request.args.get("year", ""))
    except ValueError:
        y = None
    return (y if y in years else (years[0] if years else None)), years


def _load_rows(year: int) -> list:
    path = os.path.join(_ops_dir(), f"PAID_BACKFILL_{year}.csv")
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    for i, r in enumerate(rows):
        r["idx"] = i
        r["basename"] = os.path.basename(r.get("unc_path") or "")
    return rows


def _decisions_path(year: int) -> str:
    return os.path.join(_ops_dir(), f"PAID_BACKFILL_{year}_DECISIONS.csv")


_DECISION_COLS = ("unc_path", "verdict", "matched_id", "note",
                  "decided_by", "decided_at")


def _load_decisions(year: int) -> dict:
    """{unc_path: decision-row} — append-only file, LAST row per path wins."""
    try:
        with open(_decisions_path(year), encoding="utf-8-sig", newline="") as f:
            out = {}
            for r in csv.DictReader(f):
                if r.get("unc_path"):
                    out[r["unc_path"]] = r
            return out
    except OSError:
        return {}


def _save_decision(year: int, row: dict):
    path = _decisions_path(year)
    with _decisions_lock:
        fresh = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(_DECISION_COLS)
            w.writerow([row.get(c, "") for c in _DECISION_COLS])


def _local_doc_path(unc_path: str, cfg: dict):
    r"""The server-local file behind a CSV row's UNC path, or None if it isn't
    a legal, existing document. Inverse of filing.display_path: swap the UNC
    prefix (\\LACKSSERVER2021\File-Cabinet) for the server root (F:\...), then
    demand the result exists inside the archive or cabinet root."""
    p = os.path.normpath((unc_path or "").strip())
    if not p:
        return None
    unc = os.path.normpath((cfg.get("server_root_unc")
                            or r"\\LACKSSERVER2021\File-Cabinet").rstrip("\\/"))
    root = (cfg.get("server_root") or r"F:\File-Cabinet").rstrip("\\/")
    if os.path.normcase(p).startswith(os.path.normcase(unc) + os.sep):
        p = root + p[len(unc):]
    allowed = (filing._is_under(p, cfg.get("archive_folder", ""))
               or filing._is_under(p, filing.cabinet_root(cfg)))
    if allowed and os.path.isfile(p):
        return p
    return None


def _matched_entry(row):
    try:
        return database.get_entry(int(row.get("matched_id") or ""))
    except (ValueError, TypeError):
        return None


def backfill_list():
    year, years = _year_from_request()
    if year is None:
        return render_template("paid_backfill.html", year=None, years=[],
                               rows=[], groups=[], decisions={}, disp="",
                               msg="No dry-run artifact found — run "
                                   "paid_backfill.py first.")
    rows = _load_rows(year)
    decisions = _load_decisions(year)
    disp = request.args.get("disp", "held")
    if disp not in {r["disposition"] for r in rows} | set(_DISP_ORDER):
        disp = "held"
    groups = []
    for d in _DISP_ORDER:
        in_d = [r for r in rows if r["disposition"] == d]
        if not in_d:
            continue
        done = sum(1 for r in in_d if r["unc_path"] in decisions)
        groups.append(dict(disp=d, total=len(in_d), done=done,
                           decidable=d in _DECIDABLE))
    shown = [r for r in rows if r["disposition"] == disp]
    return render_template("paid_backfill.html", year=year, years=years,
                           rows=shown, groups=groups, decisions=decisions,
                           disp=disp, msg=request.args.get("msg", ""))


def backfill_item(idx):
    year, _years_ = _year_from_request()
    rows = _load_rows(year) if year else []
    if not (0 <= idx < len(rows)):
        return redirect(url_for("paid_backfill_list"))
    row = rows[idx]
    decisions = _load_decisions(year)
    entry = _matched_entry(row)
    cfg = _M.load_config()
    doc_ok = _local_doc_path(row.get("unc_path"), cfg) is not None
    disp = request.args.get("disp", row["disposition"])
    peers = [r for r in rows if r["disposition"] == disp]
    pos = next((i for i, r in enumerate(peers) if r["idx"] == idx), 0)
    prev_idx = peers[pos - 1]["idx"] if pos > 0 else None
    next_idx = peers[pos + 1]["idx"] if pos + 1 < len(peers) else None
    return render_template("paid_backfill_item.html", year=year, row=row,
                           idx=idx, entry=entry, doc_ok=doc_ok, disp=disp,
                           decision=decisions.get(row["unc_path"]),
                           decidable=row["disposition"] in _DECIDABLE,
                           pos=pos + 1, total=len(peers),
                           prev_idx=prev_idx, next_idx=next_idx)


def backfill_doc(idx):
    year, _years_ = _year_from_request()
    rows = _load_rows(year) if year else []
    if not (0 <= idx < len(rows)):
        abort(404)
    path = _local_doc_path(rows[idx].get("unc_path"), _M.load_config())
    if not path:
        abort(404)
    return send_file(path)


def backfill_decide(idx):
    year, _years_ = _year_from_request()
    rows = _load_rows(year) if year else []
    if not (0 <= idx < len(rows)):
        return redirect(url_for("paid_backfill_list"))
    row = rows[idx]
    verdict = request.form.get("verdict", "")
    if verdict not in _VERDICTS or row["disposition"] not in _DECIDABLE:
        return redirect(url_for("paid_backfill_item", idx=idx, year=year))
    if verdict == "attach" and not row.get("matched_id"):
        return redirect(url_for("paid_backfill_item", idx=idx, year=year))
    _save_decision(year, {
        "unc_path": row["unc_path"], "verdict": verdict,
        "matched_id": row.get("matched_id") or "",
        "note": (request.form.get("note") or "").strip()[:500],
        "decided_by": session.get("user", ""),
        "decided_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    # Walk-through flow (like review's auto-advance): next UNDECIDED packet in
    # the same disposition, else back to the list with a done note.
    disp = request.args.get("disp", row["disposition"])
    decisions = _load_decisions(year)
    nxt = next((r for r in rows if r["disposition"] == disp
                and r["unc_path"] not in decisions), None)
    if nxt is not None:
        return redirect(url_for("paid_backfill_item", idx=nxt["idx"],
                                year=year, disp=disp))
    return redirect(url_for("paid_backfill_list", year=year, disp=disp,
                            msg=f"All {disp} packets are decided."))
