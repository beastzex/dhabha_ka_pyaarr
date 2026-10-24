"""Unit tests for Innovation 1: Deterministic Billing Reality Fact-Sheet Injector."""

import json
from pathlib import Path
import pytest

from app.billing_context import BillingFactSheet, compute_billing_facts
from app.llm_client import llm_client
from app.schemas import TicketInput


@pytest.fixture(scope="module")
def tickets_map():
    """Load dhaba_tickets.json as a dictionary keyed by ticket ID."""
    path = Path(__file__).parent.parent / "dhaba_tickets.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {t["id"]: TicketInput.model_validate(t) for t in data["tickets"]}


def test_t1005_billing_facts_unsettled_claim(tickets_map):
    """
    T-1005: User claims Rs 249 debited from bank, but pay_E2 is 'initiated'.
    Only Rs 1 trial (pay_E1) was settled.
    """
    ticket = tickets_map["T-1005"]
    facts = compute_billing_facts(ticket)

    assert facts.total_settled_inr == 1
    assert facts.settled_renewal_count == 0
    assert facts.latest_settled_renewal_id is None
    assert facts.latest_settled_renewal_inr == 0
    assert facts.pending_initiated_ids == ["pay_E2"]
    assert facts.has_unsettled_payment_claim is True
    assert facts.has_false_double_charge_pattern is False

    prompt_block = facts.to_prompt_block()
    assert "<verified_billing_facts>" in prompt_block
    assert "</verified_billing_facts>" in prompt_block
    assert "pay_E2" in prompt_block
    assert "initiated" in prompt_block.lower()


def test_t1009_billing_facts_false_double_charge(tickets_map):
    """
    T-1009: User complains of double charge on same day.
    Telemetry proves pay_I1 failed and pay_I2 succeeded -> only 1 charge settled.
    """
    ticket = tickets_map["T-1009"]
    facts = compute_billing_facts(ticket)

    assert facts.settled_renewal_count == 1
    assert facts.latest_settled_renewal_id == "pay_I2"
    assert facts.latest_settled_renewal_inr == 249
    assert facts.failed_attempt_ids == ["pay_I1"]
    assert facts.has_false_double_charge_pattern is True
    assert facts.has_unsettled_payment_claim is False

    prompt_block = facts.to_prompt_block()
    assert "False Double-Charge Pattern: YES" in prompt_block
    assert "pay_I1" in prompt_block


def test_t1001_billing_facts_eligible_monthly(tickets_map):
    """
    T-1001: Rs 249 renewal (pay_A2) settled ~13.17 hours ago.
    Total settled = Rs 250 (1 trial + 249 renewal).
    """
    ticket = tickets_map["T-1001"]
    facts = compute_billing_facts(ticket)

    assert facts.total_settled_inr == 250
    assert facts.settled_renewal_count == 1
    assert facts.latest_settled_renewal_id == "pay_A2"
    assert facts.latest_settled_renewal_inr == 249
    assert facts.hours_since_latest_settled_renewal is not None
    assert 13.0 < facts.hours_since_latest_settled_renewal < 13.5
    assert facts.has_unsettled_payment_claim is False
    assert facts.has_false_double_charge_pattern is False


def test_t1011_billing_facts_zero_purchases(tickets_map):
    """
    T-1011: Pre-sales query with no prior purchases (empty list).
    """
    ticket = tickets_map["T-1011"]
    facts = compute_billing_facts(ticket)

    assert facts.total_settled_inr == 0
    assert facts.settled_renewal_count == 0
    assert facts.latest_settled_renewal_id is None
    assert facts.latest_settled_renewal_inr == 0
    assert facts.hours_since_latest_settled_renewal is None
    assert facts.pending_initiated_ids == []
    assert facts.failed_attempt_ids == []
    assert facts.has_false_double_charge_pattern is False
    assert facts.has_unsettled_payment_claim is False


def test_llm_client_prompt_contains_billing_facts(tickets_map):
    """
    Verify that format_ticket_prompt embeds the <verified_billing_facts> XML block
    alongside the untrusted ticket content.
    """
    ticket = tickets_map["T-1005"]
    prompt = llm_client.format_ticket_prompt(ticket)

    assert "<verified_billing_facts>" in prompt
    assert "</verified_billing_facts>" in prompt
    assert "<untrusted_ticket>" in prompt
    assert "</untrusted_ticket>" in prompt
    assert "pay_E2" in prompt
