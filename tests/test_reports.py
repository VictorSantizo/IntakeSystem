"""Report-a-problem: screenshot capture storage and admin-only serving."""
import base64
import os

import app as klr
import database

# smallest thing the validator accepts: JPEG magic + padding
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
DATA_URL = "data:image/jpeg;base64," + base64.b64encode(FAKE_JPEG).decode()


def _report(c, headers, **extra):
    payload = {"description": "The button did a weird thing", "page_url": "/entry/5/view"}
    payload.update(extra)
    return c.post("/report", json=payload, headers=headers)


def test_report_with_screenshot_saved(env, staff_client):
    c, headers = staff_client
    r = _report(c, headers, screenshot=DATA_URL)
    assert r.status_code == 200 and r.get_json()["ok"]
    rep = database.get_bug_reports()[0]
    assert rep["record_ref"] == "entry #5"
    import json as _json
    path = _json.loads(rep["context"]).get("screenshot_path")
    assert path and os.path.exists(path)
    assert open(path, "rb").read() == FAKE_JPEG


def test_report_survives_bad_screenshot(env, staff_client):
    c, headers = staff_client
    for bad in ("data:image/png;base64,AAAA", "nonsense", None,
                "data:image/jpeg;base64,!!!notbase64!!!",
                "data:image/jpeg;base64," + base64.b64encode(b"tiny").decode()):
        r = _report(c, headers, screenshot=bad)
        assert r.status_code == 200 and r.get_json()["ok"]
    import json as _json
    for rep in database.get_bug_reports():
        assert "screenshot_path" not in _json.loads(rep["context"])


def test_screenshot_route_admin_only(env, staff_client, admin_client):
    c, headers = staff_client
    _report(c, headers, screenshot=DATA_URL)
    rid = database.get_bug_reports()[0]["id"]
    # staff: redirected away (admin_required)
    r = c.get(f"/reports/{rid}/screenshot")
    assert r.status_code == 302
    # admin: served
    a, _ = admin_client
    r = a.get(f"/reports/{rid}/screenshot")
    assert r.status_code == 200 and r.data == FAKE_JPEG


def test_cli_resolve_report(env, staff_client):
    """The server-side `resolve-report` command flips status via the app process
    (the safe writer). Bad ids are skipped; since the 2026-08-04 workflow it
    maps to Dev-resolved — verification stays with a human (/reports)."""
    database.add_bug_report("Nancy", "/x", "entry #395", "couldn't find the bill")
    database.add_bug_report("Joshua", "/bills", "", "add received view")
    open_ids = [r["id"] for r in database.get_bug_reports(status="Open")]
    assert len(open_ids) == 2
    rid = open_ids[-1]   # oldest of the two

    assert klr._cli_reports(["resolve-report", str(rid), "999", "abc"]) is True
    resolved = {r["id"]: r["status"] for r in database.get_bug_reports()}
    assert resolved[rid] == "Dev-resolved"
    # Dev-resolved still counts as open work (it awaits verification), so both
    # reports remain on the nav badge; the non-existent id changed nothing
    assert database.get_open_report_count() == 2
    # idempotent + unknown command falls through so normal startup proceeds
    assert klr._cli_reports(["resolve-report", str(rid)]) is True
    assert database.get_bug_reports(status="Dev-resolved")[0]["id"] == rid
    assert klr._cli_reports(["list-reports"]) is True
    assert klr._cli_reports(["serve-please"]) is False


def test_screenshot_route_refuses_paths_outside_folder(env, admin_client, tmp_path):
    # a corrupted/hostile context row must not turn send_file into a file reader
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(FAKE_JPEG)
    import json as _json
    database.add_bug_report("x", "", "", "desc",
                            _json.dumps({"screenshot_path": str(outside)}))
    rid = database.get_bug_reports()[0]["id"]
    a, _ = admin_client
    assert a.get(f"/reports/{rid}/screenshot").status_code == 404
