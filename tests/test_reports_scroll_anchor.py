"""Acting on a report keeps your place — report #89 (Joshua).

"When i click a button on this page, it submits my response and scrolls to the
top. It's very annoying to have to then scroll back down to find your spot."

Every button on /reports is a POST that redirects back to the list; the
redirect now names the card that was acted on, so the browser lands there.

His bounce, 2026-08-06 — "i clicked verified and it scrolled right back up" —
was right even with that live: Verified takes the card OUT of the validate
view, so the anchor pointed at a card the page no longer rendered and the
browser gave up at the top. The redirect now also carries ?just=<id>, which
holds that one card in the list for a single render.
"""
import database
from database import add_bug_report


def _report(text="something is broken"):
    return add_bug_report("Joshua", "http://host/bills", "", text, "{}")


def test_every_card_is_an_anchor(admin_client):
    c, _ = admin_client
    rid = _report()
    html = c.get("/reports?view=all").get_data(as_text=True)
    assert f'id="report-{rid}"' in html


def test_a_note_comes_back_to_the_card_it_was_added_to(admin_client):
    c, h = admin_client
    rid = _report()
    r = c.post(f"/reports/{rid}/note", data={"text": "checked this one"},
               headers={**h, "Referer": "/reports?view=all"})
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith(f"#report-{rid}")
    assert "/reports?view=all" in r.headers["Location"], "the view's filters are kept too"


def test_the_other_buttons_land_on_their_card_as_well(admin_client):
    c, h = admin_client
    rid = _report()
    hh = {**h, "Referer": "/reports"}
    for path, data in ((f"/reports/{rid}/priority", {"priority": "1"}),
                       (f"/reports/{rid}/topic", {"topic": "Issue Reports page"}),
                       (f"/reports/{rid}/status", {"status": "In progress"}),
                       (f"/reports/{rid}/bounce", {"text": "still broken"})):
        r = c.post(path, data=data, headers=hh)
        assert r.headers["Location"].endswith(f"#report-{rid}"), path


# ── the bounce: the card you just verified has to still be there ──────────────

def _dev_resolved(text="something is broken"):
    rid = _report(text)
    database.set_bug_report_status(rid, "Dev-resolved")
    return rid


def test_verifying_leaves_the_card_in_place_for_one_render(admin_client):
    c, h = admin_client
    rid = _dev_resolved()
    r = c.post(f"/reports/{rid}/status", data={"status": "Verified"},
               headers={**h, "Referer": "/reports?view=validate"})
    loc = r.headers["Location"]
    assert f"just={rid}" in loc and loc.endswith(f"#report-{rid}")
    assert "view=validate" in loc, "the view's filters survive"
    # Following it: the anchor the browser is aiming at exists on the page.
    html = c.get(f"/reports?view=validate&just={rid}").get_data(as_text=True)
    assert f'id="report-{rid}"' in html
    assert "just now" in html
    # ...and only for that one render — a plain reload drops it again.
    assert f'id="report-{rid}"' not in c.get("/reports?view=validate").get_data(as_text=True)


def test_just_never_duplicates_a_card_already_in_the_view(admin_client):
    c, _ = admin_client
    rid = _dev_resolved()
    html = c.get(f"/reports?view=validate&just={rid}").get_data(as_text=True)
    assert html.count(f'id="report-{rid}"') == 1


def test_a_nonsense_just_is_ignored(admin_client):
    c, _ = admin_client
    _dev_resolved()
    for bad in ("abc", "999999", ""):
        assert c.get(f"/reports?view=validate&just={bad}").status_code == 200


def test_a_second_action_replaces_the_first_just(admin_client):
    c, h = admin_client
    first, second = _dev_resolved("one"), _dev_resolved("two")
    r = c.post(f"/reports/{second}/note", data={"text": "n"},
               headers={**h, "Referer": f"/reports?view=validate&just={first}"})
    loc = r.headers["Location"]
    assert f"just={second}" in loc and f"just={first}" not in loc
