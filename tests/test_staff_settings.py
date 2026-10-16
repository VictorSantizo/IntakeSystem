"""Staff management (Settings > Staff tab): add / reset password / toggle
admin / remove, all admin-gated, all persisted through save_config so
config.json stays ASCII-escaped. See app.py's "Staff management" section.
"""
import json

import app as klr
from werkzeug.security import check_password_hash


def _staff_by_name(cfg, name):
    return next((s for s in cfg.get("staff", []) if s.get("name") == name), None)


# ── Add ──────────────────────────────────────────────────────────────────────

def test_admin_adds_staff_member(env, admin_client):
    c, headers = admin_client
    r = c.post("/settings/staff/add", headers=headers,
               data={"name": "Nancy", "password": "secret12"})
    assert r.status_code == 302
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    entry = _staff_by_name(cfg, "Nancy")
    assert entry is not None
    assert entry["password"] != "secret12"                  # never stored plain
    assert entry["password"].startswith("pbkdf2:")
    assert check_password_hash(entry["password"], "secret12")
    assert entry.get("admin") is False                     # checkbox left unticked


def test_admin_flag_respected_when_adding(env, admin_client):
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers,
           data={"name": "Nancy", "password": "secret12", "admin": "on"})
    cfg = klr.load_config()
    assert _staff_by_name(cfg, "Nancy")["admin"] is True


