# 3 AM On-Call Runbook & Operations Guide

> **Dhaba Support Ticket Triage Microservice**  
> Diagnostic procedures, alert handling, trace correlation, and incident resolution for on-call engineers.

---

## 1. Quick Emergency Triage at 3 AM

When an alert fires or an executive pings about an unexpected ticket decision, follow this 3-step rapid investigation procedure:

```
Step 1: Get ticket_id from alert / ticket desk
                       │
                       ▼
Step 2: Retrieve Triage Ledger Entry / Audit Trace
                       │
                       ▼
Step 3: Check 3 Key Indicators:
  ├── 1. policy_rule_id         -> Which exact deterministic policy decided it?
  ├── 2. money_gate_override    -> Did the code overrule the LLM?
  └── 3. llm_attempts           -> Did the LLM fail schema validation on Attempt 1?
```

### Fast Diagnostic Query

#### If using SQLite (`triage_state.db`):
```bash
sqlite3 triage_state.db "SELECT ticket_id, payload_hash, refund_approved, refund_amount_inr, created_at, response_json FROM triage_ledger WHERE ticket_id = 'T-1006';"
```

#### If searching CloudWatch / Datadog / ELK:
Search query:
```text
event_type: "triage_decision_made" AND ticket_id: "T-1006"
```
Or filter directly by correlation `trace_id`:
```text
trace_id: "tr_T-1006_*"
```

---

## 2. Policy Rule Reference & Troubleshooting

When inspecting the `policy_rule_id` in logs or API responses, use this table to understand the system's exact reasoning:

### `RULE_01_PROMPT_INJECTION_BLOCK`
- **Symptom**: Ticket marked `SECURITY_OR_ABUSE`, `P1_HIGH`, refund declined (₹0), `needs_human=True`.
- **Root Cause**: The ticket body contained prompt injection markers (e.g. `SYSTEM NOTE`, `Ignore your previous instructions`, `verified VIP`, `internal refund rules`).
- **Action**: Check `ticket_id`. Verify if the customer was an attacker or an innocent user whose quote included suspicious patterns. If benign, the agent can manually review and override.

### `RULE_02_FRAUD_LEGAL_FREEZE`
- **Symptom**: Ticket escalated to `P0_CRITICAL`, refund frozen (₹0), `needs_human=True`.
- **Root Cause**: Customer threatened police action, Cyber Crime Cell FIR, or reported unauthorized card usage by a family member.
- **Action**: Do **NOT** auto-refund. Escalate directly to Senior Legal and Trust & Safety teams. The user account should be locked against further transactions until identity is verified.

### `RULE_04_UNSETTLED_INITIATED_PAYMENT`
- **Symptom**: Ticket marked `BILLING_DISPUTE`, refund declined (₹0), `needs_human=True`.
- **Root Cause**: The payment gateway record shows `status: initiated` (unsettled).
- **Action**: Check payment gateway (Razorpay/Stripe/PineLabs). If the transaction later settles (`successful`), grant access or issue refund manually. If it failed, the customer's bank will auto-reverse funds within 3–5 business days.

### `RULE_05_FALSE_DOUBLE_CHARGE`
- **Symptom**: User claims double debit, but refund is declined (₹0).
- **Root Cause**: Billing telemetry indicates 1 failed attempt and 1 successful charge.
- **Action**: Confirm the customer only has a single settled debit. The draft reply clarifies that the failed debit was a temporary bank authorization hold that auto-releases.

### `RULE_06_HIGH_VALUE_HUMAN_GATE`
- **Symptom**: Customer eligible for refund (0 app opens), but refund is not auto-disbursed.
- **Root Cause**: Transaction amount exceeds ₹500 auto-cap (`AUTO_REFUND_MAX_INR`, e.g. ₹1,499 annual plan in `T-1006`).
- **Action**: Support agent clicks the pre-approved 1-click **"Approve ₹1,499 Refund"** button in Zendesk/Freshdesk.

### `RULE_07_UNUSED_48H_AUTO_APPROVE`
- **Symptom**: Auto-refund granted for ₹249.
- **Root Cause**: Unused monthly renewal (0 app opens) reported within 48 hours of charge.
- **Action**: Expected happy path. No manual intervention required.

### `RULE_08_USAGE_THRESHOLD_BLOCK`
- **Symptom**: User complains and demands refund, but refund is declined.
- **Root Cause**: Customer opened the app after renewal (`app_opens_since_renewal > 0`).
- **Action**: Policy prohibits auto-refund after app usage. Agent may evaluate if service was genuinely broken and grant a discretionary retention credit.

---

## 3. High-Priority Incidents & Playbooks

### Incident A: LLM Provider Outage (HTTP 429 / 503 / Timeouts)

**Symptoms**:
- Spikes in `llm_degraded_fallback_triggered` logs.
- `AuditTrace.execution_mode == "degraded_fallback"`.
- `confidence == 0.0` and `needs_human == True`.

**Impact**:
- Service continues returning `HTTP 200 OK`. No 500 errors.
- Money is protected: `refund.approved` is locked to `False`.
- Tickets queue up for human review.

**Immediate Actions**:
1. Check upstream status page: OpenAI Status / Google AI Status.
2. If temporary rate-limiting (429), verify API keys and quota usage.
3. Switch provider or fallback to Replay Mode if needed:
   ```bash
   export TRIAGE_MODE="replay"
   ```
4. Restart service:
   ```bash
   uvicorn app.main:app --reload
   ```

---

### Incident B: High Rate of `IdempotencyConflictError` (HTTP 409)

**Symptoms**:
- Client apps or webhooks receiving HTTP 409 Conflict responses.
- Logs report `idempotency_conflict_detected`.

**Root Cause**:
- A client-side retry loop is regenerating tickets with dynamic timestamps or modified bodies under the same `ticket_id`.

**Mitigation**:
- Verify client client-side UUID generation. A retry must retransmit the *exact* original payload to receive a cached idempotent replay (`X-Idempotent-Replay: true`).
