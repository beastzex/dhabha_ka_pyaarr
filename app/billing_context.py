"""Deterministic Billing Reality Fact-Sheet Injector.

Pre-computes verified financial facts from billing telemetry before invoking the LLM,
preventing model hallucinations regarding transaction amounts, payment states, or false double charges.
"""

from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field

from app.schemas import PurchaseStatus, PurchaseType, TicketInput


class BillingFactSheet(BaseModel):
    """Authoritative, pre-computed billing facts verified against database records."""

    model_config = ConfigDict(extra="forbid")

    total_settled_inr: int = Field(
        ge=0,
        description="Total sum of amount_inr strictly for settled (successful) purchases",
    )
    settled_renewal_count: int = Field(
        ge=0,
        description="Total count of successfully settled renewal transactions",
    )
    latest_settled_renewal_id: Optional[str] = Field(
        default=None,
        description="Transaction ID of the most recent successful renewal",
    )
    latest_settled_renewal_inr: int = Field(
        default=0,
        ge=0,
        description="Amount in INR of the most recent successful renewal",
    )
    hours_since_latest_settled_renewal: Optional[float] = Field(
        default=None,
        description="Elapsed hours between the latest successful renewal and ticket receipt",
    )
    pending_initiated_ids: List[str] = Field(
        default_factory=list,
        description="Purchase IDs currently in unsettled 'initiated' state",
    )
    failed_attempt_ids: List[str] = Field(
        default_factory=list,
        description="Purchase IDs recorded with 'failed' status",
    )
    has_false_double_charge_pattern: bool = Field(
        default=False,
        description="True if user experienced 1 failed attempt alongside 1 successful charge on the same date",
    )
    has_unsettled_payment_claim: bool = Field(
        default=False,
        description="True if the user's latest renewal is pending in 'initiated' status",
    )

    def to_prompt_block(self) -> str:
        """
        Render strict XML block for prompt injection, establishing grounded financial reality.
        """
        hours_str = (
            f"{self.hours_since_latest_settled_renewal:.1f} hours"
            if self.hours_since_latest_settled_renewal is not None
            else "N/A"
        )
        pending_str = ", ".join(self.pending_initiated_ids) if self.pending_initiated_ids else "None"
        failed_str = ", ".join(self.failed_attempt_ids) if self.failed_attempt_ids else "None"
        double_charge_str = (
            "YES (Telemetry confirms only 1 charge succeeded; prior attempt failed and was not settled)"
            if self.has_false_double_charge_pattern
            else "NO"
        )
        unsettled_str = (
            "YES (Latest renewal payment is currently in 'initiated' state pending bank settlement)"
            if self.has_unsettled_payment_claim
            else "NO"
        )

        return (
            "<verified_billing_facts>\n"
            f"Total Settled Amount: Rs {self.total_settled_inr}\n"
            f"Settled Renewal Count: {self.settled_renewal_count}\n"
            f"Latest Settled Renewal: {self.latest_settled_renewal_id or 'None'} (Rs {self.latest_settled_renewal_inr}, {hours_str} ago)\n"
            f"Pending Unsettled Purchases: {pending_str}\n"
            f"Failed Payment Attempts: {failed_str}\n"
            f"False Double-Charge Pattern: {double_charge_str}\n"
            f"Unsettled Payment in Progress: {unsettled_str}\n"
            "</verified_billing_facts>"
        )


def compute_billing_facts(ticket: TicketInput) -> BillingFactSheet:
    """
    Extract deterministic billing metrics from ticket telemetry.
    Zero LLM involvement; 100% verified ground truth.
    """
    settled = [p for p in ticket.purchases if p.status == PurchaseStatus.SUCCESSFUL]
    total_settled_inr = sum(p.amount_inr for p in settled)

    settled_renewals = sorted(
        [p for p in settled if p.type == PurchaseType.RENEWAL],
        key=lambda x: x.at,
        reverse=True,
    )
    settled_renewal_count = len(settled_renewals)

    latest_settled_renewal_id = None
    latest_settled_renewal_inr = 0
    hours_since_latest_settled_renewal = None

    if settled_renewals:
        latest = settled_renewals[0]
        latest_settled_renewal_id = latest.id
        latest_settled_renewal_inr = latest.amount_inr
        delta_seconds = (ticket.received_at - latest.at).total_seconds()
        hours_since_latest_settled_renewal = round(delta_seconds / 3600.0, 2)

    pending_initiated_ids = [
        p.id for p in ticket.purchases if p.status == PurchaseStatus.INITIATED
    ]
    failed_attempt_ids = [
        p.id for p in ticket.purchases if p.status == PurchaseStatus.FAILED
    ]

    all_renewals = sorted(
        [p for p in ticket.purchases if p.type == PurchaseType.RENEWAL],
        key=lambda x: x.at,
        reverse=True,
    )

    has_unsettled_payment_claim = bool(
        all_renewals and all_renewals[0].status == PurchaseStatus.INITIATED
    )

    # Detect false double charge: failed attempt within 24 hours of successful renewal
    has_false_double_charge_pattern = False
    failed_renewals = [p for p in all_renewals if p.status == PurchaseStatus.FAILED]
    successful_renewals = [p for p in all_renewals if p.status == PurchaseStatus.SUCCESSFUL]

    if settled_renewal_count <= 1:
        for failed in failed_renewals:
            for succ in successful_renewals:
                # Check if the attempts occurred within 24 hours (86,400s), handling midnight crossing
                if abs((succ.at - failed.at).total_seconds()) <= 86400:
                    has_false_double_charge_pattern = True
                    break
            if has_false_double_charge_pattern:
                break

    return BillingFactSheet(
        total_settled_inr=total_settled_inr,
        settled_renewal_count=settled_renewal_count,
        latest_settled_renewal_id=latest_settled_renewal_id,
        latest_settled_renewal_inr=latest_settled_renewal_inr,
        hours_since_latest_settled_renewal=hours_since_latest_settled_renewal,
        pending_initiated_ids=pending_initiated_ids,
        failed_attempt_ids=failed_attempt_ids,
        has_false_double_charge_pattern=has_false_double_charge_pattern,
        has_unsettled_payment_claim=has_unsettled_payment_claim,
    )
