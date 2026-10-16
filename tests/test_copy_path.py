"""Copy path really copies — report #92 (Joshua).

"The 'copy path' button does not work. Using a workaround, when I highlight the
path, right click copy, I can paste that path in a web browser; however, when I
try to paste that path into File Explorer it will not appear... When I paste it
into notepad, to strip away any formatting, and then copy it from there it
easily pastes inside the File Explorer address bar."

Root cause: the button reached straight for navigator.clipboard, which does not
exist at all on an insecure origin — and the app is served over plain HTTP on
the LAN. So the button never copied anything and the only way through was
hand-selecting the path off the page, which carries HTML formatting that File
Explorer's address bar rejects. The record-number chip already had the
hidden-textarea fallback; now there is one copy helper and everything uses it.
"""
import os
import re

import pytest

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates")


def _read(*parts):
    with open(os.path.join(TEMPLATES, *parts), encoding="utf-8") as f:
        return f.read()


def test_the_shared_helper_falls_back_when_there_is_no_clipboard_api():
    js = _read("_chrome_page_js.html")
    assert "function copyTextToClipboard" in js
    helper = js.split("function copyTextToClipboard", 1)[1].split("\n  window.", 1)[0]
    assert "execCommand('copy')" in helper, "no plain-HTTP fallback"
    assert "window.isSecureContext" in helper, "must not trust clipboard on http://"


@pytest.mark.parametrize("template", ["entry_view.html", "compare.html",
                                      "files.html", "paid_backfill_item.html"])
def test_copy_buttons_go_through_the_shared_helper(template):
    """Every Copy path button is the app-wide data-copy-path pair."""
    text = _read(template)
    assert "data-copy-path=" in text, f"{template} has no shared copy button"
    assert "navigator.clipboard" not in re.sub(r"\{#.*?#\}", "", text, flags=re.S), (
        f"{template} calls navigator.clipboard directly again — "
        "on plain HTTP that object does not exist (report #92)")


# ── Report #22 bounce (Joshua, 2026-08-10): "Copy Path still not working. the
# button name should change from 'Copy Path' 'Path Copied' after it attaches to
# the clipboard." Three separate faults behind that one sentence. ────────────

def test_the_button_says_path_copied_and_never_fails_silently():
    """The label Joshua asked for, and an honest answer when the copy fails."""
    js = _read("_chrome_page_js.html")
    assert "function copyPathButton" in js
    helper = js.split("function copyPathButton", 1)[1].split("\n  }", 1)[0]
    assert "Path Copied" in helper, "the button must say so after it copies"
    assert "window.prompt" in helper, (
        "a refused clipboard must hand the path over, not fail silently")
    assert "dataset.copyLabel" in helper, (
        "restore the ORIGINAL label — clicking twice inside the restore window "
        "must not strand the button reading 'Path Copied'")


def test_every_copy_button_reports_back():
    """No page may invent its own quiet copy button again."""
    offenders = []
    for root, _dirs, files in os.walk(TEMPLATES):
        for name in files:
            if not name.endswith(".html") or name == "_chrome_page_js.html":
                continue
            with open(os.path.join(root, name), encoding="utf-8") as f:
                text = re.sub(r"\{#.*?#\}", "", f.read(), flags=re.S)
            for line in text.splitlines():
                code = line.split("//", 1)[0]
                if "copyTextToClipboard(" in code and "copyPathButton" not in code:
                    offenders.append(f"{name}: {line.strip()[:90]}")
    assert not offenders, (
        "copy-path buttons must go through copyPathButton so they show "
        "'Path Copied' and fall back when the clipboard refuses:\n"
        + "\n".join(offenders))


def test_the_files_page_copies_the_unc_path_not_the_servers_drive_letter(env, monkeypatch):
    r"""The second fault behind the bounce. /files handed its copy button
    f.path raw — the server's own F:\File-Cabinet\..., a drive letter that
    exists on the server alone. It reached the clipboard fine, and File
    Explorer on every desk then refused to open it. Staff-facing paths are full
    UNC (house rule: drive letters differ per desk)."""
    import app as app_mod
    from flask import render_template

    cfg = dict(app_mod.load_config())
    cfg["server_root"] = "F:\\File-Cabinet"
    cfg["server_root_unc"] = "\\\\LACKSSERVER2021\\File-Cabinet"
    monkeypatch.setattr(app_mod, "load_config", lambda: cfg)

    here = "F:\\File-Cabinet\\Files\\2026"
    row = {"name": "bill.pdf", "path": here + "\\bill.pdf", "mtime": "2026-08-01",
           "size": "12 KB", "is_pdf": True, "chip": None}

    with app_mod.app.test_request_context("/files"):
        html = render_template(
            "files.html", crumbs=[], dirs=[], files=[row], here=here,
            here_unc=app_mod.filing.display_path(here, cfg), q="", more=0,
            truncated=False, at_roots=False)

    targets = _copy_targets(html)
    assert targets, "the /files copy buttons lost their paths"
    for copied in targets:
        assert not re.match(r"^[A-Za-z]:", copied), (
            f"Copy path hands over a drive letter no other desk has: {copied}")
    assert row["path"].replace("F:\\File-Cabinet",
                               "\\\\LACKSSERVER2021\\File-Cabinet") in targets


