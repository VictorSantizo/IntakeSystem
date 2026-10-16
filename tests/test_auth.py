"""Auth, CSRF, throttle, and route gating — all against the temp environment."""
import app as klr


def test_anonymous_redirected_to_login(client):
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_api_anonymous_gets_401(client):
    assert client.post("/api/clear_queue").status_code == 401


def test_login_page_has_rendered_csrf_token(client):
    r = client.get("/login")
    assert r.status_code == 200
    with client.session_transaction() as s:
        token = s.get("csrf")
    assert token and f'name="csrf_token" value="{token}"' in r.get_data(as_text=True)


def test_login_without_csrf_rejected(client):
    client.get("/login")
    r = client.post("/login", data={"name": "TestAdmin", "password": "adminpw"})
    assert r.status_code == 403


def test_full_login_flow(client):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    r = client.post("/login", data={"name": "TestAdmin", "password": "adminpw",
                                    "csrf_token": token})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    # and the dashboard now renders
    assert client.get("/").status_code == 200


def test_wrong_password_rejected(client):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    r = client.post("/login", data={"name": "TestAdmin", "password": "nope",
                                    "csrf_token": token})
    assert b"Incorrect name or password" in r.data


def test_login_throttle_after_repeated_failures(client):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    for _ in range(klr._LOGIN_MAX_FAILS):
        client.post("/login", data={"name": "x", "password": "bad", "csrf_token": token})
    r = client.post("/login", data={"name": "TestAdmin", "password": "adminpw",
                                    "csrf_token": token})
    assert b"Too many failed attempts" in r.data


def test_admin_route_blocked_for_staff(staff_client):
    c, headers = staff_client
    r = c.post("/api/clear_queue", headers=headers)
    assert r.status_code == 403 and b"Admin access required" in r.data


def test_admin_route_allowed_for_admin(admin_client):
    c, headers = admin_client
    r = c.post("/api/clear_queue", headers=headers)
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_status_update_validation(staff_client):
    c, headers = staff_client
    r = c.post("/entry/1/status", json={"status": "MadeUp"}, headers=headers)
    assert r.status_code == 400
    r = c.post("/entry/1/status", data="notjson", headers=headers)
    assert r.status_code == 400


def test_scan_preview_requires_login(client):
    r = client.get("/scan/preview/1")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
