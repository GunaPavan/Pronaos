# ADR 0001 — Python first, Go reserved for the hot path

- Status: Accepted
- Date: 2026-04-22

## Context

The gateway sits on the critical path of every LLM request in an organization. It must be fast, but it must also be easy to iterate on — especially in the first 12 weeks when the API surface, routing heuristics, guardrails, and observability are all moving targets.

Two reasonable starting languages:

- **Go or Rust**: superior latency, tail latency, and throughput on proxy-style workloads. Credible "I built production infra" signal.
- **Python (FastAPI + uvicorn + uvloop)**: fastest iteration speed, richest LLM ecosystem, matches the existing author skillset, and *well within* the throughput envelope needed to demonstrate the architecture convincingly.

## Decision

Build the gateway in Python 3.12 with FastAPI and uvicorn (uvloop). Reserve Go as an optional later rewrite of the bare proxy data-path if the project grows beyond a single-binary need.

## Rationale

1. **Iteration velocity dominates week 1–12.** Most of the hard problems in this project are design problems, not language problems: routing, caching semantics, policy, audit, eval. Python removes friction from those.
2. **FastAPI + uvloop can sustain the demo envelope.** Independent benchmarks put this stack comfortably at 10k+ RPS on a modest node — well above what a portfolio demo needs to be credible.
3. **Every LLM provider SDK is Python-first.** Anthropic, OpenAI, Google, Mistral, Bedrock all ship Python SDKs before or better than their Go counterparts.
4. **The hard parts live outside the data path.** Postgres control plane, policy engine, eval harness, admin UI — none of these benefit from Go. Keeping everything in one language reduces operational surface until a true bottleneck appears.
5. **Rewriting the hot proxy path is a well-scoped future exercise.** If and when p99 latency becomes the binding constraint, extracting just the streaming proxy into a Go binary is a bounded weekend project — and it makes for a great "what I'd do next" conversation with recruiters.

## Consequences

- All code in `src/` is strictly typed Python 3.12, mypy strict mode.
- Async-first throughout; no sync I/O on the request path.
- If benchmarks show the Python proxy consuming > 3 ms p50 on a passthrough request, revisit this decision.
