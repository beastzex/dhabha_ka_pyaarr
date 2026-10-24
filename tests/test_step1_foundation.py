"""Test Step 1 Foundation: Schemas, Configuration, and PII-Safe Logging."""

import json
import logging
from pathlib import Path
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logger import (
    JSONFormatter,
    PIIScrubbingFilter,
    log_event,
    logger,
    scrub_pii,
)
from app.schemas import (
    AuditTrace,
    DhabaTicketsFile,
    LLMTriageDraft,
    Purchase,
    PurchaseStatus,
    PurchaseType,
    RefundDecision,
    SeverityLevel,
    TicketInput,
    TriageCategory,
    TriageResponse,
)


def test_load_and_validate_all_12_dhaba_tickets():
    """Verify all 12 tickets from dhaba_tickets.json parse cleanly into TicketInput."""
    json_path = Path(__file__).parent.parent / "dhaba_tickets.json"
    assert json_path.exists(), f"dhaba_tickets.json not found at {json_path}"

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate file wrapper schema
    tickets_file = DhabaTicketsFile.model_validate(data)
    assert len(tickets_file.tickets) == 12

    # Validate each individual ticket model
    for raw_ticket in data["tickets"]:
        ticket = TicketInput.model_validate(raw_ticket)
        assert ticket.id.startswith("T-")
        assert ticket.received_at is not None
        assert isinstance(ticket.body, str)
        assert isinstance(ticket.subject, str)
        assert ticket.app_opens_since_renewal >= 0
        for p in ticket.purchases:
            assert isinstance(p, Purchase)
            assert p.amount_inr >= 0
            assert p.type in [PurchaseType.TRIAL, PurchaseType.RENEWAL]
            assert p.status in [
                PurchaseStatus.INITIATED,
                PurchaseStatus.FAILED,
                PurchaseStatus.SUCCESSFUL,
                PurchaseStatus.RENEWAL,
            ]


def test_invalid_category_fails_validation():
    """Verify unknown category string fails Pydantic schema validation."""
    with pytest.raises(ValidationError):
        TriageResponse(
            ticket_id="T-1001",
            category="INVALID_NON_EXISTENT_CATEGORY",  # type: ignore
            severity=SeverityLevel.P1_HIGH,
            refund=RefundDecision(approved=False, amount_inr=0, reason="None"),
            reply_draft="Hello",
            needs_human=False,
            confidence=0.9,
            audit_trace=AuditTrace(
                trace_id="tr_T-1001_test",
                execution_mode="replay",
                llm_attempts=1,
                llm_suggested_refund=False,
                money_gate_override=False,
                policy_rule_id="RULE_00_NO_REFUND_NEEDED",
                security_flags=[],
            ),
        )


def test_negative_refund_amount_fails_validation():
    """Verify negative refund amount fails Pydantic Field(ge=0) constraint."""
    with pytest.raises(ValidationError) as exc_info:
        RefundDecision(
            approved=True,
            amount_inr=-249,
            reason="Illegal negative amount",
        )
    assert "Input should be greater than or equal to 0" in str(exc_info.value)


def test_extra_fields_forbidden():
    """Verify extra='forbid' prevents unauthorized schema fields."""
    with pytest.raises(ValidationError):
        RefundDecision(
            approved=True,
            amount_inr=249,
            reason="Valid reason",
            unauthorized_field="exploit",  # type: ignore
        )

    with pytest.raises(ValidationError):
        TicketInput(
            id="T-9999",
            received_at="2026-09-02T09:14:00+05:30",
            subject="Test",
            body="Test body",
            purchases=[],
            app_opens_since_renewal=0,
            vip_override=True,  # type: ignore
        )


