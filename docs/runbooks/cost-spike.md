# Runbook — Unexpected cost spike

**Severity:** financial; can escalate to P1 depending on magnitude  
**Owner:** on-call gateway engineer  
**Expected MTTR:** stop-the-bleed within 5 minutes

## 1. Detect

Alerts that fire:
- `pronaos_cost_cents_per_minute` exceeds 2× rolling 24h baseline for 5 consecutive minutes
- Any tenant crosses 80% of monthly budget before day 25 of the cycle

## 2. Identify the blast radius

On the `Pronaos / Cost` Grafana dashboard, drill down by:
1. Tenant → team → API key
2. Model and provider
3. Average tokens per request (sudden inflation suggests runaway prompt or infinite loop)

## 3. Stop the bleed

Pick the least-disruptive stop:

| Scope                | Action                                                               |
|----------------------|----------------------------------------------------------------------|
| Single key           | `pronaos-cli key revoke <key-id>`                                      |
| Single team          | Lower team quota to near-current; soft 429 routes new traffic back   |
| Single tenant        | Enable `cost_circuit_breaker` on the tenant                          |
| Runaway model        | Remove from allowed model list for the affected tenant               |
| Global anomaly       | Flip the `pronaos.emergency_spend_cap` flag                            |

## 4. Communicate

- DM the tenant owner within 5 minutes. Include: what we stopped, why, and how to re-enable.
- If spike looks like an attack (unusual key usage pattern, impossible volume), treat as a security incident and rotate the key.

## 5. Root cause

Common causes:
1. Customer code bug: unbounded retry loop, recursive agent, missing `max_tokens`.
2. Prompt injection making the model emit long outputs.
3. Stale cache invalidation causing cache stampede against upstream.
4. Pricing table out of sync with provider's real prices.

## 6. Post-incident

- File incident report.
- If root cause was a gateway gap (e.g. missing cost-per-request cap on a route), add it to the default policy and ship.
- Update the tenant's `max_cost_per_request_cents` setting if unset.
