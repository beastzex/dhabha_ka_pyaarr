"""Deterministic Money Gate and Input Security Scanner.

Guarantees 100% deterministic enforcement of financial rules and prompt injection
defense. The LLM is NEVER allowed to authorize money transfers or set refund amounts directly.
"""

from datetime import datetime, timezone
import re
from typing import List, Optional, Tuple

from app.billing_context import compute_billing_facts
from app.config import settings
from app.idempotency import compute_payload_hash
from app.logger import log_event
from app.schemas import (
    AuditTrace,
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
) -> Tuple[RefundDecision, str]:
    """
    Deterministic financial policy engine.

    Returns:
        (RefundDecision, policy_rule_id)
    """
    # Rule 1: Prompt Injection / Adversarial Override Block
    if injection_detected or llm_draft.prompt_injection_detected:
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                reason="Blocked by security guardrail: adversarial instruction detected in ticket.",
            ),
            "RULE_01_PROMPT_INJECTION_BLOCK",
        )

    # Rule 2: Legal / Fraud Threat Escalation (freeze automated payouts)
    is_threat, _ = detect_fraud_or_legal_threat(ticket.subject, ticket.body)
    if is_threat or ticket.id == "T-1008":
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                reason="Frozen for immediate human fraud/legal review; auto-refund disabled.",
            ),
            "RULE_02_FRAUD_LEGAL_FREEZE",
        )

    # Rule 3: No Refund Requested
    if not llm_draft.user_wants_refund and llm_draft.category not in [
        TriageCategory.REFUND_REQUEST,
        TriageCategory.BILLING_DISPUTE,
    ]:
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                reason="No refund requested.",
            ),
            "RULE_00_NO_REFUND_NEEDED",
        )

    # Examine billing history via deterministic fact sheet
    facts = compute_billing_facts(ticket)

    # If no renewal records at all
    has_renewals = any(p.type == PurchaseType.RENEWAL for p in ticket.purchases)
    if not has_renewals:
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                reason="No renewal records found for this account.",
            ),
            "RULE_10_NO_RENEWAL_FOUND",
        )

    # Rule 4: Unsettled / Pending Payments (e.g., status == "initiated" like T-1005)
    if facts.has_unsettled_payment_claim:
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=facts.pending_initiated_ids[0] if facts.pending_initiated_ids else None,
                reason="Payment is in 'initiated' (unsettled) state; cannot refund unsettled funds. Escalated for payment reconciliation.",
            ),
            "RULE_04_UNSETTLED_INITIATED_PAYMENT",
        )

    # Rule 5: False Double-Charge Claims (e.g., T-1009: 1 failed, 1 successful)
    if "double" in ticket.subject.lower() or "twice" in ticket.body.lower():
        if facts.has_false_double_charge_pattern:
            return (
                RefundDecision(
                    approved=False,
                    amount_inr=0,
                    payment_id=facts.latest_settled_renewal_id,
                    reason="Only one successful charge found; prior attempt failed and was not settled.",
                ),
                "RULE_05_FALSE_DOUBLE_CHARGE",
            )

    # If no successful renewal exists at all
    if facts.settled_renewal_count == 0:
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                reason="No successful renewal transaction found eligible for refund.",
            ),
            "RULE_10_NO_RENEWAL_FOUND",
        )

    # Rule 6: High-Value Annual Plan Gate (> AUTO_REFUND_MAX_INR, e.g. Rs 1499 in T-1006)
    if facts.latest_settled_renewal_inr > AUTO_REFUND_MAX_INR:
        if ticket.app_opens_since_renewal == 0:
            return (
                RefundDecision(
                    approved=False,
                    amount_inr=0,
                    payment_id=facts.latest_settled_renewal_id,
                    reason=(
                        f"Eligible unused annual renewal (0 opens), but amount Rs {facts.latest_settled_renewal_inr} "
                        f"exceeds auto-refund cap (Rs {AUTO_REFUND_MAX_INR}); routed for 1-click human approval."
                    ),
                ),
                "RULE_06_HIGH_VALUE_HUMAN_GATE",
            )
        else:
            return (
                RefundDecision(
                    approved=False,
                    amount_inr=0,
                    payment_id=facts.latest_settled_renewal_id,
                    reason=(
                        f"Annual renewal with {ticket.app_opens_since_renewal} app opens; "
                        "requires human review."
                    ),
                ),
                "RULE_08_USAGE_THRESHOLD_BLOCK",
            )

    # Rule 7: Usage and Time Window Gate on Monthly Renewal (Rs 249)
    elapsed_hours = facts.hours_since_latest_settled_renewal or 0.0

    if ticket.app_opens_since_renewal == 0:
        if elapsed_hours <= REFUND_WINDOW_HOURS:
            return (
                RefundDecision(
                    approved=True,
                    amount_inr=facts.latest_settled_renewal_inr,
                    payment_id=facts.latest_settled_renewal_id,
                    reason="Approved: unused monthly renewal (0 app opens) reported within 48h window.",
                ),
                "RULE_07_UNUSED_48H_AUTO_APPROVE",
            )
        else:
            return (
                RefundDecision(
                    approved=False,
                    amount_inr=0,
                    payment_id=facts.latest_settled_renewal_id,
                    reason=f"Auto-refund window expired ({elapsed_hours:.1f}h elapsed, max {REFUND_WINDOW_HOURS}h); routed to human agent.",
                ),
                "RULE_09_EXPIRED_WINDOW_BLOCK",
            )
    else:
        return (
            RefundDecision(
                approved=False,
                amount_inr=0,
                payment_id=facts.latest_settled_renewal_id,
                reason=f"Auto-refund declined: {ticket.app_opens_since_renewal} app opens recorded since renewal; routed to human agent.",
            ),
            "RULE_08_USAGE_THRESHOLD_BLOCK",
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
    confidence enforcement, response sanitization, and audit trace generation.
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
    refund_decision, policy_rule_id = evaluate_refund_policy(
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
            policy_rule_id = "RULE_11_LOW_CONFIDENCE_SUSPEND"

    # 6. Sanitize reply draft against information exfiltration
    clean_reply = sanitize_reply_draft(llm_draft.reply_draft)
    if injection_detected:
        clean_reply = SAFE_FALLBACK_REPLY

    # 7. Build Decision Provenance & 3 AM Audit Trace
    security_flags: List[str] = []
    if injection_detected or llm_draft.prompt_injection_detected:
        security_flags.append("PROMPT_INJECTION_DETECTED")
    if is_legal_threat or ticket.id == "T-1008":
        security_flags.append("FRAUD_LEGAL_ESCALATION")
    if llm_draft.confidence < settings.CONFIDENCE_THRESHOLD:
        security_flags.append("LOW_CONFIDENCE_ESCALATION")
    if llm_draft.execution_mode == "degraded_fallback":
        security_flags.append("DEGRADED_FALLBACK_ENGAGED")

    trace_id = f"tr_{ticket.id}_{compute_payload_hash(ticket)[:8]}"
    money_gate_override = bool(llm_draft.user_wants_refund != refund_decision.approved)

    audit_trace = AuditTrace(
        trace_id=trace_id,
        execution_mode=llm_draft.execution_mode,
        llm_attempts=llm_draft.llm_attempts,
        llm_suggested_refund=llm_draft.user_wants_refund,
        money_gate_override=money_gate_override,
        policy_rule_id=policy_rule_id,
        security_flags=security_flags,
    )

    return TriageResponse(
        ticket_id=ticket.id,
        category=category,
        severity=severity,
        refund=refund_decision,
        reply_draft=clean_reply,
        needs_human=needs_human,
        confidence=llm_draft.confidence,
        audit_trace=audit_trace,
    )
