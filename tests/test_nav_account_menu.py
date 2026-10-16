"""The top nav, tidied — reports #76, #79 and the #53 bounce.

#76 (Joshua): "if you are looking for a document you should be able to find it
either in 'Needs Review' or the 'Intake Log', but not both — let's move the
Intake Log button to the right of Needs Review."

#79 (Joshua): "Tuck the 'Settings' under a drop-down when you click your
username, just like every other app out there?"

#53 (Joshua, bounced 8/5): "the menus at the top are looking very cluttered" —
his bounce said resolving the other two UI-Layout tickets resolves this one, so
these tests are also #53's evidence: the bar carries the daily workflow and
nothing else.
"""
import re


def _nav(html):
    """Just the top bar (classic chrome), where order is the whole point."""
    return html.split("<nav>", 1)[1].split("</nav>", 1)[0]


def test_intake_log_sits_next_to_needs_review(admin_client):
    c, _ = admin_client
    nav = _nav(c.get("/bills").get_data(as_text=True))
    review = nav.index('data-kk="nav-review"')
    log = nav.index('data-kk="nav-log"')
    bills = nav.index('data-kk="nav-bills"')
    assert review < log < bills, "Intake Log belongs directly after Needs Review"
    # ...and nothing sneaks between the pair.
    assert 'data-kk="nav-' not in nav[review:log].split('data-kk="nav-review"', 1)[1]


def test_settings_is_not_a_top_level_nav_button_any_more(admin_client):
    c, _ = admin_client
    html = c.get("/bills").get_data(as_text=True)
    nav = _nav(html)
    top = nav.split("<details", 1)[0]        # the bar itself, before the menu
    assert 'data-kk="nav-settings"' not in top
    assert 'data-kk="nav-ops-reports"' not in top
    # Still reachable — inside the account menu, one click from anywhere.
    assert 'data-kk="usermenu"' in nav
    menu = nav.split("<details", 1)[1]
    assert 'href="/settings"' in menu
    assert 'href="/ops-reports"' in menu
    assert 'href="/account"' in menu
    assert 'href="/logout"' in menu


def test_account_menu_shows_for_staff_without_the_admin_pages(staff_client):
    c, _ = staff_client
    nav = _nav(c.get("/bills").get_data(as_text=True))
    assert 'data-kk="usermenu"' in nav
    menu = nav.split("<details", 1)[1]
    assert 'href="/account"' in menu and 'href="/logout"' in menu
    assert 'href="/settings"' not in menu        # admin-only, as it always was


def test_account_menu_needs_no_javascript_to_open(admin_client):
    """It is a <details>/<summary>, so it opens on click with scripting off —
    the chrome's JS only adds the click-outside/Escape manners."""
    c, _ = admin_client
    nav = _nav(c.get("/bills").get_data(as_text=True))
    assert re.search(r"<details[^>]*class=\"usermenu\"", nav)
    assert "<summary" in nav.split("<details", 1)[1]


# The new look's chrome (templates/new/base.html) carries the same two moves —
# Find beside Review, Settings and Ops Reports inside the account menu — and
# the CSS for the menu is in static/newlook.css. It is deliberately NOT pinned
# here yet: on the night this shipped another session was mid-build on that
# same file (the sidenav/hamburger chrome), so its copy of these changes lands
# with that session's commit rather than being half-committed from under it.
# Add the mirror test when new/base.html is next committed.