def test_confidence_range_validation():
    """Verify confidence must be between 0.0 and 1.0."""
    with pytest.raises(ValidationError):
        TriageResponse(
            ticket_id="T-1001",
            category=TriageCategory.BUG_REPORT,
            severity=SeverityLevel.P1_HIGH,
            refund=RefundDecision(approved=False, amount_inr=0, reason="None"),
            reply_draft="Hello",
            needs_human=False,
            confidence=1.5,  # Exceeds 1.0
            audit_trace=AuditTrace(
                trace_id="tr_T-1001_test",
                execution_mode="replay",
                llm_attempts=1,
                llm_suggested_refund=False,
                money_gate_override=False,
                policy_rule_id="RULE_00_NO_REFUND_NEEDED",
                security_flags=[],
            ),
        )

    with pytest.raises(ValidationError):
        LLMTriageDraft(
            category=TriageCategory.BUG_REPORT,
            severity=SeverityLevel.P1_HIGH,
            user_wants_refund=False,
            reply_draft="Draft",
            needs_human=False,
            confidence=-0.1,  # Below 0.0
            reasoning="Test",
        )


def test_pii_scrubbing_ticket_t1010_acme_and_gstin(caplog):
    """
    Test that logging Ticket T-1010 text ('Acme Interiors, 29ABCDE1234F1Z5')
    redacts both the company name and GSTIN in the log output.
    """
    sample_text = "Acme Interiors, 29ABCDE1234F1Z5"

    with caplog.at_level(logging.INFO, logger="dhaba_triage"):
        logger.info(f"Customer business info: {sample_text}")

    captured_logs = caplog.text
    assert "29ABCDE1234F1Z5" not in captured_logs
    assert "Acme Interiors" not in captured_logs
    assert "[REDACTED_ENTITY]" in captured_logs
    assert "[REDACTED_GSTIN]" in captured_logs


def test_pii_scrubbing_emails_upi_and_numbers():
    """Verify emails, UPI handles, and credit card/phone numbers are redacted."""
    raw_text = (
        "Contact me at user@okhdfcbank or john.doe@example.com. "
        "My phone is +91 9876543210 and card is 4111-2222-3333-4444."
    )
    cleaned = scrub_pii(raw_text)

    assert "user@okhdfcbank" not in cleaned
    assert "john.doe@example.com" not in cleaned
    assert "9876543210" not in cleaned
    assert "4111-2222-3333-4444" not in cleaned
    assert "[REDACTED_EMAIL_UPI]" in cleaned
    assert "[REDACTED_NUMBER]" in cleaned


def test_log_event_forbids_body_and_subject():
    """Verify log_event rejects body and subject fields to prevent raw ticket leakage."""
    with pytest.raises(ValueError) as exc1:
        log_event("ticket_received", "T-1001", body="i only paid 1 rupee")
    assert "Direct logging of raw ticket 'body' is strictly forbidden" in str(exc1.value)

    with pytest.raises(ValueError) as exc2:
        log_event("ticket_received", "T-1001", subject="charged 249 without telling me")
    assert "Direct logging of raw ticket 'subject' is strictly forbidden" in str(exc2.value)


def test_log_event_scrubs_extra_fields(caplog):
    """Verify extra dictionary attributes passed to log_event are scrubbed of PII."""
    with caplog.at_level(logging.INFO, logger="dhaba_triage"):
        log_event(
            "triage_decision_made",
            "T-1010",
            category="INVOICE_REQUEST",
            tax_id="29ABCDE1234F1Z5",
            client="Acme Interiors",
        )

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.tax_id == "[REDACTED_GSTIN]"
    assert record.client == "[REDACTED_ENTITY]"
    assert "29ABCDE1234F1Z5" not in record.tax_id
    assert "Acme Interiors" not in record.client


def test_json_formatter_structure():
    """Verify JSONFormatter produces valid JSON with required metadata keys."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="dhaba_triage",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Structured event message",
        args=(),
        exc_info=None,
    )
    record.ticket_id = "T-1001"
    record.event_type = "triage_start"

    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "dhaba_triage"
    assert parsed["ticket_id"] == "T-1001"
    assert parsed["event_type"] == "triage_start"
    assert "timestamp" in parsed


def test_config_defaults_without_keys():
    """Verify default config has TRIAGE_MODE='replay' when no API keys are provided."""
    cfg = Settings(
        GEMINI_API_KEY=None,
        OPENAI_API_KEY=None,
        _env_file=None,  # Do not read .env
    )
    assert cfg.TRIAGE_MODE == "replay"
    assert cfg.DB_PATH == "triage_state.db"
    assert cfg.CONFIDENCE_THRESHOLD == 0.75
