"""night_api.py — the stable CLI wrapper night-shift/dev sessions use for the
dev API (VALIDATION_WORKFLOW.md). Payload building is pure and tested here;
the network call itself is a thin urllib POST exercised by the API's own
route tests (tests/test_reports_workflow.py)."""
import pytest

import night_api


def test_note_payload():
    path, p = night_api.build_payload(["note", "74", "found", "the", "cause"])
    assert path == "/api/dev/report_note"
    assert p == {"report_id": 74, "text": "found the cause", "actor": "Night Shift"}


def test_status_payload_with_note_and_actor():
    path, p = night_api.build_payload(
        ["status", "68", "Dev-resolved", "did X; check Y", "--actor", "Dev session"])
    assert path == "/api/dev/report_status"
    assert p == {"report_id": 68, "status": "Dev-resolved",
                 "note": "did X; check Y", "actor": "Dev session"}


def test_new_payload_with_page():
    path, p = night_api.build_payload(
        ["new", "titles all say Klick Klack", "--page", "/bills?view=due"])
    assert path == "/api/dev/report_new"
    assert p == {"description": "titles all say Klick Klack",
                 "page_url": "/bills?view=due", "actor": "Night Shift"}


def test_probe_and_bad_input():
    assert night_api.build_payload(["probe"]) == ("probe", None)
    for bad in ([], ["note"], ["note", "x", "text"], ["status", "5"],
                ["status", "5", "Verified", "n"], ["nonsense"]):
        with pytest.raises(SystemExit):
            night_api.build_payload(bad)
