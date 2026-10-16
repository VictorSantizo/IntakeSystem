"""Rename-cascade coverage (2026-07-25 code review, P2): rename_entity_code and
rename_property_code look parallel to rename_bill_type_code/rename_vendor but
weren't -- none of the four cascaded into account_hints (the account
auto-assign cache) or expected_bills (the recurring-bill radar), silently
leaving those two tables holding stale codes after a rename. Entity/property
renames now cascade into both tables' relevant columns; bill-type and vendor
renames don't need to (account_hints/expected_bills have no bill_type_code
column, and expected_bills.vendor was already cascaded by rename_vendor)."""
import database


def test_rename_entity_code_cascades_into_account_hints(env):
    database.save_account_hint("12-3456", "TRUST1", "T27")
    database.rename_entity_code("TRUST1", "TRUST9", "Nine")
    hint = database.get_account_hint("12-3456")
    assert hint["entity_code"] == "TRUST9"


def test_rename_entity_code_cascades_into_expected_bills(env):
    eb_id = database.add_expected_bill("Southern California Edison",
                                       entity_code="TRUST1", property_code="T27")
    database.rename_entity_code("TRUST1", "TRUST9", "Nine")
    eb = next(e for e in database.get_expected_bills() if e["id"] == eb_id)
    assert eb["entity_code"] == "TRUST9"


def test_rename_property_code_cascades_into_account_hints(env):
    """account_hints.property_codes is comma-separated, same as vendor_hints's
    field of the same name -- exact-token match, so 'T2' inside 'T27' must not
    be swapped when renaming 'T27'."""
    database.save_account_hint("12-3456", "TRUST1", "T27, PP16")
    database.rename_property_code("T27", "T99")
    hint = database.get_account_hint("12-3456")
    codes = [c.strip() for c in hint["property_codes"].split(",")]
    assert "T99" in codes and "T27" not in codes and "PP16" in codes


def test_rename_property_code_cascades_into_expected_bills(env):
    eb_id = database.add_expected_bill("Goleta Water", entity_code="TRUST1",
                                       property_code="T27")
    database.rename_property_code("T27", "T99")
    eb = next(e for e in database.get_expected_bills() if e["id"] == eb_id)
    assert eb["property_code"] == "T99"


def test_rename_bill_type_and_vendor_do_not_touch_account_hints_or_expected_bills(env):
    """account_hints/expected_bills have no bill_type_code column, and
    expected_bills.vendor is already handled by rename_vendor itself -- these
    two rename functions correctly have no NEW cascade work to do here."""
    database.save_account_hint("99-0000", "TRUST1", "T27")
    eb_id = database.add_expected_bill("Old Vendor Name", entity_code="TRUST1",
                                       property_code="T27")
    database.rename_bill_type_code("ELEC", "ELECX")
    database.rename_vendor("Old Vendor Name", "New Vendor Name")

    hint = database.get_account_hint("99-0000")
    assert hint["entity_code"] == "TRUST1" and hint["property_codes"] == "T27"
    eb = next(e for e in database.get_expected_bills() if e["id"] == eb_id)
    assert eb["vendor"] == "New Vendor Name"          # rename_vendor's own job
    assert eb["entity_code"] == "TRUST1" and eb["property_code"] == "T27"
