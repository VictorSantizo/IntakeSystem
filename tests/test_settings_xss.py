"""Regression for the 2026-07-25 code review's XSS finding: five admin "manage"
pages built their delete-confirm dialog by interpolating a raw name/code
directly into an inline onclick="confirm('...')" string. Jinja's autoescaping
stops that value from breaking out of the HTML *attribute*, but the browser
HTML-decodes the attribute before the JS parser sees it, so a literal quote
character in the name still breaks out of the JS *string literal* -- an
ordinary name like "O'Brien" already did it, and a deliberately crafted one
could inject script into another admin's session.

Fix: the value now goes into a data-* attribute (which gets Jinja's normal,
always-safe HTML-attribute escaping) and JS reads it via `this.dataset`,
never re-interpolating it into a string literal. These tests plant a name/code
containing both a single and a double quote and check the rendered page never
contains the raw value sitting inside the onclick string.
"""
import app as klr

NASTY = """Weird'"Name"""  # both quote styles in one string


def _no_raw_value_in_onclick(html: str, value: str) -> bool:
    """True if `value` never appears raw right after one of the confirm()
    prefixes this codebase's onclick handlers actually use (i.e. it only shows
    up already HTML-escaped, e.g. inside a data-* attribute or with its quotes
    turned into &#39;/&#34;). Every prefix these five templates use is checked
    explicitly -- a helper that only matched "Delete {value}" would silently
    never fire for "Delete entity {value}" and give a false pass."""
    prefixes = ("Delete entity ", "Delete property ", "Delete bill type ",
                "Delete mailbox ", "Remove staff member ")
    return not any(f"onclick=\"return confirm('{p}{value}" in html for p in prefixes)


def test_entity_delete_confirm_escapes_quotes(env, admin_client):
    c, headers = admin_client
    c.post("/settings/entities/add", headers=headers,
           data={"code": NASTY, "name": "Test Entity"})
    html = c.get("/settings/entities").get_data(as_text=True)
    assert _no_raw_value_in_onclick(html, NASTY)
    assert 'data-entity-code="Weird&#39;&#34;Name"' in html
    assert "this.dataset.entityCode" in html


def test_property_delete_confirm_escapes_quotes(env, admin_client):
    c, headers = admin_client
    c.post("/settings/properties/add", headers=headers,
           data={"code": NASTY, "address": "1 Test Way"})
    html = c.get("/settings/properties").get_data(as_text=True)
    assert _no_raw_value_in_onclick(html, NASTY)
    assert 'data-property-code="Weird&#39;&#34;Name"' in html
    assert "this.dataset.propertyCode" in html


def test_bill_type_delete_confirm_escapes_quotes(env, admin_client):
    c, headers = admin_client
    c.post("/settings/bill_types/add", headers=headers,
           data={"code": NASTY, "description": "Test bill type"})
    html = c.get("/settings/bill_types").get_data(as_text=True)
    assert _no_raw_value_in_onclick(html, NASTY)
    assert 'data-billtype-code="Weird&#39;&#34;Name"' in html
    assert "this.dataset.billtypeCode" in html


def test_mail_account_delete_confirm_escapes_quotes(env, admin_client):
    c, headers = admin_client
    c.post("/settings/mail/add", headers=headers,
           data={"host": "imap.example.com", "username": NASTY, "password": "secret1"})
    html = c.get("/settings?tab=mail").get_data(as_text=True)
    assert _no_raw_value_in_onclick(html, NASTY)
    assert 'data-mailbox-name="Weird&#39;&#34;Name"' in html
    assert "this.dataset.mailboxName" in html


def test_staff_remove_confirm_escapes_quotes(env, admin_client):
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers,
           data={"name": NASTY, "password": "secret12"})
    html = c.get("/settings?tab=staff").get_data(as_text=True)
    assert _no_raw_value_in_onclick(html, NASTY)
    assert 'data-staff-name="Weird&#39;&#34;Name"' in html
    assert "this.dataset.staffName" in html
