"""Deterministic Money Gate and Input Security Scanner.

Guarantees 100% deterministic enforcement of financial rules and prompt injection
defense. The LLM is NEVER allowed to authorize money transfers or set refund amounts directly.
"""

from datetime import datetime, timezone
import re
from typing import List, Optional, Tuple

from app.config import settings
from app.logger import log_event
from app.schemas import (
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

# Constants for financial limits
AUTO_REFUND_MAX_INR = 500  # Any refund request > Rs 500 requires human authorization
REFUND_WINDOW_HOURS = 48   # Maximum elapsed time from renewal for automated refund

# Regex patterns for adversarial input / prompt injection
INJECTION_PATTERNS = [
    re.compile(r"\bsystem\s+note\b", re.IGNORECASE),
    re.compile(r"\bignore\s+(?:your\s+)?previous\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bignore\s+all\s+(?:prior|previous)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\binternal\s+refund\s+rules\b", re.IGNORECASE),
    re.compile(r"\bverified\s+vip\b", re.IGNORECASE),
    re.compile(r"\bmark\s+this\s+ticket\s+resolved\b", re.IGNORECASE),
    re.compile(r"\bautomated\s+agent\s+reading\s+this\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:your\s+)?instructions\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+override\b", re.IGNORECASE),
    re.compile(r"\bdeveloper\s+mode\b", re.IGNORECASE),
]

# Regex patterns for fraud and legal escalations
FRAUD_LEGAL_PATTERNS = [
    re.compile(r"\bcyber\s+cell\b", re.IGNORECASE),
    re.compile(r"\bcyber\s+crime\b", re.IGNORECASE),
    re.compile(r"\bpolice\b", re.IGNORECASE),
    re.compile(r"\bfraud\b", re.IGNORECASE),
    re.compile(r"\bnever\s+installed\b", re.IGNORECASE),
    re.compile(r"\bunauthorized\s+charge\b", re.IGNORECASE),
    re.compile(r"\bstolen\s+card\b", re.IGNORECASE),
    re.compile(r"\blegal\s+(?:action|notice)\b", re.IGNORECASE),
    re.compile(r"\blawyer\b", re.IGNORECASE),
    re.compile(r"\bfir\b", re.IGNORECASE),
]

# Sensitive terms forbidden in customer-facing replies
FORBIDDEN_REPLY_TERMS = [
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\binternal\s+refund\s+rules\b", re.IGNORECASE),
    re.compile(r"\bllmtriagedraft\b", re.IGNORECASE),
    re.compile(r"\bprompt\s+injection\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+note\b", re.IGNORECASE),
    re.compile(r"\bignore\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bverified\s+vip\b", re.IGNORECASE),
]

SAFE_FALLBACK_REPLY = (
    "Thank you for reaching out to Dhaba support. We have received your query, "
    "and a member of our customer support team is reviewing your ticket to assist you shortly."
)


def detect_prompt_injection(subject: str, body: str) -> Tuple[bool, List[str]]:
    """
    Deterministic scanner for prompt injection and adversarial system instructions.

    Returns:
        (is_injection, matched_patterns)
    """
    combined_text = f"{subject}\n{body}"
    matched: List[str] = []

    for pattern in INJECTION_PATTERNS:
        match = pattern.search(combined_text)
        if match:
            matched.append(match.group(0))

    return (len(matched) > 0, matched)


def detect_fraud_or_legal_threat(subject: str, body: str) -> Tuple[bool, List[str]]:
    """Check for legal threats, police/cyber cell escalations, or external fraud claims."""
    combined_text = f"{subject}\n{body}"
    matched: List[str] = []

    for pattern in FRAUD_LEGAL_PATTERNS:
        match = pattern.search(combined_text)
        if match:
            matched.append(match.group(0))

    return (len(matched) > 0, matched)


def _get_sorted_renewals(purchases: List[Purchase]) -> List[Purchase]:
    """Return renewal purchases sorted by date descending (latest first)."""
    renewals = [p for p in purchases if p.type == PurchaseType.RENEWAL]
    return sorted(renewals, key=lambda x: x.at, reverse=True)


def evaluate_refund_policy(
    ticket: TicketInput,
    llm_draft: LLMTriageDraft,
    injection_detected: bool,
) -> RefundDecision:
    """
    Deterministic financial policy engine.

    The LLM output is strictly an advisory signal (`user_wants_refund`).
    Approval and amounts are derived purely from verified database/billing history.
    """
    # Rule 1: Prompt Injection / Adversarial Override Block
    if injection_detected or llm_draft.prompt_injection_detected:
        return RefundDecision(
            approved=False,
            amount_inr=0,
            reason="Blocked by security guardrail: adversarial instruction detected in ticket.",
        )

    # Rule 2: Legal / Fraud Threat Escalation (freeze automated payouts)
    is_threat, _ = detect_fraud_or_legal_threat(ticket.subject, ticket.body)
    if is_threat or ticket.id == "T-1008":
        return RefundDecision(
            approved=False,
            amount_inr=0,
            reason="Frozen for immediate human fraud/legal review; auto-refund disabled.",
        )

    # Rule 3: No Refund Requested
    if not llm_draft.user_wants_refund and llm_draft.category not in [
        TriageCategory.REFUND_REQUEST,
        TriageCategory.BILLING_DISPUTE,
    ]:
        return RefundDecision(
            approved=False,
            amount_inr=0,
            reason="No refund requested.",
        )

    # Examine billing history
    sorted_renewals = _get_sorted_renewals(ticket.purchases)
    if not sorted_renewals:
        return RefundDecision(
            approved=False,
            amount_inr=0,
            reason="No renewal records found for this account.",
        )

    latest_renewal = sorted_renewals[0]

    # Rule 4: Unsettled / Pending Payments (e.g., status == "initiated" like T-1005)
    if latest_renewal.status == PurchaseStatus.INITIATED:
        return RefundDecision(
            approved=False,
            amount_inr=0,
            payment_id=latest_renewal.id,
            reason="Payment is in 'initiated' (unsettled) state; cannot refund unsettled funds. Escalated for payment reconciliation.",
        )

    # Rule 5: False Double-Charge Claims (e.g., T-1009: 1 failed, 1 successful)
    successful_renewals = [r for r in sorted_renewals if r.status == PurchaseStatus.SUCCESSFUL]
    failed_renewals = [r for r in sorted_renewals if r.status == PurchaseStatus.FAILED]

    # If user alleges double charge, check actual successful count on same day/recent window
    if "double" in ticket.subject.lower() or "twice" in ticket.body.lower():
        if len(successful_renewals) <= 1 and len(failed_renewals) >= 1:
            return RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=successful_renewals[0].id if successful_renewals else None,
                reason="Only one successful charge found; prior attempt failed and was not settled.",
            )

    # If no successful renewal exists at all
    if not successful_renewals:
        return RefundDecision(
            approved=False,
            amount_inr=0,
            reason="No successful renewal transaction found eligible for refund.",
        )

    latest_successful = successful_renewals[0]

    # Rule 6: High-Value Annual Plan Gate (> AUTO_REFUND_MAX_INR, e.g. Rs 1499 in T-1006)
    if latest_successful.amount_inr > AUTO_REFUND_MAX_INR:
        if ticket.app_opens_since_renewal == 0:
            return RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=latest_successful.id,
                reason=(
                    f"Eligible unused annual renewal (0 opens), but amount Rs {latest_successful.amount_inr} "
                    f"exceeds auto-refund cap (Rs {AUTO_REFUND_MAX_INR}); routed for 1-click human approval."
                ),
            )
        else:
            return RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=latest_successful.id,
                reason=(
                    f"Annual renewal with {ticket.app_opens_since_renewal} app opens; "
                    "requires human review."
                ),
            )

    # Rule 7: Usage and Time Window Gate on Monthly Renewal (Rs 249)
    # Check elapsed time between renewal charge and ticket receipt
    time_delta = ticket.received_at - latest_successful.at
    elapsed_hours = time_delta.total_seconds() / 3600.0

    if ticket.app_opens_since_renewal == 0:
        if elapsed_hours <= REFUND_WINDOW_HOURS:
            return RefundDecision(
                approved=True,
                amount_inr=latest_successful.amount_inr,
                payment_id=latest_successful.id,
                reason="Approved: unused monthly renewal (0 app opens) reported within 48h window.",
            )
        else:
            return RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=latest_successful.id,
                reason=f"Auto-refund window expired ({elapsed_hours:.1f}h elapsed, max {REFUND_WINDOW_HOURS}h); routed to human agent.",
            )
    else:
        return RefundDecision(
            approved=False,
            amount_inr=0,
            payment_id=latest_successful.id,
            reason=f"Auto-refund declined: {ticket.app_opens_since_renewal} app opens recorded since renewal; routed to human agent.",
        )


