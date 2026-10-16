"""The "new look" second view (routes_newlook.py) — the Outlook-style toggle.

Staff-facing names (Karen, 2026-08-10): "Classy" = new, "Colorful" = classic;
the internal skin keys stay classic/new (they live in config.json staff
entries). The Classy switch (_skin_switch.html) is always visible top-right
in BOTH chromes, and its position comes from the person's SAVED pick — so a
Classy person on a not-yet-rebuilt page still sees it ON.

Classic stays the default and untouched; opting in swaps only pages that have
a templates/new/ variant, everything else falls back to classic. The
redesigned dashboard must keep the live review-row contract the classic one
honors (hidden-not-absent at zero, styleReviewTile signature).
"""
from conftest import _login_session

import app as klr
import database
import routes_newlook


def _set_skin(c, headers, skin):
    return c.post("/account/skin", data={"skin": skin}, headers=headers)


def test_classic_is_default(staff_client):
    c, _ = staff_client
    r = c.get("/")
    assert r.status_code == 200
    assert b'class="brand"' in r.data          # classic navy nav
    assert b"newlook.css" not in r.data


def test_classic_chrome_has_the_switch_off(staff_client):
    # The Classy switch is always visible top-right (Outlook-style). On the
    # default look it reads OFF and offers the way IN (posts skin=new).
    c, _ = staff_client
    html = c.get("/").data.decode("utf-8")
    assert 'data-kk="skin-switch"' in html
    assert 'action="/account/skin"' in html
    switch = html.split('class="skin-flip"', 1)[1]
    assert 'value="new"' in switch.split("</form>", 1)[0]
    assert 'aria-checked="false"' in switch.split("</form>", 1)[0]
    assert ">Classy<" in switch                # the constant Outlook-style label


def test_toggle_on_renders_new_dashboard_and_persists(staff_client):
    c, h = staff_client
    r = _set_skin(c, h, "new")
    assert r.status_code == 302
    r = c.get("/")
    assert b"newlook.css" in r.data            # new chrome
    assert b'class="topbar"' in r.data
    assert b'class="brand"' not in r.data      # not the classic nav
    # the way back is the always-visible switch, now ON, posting skin=classic
    switch = r.data.decode("utf-8").split('class="skin-flip"', 1)[1].split("</form>", 1)[0]
    assert 'aria-checked="true"' in switch
    assert 'value="classic"' in switch
    # durable copy on the staff entry — the choice follows the person
    entry = next(s for s in klr.load_config()["staff"] if s["name"] == "TestUser")
    assert entry.get("ui_skin") == "new"
    # ...including to a fresh session on another machine
    c2 = klr.app.test_client()
    _login_session(c2, "TestUser")
    assert b"newlook.css" in c2.get("/").data


def test_toggle_back_to_classic(staff_client):
    c, h = staff_client
    _set_skin(c, h, "new")
    _set_skin(c, h, "classic")
    r = c.get("/")
    assert b"newlook.css" not in r.data
    assert b'class="brand"' in r.data
    entry = next(s for s in klr.load_config()["staff"] if s["name"] == "TestUser")
    assert entry.get("ui_skin") == "classic"


def test_other_users_unaffected(staff_client):
    c, h = staff_client
    _set_skin(c, h, "new")
    c2 = klr.app.test_client()
    _login_session(c2, "TestAdmin")
    assert b"newlook.css" not in c2.get("/").data   # admin still classic


def test_new_dashboard_keeps_live_review_contract(staff_client):
    c, h = staff_client
    _set_skin(c, h, "new")
    html = c.get("/").data.decode("utf-8")
    # The review row exists in the DOM even with an empty queue — hidden, not
    # absent — so the shared poller can reveal it in place (classic contract).
    assert 'id="review-tile"' in html
    assert 'id="review-count"' in html and 'id="review-sub"' in html
    tile_tag = html.split('id="review-tile"', 1)[1][:220]
    assert "display:none" in tile_tag
    assert "function styleReviewTile" in html
    # the shared chrome came along: CSRF injection, poller, helper, show-me
    assert "window.__CSRF__" in html
    assert "/api/pending_count" in html
    assert "kk-helper-btn" in html
    # helper "show me" nav targets survive the redesign (assistant APP_GUIDE)
    for target in ("nav-dashboard", "nav-review", "nav-bills",
                   "nav-catchup", "nav-log", "nav-scan"):
        assert 'data-kk="%s"' % target in html


def test_new_dashboard_filled_button_is_oldest_received_bill(staff_client):
    c, h = staff_client
    database.insert_entry({"doc_type": "Bill", "vendor": "Frontier",
                           "amount": 147.48, "status": "Pending",
                           "date_received": "2026-07-14"})
    database.insert_entry({"doc_type": "Bill", "vendor": "Newer Vendor",
                           "amount": 50.0, "status": "Pending",
                           "date_received": "2026-07-20"})
    database.insert_entry({"doc_type": "Bill", "vendor": "Already Paid",
                           "amount": 10.0, "status": "Paid",
                           "date_received": "2026-07-01"})
    _set_skin(c, h, "new")
    html = c.get("/").data.decode("utf-8")
    assert "Pay the next bill" in html
    assert "Frontier" in html                  # oldest received, unpaid
    assert "you pay in received order" in html


