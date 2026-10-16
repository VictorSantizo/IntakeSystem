"""Per-page browser titles — report #80 (Joshua, filed via chat).

"Every page shows the same browser title 'Klick Klack', so browser history and
the back-button list are useless for finding which page you were on."
"""
import page_titles


def test_title_leads_with_the_page_then_the_app():
    assert page_titles.title_for("bills") == "Bills - Klick Klack"
    assert page_titles.title_for("review_queue") == "Needs Review - Klick Klack"


def test_an_unmapped_or_missing_endpoint_falls_back_to_the_app_name():
    assert page_titles.title_for("some_api_endpoint") == "Klick Klack"
    assert page_titles.title_for(None) == "Klick Klack"


def test_a_route_can_override_with_its_own_title():
    assert page_titles.title_for("view_entry", "260805-007") == "260805-007 - Klick Klack"
    assert page_titles.title_for("bills", "   ") == "Bills - Klick Klack"


def _title(html):
    return html.split("<title>", 1)[1].split("</title>", 1)[0].strip()


def test_pages_render_their_own_title(admin_client):
    c, _ = admin_client
    seen = {}
    for path, expected in (("/bills", "Bills - Klick Klack"),
                           ("/review", "Needs Review - Klick Klack"),
                           ("/log", "Intake Log - Klick Klack"),
                           ("/catchup", "Catch Up - Klick Klack"),
                           ("/settings", "Settings - Klick Klack"),
                           ("/reports", "Issue Reports - Klick Klack")):
        got = _title(c.get(path).get_data(as_text=True))
        assert got == expected, f"{path} titled itself {got!r}"
        seen[path] = got
    # The actual complaint: a history list of identical entries.
    assert len(set(seen.values())) == len(seen)


# The new look's chrome prints the same page_title; not pinned here because
# templates/new/base.html was being rewritten by a concurrent session the night
# this shipped (see test_nav_account_menu.py for the same note).


def test_every_mapped_endpoint_still_exists(admin_client):
    """The map is hand-written; an endpoint renamed out from under it would
    quietly give that page its old blank title back."""
    import app as klr
    live = {r.endpoint for r in klr.app.url_map.iter_rules()}
    missing = sorted(e for e in page_titles.TITLES if e not in live)
    assert not missing, f"page_titles.py names endpoints that no longer exist: {missing}"
