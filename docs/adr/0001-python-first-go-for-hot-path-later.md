# ADR 0001 — Python first, Go reserved for the hot path

- Status: Accepted
- Date: 2026-04-22

## Context

The gateway sits on the critical path of every LLM request in an organization. It must be fast, but it must also be easy to iterate on — the API surface, routing heuristics, guardrails, and observability are all evolving alongside the product.

Two reasonable starting languages:

- **Go or Rust**: superior latency, tail latency, and throughput on proxy-style workloads. Strong production-infrastructure signal.
- **Python (FastAPI + uvicorn + uvloop)**: fastest iteration speed, richest LLM ecosystem, and well within the throughput envelope needed for early production deployments.

## Decision

Build the gateway in Python 3.12 with FastAPI and uvicorn (uvloop). Reserve Go as an optional later rewrite of the bare proxy data-path if the project grows beyond a single-binary need.

## Rationale

1. **Iteration velocity dominates the early phases.** Most of the hard problems in this project are design problems, not language problems: routing, caching semantics, policy, audit, eval. Python removes friction from those.
2. **FastAPI + uvloop sustain the target throughput envelope.** Independent benchmarks put this stack comfortably at 10k+ RPS on a modest node — well above the requirements for the initial deployment profile.
3. **Every LLM provider SDK is Python-first.** Anthropic, OpenAI, Google, Mistral, Bedrock all ship Python SDKs before or better than their Go counterparts.
4. **The hard parts live outside the data path.** Postgres control plane, policy engine, eval harness, admin UI — none of these benefit from Go. Keeping everything in one language reduces operational surface until a true bottleneck appears.
5. **Rewriting the hot proxy path is a well-scoped future exercise.** If and when p99 latency becomes the binding constraint, extracting just the streaming proxy into a Go binary is a bounded effort with a clear performance target.

## Consequences

- All code in `src/` is strictly typed Python 3.12, mypy strict mode.
- Async-first throughout; no sync I/O on the request path.
- If benchmarks show the Python proxy consuming > 3 ms p50 on a passthrough request, revisit this decision.
