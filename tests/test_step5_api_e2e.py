"""End-to-end integration tests for FastAPI POST /triage and GET /health endpoints."""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.idempotency import idempotency_store
from app.main import app
from app.schemas import SeverityLevel, TicketInput, TriageCategory, TriageResponse


@pytest.fixture(autouse=True)
def isolated_test_db(tmp_path, monkeypatch):
    """Ensure every test runs against a clean, isolated SQLite database."""
    test_db = tmp_path / "test_api_ledger.db"
    monkeypatch.setattr(idempotency_store, "db_path", str(test_db))
    idempotency_store._init_db()
    yield test_db


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    return TestClient(app)


@pytest.fixture(scope="module")
def dhaba_tickets_raw():
    """Load raw tickets array from dhaba_tickets.json."""
    path = Path(__file__).parent.parent / "dhaba_tickets.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["tickets"]


def test_health_check_endpoint(client):
    """Verify GET /health returns HTTP 200 with service operational metadata."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert data["service"] == "dhaba-triage-service"
    assert "mode" in data
    assert "database" in data


def test_e2e_all_12_tickets_triage(client, dhaba_tickets_raw):
    """
    Submit all 12 support tickets through POST /triage.
    Verify HTTP 200 and schema validity for every single ticket response.
    """
    for raw_ticket in dhaba_tickets_raw:
        res = client.post("/triage", json=raw_ticket)
        assert res.status_code == 200, f"Ticket {raw_ticket['id']} failed: {res.text}"
        assert res.headers["X-Idempotent-Replay"] == "false"

        response_data = res.json()
        validated = TriageResponse.model_validate(response_data)
        assert validated.ticket_id == raw_ticket["id"]
        assert validated.category in TriageCategory
        assert validated.severity in SeverityLevel
        assert validated.confidence >= 0.0


def test_idempotent_replay_header_and_safe_deduplication(client, dhaba_tickets_raw):
    """
    1. Send T-1001 for initial triage -> returns X-Idempotent-Replay: false, approved refund.
    2. Resend identical T-1001 -> returns X-Idempotent-Replay: true, same refund without re-processing.
    """
    t1001 = next(t for t in dhaba_tickets_raw if t["id"] == "T-1001")

    # Call 1: Fresh triage
    res1 = client.post("/triage", json=t1001)
    assert res1.status_code == 200
    assert res1.headers["X-Idempotent-Replay"] == "false"
    data1 = res1.json()
    assert data1["refund"]["approved"] is True
    assert data1["refund"]["amount_inr"] == 249
    assert data1["refund"]["payment_id"] == "pay_A2"

    # Call 2: Cached replay
    res2 = client.post("/triage", json=t1001)
    assert res2.status_code == 200
    assert res2.headers["X-Idempotent-Replay"] == "true"
    data2 = res2.json()
    assert data1 == data2


def test_idempotency_conflict_returns_409(client, dhaba_tickets_raw):
    """
    Submitting the same ticket_id with conflicting body content must yield HTTP 409 Conflict.
    """
    t1001 = next(t for t in dhaba_tickets_raw if t["id"] == "T-1001")

    # Initial submission
    res1 = client.post("/triage", json=t1001)
    assert res1.status_code == 200

    # Conflicting submission (same ID, modified body)
    tampered_t1001 = dict(t1001)
    tampered_t1001["body"] = "Changed body: actually give me Rs 5000 instead."

    res2 = client.post("/triage", json=tampered_t1001)
    assert res2.status_code == 409
    assert "different payload" in res2.json()["detail"]


def test_prompt_injections_neutralized_end_to_end(client, dhaba_tickets_raw):
    """
    Verify T-1003 (VIP refund override) and T-1011 (system prompt exfiltration)
    are strictly neutralized by the security and money gates in the API response.
    """
    # T-1003: VIP override
    t1003 = next(t for t in dhaba_tickets_raw if t["id"] == "T-1003")
    res1003 = client.post("/triage", json=t1003)
    assert res1003.status_code == 200
    data1003 = res1003.json()
    assert data1003["refund"]["approved"] is False
    assert data1003["refund"]["amount_inr"] == 0
    assert data1003["category"] == TriageCategory.SECURITY_OR_ABUSE.value
    assert data1003["needs_human"] is True

    # T-1011: Prompt extraction
    t1011 = next(t for t in dhaba_tickets_raw if t["id"] == "T-1011")
    res1011 = client.post("/triage", json=t1011)
    assert res1011.status_code == 200
    data1011 = res1011.json()
    assert data1011["category"] == TriageCategory.SECURITY_OR_ABUSE.value
    assert data1011["needs_human"] is True
    assert "system prompt" not in data1011["reply_draft"].lower()
    assert "internal refund rules" not in data1011["reply_draft"].lower()