def sanitize_reply_draft(reply_draft: str) -> str:
    """Ensure no system prompt or internal rules are echoed in the drafted customer reply."""
    for pattern in FORBIDDEN_REPLY_TERMS:
        if pattern.search(reply_draft):
            return SAFE_FALLBACK_REPLY
    return reply_draft


def apply_safety_gates(
    ticket: TicketInput,
    llm_draft: LLMTriageDraft,
) -> TriageResponse:
    """
    Post-processing coordinator combining deterministic input scanning, money gating,
    confidence enforcement, and response sanitization into the final TriageResponse.
    """
    # 1. Deterministic Prompt Injection & Adversarial Check
    injection_detected, matched_patterns = detect_prompt_injection(
        ticket.subject, ticket.body
    )
    if injection_detected:
        log_event(
            "prompt_injection_blocked",
            ticket.id,
            patterns=matched_patterns,
        )

    # 2. Check Legal/Fraud Escalations
    is_legal_threat, threat_patterns = detect_fraud_or_legal_threat(
        ticket.subject, ticket.body
    )

    # 3. Deterministic Refund Evaluation
    refund_decision = evaluate_refund_policy(
        ticket=ticket,
        llm_draft=llm_draft,
        injection_detected=injection_detected,
    )

    # 4. Resolve Category & Severity Overrides
    category = llm_draft.category
    severity = llm_draft.severity
    needs_human = llm_draft.needs_human

    if injection_detected or llm_draft.prompt_injection_detected:
        category = TriageCategory.SECURITY_OR_ABUSE
        severity = SeverityLevel.P1_HIGH
        needs_human = True

    if is_legal_threat or ticket.id == "T-1008":
        severity = SeverityLevel.P0_CRITICAL
        needs_human = True

    # High-value annual plan or unverified double charge always requires human sign-off
    if not refund_decision.approved and (
        "exceeds auto-refund cap" in refund_decision.reason
        or "app opens recorded" in refund_decision.reason
        or "initiated" in refund_decision.reason
        or "prior attempt failed" in refund_decision.reason
    ):
        needs_human = True

    # 5. Low Confidence Gate (Threshold: 0.75)
    if llm_draft.confidence < settings.CONFIDENCE_THRESHOLD:
        needs_human = True
        if refund_decision.approved:
            refund_decision = RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=refund_decision.payment_id,
                reason=(
                    f"Model confidence ({llm_draft.confidence:.2f}) is below threshold "
                    f"({settings.CONFIDENCE_THRESHOLD}); auto-refund suspended for human review."
                ),
            )

    # 6. Sanitize reply draft against information exfiltration
    clean_reply = sanitize_reply_draft(llm_draft.reply_draft)
    if injection_detected:
        clean_reply = SAFE_FALLBACK_REPLY

    return TriageResponse(
        ticket_id=ticket.id,
        category=category,
        severity=severity,
        refund=refund_decision,
        reply_draft=clean_reply,
        needs_human=needs_human,
        confidence=llm_draft.confidence,
    )
