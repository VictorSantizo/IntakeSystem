"""Regression for the 2026-07-25 code review's P3 finding: the Claude API key
field on /settings echoed the real saved key back into the rendered HTML
(visible in page source/dev tools even though the input was type="password").
Fix: the field is now always rendered blank with a "(saved)" placeholder, and
a blank submission keeps the existing key rather than wiping it -- matching
the mail-account password UX already used elsewhere in Settings.
"""
import app as klr


def test_saved_key_never_echoed_in_page_source(env, admin_client):
    c, headers = admin_client
    c.post("/settings", headers=headers,
           data={"scan_folder": r"Z:\SCAN", "filed_folder": r"Z:\FILED",
                 "claude_api_key": "sk-ant-supersecret123"})
    html = c.get("/settings").get_data(as_text=True)
    assert "sk-ant-supersecret123" not in html
    assert "(saved" in html


def test_blank_submission_keeps_existing_key(env, admin_client):
    c, headers = admin_client
    c.post("/settings", headers=headers,
           data={"scan_folder": r"Z:\SCAN", "filed_folder": r"Z:\FILED",
                 "claude_api_key": "sk-ant-original456"})
    # A later save that doesn't touch the key field (e.g. just changing a
    # folder) must not blank it out.
    c.post("/settings", headers=headers,
           data={"scan_folder": r"Z:\SCAN2", "filed_folder": r"Z:\FILED",
                 "claude_api_key": ""})
    assert klr.load_config()["claude_api_key"] == "sk-ant-original456"


def test_non_blank_submission_replaces_key(env, admin_client):
    c, headers = admin_client
    c.post("/settings", headers=headers,
           data={"scan_folder": r"Z:\SCAN", "filed_folder": r"Z:\FILED",
                 "claude_api_key": "sk-ant-original456"})
    c.post("/settings", headers=headers,
           data={"scan_folder": r"Z:\SCAN", "filed_folder": r"Z:\FILED",
                 "claude_api_key": "sk-ant-replaced789"})
    assert klr.load_config()["claude_api_key"] == "sk-ant-replaced789"
