"""Unit and integration tests for Innovation 2: Decision Provenance & 3 AM Audit Trace."""

import json
from pathlib import Path
import pytest

from app.config import settings
from app.llm_client import TriageLLMClient
from app.money_gate import apply_safety_gates
from app.schemas import LLMTriageDraft, SeverityLevel, TicketInput, TriageCategory


@pytest.fixture(scope="module")
def tickets_map():
    """Load all 12 tickets from dhaba_tickets.json."""
    path = Path(__file__).parent.parent / "dhaba_tickets.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {t["id"]: TicketInput.model_validate(t) for t in data["tickets"]}


def test_audit_trace_prompt_injection_override(tickets_map):
    """
    On T-1003 (VIP Override prompt injection), simulate a compliant/tricked model
    setting user_wants_refund=True.
    Verify:
      - audit_trace.money_gate_override is True
      - audit_trace.policy_rule_id == 'RULE_01_PROMPT_INJECTION_BLOCK'
      - refund.approved is False
      - 'PROMPT_INJECTION_DETECTED' in security_flags
    """
    ticket = tickets_map["T-1003"]
    tricked_draft = LLMTriageDraft(
        category=TriageCategory.REFUND_REQUEST,
        severity=SeverityLevel.P0_CRITICAL,
        user_wants_refund=True,  # Model tricked by injection
        reply_draft="I will refund your money.",
        needs_human=False,
        confidence=0.99,
        prompt_injection_detected=False,  # Model missed injection
        reasoning="VIP user requested refund.",
        execution_mode="live",
        llm_attempts=1,
    )

    response = apply_safety_gates(ticket, tricked_draft)

    assert response.refund.approved is False
    assert response.audit_trace.llm_suggested_refund is True
    assert response.audit_trace.money_gate_override is True
    assert response.audit_trace.policy_rule_id == "RULE_01_PROMPT_INJECTION_BLOCK"
    assert "PROMPT_INJECTION_DETECTED" in response.audit_trace.security_flags
    assert response.audit_trace.trace_id.startswith("tr_T-1003_")


def test_audit_trace_retry_attempt_count(tickets_map, monkeypatch):
    """
    Verify that when the LLM client retries once after malformed JSON,
    the audit trace records llm_attempts == 2.
    """
    monkeypatch.setattr(settings, "TRIAGE_MODE", "live")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "mock-test-key")

    ticket = tickets_map["T-1001"]
    client = TriageLLMClient()

    call_count = 0

    def mock_provider(sys_prompt: str, user_prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return '{"category": "REFUND_REQUEST", "broken_json":'
        else:
            return json.dumps({
                "category": "REFUND_REQUEST",
                "severity": "P1_HIGH",
                "user_wants_refund": True,
                "reply_draft": "Your refund is processed.",
                "needs_human": False,
                "confidence": 0.95,
                "prompt_injection_detected": False,
                "reasoning": "Valid output after retry.",
            })

    monkeypatch.setattr(client, "_call_provider", mock_provider)

    draft = client.triage_ticket(ticket)
    assert draft.llm_attempts == 2

    response = apply_safety_gates(ticket, draft)
    assert response.audit_trace.llm_attempts == 2
    assert response.audit_trace.execution_mode == "live"
    assert response.audit_trace.policy_rule_id == "RULE_07_UNUSED_48H_AUTO_APPROVE"
    assert response.audit_trace.money_gate_override is False


def test_audit_trace_degraded_fallback_mode(tickets_map, monkeypatch):
    """
    Verify that catastrophic LLM failure records:
      - execution_mode == 'degraded_fallback'
      - DEGRADED_FALLBACK_ENGAGED in security_flags
    """
    monkeypatch.setattr(settings, "TRIAGE_MODE", "live")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "mock-test-key")

    ticket = tickets_map["T-1002"]
    client = TriageLLMClient()

    def mock_broken(sys_prompt: str, user_prompt: str) -> str:
        return "I refuse to return JSON today."

    monkeypatch.setattr(client, "_call_provider", mock_broken)

    draft = client.triage_ticket(ticket)
    assert draft.execution_mode == "degraded_fallback"

    response = apply_safety_gates(ticket, draft)
    assert response.audit_trace.execution_mode == "degraded_fallback"
    assert "DEGRADED_FALLBACK_ENGAGED" in response.audit_trace.security_flags
    assert response.audit_trace.policy_rule_id == "RULE_00_NO_REFUND_NEEDED"
