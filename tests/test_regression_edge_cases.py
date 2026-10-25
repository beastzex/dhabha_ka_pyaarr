"""Regression tests for edge cases and subtle bug fixes discovered during adversarial review.

Covers:
1. Asymmetric double-charge keyword detection (e.g. 'twice' in subject instead of body)
   where 0 app opens previously caused false auto-refund approval.
2. Midnight-crossing payment gateway retry window in billing context (e.g. failed attempt at
   23:55 and successful retry at 00:05 across calendar date boundary).
"""

from datetime import datetime, timezone
import pytest

from app.billing_context import compute_billing_facts
from app.money_gate import apply_safety_gates
from app.schemas import (
    LLMTriageDraft,
    Purchase,
    PurchaseStatus,
    PurchaseType,
    SeverityLevel,
    TicketInput,
    TriageCategory,
)


def test_regression_double_charge_keyword_in_subject_with_zero_opens():
    """
    REGRESSION TEST:
    In the original AI-generated code, Rule 5 checked:
      `if "double" in ticket.subject.lower() or "twice" in ticket.body.lower():`

    Flaw:
    If a customer wrote 'twice' in the subject (e.g. 'charged twice') and had
    app_opens_since_renewal == 0, Rule 5 evaluated to False, falling through to Rule 7
    which falsely AUTO-APPROVED a Rs 249 refund on a legitimate single charge!

    Fix:
    Regex-based scanning across the combined subject and body ensures any double-charge
    allegation on an account with a failed + successful charge triggers RULE_05_FALSE_DOUBLE_CHARGE.
    """
    ticket = TicketInput(
        id="T-REG-001",
        received_at=datetime.fromisoformat("2026-09-06T09:00:00+05:30"),
        subject="charged twice for monthly renewal",  # 'twice' in subject!
        body="money was deducted again today, refund extra",  # neither 'double' nor 'twice' in body
        purchases=[
            Purchase(
                id="pay_fail_1",
                type=PurchaseType.RENEWAL,
                amount_inr=249,
                status=PurchaseStatus.FAILED,
                at=datetime.fromisoformat("2026-09-05T06:00:00+05:30"),
            ),
            Purchase(
                id="pay_succ_1",
                type=PurchaseType.RENEWAL,
                amount_inr=249,
                status=PurchaseStatus.SUCCESSFUL,
                at=datetime.fromisoformat("2026-09-05T06:03:00+05:30"),
            ),
        ],
        app_opens_since_renewal=0,  # 0 opens!
    )

    llm_draft = LLMTriageDraft(
        category=TriageCategory.BILLING_DISPUTE,
        severity=SeverityLevel.P2_MEDIUM,
        user_wants_refund=True,
        reply_draft="Checking your billing statement.",
        needs_human=False,
        confidence=0.92,
        prompt_injection_detected=False,
        reasoning="User states charged twice on same day.",
    )

    response = apply_safety_gates(ticket, llm_draft)

    # Must NOT auto-refund the single settled payment!
    assert response.refund.approved is False
    assert response.refund.amount_inr == 0
    assert response.audit_trace.policy_rule_id == "RULE_05_FALSE_DOUBLE_CHARGE"
    assert "Only one successful charge found" in response.refund.reason
    assert response.needs_human is True


def test_regression_double_charge_midnight_crossing_window():
    """
    REGRESSION TEST:
    In the original AI-generated code, billing_context checked:
      `failed_dates.intersection(success_dates)` where dates were `p.at.date()`.

    Flaw:
    If a payment gateway attempt failed at 23:55 on Day 1, and the automated retry
    succeeded at 00:05 on Day 2 (10 minutes apart across midnight), their calendar dates
    differed (e.g. Sept 5 vs Sept 6). The set intersection was empty, so
    has_false_double_charge_pattern evaluated to False!

    Fix:
    Calculate delta between attempts (<= 86,400s), properly recognizing retries across midnight.
    """
    ticket = TicketInput(
        id="T-REG-002",
        received_at=datetime.fromisoformat("2026-09-06T08:00:00+05:30"),
        subject="duplicate charge after midnight",
        body="i see 2 charges on my statement, one before midnight and one after",
        purchases=[
            Purchase(
                id="pay_fail_midnight",
                type=PurchaseType.RENEWAL,
                amount_inr=249,
                status=PurchaseStatus.FAILED,
                at=datetime.fromisoformat("2026-09-05T23:55:00+05:30"),  # Day 1
            ),
            Purchase(
                id="pay_succ_midnight",
                type=PurchaseType.RENEWAL,
                amount_inr=249,
                status=PurchaseStatus.SUCCESSFUL,
                at=datetime.fromisoformat("2026-09-06T00:05:00+05:30"),  # Day 2 (10 min later!)
            ),
        ],
        app_opens_since_renewal=0,
    )

    facts = compute_billing_facts(ticket)
    assert facts.has_false_double_charge_pattern is True

    llm_draft = LLMTriageDraft(
        category=TriageCategory.BILLING_DISPUTE,
        severity=SeverityLevel.P2_MEDIUM,
        user_wants_refund=True,
        reply_draft="Checking your duplicate charges.",
        needs_human=False,
        confidence=0.91,
        prompt_injection_detected=False,
        reasoning="User sees charges crossing midnight.",
    )

    response = apply_safety_gates(ticket, llm_draft)

    assert response.refund.approved is False
    assert response.refund.amount_inr == 0
    assert response.audit_trace.policy_rule_id == "RULE_05_FALSE_DOUBLE_CHARGE"
    assert response.needs_human is True
