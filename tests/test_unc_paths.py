r"""House rule, reports #33/#36: every staff-facing document path renders as a
full UNC (\\LACKSSERVER2021\File-Cabinet\...), never a drive letter.

Nancy's desk maps the cabinet as Z:, Karen's as T:, and F:\ exists only on the
server itself — so a path printed with a drive letter is wrong for somebody.
#33 was bounced back on 2026-08-04 because the Needs Review list (and the
duplicate side-by-side) were still printing the raw server path.

The sweep at the bottom is the root-cause pin: a new template that prints a
document path without the `unc` filter fails here rather than in front of
Nancy.
"""
import os
import re

import app as klr
import database
from conftest import make_pdf

from test_folder_browser import _cabinet

UNC = r"\\TESTSRV\Cabinet"


def _doc_in_cabinet(env, name="bill.pdf"):
    """A document living under the fake server root, plus its two path forms."""
    root = _cabinet(env)
    folder = os.path.join(root, "Files")
    path = os.path.join(folder, name)
    make_pdf(path, "ACME Utilities invoice")
    return path, UNC + "\\" + os.path.join("Files", name)


def test_needs_review_list_shows_unc_not_drive_letter(env, staff_client):
    """#33 bounce: the Needs Review queue printed the raw server path."""
    c, _ = staff_client
    path, unc = _doc_in_cabinet(env)
    klr._queue_add({"pdf_path": path, "vendor": "ACME Utilities"})
    html = c.get("/review").get_data(as_text=True)
    assert unc in html
    assert path not in html


def test_duplicate_compare_page_shows_unc_not_drive_letter(env, staff_client):
    """#33 bounce, second surface: the side-by-side duplicate check."""
    c, _ = staff_client
    scan_path, scan_unc = _doc_in_cabinet(env, "queued.pdf")
    filed_path, filed_unc = _doc_in_cabinet(env, "filed.pdf")
    sid = klr._queue_add({"pdf_path": scan_path, "vendor": "ACME Utilities",
                          "amount": "147.48"})
    eid = database.insert_entry({"vendor": "ACME Utilities", "amount": 147.48,
                                 "doc_type": "Bill", "date_received": "2026-07-06",
                                 "filed_path": filed_path})
    html = c.get(f"/review/{sid}/compare/{eid}").get_data(as_text=True)
    assert scan_unc in html and filed_unc in html
    assert scan_path not in html and filed_path not in html


# ── the root-cause pin ────────────────────────────────────────────────────────

_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
# Document paths only. config.json's own server folders (settings.html) stay
# F:\ on purpose — that editor writes the real server-local value.
_PATH_FIELDS = ("pdf_path", "filed_path", "left_path", "right_path")


def test_no_template_prints_a_document_path_without_the_unc_filter():
    tpl_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "templates")
    offenders = []
    for name in sorted(os.listdir(tpl_dir)):
        if not name.endswith(".html"):
            continue
        text = open(os.path.join(tpl_dir, name), encoding="utf-8").read()
        for m in _EXPR.finditer(text):
            expr = m.group(1)
            if not any(f in expr for f in _PATH_FIELDS):
                continue
            # `|unc` renders it correctly; `.split('\\')[-1]` is a bare
            # filename, which carries no drive letter at all.
            if "unc" in expr or ".split(" in expr:
                continue
            offenders.append(f"{name}:{text[:m.start()].count(chr(10)) + 1}: "
                             f"{expr.strip()[:120]}")
    assert not offenders, (
        "staff-facing document paths must render through the `unc` filter "
        "(house rule, reports #33/#36):\n  " + "\n  ".join(offenders))
