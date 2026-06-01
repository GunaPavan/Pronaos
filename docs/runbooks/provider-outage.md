# Runbook — Provider outage

**Severity:** varies by tenant impact  
**Owner:** on-call gateway engineer  
**Expected MTTR:** < 10 minutes (detection + failover confirmation)

## 1. Detect

Alerts that fire:
- `pronaos_provider_error_rate > 0.2` for 60 seconds (per provider label)
- `pronaos_provider_latency_p99_seconds > 15` for 120 seconds
- `circuit.tripped` webhook fires to the tenant's configured receiver
  the moment the breaker transitions CLOSED → OPEN
- Grafana dashboard `Pronaos / Providers` goes red

## 2. Triage (2 minutes)

1. Open the `Pronaos / Providers` dashboard.
2. Identify affected provider(s) and scope: regional, global, specific model.
3. Check the provider's status page (link in dashboard footer).
4. Check our circuit breaker state: `pronaos_circuit_open{provider="..."}` should already be `1` if automation is working.

## 3. Confirm failover + circuit breaker

Automatic failover + circuit breaker should have already kicked in.
Verify:

```promql
# Trips on the affected provider:
increase(pronaos_circuit_trips_total{provider="..."}[5m]) > 0

# Upstream calls the breaker saved (skipped because OPEN):
rate(pronaos_circuit_skipped_requests_total{provider="..."}[5m]) > 0

# Successful fallback traffic to other providers:
rate(pronaos_provider_requests_total{provider!="<bad>",status="success"}[5m])
```

If breaker state stays CLOSED despite obvious failures:
- The errors may be `AuthError` (4xx with credential reason) which
  deliberately don't trip the breaker — a misconfigured key isn't a
  provider-health signal. Rotate / fix the key.
- Otherwise the failure-counting logic itself is broken: page
  escalation and consider manually updating `provider_routes` to
  promote fallback to primary for impacted tenants.

For multi-team mitigations, push affected teams to a different model
family on the same provider, or to a different provider entirely via
the allowlist + auto-routing pair (Phase 21):

```bash
pronaos-cli team set-allowed-models <team-id> --models 'cerebras/*,together/*'
pronaos-cli team set-routing-strategy <team-id> --strategy fastest
# Clients sending model="auto" now route away from the bad provider.
```

## 4. Communicate

- Post in `#pronaos` Slack with affected provider + tenants + ETA.
- Update status page if tenant impact > 2 minutes.
- If tenant impact persists beyond 10 minutes, open an incident channel and page leadership per severity matrix.

## 5. Recovery

When the provider recovers:
1. Circuit breaker moves to half-open automatically after 30 s.
2. Traffic restoration is gradual (exponential ramp) — do not force immediate full restore.
3. Watch error rate for 5 minutes before declaring resolved.

## 6. Post-incident

- File incident report using the template in `docs/runbooks/_templates/incident.md`.
- Add a regression chaos test if this failure was not previously covered.
- Update this runbook if any step was missing or unclear.
