"""Tests for Step 3: SQLite Idempotency Store and Conflict Handling."""

import json
from pathlib import Path
import pytest

from app.idempotency import (
    IdempotencyConflictError,
    IdempotencyStore,
    compute_payload_hash,
)
from app.schemas import (
    AuditTrace,
    RefundDecision,
    SeverityLevel,
    TicketInput,
    TriageCategory,
    TriageResponse,
)


@pytest.fixture
def temp_store(tmp_path):
    """Provide an isolated IdempotencyStore backed by a temporary SQLite database."""
    db_file = tmp_path / "test_idempotency.db"
    return IdempotencyStore(db_path=str(db_file))


@pytest.fixture
def t1001_ticket():
    """Load T-1001 from dhaba_tickets.json."""
    path = Path(__file__).parent.parent / "dhaba_tickets.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for raw in data["tickets"]:
        if raw["id"] == "T-1001":
            return TicketInput.model_validate(raw)
    raise ValueError("T-1001 not found")


@pytest.fixture
def t1001_response():
    """Sample valid TriageResponse for T-1001."""
    return TriageResponse(
        ticket_id="T-1001",
        category=TriageCategory.REFUND_REQUEST,
        severity=SeverityLevel.P1_HIGH,
        refund=RefundDecision(
            approved=True,
            amount_inr=249,
            payment_id="pay_A2",
            reason="Approved: unused monthly renewal (0 app opens) reported within 48h window.",
        ),
        reply_draft="We have processed a full refund of Rs 249 for your unused renewal.",
        needs_human=False,
        confidence=0.96,
        audit_trace=AuditTrace(
            trace_id="tr_T-1001_091a4158",
            execution_mode="replay",
            llm_attempts=1,
            llm_suggested_refund=True,
            money_gate_override=False,
            policy_rule_id="RULE_07_UNUSED_48H_AUTO_APPROVE",
            security_flags=[],
        ),
    )


def test_idempotent_save_and_retrieval(temp_store, t1001_ticket, t1001_response):
    """
    1. Save a TriageResponse for T-1001.
    2. Query it a second time with identical payload.
    3. Verify exact match returned.
    """
    payload_hash = compute_payload_hash(t1001_ticket)

    # Initial lookup returns None
    assert temp_store.get_existing(t1001_ticket.id, payload_hash) is None

    # First save succeeds
    inserted = temp_store.save_result(t1001_ticket.id, payload_hash, t1001_response)
    assert inserted is True

    # Second lookup returns exact cached object
    cached = temp_store.get_existing(t1001_ticket.id, payload_hash)
    assert cached is not None
    assert cached.ticket_id == "T-1001"
    assert cached.refund.approved is True
    assert cached.refund.amount_inr == 249
    assert cached.refund.payment_id == "pay_A2"
    assert cached.category == TriageCategory.REFUND_REQUEST
    assert cached.reply_draft == t1001_response.reply_draft


def test_idempotency_conflict_on_modified_body(temp_store, t1001_ticket, t1001_response):
    """
    Verify that submitting the same ticket_id with modified body/subject
    triggers an IdempotencyConflictError.
    """
    original_hash = compute_payload_hash(t1001_ticket)
    temp_store.save_result(t1001_ticket.id, original_hash, t1001_response)

    # Modify the ticket content (same ID, altered body)
    modified_ticket = t1001_ticket.model_copy(
        update={"body": "Modified content: actually never mind do not refund."}
    )
    modified_hash = compute_payload_hash(modified_ticket)
    assert modified_hash != original_hash

    with pytest.raises(IdempotencyConflictError) as exc_info:
        temp_store.get_existing(t1001_ticket.id, modified_hash)

    assert "different payload" in str(exc_info.value)
    assert exc_info.value.ticket_id == "T-1001"


def test_concurrent_duplicate_insert_ignored(temp_store, t1001_ticket, t1001_response):
    """
    Verify INSERT OR IGNORE behavior: a second insert attempt for the same ticket_id
    returns False and does not alter the original record.
    """
    payload_hash = compute_payload_hash(t1001_ticket)

    # First insert succeeds
    first_save = temp_store.save_result(t1001_ticket.id, payload_hash, t1001_response)
    assert first_save is True

    # Duplicate attempt with modified response object
    tampered_response = t1001_response.model_copy(
        update={
            "refund": RefundDecision(
                approved=True,
                amount_inr=9999,
                reason="Tampered duplicate refund",
            )
        }
    )

    second_save = temp_store.save_result(t1001_ticket.id, payload_hash, tampered_response)
    assert second_save is False

    # Stored record remains original (amount is still 249, not 9999)
    current = temp_store.get_existing(t1001_ticket.id, payload_hash)
    assert current is not None
    assert current.refund.amount_inr == 249