def test_no_template_reaches_for_the_clipboard_without_the_fallback():
    """The root cause was a second, worse copy implementation. Pin the rule."""
    offenders = []
    for root, _dirs, files in os.walk(TEMPLATES):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(root, name)
            if name == "_chrome_page_js.html":
                continue                      # the helper itself
            with open(path, encoding="utf-8") as f:
                text = f.read()
            text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)     # Jinja comments
            for line in text.splitlines():
                code = line.split("//", 1)[0]                     # JS comments
                if "navigator.clipboard" in code and "copyTextToClipboard" not in code:
                    offenders.append(f"{name}: {line.strip()[:90]}")
    assert not offenders, ("copy these through copyTextToClipboard instead:\n"
                           + "\n".join(offenders))


def _copy_targets(html):
    """Every data-copy-path value a BROWSER would see, parsed the way a browser
    parses attributes — the point of the #22 bounce is that the old markup read
    fine as source and fell apart in the parser."""
    from html.parser import HTMLParser

    found = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if "data-copy-path" in d:
                found.append(d["data-copy-path"])

    P().feed(html)
    return found


def test_the_record_page_offers_the_button_with_a_unc_path(env, staff_client, tmp_path):
    from database import insert_entry
    c, _ = staff_client
    doc = tmp_path / "bill.pdf"
    doc.write_bytes(b"%PDF-1.4\n")
    eid = insert_entry({"vendor": "Network Solutions", "doc_type": "Bill",
                        "amount": 12.0, "status": "Pending",
                        "date_received": "2026-08-05", "filed_path": str(doc)})
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    targets = _copy_targets(html)
    assert targets, "Copy path button lost its path"
    # Whatever is handed to the clipboard is a single plain string, no markup.
    assert "<" not in targets[0] and "\n" not in targets[0]
    assert targets[0].endswith("bill.pdf")


def test_a_path_with_a_quote_in_it_still_reaches_the_button(env, staff_client, tmp_path):
    r"""THE fault behind "Copy Path still not working" (report #22 bounce).

    The path used to ride an inline onclick argument rendered by |tojson —
    which escapes < > & and the single quote but NOT the double quote. So
        onclick="copyDocPath(this, {{ p|tojson }})"
    reached the browser as onclick="copyDocPath(this, " — the attribute ended
    at the JSON string's own opening quote, the handler was a syntax error, and
    the click did nothing whatsoever. Joshua never saw a label change because
    no code ever ran. Rendered through a real HTML parser, with a filename that
    carries the characters that break it, the path must still arrive whole."""
    from database import insert_entry
    c, _ = staff_client
    doc = tmp_path / "6in pipe 'A' & B re-bill.pdf"
    doc.write_bytes(b"%PDF-1.4\n")
    eid = insert_entry({"vendor": "Goleta Water", "doc_type": "Bill",
                        "amount": 12.0, "status": "Pending",
                        "date_received": "2026-08-05", "filed_path": str(doc)})
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    targets = _copy_targets(html)
    assert targets, "Copy path button lost its path"
    assert targets[0] == str(doc), (
        f"the clipboard would get {targets[0]!r}, not the real path")


def test_no_path_is_ever_handed_to_an_inline_onclick_again():
    """The class of bug, not just the instance: |tojson does not escape the
    double quote, so it can never be the argument of an on* attribute."""
    offenders = []
    for root, _dirs, files in os.walk(TEMPLATES):
        for name in files:
            if not name.endswith(".html"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as f:
                text = re.sub(r"\{#.*?#\}", "", f.read(), flags=re.S)
            for line in text.splitlines():
                code = line.split("//", 1)[0]                     # JS comments
                if re.search(r'\bon[a-z]+="[^"]*\|\s*tojson', code):
                    offenders.append(f"{name}: {line.strip()[:100]}")
    assert not offenders, (
        "|tojson inside a double-quoted event attribute ends the attribute at "
        "the JSON string's opening quote — the handler silently never runs.\n"
        "Pass the value in a data-* attribute and read it in a listener:\n"
        + "\n".join(offenders))
