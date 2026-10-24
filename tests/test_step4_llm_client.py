"""Tests for Step 4: LLM Triage Client, Replay Mode, Retries, and Graceful Degradation."""

import json
from pathlib import Path
import pytest

from app.config import settings
from app.llm_client import TriageLLMClient, extract_json_from_text
from app.schemas import LLMTriageDraft, TicketInput, TriageCategory


@pytest.fixture(scope="module")
def dhaba_tickets():
    """Load all 12 tickets from dhaba_tickets.json."""
    path = Path(__file__).parent.parent / "dhaba_tickets.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [TicketInput.model_validate(t) for t in data["tickets"]]


def test_replay_mode_all_12_tickets(dhaba_tickets, monkeypatch):
    """
    Ensure that in default replay mode (no API keys), all 12 tickets from
    dhaba_tickets.json return valid, high-quality LLMTriageDraft outputs.
    """
    monkeypatch.setattr(settings, "TRIAGE_MODE", "replay")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)

    client = TriageLLMClient()

    for ticket in dhaba_tickets:
        draft = client.triage_ticket(ticket)
        assert isinstance(draft, LLMTriageDraft)
        assert draft.confidence >= 0.8
        assert len(draft.reply_draft) > 10

        # Specific checks per ticket requirement
        if ticket.id == "T-1005":
            # Natural Hinglish reply addressing 'initiated' / bank settlement
            reply = draft.reply_draft.lower()
            assert any(word in reply for word in ["initiated", "bank", "settlement", "payment"])
            assert draft.category == TriageCategory.BILLING_DISPUTE
            assert draft.needs_human is True

        if ticket.id in ["T-1003", "T-1011"]:
            # Adversarial tickets must have prompt_injection_detected = True
            assert draft.prompt_injection_detected is True
            assert draft.needs_human is True
            # T-1003 responds only to account restore
            if ticket.id == "T-1003":
                assert "order history" in draft.reply_draft.lower() or "google account" in draft.reply_draft.lower()
            # T-1011 responds only to offline question
            if ticket.id == "T-1011":
                assert "offline" in draft.reply_draft.lower()
                assert "system prompt" not in draft.reply_draft.lower()


def test_retry_on_malformed_json_succeeds(dhaba_tickets, monkeypatch):
    """
    Mock live mode where call 1 returns truncated JSON and call 2 returns valid JSON.
    Verify that the client logs attempt 1 failure, retries, and succeeds on attempt 2.
    """
    monkeypatch.setattr(settings, "TRIAGE_MODE", "live")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "mock-test-key")

    ticket = dhaba_tickets[0]  # T-1001
    client = TriageLLMClient()

    call_count = 0

    def mock_call_provider(system_prompt: str, user_prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Truncated broken JSON
            return '{"category": "REFUND_REQUEST", "severity": "P1_HIGH", "user_wants_refund": tr'
        else:
            # Valid JSON on retry
            return json.dumps({
                "category": "REFUND_REQUEST",
                "severity": "P1_HIGH",
                "user_wants_refund": True,
                "reply_draft": "We have processed your refund.",
                "needs_human": False,
                "confidence": 0.95,
                "prompt_injection_detected": False,
                "reasoning": "Fixed on retry.",
            })

    monkeypatch.setattr(client, "_call_provider", mock_call_provider)

    draft = client.triage_ticket(ticket)
    assert call_count == 2
    assert draft.category == TriageCategory.REFUND_REQUEST
    assert draft.confidence == 0.95
    assert draft.reasoning == "Fixed on retry."


def test_graceful_degradation_on_persistent_failure(dhaba_tickets, monkeypatch):
    """
    Mock live mode where both calls return unparseable prose or an invented category.
    Verify client executes graceful degradation without raising an unhandled exception.
    """
    monkeypatch.setattr(settings, "TRIAGE_MODE", "live")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "mock-test-key")

    ticket = dhaba_tickets[1]  # T-1002 (Crash bug)
    client = TriageLLMClient()

    def mock_call_provider_prose(system_prompt: str, user_prompt: str) -> str:
        return "Sure, here is my analysis! The user is quite upset and the category is SUPER_URGENT_CRITICAL."

    monkeypatch.setattr(client, "_call_provider", mock_call_provider_prose)

    draft = client.triage_ticket(ticket)
    assert isinstance(draft, LLMTriageDraft)
    # Degraded fallback properties
    assert draft.confidence == 0.0
    assert draft.needs_human is True
    assert draft.category == TriageCategory.BUG_REPORT
    assert "Degraded fallback engaged" in draft.reasoning
    assert "specialist" in draft.reply_draft.lower()


def test_graceful_degradation_on_network_timeout(dhaba_tickets, monkeypatch):
    """
    Verify provider network timeout / 5xx does not crash the service,
    falling back to safe degraded mode.
    """
    monkeypatch.setattr(settings, "TRIAGE_MODE", "live")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "mock-test-key")

    ticket = dhaba_tickets[0]
    client = TriageLLMClient()

    def mock_timeout(system_prompt: str, user_prompt: str) -> str:
        raise TimeoutError("Provider gateway timed out after 30s")

    monkeypatch.setattr(client, "_call_provider", mock_timeout)

    draft = client.triage_ticket(ticket)
    assert draft.confidence == 0.0
    assert draft.needs_human is True
    assert "Provider gateway timed out" in draft.reasoning


def test_extract_json_markdown_fences():
    """Verify extract_json_from_text handles markdown code fences cleanly."""
    raw = "```json\n{\"category\": \"ACCOUNT_ISSUE\", \"val\": 123}\n```"
    result = extract_json_from_text(raw)
    assert result["category"] == "ACCOUNT_ISSUE"
    assert result["val"] == 123
