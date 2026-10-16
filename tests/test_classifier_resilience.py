"""Classifier resilience to AI failures (P1-6, 2026-07-25 code review) + a
direct unit check of the prompt-truncation fix (P1-5).

Before this fix, classify_with_claude's `except Exception` treated a live
Anthropic outage, a rate limit, and a bug in our own code identically:
silently default to the regex fallback (which always sets important=False),
with the only trace being a note buried inside that one record. During a real
outage that silently drops eviction notices, shutoff warnings and legal
filings into the "not important" bucket — the exact mail this app exists to
flag — with no operational visibility. The fix differentiates: API/network
failures (anthropic.* errors, malformed JSON responses) and unexpected
internal errors both still fall back (ingestion must never crash), but each
now prints a loud, distinguishable [Classifier] line, logs a traceback for the
internal-error path, AND forces the document into mandatory manual review
(important=True) rather than letting the regex guess's important=False blend
it into the normal queue — approved as a deliberate workflow tradeoff (more
review load in exchange for never silently losing a legal/urgent notice during
an outage). An earlier version of this fix (written concurrently, superseded
here) logged loudly but stopped short of forcing the flag, reasoning an outage
"must not flood the dashboard with false positives" — that's a real, different
answer to the same question; flagged for a human to reconcile which stands.
"""
import os
import sys

import anthropic
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classifier   # noqa: E402


def _config_with_key(**extra):
    cfg = {"claude_api_key": "sk-test-key", "entities": [], "properties": [], "bill_types": []}
    cfg.update(extra)
    return cfg


class _RaisingMessages:
    """Stand-in for anthropic.Anthropic().messages whose .create() raises
    whatever is handed to it — simulates the API call itself failing."""
    def __init__(self, to_raise):
        self._to_raise = to_raise

    def create(self, **kw):
        raise self._to_raise


class _RaisingClient:
    def __init__(self, to_raise):
        self._to_raise = to_raise
        self.messages = _RaisingMessages(to_raise)

    def __call__(self, api_key=None):
        return self


# ------------------------------------------------------- P1-6: never crashes

def test_anthropic_api_error_falls_back_to_regex_result(env, monkeypatch, capsys):
    """A live Anthropic outage/rate-limit/timeout (anthropic.* error family)
    must still return a usable result — never raise — and must be visible at
    the process level, not just buried in one record's notes."""
    # AnthropicError is the base of the SDK's whole error hierarchy (
    # APIConnectionError, APITimeoutError, RateLimitError, ... all derive from
    # it), so raising it directly stands in for the whole family.
    boom = anthropic.AnthropicError("simulated outage: rate limited")
    monkeypatch.setattr(anthropic, "Anthropic", _RaisingClient(boom))

    # Deliberately avoids "rent"/"lease"/"tenancy" and "bill"/"amount due"
    # substrings so classify_with_regex's keyword ladder falls all the way
    # through to the notice/legal/court/lien bucket.
    text = "COURT SUMMONS: LEGAL NOTICE OF LIEN FILED AGAINST THE PROPERTY"
    result = classifier.classify_with_claude(text, _config_with_key())

    # Falls back to the regex path's shape — never raised, never crashed.
    assert result["doc_type"] == "Legal"          # regex keyword match on "notice"/"legal"/"lien"
    assert result["ai_confidence"] == "None"
    # Existing degraded-classification marker is preserved (app.py's outage
    # banner / _ai_outage_sids() key off this exact substring in notes).
    assert "[AI unavailable" in result["notes"]
    # Force mandatory review during an outage (approved workflow change,
    # 2026-07-26) — a legal/urgent notice must never silently ride the
    # regex fallback's important=False into the ordinary queue.
    assert result["important"] is True
    assert result["important_reason"]

    out = capsys.readouterr().out
    assert "[Classifier]" in out
    assert "AnthropicError" in out


def test_malformed_json_response_falls_back_to_regex_result(env, monkeypatch, capsys):
    """A response that doesn't parse as JSON (garbled/truncated model output)
    is an API/response problem, not a bug in our code — same graceful
    fallback and loud logging as a network failure."""
    class _Messages:
        def create(self, **kw):
            return __import__("types").SimpleNamespace(
                content=[__import__("types").SimpleNamespace(
                    type="text", text="not actually json {{{")])

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)

    result = classifier.classify_with_claude("some ordinary bill text", _config_with_key())
    assert "[AI unavailable" in result["notes"]
    assert result["ai_confidence"] == "None"
    assert result["important"] is True

    out = capsys.readouterr().out
    assert "[Classifier]" in out


