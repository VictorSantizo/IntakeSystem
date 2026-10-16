"""Units become first-class data (design question #7, reports #20 and #54).

The `units` table shipped with the relational core but was never populated -- the app
had a units *count* per property and no units. The unit vocabulary did already exist,
though, as a 13-year filename convention ("<PROPCODE>-<UNIT>", e.g. TL222-5), so
property_model.SEED_UNITS is that convention mined out of the ops index and baked in
as a literal (same treatment PROXY_UNIT_COUNTS got, and for the same reason: the ops
index lives on a share the app server can't read at runtime).

These tests pin the two properties that matter: the seed lands, and it stays idempotent
across the restarts that re-run it (app.py calls seed_property_model() on every boot).
"""
import property_model


def test_seed_creates_units_for_a_multi_unit_property(env):
    property_model.seed_property_model()

    units = property_model.get_units_for_property("T27")
    labels = [u["label"] for u in units]

    # T27's archive run is 1..30 plus one straggler seen a single time.
    assert labels[:30] == [str(n) for n in range(1, 31)]
    assert "43" in labels


def test_units_sort_in_mailbox_order_not_string_order(env):
    property_model.seed_property_model()

    labels = [u["label"] for u in property_model.get_units_for_property("T27")]
    # Plain string sort would put "10" before "2" and make a 30-unit property unreadable.
    assert labels.index("2") < labels.index("10")


def test_seed_is_idempotent_across_restarts(env):
    property_model.seed_property_model()
    first = len(property_model.get_units_for_property("T27"))

    property_model.seed_property_model()  # every app boot re-runs this
    assert len(property_model.get_units_for_property("T27")) == first


def test_seed_carries_archive_support_so_junk_is_prunable(env):
    property_model.seed_property_model()

    by_label = {u["label"]: u for u in property_model.get_units_for_property("T27")}
    # A real unit is backed by many filings; the straggler by exactly one. The Units
    # editor surfaces this number so Karen can delete the residue on sight.
    assert by_label["8"]["archive_hits"] > 50
    assert by_label["43"]["archive_hits"] == 1
    assert by_label["8"]["source"] == "archive"


def test_property_with_no_units_stays_empty(env):
    """Single-family homes and offices legitimately have no unit rows -- callers must
    get an empty list, not a fabricated unit 1."""
    property_model.seed_property_model()
    assert property_model.get_units_for_property("PP16") != []      # multi-unit
    assert property_model.get_units_for_property("NOSUCH") == []


def test_units_are_scoped_to_their_own_property(env):
    property_model.seed_property_model()

    t27 = {u["unit_code"] for u in property_model.get_units_for_property("T27")}
    pp16 = {u["unit_code"] for u in property_model.get_units_for_property("PP16")}
    assert t27 & pp16 == set()
    assert "T27-5" in t27


def test_units_by_property_matches_per_property_lookup(env):
    property_model.seed_property_model()

    bulk = property_model.get_units_by_property()
    assert [u["label"] for u in bulk["T27"]] == \
           [u["label"] for u in property_model.get_units_for_property("T27")]
