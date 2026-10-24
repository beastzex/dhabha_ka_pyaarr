"""Atomic SQLite-backed idempotency ledger for Dhaba Ticket Triage Service.

Ensures that the same ticket ID arriving again never triggers multiple model runs
or duplicate refund issuances. Enforces cryptographic hash comparison on inputs to
detect and reject payload conflicts (HTTP 409).
"""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Optional

from app.config import settings
from app.logger import log_event
from app.schemas import TicketInput, TriageResponse


class IdempotencyConflictError(Exception):
    """Raised when a ticket is submitted with a different payload than previously recorded."""

    def __init__(self, ticket_id: str, message: Optional[str] = None):
        self.ticket_id = ticket_id
        msg = (
            message
            or f"Ticket '{ticket_id}' was already processed with a different payload. "
            "Conflicting resubmissions are rejected to prevent state ambiguity."
        )
        super().__init__(msg)


def compute_payload_hash(ticket: TicketInput) -> str:
    """
    Generate a deterministic SHA-256 hash of the canonical JSON representation of a ticket.
    Guarantees that whitespace or key order differences do not alter the fingerprint.
    """
    canonical_json = json.dumps(
        ticket.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """SQLite-backed persistent ledger guaranteeing at-most-once processing semantics."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize the triage ledger table if it does not already exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS triage_ledger (
                    ticket_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    refund_approved INTEGER NOT NULL,
                    refund_amount_inr INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def get_existing(
        self,
        ticket_id: str,
        payload_hash: str,
    ) -> Optional[TriageResponse]:
        """
        Check if a ticket has already been processed.

        Returns:
            - TriageResponse: If ticket exists with the identical payload hash.
            - None: If ticket has never been triaged.

        Raises:
            - IdempotencyConflictError: If ticket exists but payload hash differs.
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT payload_hash, response_json FROM triage_ledger WHERE ticket_id = ?;",
                (ticket_id,),
            )
            row = cur.fetchone()

        if not row:
            return None

        stored_hash = row["payload_hash"]
        if stored_hash != payload_hash:
            log_event(
                "idempotency_conflict_detected",
                ticket_id,
                stored_hash=stored_hash,
                new_hash=payload_hash,
            )
            raise IdempotencyConflictError(ticket_id)

        log_event("idempotency_cache_hit", ticket_id)
        return TriageResponse.model_validate_json(row["response_json"])

    def save_result(
        self,
        ticket_id: str,
        payload_hash: str,
        response: TriageResponse,
    ) -> bool:
        """
        Persist a triage decision into the ledger atomically.
        Uses INSERT OR IGNORE to prevent race conditions from dual-inserting or double-refunding.

        Returns:
            True if newly inserted, False if record already existed.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        response_json = response.model_dump_json()

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO triage_ledger (
                    ticket_id,
                    payload_hash,
                    response_json,
                    refund_approved,
                    refund_amount_inr,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    ticket_id,
                    payload_hash,
                    response_json,
                    1 if response.refund.approved else 0,
                    response.refund.amount_inr,
                    created_at,
                ),
            )
            conn.commit()
            was_inserted = cur.rowcount > 0

        if was_inserted:
            log_event(
                "triage_ledger_saved",
                ticket_id,
                refund_approved=response.refund.approved,
                refund_amount_inr=response.refund.amount_inr,
            )

        return was_inserted


# Default shared instance
idempotency_store = IdempotencyStore()
