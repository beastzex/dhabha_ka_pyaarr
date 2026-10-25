"""FastAPI application for Dhaba Support Ticket Triage Service.

Exposes:
- POST /triage: End-to-end triage pipeline with idempotency, LLM draft, deterministic money gate,
  and PII-safe logging.
- GET /health: Health check and service configuration status.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.idempotency import (
    IdempotencyConflictError,
    compute_payload_hash,
    idempotency_store,
)
from app.llm_client import llm_client
from app.logger import log_event, logger
from app.money_gate import apply_safety_gates
from app.schemas import TicketInput, TriageResponse


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan management."""
    logger.info(
        f"Starting Dhaba Triage Service in '{settings.TRIAGE_MODE}' mode with DB '{settings.DB_PATH}'"
    )
    yield
    logger.info("Dhaba Triage Service shutdown cleanly.")


app = FastAPI(
    title="Dhaba Support Ticket Triage Service",
    description="Automated, PII-safe support ticket triage pipeline with deterministic financial gates.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    """Redirect root path to interactive Swagger UI documentation."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["System"])
def health_check() -> dict:
    """Return operational health status and active triage mode."""
    return {
        "status": "healthy",
        "service": "dhaba-triage-service",
        "mode": settings.TRIAGE_MODE,
        "database": settings.DB_PATH,
        "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
    }


@app.post(
    "/triage",
    response_model=TriageResponse,
    status_code=status.HTTP_200_OK,
    tags=["Triage"],
    responses={
        200: {
            "description": "Ticket successfully triaged or retrieved from idempotency ledger.",
            "headers": {
                "X-Idempotent-Replay": {
                    "description": "True if returned from cached ledger; False if fresh evaluation.",
                    "schema": {"type": "string"},
                }
            },
        },
        409: {"description": "Ticket resubmitted with conflicting payload data."},
    },
)
def triage_ticket(ticket: TicketInput, response: Response) -> TriageResponse:
    """
    Process incoming support ticket through the multi-stage defensive triage pipeline:
    1. Cryptographic payload hash & idempotency ledger check (prevents double refunds).
    2. Model classification (with 1x retry & graceful fallback on failure).
    3. Deterministic money gate & prompt injection security enforcement.
    4. Atomic persistence in triage ledger.
    5. PII-scrubbed audit logging.
    """
    # Step 1: Compute canonical payload hash
    payload_hash = compute_payload_hash(ticket)

    # Step 2: Idempotency ledger check
    try:
        existing_response = idempotency_store.get_existing(ticket.id, payload_hash)
        if existing_response is not None:
            response.headers["X-Idempotent-Replay"] = "true"
            log_event(
                "triage_idempotent_replay",
                ticket.id,
                category=existing_response.category.value,
                refund_approved=existing_response.refund.approved,
                trace_id=existing_response.audit_trace.trace_id,
                policy_rule_id=existing_response.audit_trace.policy_rule_id,
            )
            return existing_response
    except IdempotencyConflictError as conflict_err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(conflict_err),
        )

    # Step 3: LLM Classification Draft (Replay or Live, with retry & fallback)
    llm_draft = llm_client.triage_ticket(ticket)

    # Step 4: Deterministic Policy & Money Gate Enforcement
    final_response = apply_safety_gates(ticket, llm_draft)

    # Step 5: Atomically save to idempotency ledger
    idempotency_store.save_result(ticket.id, payload_hash, final_response)

    # Step 6: PII-safe audit logging
    log_event(
        "triage_processed",
        ticket.id,
        category=final_response.category.value,
        severity=final_response.severity.value,
        refund_approved=final_response.refund.approved,
        refund_amount_inr=final_response.refund.amount_inr,
        needs_human=final_response.needs_human,
        confidence=final_response.confidence,
        trace_id=final_response.audit_trace.trace_id,
        policy_rule_id=final_response.audit_trace.policy_rule_id,
    )

    # Step 7: Return final result with replay header
    response.headers["X-Idempotent-Replay"] = "false"
    return final_response
