"""The Filtered area — email docs the AI judged junk, recoverable and never
hard-deleted — extracted from app.py in the 2026-07-29 modular split (GH #6).
Same pattern as routes_karen.py (see its docstring). Push-back-to-review goes
through _M._queue_add (the composition root still owns the queue); discard
moves the file to _Trash beside it, never a hard delete.
"""
import json
import os

from flask import redirect, render_template, request, send_file, url_for

from database import delete_filtered_doc, get_filtered_doc, get_filtered_docs

_M = None   # the app module (composition root); set by register()


def register(app, appmod):
    global _M
    _M = appmod
    r = app.add_url_rule
    r("/filtered", "filtered_queue", filtered_queue)
    r("/filtered/<int:fid>/review", "filtered_to_review", filtered_to_review, methods=["POST"])
    r("/filtered/<int:fid>/discard", "filtered_discard", filtered_discard, methods=["POST"])
    r("/filtered/clear", "filtered_clear", filtered_clear, methods=["POST"])
    r("/filtered/preview/<int:fid>", "filtered_preview", filtered_preview)


def filtered_queue():
    return render_template("filtered.html", items=get_filtered_docs())


def filtered_to_review(fid):
    rec = get_filtered_doc(fid)
    if rec:
        try:
            data = json.loads(rec.get("data") or "{}")
        except Exception:
            data = {}
        data["pdf_path"] = rec.get("file_path")
        data.setdefault("mail_type", "Email")
        _M._queue_add(data)
        delete_filtered_doc(fid)
    return redirect(url_for("filtered_queue"))


def filtered_discard(fid):
    # Discard = move to _Trash beside the file, never a hard delete: any staff
    # member can discard with one confirm, and a mis-filtered real bill must
    # remain recoverable by hand.
    rec = get_filtered_doc(fid)
    if rec:
        p = rec.get("file_path")
        if p and os.path.exists(p):
            _M._move_aside(p, "_Trash")
        delete_filtered_doc(fid)
    return redirect(url_for("filtered_queue"))


def filtered_clear():
    for rec in get_filtered_docs():
        p = rec.get("file_path")
        if p and os.path.exists(p):
            _M._move_aside(p, "_Trash")
        delete_filtered_doc(rec["id"])
    return redirect(url_for("filtered_queue"))


def filtered_preview(fid):
    rec = get_filtered_doc(fid)
    if not rec or not rec.get("file_path") or not os.path.exists(rec["file_path"]):
        return "File not found", 404
    return send_file(rec["file_path"])