def test_add_requires_name(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    c.post("/settings/staff/add", headers=headers, data={"name": "", "password": "secret12"})
    assert klr.load_config()["staff"] == before


def test_add_requires_min_password_length(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    c.post("/settings/staff/add", headers=headers, data={"name": "Nancy", "password": "abc"})
    assert klr.load_config()["staff"] == before


def test_duplicate_name_case_insensitive_rejected(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    r = c.post("/settings/staff/add", headers=headers,
               data={"name": "testadmin", "password": "secret12"})   # existing: "TestAdmin"
    assert r.status_code == 302
    assert klr.load_config()["staff"] == before             # no change at all


# ── Non-admin gating ─────────────────────────────────────────────────────────

def test_non_admin_blocked_from_all_staff_routes(env, staff_client):
    c, headers = staff_client
    before = klr.load_config()["staff"]
    routes = [
        ("/settings/staff/add", {"name": "Nancy", "password": "secret12"}),
        ("/settings/staff/password", {"name": "TestAdmin", "password": "newpass1"}),
        ("/settings/staff/toggle_admin", {"name": "TestAdmin"}),
        ("/settings/staff/remove", {"name": "TestUser"}),
    ]
    for path, data in routes:
        r = c.post(path, headers=headers, data=data)
        assert r.status_code == 302 and r.headers["Location"] == "/"   # bounced to dashboard
    assert klr.load_config()["staff"] == before             # nothing mutated


# ── Password reset ───────────────────────────────────────────────────────────

def test_password_reset_changes_hash_and_verifies(env, admin_client):
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers, data={"name": "Nancy", "password": "abcd1234"})
    old_hash = _staff_by_name(klr.load_config(), "Nancy")["password"]

    r = c.post("/settings/staff/password", headers=headers,
               data={"name": "Nancy", "password": "newpass1"})
    assert r.status_code == 302
    entry = _staff_by_name(klr.load_config(), "Nancy")
    assert entry["password"] != old_hash
    assert check_password_hash(entry["password"], "newpass1")
    assert not check_password_hash(entry["password"], "abcd1234")


def test_password_reset_requires_min_length(env, admin_client):
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers, data={"name": "Nancy", "password": "abcd1234"})
    old_hash = _staff_by_name(klr.load_config(), "Nancy")["password"]
    c.post("/settings/staff/password", headers=headers, data={"name": "Nancy", "password": "ab"})
    assert _staff_by_name(klr.load_config(), "Nancy")["password"] == old_hash


def test_password_reset_unknown_name_is_a_no_op(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    r = c.post("/settings/staff/password", headers=headers,
               data={"name": "Ghost", "password": "newpass1"})
    assert r.status_code == 302
    assert klr.load_config()["staff"] == before


# ── Self / last-admin protection ─────────────────────────────────────────────
# The fixture's "TestAdmin" (the logged-in session user) is the ONLY admin, so
# self-removal and last-admin-removal are the same action here — admin_required
# already guarantees a non-admin could never reach these routes to test the
# "last admin, but not self" case in isolation (if there's only one admin left,
# it must be the session user, since the caller has to be an admin to get in).

def test_cannot_remove_self(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    r = c.post("/settings/staff/remove", headers=headers, data={"name": "TestAdmin"})
    assert r.status_code == 302
    assert klr.load_config()["staff"] == before


def test_cannot_demote_self(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    r = c.post("/settings/staff/toggle_admin", headers=headers, data={"name": "TestAdmin"})
    assert r.status_code == 302
    assert klr.load_config()["staff"] == before
    assert _staff_by_name(klr.load_config(), "TestAdmin")["admin"] is True


def test_cannot_remove_self_even_with_a_second_admin(env, admin_client):
    """Self-protection is unconditional, not just a side effect of the
    last-admin count check."""
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers,
           data={"name": "Bob", "password": "secret12", "admin": "on"})
    r = c.post("/settings/staff/remove", headers=headers, data={"name": "TestAdmin"})
    assert r.status_code == 302
    cfg = klr.load_config()
    assert _staff_by_name(cfg, "TestAdmin") is not None     # still there
    assert _staff_by_name(cfg, "Bob") is not None            # untouched too


def test_can_demote_and_remove_a_non_last_admin(env, admin_client):
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers,
           data={"name": "Bob", "password": "secret12", "admin": "on"})
    r = c.post("/settings/staff/toggle_admin", headers=headers, data={"name": "Bob"})
    assert r.status_code == 302
    assert _staff_by_name(klr.load_config(), "Bob")["admin"] is False

    r = c.post("/settings/staff/remove", headers=headers, data={"name": "Bob"})
    assert r.status_code == 302
    assert _staff_by_name(klr.load_config(), "Bob") is None


def test_remove_unknown_name_is_a_no_op(env, admin_client):
    c, headers = admin_client
    before = klr.load_config()["staff"]
    r = c.post("/settings/staff/remove", headers=headers, data={"name": "Ghost"})
    assert r.status_code == 302
    assert klr.load_config()["staff"] == before


# ── Settings page rendering ──────────────────────────────────────────────────

def test_settings_page_has_tab_markup_and_staff_section(env, admin_client):
    c, headers = admin_client
    page = c.get("/settings").data.decode()
    assert 'data-tab="staff"' in page
    assert 'data-tabpanel="staff"' in page
    assert "Add staff member" in page
    assert "TestAdmin" in page
    assert "pbkdf2:" not in page                            # never leak a hash


# ── Running-code version next to the Restart button ──────────────────────────

def test_settings_shows_running_code_version(env, admin_client, monkeypatch):
    """The App tab shows which commit the running process started on, and warns
    when a newer commit is on disk — so a restart's effect is visible in-app
    instead of only in STARTUP_LOG.txt."""
    c, headers = admin_client
    page = c.get("/settings").data.decode()
    assert "Running code" in page and klr._RUNNING_CODE in page
    # process code == disk code -> the quiet "up to date" note, no warning
    assert "up to date with the latest commit" in page
    assert "restart to pick it up" not in page
    # a commit landed since this process started -> the restart warning
    monkeypatch.setattr(klr, "_RUNNING_CODE", "0000000")
    page = c.get("/settings").data.decode()
    assert "restart to pick it up" in page


def test_staff_password_minimum_is_eight(env, admin_client):
    """Design q4 (2026-07-31): minimum staff password length raised 4 -> 8 for
    NEW passwords and resets only. 7 chars is refused for both add and reset;
    8 is accepted. Existing stored hashes are untouched by definition -- the
    constant is only checked in add_staff / reset_staff_password."""
    c, headers = admin_client
    c.post("/settings/staff/add", headers=headers,
           data={"name": "Shorty", "password": "seven77"})          # 7 -> refused
    assert not any(s["name"] == "Shorty" for s in klr.load_config().get("staff", []))

    c.post("/settings/staff/add", headers=headers,
           data={"name": "Shorty", "password": "eight888"})         # 8 -> accepted
    assert any(s["name"] == "Shorty" for s in klr.load_config().get("staff", []))

    before = next(s["password"] for s in klr.load_config()["staff"]
                  if s["name"] == "Shorty")
    c.post("/settings/staff/password", headers=headers,
           data={"name": "Shorty", "password": "seven77"})          # reset: 7 -> refused
    after = next(s["password"] for s in klr.load_config()["staff"]
                 if s["name"] == "Shorty")
    assert after == before
