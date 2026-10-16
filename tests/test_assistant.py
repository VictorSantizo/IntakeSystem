"""Klick Klack Helper: the /api/assistant route, the ask_assistant call shape,
history sanitizing, the read-only data tools, and the chat widget."""
from types import SimpleNamespace

import app as klr
import assistant
import database

from test_dedup_triage import _log_custom_bill


# ── history sanitizing ─────────────────────────────────────────────────────────

def test_trim_history_caps_and_sanitizes():
    junk = [{"role": "assistant", "content": "orphan lead"},   # dropped: first must be user
            {"role": "hacker", "content": "bad role"},         # dropped
            {"role": "user", "content": ""},                   # dropped: empty
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1" * 5000}]     # capped
    out = assistant._trim_history(junk)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert len(out[1]["content"]) == assistant._MAX_CHARS_PER_MESSAGE
    long = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    assert len(assistant._trim_history(long)) <= assistant._MAX_HISTORY_TURNS


# ── ask_assistant call shape (no network — fake client) ────────────────────────

def test_ask_assistant_builds_request(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text",
                                                            text=" Open the queue. ")])

    class FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.messages = FakeMessages()

    monkeypatch.setattr(assistant.anthropic, "Anthropic", FakeClient)
    reply = assistant.ask_assistant(
        "how do I clear duplicates?",
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        {"user": "Karen", "page": "/review", "queue_count": 7, "today": "2026-07-14"},
        {"claude_api_key": "sk-test"})
    assert reply == "Open the queue."
    assert captured["api_key"] == "sk-test"
    assert captured["model"] == "claude-opus-4-8"          # default; config can override
    assert captured["system"][0]["text"] == assistant.APP_GUIDE
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    final = captured["messages"][-1]
    assert final["role"] == "user"
    assert "how do I clear duplicates?" in final["content"]
    assert "Karen" in final["content"] and "/review" in final["content"]
    assert captured["messages"][0] == {"role": "user", "content": "hi"}


def test_ask_assistant_requires_key():
    import pytest
    with pytest.raises(assistant.AssistantNotConfigured):
        assistant.ask_assistant("hi", [], {}, {"claude_api_key": "YOUR_API_KEY_HERE"})


# ── the read-only data tools ───────────────────────────────────────────────────

def _seed_two_bills(env, staff_client):
    e1 = _log_custom_bill(env, staff_client, "sce.pdf",
                          "Southern California Edison (SCE)", 33.91, "S-1")
    e2 = _log_custom_bill(env, staff_client, "acme2.pdf",
                          "ACME Utilities", 147.48, "INV-1")
    database.update_entry(e2["id"], {"status": "Paid", "paid_date": "2026-07-01"})
    return e1, e2


def test_find_bills_tool_totals_and_filters(env, staff_client):
    e1, e2 = _seed_two_bills(env, staff_client)
    # loose vendor match + unpaid definition == the Bills Owed page's
    out = assistant._tool_find_bills({"vendor": "sce", "unpaid_only": True})
    assert out["match_count"] == 1 and out["amount_total"] == 33.91
    assert out["records"][0]["id"] == e1["id"]
    # unpaid sweep excludes the Paid one
    out = assistant._tool_find_bills({"unpaid_only": True})
    assert out["match_count"] == 1
    # status filter finds the paid one
    out = assistant._tool_find_bills({"status": "paid"})
    assert out["match_count"] == 1 and out["records"][0]["id"] == e2["id"]
    # no matches is a clean zero, not an error
    out = assistant._tool_find_bills({"vendor": "nonexistent vendor"})
    assert out["match_count"] == 0 and out["records"] == []


def test_find_bills_query_and_property(env, staff_client):
    """Broad keyword + property filter — the report #4 fix so the Helper can
    locate a bill by its invoice number or property code, not just the vendor."""
    e1, e2 = _seed_two_bills(env, staff_client)   # both on property T27
    # keyword matches the invoice number staff would remember
    out = assistant._tool_find_bills({"query": "S-1"})
    assert out["match_count"] == 1 and out["records"][0]["id"] == e1["id"]
    # keyword matches the property code with no vendor given — both bills
    out = assistant._tool_find_bills({"query": "T27"})
    ids = {r["id"] for r in out["records"]}
    assert {e1["id"], e2["id"]} <= ids
    # explicit property filter, same result
    assert assistant._tool_find_bills({"property": "T27"})["match_count"] == 2
    # keyword narrows with the unpaid definition (e2 is Paid)
    out = assistant._tool_find_bills({"query": "T27", "unpaid_only": True})
    assert out["match_count"] == 1 and out["records"][0]["id"] == e1["id"]


