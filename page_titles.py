"""Per-page browser titles (report #80, Joshua).

Every page used to render `<title>Klick Klack</title>`, so a browser history —
or the back-button's long-press list, or a row of pinned tabs — was a dozen
identical "Klick Klack" entries and named nothing. This module gives each page
a title of its own, computed from the endpoint that rendered it, so the fix is
one map rather than an edit in forty templates.

Wired in through app.py's context processor as `page_title`; both chromes
(templates/base.html and templates/new/base.html) print it. A template that
wants a more specific title — a record's own number, say — just passes
`page_title="..."` to render_template and that wins.

The shape is "Page - Klick Klack": the distinguishing word comes first, because
history lists and tab strips truncate from the right.
"""

SUFFIX = "Klick Klack"

# endpoint -> the words that go in front. Everything a person actually navigates
# to is here; API/POST-only endpoints never render a page, so they need nothing.
TITLES = {
    "dashboard": "Dashboard",
    "home": "Home",
    "scanflow.scan_page": "Scan",
    "review_queue": "Needs Review",
    "review_item": "Review",
    "review_compare": "Duplicate check",
    "review_triage": "Duplicate triage",
    "log": "Intake Log",
    "files_view": "Files",
    "mail_page": "Mail",
    "mail_view": "Email",
    "mail_print": "Print email",
    "bills": "Bills",
    "vendor_bills": "Vendor bills",
    "expected_bills_page": "Expected bills",
    "catchup": "Catch Up",
    "owed_summary": "What's owed",
    "activity_feed": "Activity",
    "filtered_queue": "Filtered",
    "legacy_dupes": "Duplicate records",
    "new_entry": "New entry",
    "edit_entry": "Edit record",
    "view_entry": "Record",
    "coversheet": "Batch cover sheet",
    "karen_page": "Karen's Questions",
    "reports": "Issue Reports",
    "settings": "Settings",
    "manage_entities": "Entities",
    "manage_properties": "Properties",
    "manage_bill_types": "Bill types",
    "property_units": "Units",
    "ops_reports": "Ops Reports",
    "ops_report_view": "Ops Report",
    "account": "My account",
    "change_password": "Change password",
    "login": "Sign in",
    "desk": "Desk",
    "cabinet": "Cabinet",
    "rolodex": "Rolodex",
    "intray": "In-tray",
    "tray": "Tray",
    "board": "Board",
    "wall_calendar": "Calendar",
}


def title_for(endpoint, explicit=""):
    """The <title> for a rendered page.

    `explicit` is a per-render override (a template or route passing
    page_title=...); it wins over the map. An endpoint nobody mapped — and a
    request with no endpoint at all, like a 404 — falls back to the bare app
    name, which is exactly where every page started.
    """
    lead = (explicit or "").strip() or TITLES.get(endpoint or "", "")
    return f"{lead} - {SUFFIX}" if lead else SUFFIX
