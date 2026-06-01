# Runbook — Quota exhaustion

**Severity:** depends on tenant impact — typically P3 for one team, P2 for
multiple teams of the same tenant, P1 for cross-tenant pattern.

## 1. Detect

The user-visible signal is the HTTP `429` response with body shape::

    {
      "detail": {
        "type": "rate_limit" | "monthly_token_budget_exhausted" |
                "monthly_cost_budget_exhausted",
        "message": "request denied by quota policy",
        "estimated_tokens": <int>,        # preflight only
        "retry_after_seconds": <int>
      }
    }

If the response carries an `X-Pronaos-Preflight-Estimate: <int>`
header, the denial fired BEFORE any upstream call — the gateway's
heuristic estimator decided the team couldn't afford this request.
The `quota.exhausted` webhook also fires (Phase 19) so a tenant
with a configured receiver gets a Slack / PagerDuty ping at the
moment of the denial.

In Grafana, the `Pronaos / Quotas` panel shows distinct rates:

- `pronaos_quota_denials_total{reason="rate_limit"}` — per-key
  burst trippers; usually a misbehaving client.
- `pronaos_quota_denials_total{reason="monthly_token_budget_exhausted"}`
  — team hit its monthly token cap (post-flight).
- `pronaos_preflight_denials_total{reason}` — Phase 20 estimator
  denied the request before dispatching upstream. Sum across both
  for the operator-visible total.

## 2. Triage (2 minutes)

Identify which gate fired:

```bash
pronaos-cli team usage <team-id>
```

Output shows used / budget / period_resets_at. Three cases:

| `used` vs `budget` | What it means |
|---|---|
| `used < budget` | Not the budget gate — it must be the per-key RPS limit. See §3a. |
| `used >= budget`, `resets_at` near | Real consumption — the team is on track to refill within hours. See §3b. |
| `used >> budget`, sudden jump | Runaway client or prompt-injection consuming tokens. See §3c. |

## 3. Stop the bleed

### 3a — Per-key RPS trip (legitimate burst)

If the spike is the client's first time hitting this scale, raise the key's
RPS cap to the next sane bucket (10 → 25 → 100 r/s). Don't go straight
to unlimited.

```bash
pronaos-cli key set-rps <key-id> --rps 25
```

### 3b — Team near budget, real growth

Raise the team's monthly token budget. Communicate the new cap to the team
owner; otherwise they'll surprise themselves with a bigger bill next month.

```bash
pronaos-cli team set-budget <team-id> --tokens 50000000
```

### 3c — Runaway client / suspected injection

**Don't raise the budget.** Stop the bleed at the key:

```bash
pronaos-cli key revoke <key-id>
```

Then DM the tenant owner with the request id of the runaway call (visible
in logs via `request_id`). Once the cause is identified, issue a fresh key.

## 4. Communicate

For a team-scoped block: DM the tenant owner with the team name, what gate
tripped, and the `Retry-After` window so they can plan.

For a tenant-wide pattern (multiple teams of the same tenant hitting limits
within minutes): escalate per severity matrix, open an incident channel,
investigate whether a shared service is fanning out into the gateway.

## 5. Post-incident

- File incident report using `docs/runbooks/_templates/incident.md`.
- If the trip was caused by a Pronaos bug (wrong rps_limit applied, period
  not rolling over, etc.) add a regression test in
  `tests/unit/test_quota_endpoint.py`.
- If the trip exposed a customer-side bug (no exponential backoff, no
  budget awareness), update the integration docs with a "what to do on
  429" section so future customers don't hit it.