def test_oldest_unpaid_bill_query(env):
    assert database.get_oldest_unpaid_bill() is None
    database.insert_entry({"doc_type": "Bill", "vendor": "B", "amount": 2.0,
                           "status": "Pending", "date_received": "2026-07-10"})
    database.insert_entry({"doc_type": "Bill", "vendor": "A", "amount": 1.0,
                           "status": "Pending", "date_received": "2026-07-05"})
    database.insert_entry({"doc_type": "Income", "vendor": "Rent", "amount": 9.0,
                           "status": "Pending", "date_received": "2026-07-01"})
    oldest = database.get_oldest_unpaid_bill()
    assert oldest["vendor"] == "A"


def test_unbuilt_pages_fall_back_to_classic(staff_client):
    c, h = staff_client
    _set_skin(c, h, "new")
    # /account has no templates/new/ variant — render_skinned would fall back;
    # and routes that render classic directly stay classic. Either way the
    # person keeps working.
    r = c.get("/account")
    assert r.status_code == 200
    assert b"newlook.css" not in r.data
    # The switch position tracks the SAVED pick, not the rendering template:
    # this classic-rendered page still shows it ON, offering the way back.
    switch = r.data.decode("utf-8").split('class="skin-flip"', 1)[1].split("</form>", 1)[0]
    assert 'aria-checked="true"' in switch
    assert 'value="classic"' in switch
    # the helper itself: explicit fallback when no new/ template exists
    with klr.app.test_request_context("/"):
        from flask import session
        session["user"] = "TestUser"
        session["ui_skin"] = "new"
        html = routes_newlook.render_skinned("account.html",
                                             pw_success=None, pw_error=None)
        assert "newlook.css" not in html       # classic account rendered


def test_set_skin_rejects_garbage(staff_client):
    c, h = staff_client
    _set_skin(c, h, "purple-zebra")
    r = c.get("/")
    assert b"newlook.css" not in r.data        # fell back to classic, no crash


# ── The nightly one-page conversion, 2026-08-11: /files joins Classy ─────────

def _classy_cabinet(env):
    """Same small archive the classic /files tests browse."""
    import json
    import os
    from conftest import make_pdf
    archive = os.path.join(str(env.tmp), "Files")
    tree = os.path.join(archive, "2024", "PP", "CASH FLOW STATEMENTS")
    os.makedirs(tree, exist_ok=True)
    env.cfg.update({"archive_folder": archive})
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    return archive, tree, make_pdf(os.path.join(tree, "PP16 CASH FLOW - JAN 2024.pdf"))


def test_classy_files_page_renders_the_new_template(env, staff_client):
    archive, tree, _pdf = _classy_cabinet(env)
    c, h = staff_client
    _set_skin(c, h, "new")
    html = c.get("/files?p=%s" % tree).get_data(as_text=True)
    assert "newlook.css" in html                  # the Classy chrome, not classic
    assert 'class="eyebrow"' in html
    assert 'class="quiet"' in html
    # The roots page (no folder chosen) is a different branch of the template —
    # render_skinned RAISES on a broken new template, so a half-built page
    # would 500 here rather than quietly fall back to Colorful.
    r = c.get("/files")
    assert r.status_code == 200
    assert b"newlook.css" in r.data


def test_classy_files_keeps_the_copy_path_contract(env, staff_client):
    """The redress must not drop what JavaScript and the chrome reach for.

    Both Copy path buttons keep their ids/classes AND their data-copy-path —
    the shared listener in _chrome_page_js.html is what makes them work, in
    both looks (report #22). #unc-footer feeds the Open-in-Explorer call.
    """
    archive, tree, _pdf = _classy_cabinet(env)
    c, h = staff_client
    _set_skin(c, h, "new")
    html = c.get("/files?p=%s" % tree).get_data(as_text=True)
    assert 'id="copy-path-btn"' in html
    assert 'id="open-explorer-btn"' in html
    assert 'id="unc-footer"' in html
    assert "js-copy-file" in html
    assert html.count("data-copy-path=") >= 2     # the folder's and the file's
    assert "/api/desk/open_explorer" in html
    # the shared chrome came along
    assert "window.__CSRF__" in html
    assert "kk-helper-btn" in html


def test_colorful_files_page_is_untouched(env, staff_client):
    """Classic users must see exactly what they saw before."""
    archive, tree, _pdf = _classy_cabinet(env)
    c, _h = staff_client
    html = c.get("/files?p=%s" % tree).get_data(as_text=True)
    assert "newlook.css" not in html
    assert 'class="table-wrap"' in html           # the classic table chrome
    assert 'id="copy-path-btn"' in html
    assert "js-copy-file" in html
