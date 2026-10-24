"""Strict Pydantic v2 schemas for the Dhaba Ticket Triage Service.

All schemas enforce `extra='forbid'` to prevent unexpected payload injection
and guarantee deterministic serialization.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class PurchaseType(str, Enum):
    """Supported purchase types in Dhaba billing system."""

    TRIAL = "trial"
    RENEWAL = "renewal"


class PurchaseStatus(str, Enum):
    """Billing lifecycle status for a purchase record."""

    INITIATED = "initiated"
    FAILED = "failed"
    SUCCESSFUL = "successful"
    RENEWAL = "renewal"


class Purchase(BaseModel):
    """Historical or active purchase entry associated with a user."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique payment identifier (e.g., pay_A1)")
    type: PurchaseType = Field(description="Purchase type: trial or renewal")
    amount_inr: int = Field(ge=0, description="Transaction amount in INR")
    status: PurchaseStatus = Field(description="Status of transaction")
    at: datetime = Field(description="Timestamp of purchase in ISO-8601 format")


class TicketInput(BaseModel):
    """Incoming support ticket payload from in-product help form."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique ticket identifier (e.g., T-1001)")
    received_at: datetime = Field(description="Timestamp ticket was received")
    subject: str = Field(description="Subject line of the support ticket")
    body: str = Field(description="Raw message body submitted by user")
    purchases: List[Purchase] = Field(
        default_factory=list,
        description="Billing history at the moment ticket landed",
    )
    app_opens_since_renewal: int = Field(
        default=0,
        ge=0,
        description="Count of app opens recorded since the latest renewal",
    )


class DhabaTicketsFile(BaseModel):
    """Wrapper schema for reading the dhaba_tickets.json dataset file."""

    model_config = ConfigDict(extra="forbid")

    product: str
    note: str
    tickets: List[TicketInput]


class TriageCategory(str, Enum):
    """Support ticket classification categories."""

    REFUND_REQUEST = "REFUND_REQUEST"
    BILLING_DISPUTE = "BILLING_DISPUTE"
    BUG_REPORT = "BUG_REPORT"
    ACCOUNT_ISSUE = "ACCOUNT_ISSUE"
    CANCELLATION = "CANCELLATION"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    INVOICE_REQUEST = "INVOICE_REQUEST"
    PRE_SALES_QUERY = "PRE_SALES_QUERY"
    SECURITY_OR_ABUSE = "SECURITY_OR_ABUSE"


class SeverityLevel(str, Enum):
    """
    SLA and Urgency levels for triaging support tickets:

    P0_CRITICAL:
        Active security threats, suspected unauthorized account takeover or fraud,
        legal/regulatory escalation threats (e.g., reporting to Cyber Crime Cell),
        or severe systemic vulnerabilities requiring immediate intervention (< 30 min).

    P1_HIGH:
        Critical technical failures blocking paying customers from using the app
        (e.g., app crashes immediately on launch across a device family), urgent
        unauthorized recurring charges with zero app usage, or active subscription
        cancellation requests where immediate action is required to stop debits.

    P2_MEDIUM:
        Standard operational issues affecting user workflow without total blockage:
        account history restoration after device changes, payment deducted with
        pending activation (initiated state), duplicate charge verification, or
        official GST invoice / tax receipt requests.

    P3_LOW:
        Non-urgent inquiries and feedback: new feature proposals (e.g., regional
        language requests), general pre-sales inquiries about feature compatibility,
        or generic user feedback.
    """

    P0_CRITICAL = "P0_CRITICAL"
    P1_HIGH = "P1_HIGH"
    P2_MEDIUM = "P2_MEDIUM"
    P3_LOW = "P3_LOW"


class RefundDecision(BaseModel):
    """
    Deterministic refund decision output.
    Note: Refund authorization is enforced by strict business rules, not raw LLM output.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(description="Whether the refund is approved")
    amount_inr: int = Field(
        default=0,
        ge=0,
        description="Amount to refund in INR (must be non-negative)",
    )
    reason: str = Field(description="Policy or human-review rationale for decision")
    payment_id: Optional[str] = Field(
        default=None,
        description="Specific transaction ID being refunded, if approved",
    )


class AuditTrace(BaseModel):
    """
    Detailed provenance trace for 3 AM debugging and regulatory accountability.
    Tracks model execution metadata, attempts, original intentions, and deterministic overrides.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(
        description="Correlation ID linking the request to logs and DB records (e.g. tr_<ticket_id>_<short_hash>)"
    )
    execution_mode: str = Field(
        description="Execution mode used: replay, live, or degraded_fallback"
    )
    llm_attempts: int = Field(
        default=1,
        ge=1,
        le=2,
        description="Total attempts made to invoke the LLM and parse valid JSON (1 or 2 if retried)",
    )
    llm_suggested_refund: bool = Field(
        description="Whether the LLM originally recommended or detected a refund request"
    )
    money_gate_override: bool = Field(
        description="True if the deterministic money gate overridden the LLM recommendation"
    )
    policy_rule_id: str = Field(
        description="Identifier of the specific deterministic rule code that decided the refund"
    )
    security_flags: List[str] = Field(
        default_factory=list,
        description="Security and policy flags raised during triage (e.g. PROMPT_INJECTION_DETECTED)",
    )


class TriageResponse(BaseModel):
    """Final response schema returned by POST /triage."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(description="Ticket identifier matching the input ticket")
    category: TriageCategory = Field(description="Assigned triage category")
    severity: SeverityLevel = Field(description="Assigned severity level")
    refund: RefundDecision = Field(description="Deterministic refund decision")
    reply_draft: str = Field(description="Drafted response in the customer's language")
    needs_human: bool = Field(description="True if a human agent must review before sending")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence score in range [0.0, 1.0]",
    )
    audit_trace: AuditTrace = Field(
        description="Provenance trace for auditability and 3 AM debugging"
    )


class LLMTriageDraft(BaseModel):
    """
    Restricted schema defining the exact output the LLM is permitted to produce.

    Security & Money Gate:
    The LLM is explicitly barred from setting `refund.approved` or `refund.amount_inr`.
    It only reports `user_wants_refund`, while refund eligibility and transaction
    amounts are evaluated by deterministic business rule gates.
    """

    model_config = ConfigDict(extra="forbid")

    category: TriageCategory = Field(description="Predicted ticket category")
    severity: SeverityLevel = Field(description="Predicted severity level")
    user_wants_refund: bool = Field(
        description="Whether the user is requesting or demanding a refund"
    )
    reply_draft: str = Field(
        description="Drafted response in the user's language and tone"
    )
    needs_human: bool = Field(
        description="Whether ambiguity, prompt injection, or policy warrants human review"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Self-evaluated confidence score between 0.0 and 1.0",
    )
    prompt_injection_detected: bool = Field(
        default=False,
        description="Flag set if adversarial instructions or system prompt overrides are detected",
    )
    reasoning: str = Field(
        description="Internal model reasoning explaining category and severity choices"
    )
    execution_mode: str = Field(
        default="replay",
        description="Execution mode: replay, live, or degraded_fallback",
    )
    llm_attempts: int = Field(
        default=1,
        ge=1,
        le=2,
        description="Number of attempts required to produce valid JSON",
    )
