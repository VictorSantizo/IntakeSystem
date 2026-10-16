"""Click-to-sort tables and the Helper's 'show me' highlights: the shared
machinery lives in base.html; list pages opt in with class="sortable"; the
Helper's APP_GUIDE carries the catalog of highlightable targets."""
import assistant

from test_dedup_triage import _log_custom_bill


def test_base_ships_sorter_and_flash(env, staff_client):
    c, headers = staff_client
    r = c.get("/")
    assert r.status_code == 200
    assert b"table.sortable" in r.data          # the sorter JS
    assert b"kk-flash" in r.data                # the flash animation
    assert b"#kk=" in r.data                    # the hash handler
    assert b'data-kk="nav-review"' in r.data    # nav targets


def test_list_pages_opt_into_sorting(env, staff_client):
    _log_custom_bill(env, staff_client, "sce.pdf",
                     "Southern California Edison (SCE)", 33.91, "S-1")
    c, headers = staff_client
    for page in ("/catchup", "/log", "/bills"):
        r = c.get(page)
        assert r.status_code == 200, page
        assert b'class="sortable"' in r.data, page


def test_guide_lists_only_real_targets(env, staff_client):
    """Every #kk= target the guide advertises must exist in a template —
    otherwise the Helper points at nothing."""
    import os, re
    targets = set(re.findall(r"#kk=([\w-]+)", assistant.APP_GUIDE))
    assert {"scan-now", "triage-attach-selected", "mark-paid",
            "catchup-bulk-pay"} <= targets
    tpl_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "templates")
    defined = set()
    # walk: templates/new/ (the new-look second view) counts too
    for root, _dirs, names in os.walk(tpl_dir):
        for name in names:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                defined |= set(re.findall(r'data-kk="([\w-]+)"', f.read()))
    missing = targets - defined
    assert not missing, f"guide advertises targets with no element: {missing}"
