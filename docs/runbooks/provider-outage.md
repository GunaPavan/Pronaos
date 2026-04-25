# Runbook — Provider outage

**Severity:** varies by tenant impact  
**Owner:** on-call gateway engineer  
**Expected MTTR:** < 10 minutes (detection + failover confirmation)

## 1. Detect

Alerts that fire:
- `pronaos_provider_error_rate > 0.2` for 60 seconds (per provider label)
- `pronaos_provider_latency_p99_seconds > 15` for 120 seconds
- Grafana dashboard `Pronaos / Providers` goes red

## 2. Triage (2 minutes)

1. Open the `Pronaos / Providers` dashboard.
2. Identify affected provider(s) and scope: regional, global, specific model.
3. Check the provider's status page (link in dashboard footer).
4. Check our circuit breaker state: `pronaos_circuit_open{provider="..."}` should already be `1` if automation is working.

## 3. Confirm failover

Automatic failover should have already kicked in. Verify:

```promql
sum by (fallback_provider) (rate(pronaos_fallback_total[5m])) > 0
```

If fallback traffic is **not** flowing:
- Page yourself escalation; automation is broken.
- Manually update `provider_routes` to promote fallback to primary for impacted tenants.

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