def test_unexpected_internal_error_falls_back_to_regex_result(env, monkeypatch, capsys):
    """A bug in OUR code (here: a config missing an expected key, raising
    KeyError before the API is even called) must ALSO fall back gracefully —
    but is logged as a distinct, traceback-bearing case, not conflated with
    an Anthropic outage."""
    # No "entities" key at all: classify_with_claude's first line inside the
    # prompt-building try reads config["entities"] and raises KeyError — a
    # bug/bad-data case that has nothing to do with the Anthropic API.
    broken_config = {"claude_api_key": "sk-test-key"}

    text = "ACME Power. Amount due $120.50. due date: 6/15/26"
    result = classifier.classify_with_claude(text, broken_config)

    assert "[AI unavailable" in result["notes"]
    assert result["ai_confidence"] == "None"
    assert result["important"] is True
    # The regex fallback still ran and extracted what it could.
    assert result["amount"] == 120.50

    out = capsys.readouterr().out
    assert "[Classifier]" in out
    assert "Unexpected error" in out
    assert "KeyError" in out               # traceback.format_exc() content present


def test_api_error_and_internal_error_log_differently(env, monkeypatch, capsys):
    """The whole point of narrowing the except: the two failure classes must
    be distinguishable from the console output alone."""
    boom = anthropic.AnthropicError("simulated: connection refused")
    monkeypatch.setattr(anthropic, "Anthropic", _RaisingClient(boom))
    classifier.classify_with_claude("some bill", _config_with_key())
    api_out = capsys.readouterr().out

    classifier.classify_with_claude("some bill", {"claude_api_key": "sk-test-key"})
    internal_out = capsys.readouterr().out

    assert api_out != internal_out
    assert "Anthropic classification failed" in api_out
    assert "Unexpected error" in internal_out


def test_never_raises_regardless_of_failure_kind(env, monkeypatch):
    """Belt-and-suspenders: ingestion must never crash on a classify failure,
    whichever bucket the exception falls into."""
    for exc in (anthropic.AnthropicError("outage"), RuntimeError("some other bug"),
                ValueError("bad value")):
        monkeypatch.setattr(anthropic, "Anthropic", _RaisingClient(exc))
        result = classifier.classify_with_claude("some text", _config_with_key())
        assert isinstance(result, dict)
        assert "[AI unavailable" in result["notes"]


# --------------------------------------------------- P1-5: truncation itself

def test_truncation_keeps_form_values_after_long_document_text(env, monkeypatch):
    """Direct unit check of the actual fix: classify_with_claude must
    truncate `text` alone to build the AI prompt, THEN append form_values —
    not truncate the two already concatenated together (which cut the
    [FILLED-IN FORM FIELDS] block off entirely whenever the document's own
    text was already >=4000 chars)."""
    calls = {}

    class _Messages:
        def create(self, **kw):
            calls["content"] = kw["messages"][0]["content"]
            return __import__("types").SimpleNamespace(
                content=[__import__("types").SimpleNamespace(
                    type="text",
                    text='{"doc_type": "Legal", "vendor": "Residential Rental '
                         'Agreement - Lola J. Roa", "confidence": "High"}')])

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)

    long_text = "TERM AND CONDITIONS CLAUSE OF THE LEASE AGREEMENT " * 90
    assert len(long_text) >= 4000                      # the document's own text alone
    form_values = ("\n\n[FILLED-IN FORM FIELDS — a person typed these into this "
                   "document's form fields, so it is NOT a blank template]\n"
                   "Residents1: Lola J. Roa\nMonthlyRent: $2645.00")

    classifier.classify_with_claude(long_text, _config_with_key(), form_values=form_values)

    prompt_text = next(c["text"] for c in calls["content"] if c.get("type") == "text")
    assert "[FILLED-IN FORM FIELDS" in prompt_text
    assert "Lola J. Roa" in prompt_text
    assert "$2645.00" in prompt_text
