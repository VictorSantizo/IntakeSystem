"""The new look's hamburger chrome (2026-08-06, Karen's request): page links
move into a drawer behind the burger, a panel button pins the drawer open as a
sidebar, and one search box replaces the always-visible Go-to input — words go
to the universal search, a record number jumps via /goto. Markup/behavior in
templates/new/_sidenav.html; styles in static/newlook_nav.css."""


def _new_dashboard(client_fixture):
    c, h = client_fixture
    c.post("/account/skin", data={"skin": "new"}, headers=h)
    return c.get("/").get_data(as_text=True)


def test_topbar_carries_the_three_controls(staff_client):
    html = _new_dashboard(staff_client)
    assert 'id="nav-burger"' in html      # the 3 stacked lines
    assert 'id="nav-pin"' in html         # keep-it-open / tuck-it-away
    assert 'id="search-btn"' in html      # the magnifier
    assert 'href="/static/newlook_nav.css"' in html


def test_page_links_live_in_the_drawer_not_the_topbar(staff_client):
    html = _new_dashboard(staff_client)
    drawer = html.split('id="side-nav"', 1)[1].split("</aside>", 1)[0]
    for target in ("nav-dashboard", "nav-scan", "nav-review", "nav-log",
                   "nav-bills", "nav-catchup"):
        assert 'data-kk="%s"' % target in drawer
    # the ONLY <nav> is the drawer's — report #76's order tests keep watching it
    assert html.count("<nav>") == 1
    assert html.index('id="side-nav"') < html.index("<nav>")
    # live review count still poller-updatable inside the drawer
    assert "js-pending-badge" in drawer


def test_drawer_keeps_joshuas_nav_order(staff_client):
    html = _new_dashboard(staff_client)
    drawer = html.split('id="side-nav"', 1)[1].split("</aside>", 1)[0]
    review, log, bills = (drawer.index('data-kk="nav-%s"' % k)
                          for k in ("review", "log", "bills"))
    assert review < log < bills           # report #76: Review, then Find, then Bills


def test_search_overlay_owns_the_goto_box(staff_client):
    html = _new_dashboard(staff_client)
    # exactly one #goto-box (the shared "/" shortcut target), inside the overlay
    assert html.count('id="goto-box"') == 1
    assert html.index('id="search-overlay"') < html.index('id="goto-box"')
    # no-JS fallback: the form itself is a plain universal-search GET
    overlay = html.split('id="search-overlay"', 1)[1].split("</form>", 1)[0]
    assert 'action="/log"' in overlay and 'name="search"' in overlay


def test_pinned_state_is_applied_before_paint(staff_client):
    html = _new_dashboard(staff_client)
    head = html.split("</head>", 1)[0]
    assert "kk-nav" in head and "nav-pinned" in head


def test_classic_chrome_is_untouched(staff_client):
    c, _ = staff_client
    html = c.get("/").get_data(as_text=True)     # default skin = classic
    assert 'id="nav-burger"' not in html
    assert 'id="side-nav"' not in html
    assert 'id="search-overlay"' not in html
    assert 'class="goto-form"' in html           # classic keeps its Go-to box