def test_get_record_tool(env, staff_client):
    e1, _ = _seed_two_bills(env, staff_client)
    out = assistant._tool_get_record({"record_id": e1["id"]})
    assert out["vendor"].startswith("Southern California")
    assert out["amount"] == 33.91
    assert assistant._tool_get_record({"record_id": 999999})["error"]
    assert assistant._tool_get_record({"record_id": "abc"})["error"]


def test_run_tool_never_raises():
    assert assistant._run_tool("bogus_tool", {})["error"]


def test_ask_assistant_tool_loop(env, staff_client, monkeypatch):
    """The model asks for a lookup; the loop runs it and feeds the result back."""
    _seed_two_bills(env, staff_client)
    calls = []

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(stop_reason="tool_use", content=[
                    SimpleNamespace(type="tool_use", id="tu1", name="find_bills",
                                    input={"vendor": "sce", "unpaid_only": True})])
            return SimpleNamespace(stop_reason="end_turn", content=[
                SimpleNamespace(type="text", text="One unpaid SCE bill, $33.91.")])

    class FakeClient:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setattr(assistant.anthropic, "Anthropic", FakeClient)
    reply = assistant.ask_assistant("how much do we owe SCE?", [],
                                    {"user": "Karen"}, {"claude_api_key": "sk-t"})
    assert reply == "One unpaid SCE bill, $33.91."
    assert len(calls) == 2
    assert calls[0]["tools"] == assistant._TOOLS
    # the second request carries the executed tool result back to the model
    result_turn = calls[1]["messages"][-1]
    assert result_turn["role"] == "user"
    assert result_turn["content"][0]["tool_use_id"] == "tu1"
    assert '"match_count": 1' in result_turn["content"][0]["content"]
    assert "33.91" in result_turn["content"][0]["content"]


# ── the route ──────────────────────────────────────────────────────────────────

def test_assistant_route_replies(env, staff_client, monkeypatch):
    c, headers = staff_client
    seen = {}

    def fake_ask(question, history, context, cfg):
        seen.update(question=question, history=history, context=context)
        return "Go to the [Review Queue](/review)."

    monkeypatch.setattr(klr.assistant, "ask_assistant", fake_ask)
    r = c.post("/api/assistant", headers=headers,
               json={"message": "where's the queue?",
                     "history": [{"role": "user", "content": "hi"}],
                     "page": "/desk"})
    assert r.status_code == 200
    assert r.get_json()["reply"] == "Go to the [Review Queue](/review)."
    assert seen["question"] == "where's the queue?"
    assert seen["context"]["user"] == "TestUser"
    assert seen["context"]["page"] == "/desk"
    assert isinstance(seen["context"]["queue_count"], int)


def test_assistant_route_logs_reply(env, staff_client, monkeypatch):
    """The reply is saved on a successful turn (report #4 — chats help us see
    what staff ask and where search falls short)."""
    c, headers = staff_client
    monkeypatch.setattr(klr.assistant, "ask_assistant",
                        lambda q, h, ctx, cfg: "Here's your answer.")
    c.post("/api/assistant", headers=headers,
           json={"message": "find the edison bill", "page": "/bills"})
    row = database.get_helper_chats()[0]
    assert row["question"] == "find the edison bill" and row["page"] == "/bills"
    assert row["reply"] == "Here's your answer." and row["error"] is None
    assert row["user"] == "TestUser"


def test_assistant_route_logs_error_turn(env, staff_client):
    """The error the user saw is saved too — here the placeholder key makes the
    real ask_assistant raise AssistantNotConfigured before any network call."""
    c, headers = staff_client
    c.post("/api/assistant", headers=headers, json={"message": "help me"})
    row = database.get_helper_chats()[0]
    assert row["question"] == "help me" and row["reply"] is None
    assert "Joshua" in row["error"]


def test_assistant_route_requires_login(env):
    c = klr.app.test_client()
    r = c.post("/api/assistant", json={"message": "hi"})
    assert r.status_code == 401


def test_assistant_route_empty_message(env, staff_client):
    c, headers = staff_client
    r = c.post("/api/assistant", headers=headers, json={"message": "   "})
    assert r.status_code == 400


def test_assistant_route_friendly_when_unconfigured(env, staff_client):
    # test config carries the placeholder key, so the real ask_assistant raises
    # AssistantNotConfigured before any network call — the route stays friendly.
    c, headers = staff_client
    r = c.post("/api/assistant", headers=headers, json={"message": "help"})
    assert r.status_code == 200
    assert "Joshua" in r.get_json()["error"]


# ── the widget ─────────────────────────────────────────────────────────────────

def test_widget_present_for_staff_absent_on_login(env, staff_client):
    c, headers = staff_client
    r = c.get("/")
    assert r.status_code == 200 and b"kk-helper" in r.data
    anon = klr.app.test_client()
    r = anon.get("/login")
    assert r.status_code == 200 and b"kk-helper" not in r.data
