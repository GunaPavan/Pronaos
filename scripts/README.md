# Demo scripts

End-to-end demos that exercise the running gateway and produce
visible artifacts — they're the "show, don't tell" companion to the
unit-test suite.

## `demo_cache.py` — cache effectiveness

Drives a mix of exact-duplicate, paraphrased, and unique prompts at a
running gateway, then prints the hit-rate climbing in real time.

### Prerequisites

1. The gateway is running with **both** Redis and semantic cache enabled:

   ```bash
   docker compose up -d   # brings up Postgres, Redis, Qdrant, …
   export PRONAOS_REDIS_URL=redis://localhost:6379/0
   export PRONAOS_SEMANTIC_CACHE_ENABLED=true
   uvicorn pronaos.main:app --host 0.0.0.0 --port 8080
   ```

2. You have an API key. Mint one once:

   ```bash
   pronaos-cli tenant create demo
   pronaos-cli team create eng --tenant <tenant-id>
   pronaos-cli key issue --team <team-id> --label demo
   # copy the printed key
   ```

3. The gateway has credentials for **some** provider. The default
   model is Groq's free-tier `llama-3.1-8b-instant` — get a key at
   https://console.groq.com/keys (free) and put it in `.env` as
   `GROQ_API_KEY=…`. Substitute `--model anthropic/claude-haiku-4-5`,
   `--model ollama/llama3.2`, etc. to use a different provider.

### Run it

```bash
python scripts/demo_cache.py --api-key pn_live_…

# more aggressive paraphrase rate (more L2 traffic):
python scripts/demo_cache.py --api-key pn_live_… --paraphrase-rate 0.5

# 200 requests, every request printed:
python scripts/demo_cache.py --api-key pn_live_… --runs 200 -v
```

You should see the hit rate start at 0% (first time we see each
anchor) and climb steadily as duplicates and paraphrases land. At the
default mix (40% exact dup / 30% paraphrase / 30% unique) the steady
state is ~70%.

### What's actually happening

| Bucket | Path | Latency | Cache verdict |
| --- | --- | --- | --- |
| Exact duplicate of a previous anchor | `cache.get` → L1 hit | <50 ms | exact |
| Paraphrase of a previous anchor | `cache.get` → L1 miss → L2 hit (promoted to L1) | <100 ms | semantic |
| Unique new question | `cache.get` → L1 miss → L2 miss → provider call | provider RTT | miss |

The Prometheus counter is authoritative — open Grafana →
**Pronaos → FinOps** to see `pronaos_cache_lookups_total` plotted live.

## `demo_guardrails.py` — PII redaction + prompt injection

Sends a curated mix of clean prompts, prompts containing synthetic PII
(emails, phones, SSNs, credit cards with valid Luhn checksums, IPs),
and known jailbreak preambles. For each request prints:

- the original prompt (what *you* sent — PII still visible to you)
- which rules fired and what action was taken (REDACT / LOG_ONLY / BLOCK)
- the response the client received (also egress-scanned)

```bash
python scripts/demo_guardrails.py --api-key pn_live_…

# narrow to one category:
python scripts/demo_guardrails.py --api-key pn_live_… --only injection
```

### Prerequisites

Same as `demo_cache.py` — a running gateway with a working provider
credential. Guardrails are **on by default** (`PRONAOS_GUARDRAILS_ENABLED=true`)
so no extra env vars needed.

### How the script knows what fired

The gateway stamps an `X-Pronaos-Guardrails` header on every response:

| Header value | Meaning |
| --- | --- |
| absent | no rule fired |
| `redacted:pii.email,pii.phone` | listed rules' redactions were applied |
| `blocked:<rule>` | request short-circuited with 422 |

LOG_ONLY hits (which don't change the request or response) increment
the Prometheus counter but don't set this header — the script counts
them via the curated prompt categories instead.

### What's plotted in Grafana

`pronaos_guardrail_hits_total{rule, action, direction}` is the
authoritative counter. The **Pronaos — Overview** dashboard has a
*Guardrail hits per minute by rule* panel at the bottom that lights
up while the demo runs.

## `eval_cache_quality.py` — cache faithfulness experiment

The most-bothered-with question a recruiter asks of a cache layer:
*"How do you know the cache doesn't corrupt responses?"*

This script answers it empirically:

1. Clears Redis (`docker exec`) and Qdrant (`DELETE /collections/...`)
2. Runs the eval suite — every request is cache miss; records scores
3. Runs the same suite again — every request is cache hit (L1); records scores
4. Computes per-case Δscore and aggregate stats
5. Exits 0 if `max |Δ| ≤ ε` (claim holds), 1 otherwise

### Run it

```bash
python scripts/eval_cache_quality.py --api-key pn_live_…

# tighter tolerance for CI gating:
python scripts/eval_cache_quality.py --api-key pn_live_… --epsilon 0.001

# pin the comparison JSON for archival:
python scripts/eval_cache_quality.py --api-key pn_live_… \
    --output docs/cache-quality-result.json
```

### Sample output (real run, Groq 8B candidate / Groq 70B judge)

```text
fresh run  →  mean: 1.000  scored: 8/8  wall: 21.8s
cached run →  mean: 1.000  scored: 8/8  wall: 11.8s

max |Δ|:          0.0000
cases over ε:     0 / 8

✅ CLAIM HOLDS: cache preserves quality.
```

The 10-second wall-clock difference confirms the cached run actually
short-circuited every provider call. Δ = 0.0000 confirms the cache
returns byte-identical responses (no encoding bugs, no header leakage,
no mutation in transit).

### Why this matters

This isn't a unit test — it's an empirical correctness claim. A future
PR that subtly breaks the cache (say, by mutating the response dict
between cache write and return) will fail this experiment but pass the
unit suite. That's the gap between "code does what I wrote" and "code
does what I meant."

## `eval_paraphrase_cache_quality.py` — the harder claim

`eval_cache_quality.py` verifies the L1 (exact-match) cache is
**faithful** — re-asking the same prompt returns the same response with
the same score.

`eval_paraphrase_cache_quality.py` asks the **interesting** question:
when the user reasks the same intent in *different words*, does the L2
(semantic / embedding-similarity) cache serve a cached response, AND
does that response still score against the rubric?

### Method

1. Clear Redis and the Qdrant points (NOT the collection — see note)
2. Prime: run [`tests/eval/data/basic.yaml`](../tests/eval/data/basic.yaml)
   → fills the cache, records fresh scores
3. Send [`tests/eval/data/basic_paraphrased.yaml`](../tests/eval/data/basic_paraphrased.yaml)
   prompts → reads `X-Pronaos-Cache: hit:semantic:<sim>` headers to
   detect L2 hits, scores each response
4. For every case, compute Δ = paraphrased score − fresh score
5. Aggregate: L2 hit rate, mean Δ, max |Δ|, verdict

### The threshold-curve story (real numbers)

| `PRONAOS_SEMANTIC_CACHE_THRESHOLD` | L2 hit rate | Max \|Δ\| |
| --- | --- | --- |
| 0.95 | 12.5% (1/8) | 0.0000 |
| 0.85 | 87.5% (7/8) | 0.0000 |

At threshold 0.85, the cache returns a single stored response for 7
different phrasings of the same intent — and the judge scores all 7
identically. That's empirical proof that the semantic cache is doing
what it claims to.

### Important caveats

- **The L2 cache requires gateway restart between threshold changes.**
  The threshold is read from `Settings` at lifespan startup.
- **The script clears Qdrant POINTS not the COLLECTION.** Dropping the
  collection would silently break writes until the next restart
  (the collection is created in `ensure_ready()` which only runs at
  lifespan startup). An earlier version of this script had that bug;
  the fix is to use `POST /collections/.../points/delete` with an
  empty match-all filter.
- **The judge sees the cached response, not the paraphrased prompt.**
  This is by design — the cache's job is to serve correct answers,
  not to match prompts cosmetically. The rubric grades correctness,
  so a slightly-off cached response would lose score.

## `eval_guardrail_quality.py` — does redaction degrade answers?

The trust-and-safety question that most teams skip. We measured it.

### Method

1. Take [`tests/eval/data/basic.yaml`](../tests/eval/data/basic.yaml)
   — clean prompts, no PII, baseline scores
2. Take [`tests/eval/data/basic_with_pii.yaml`](../tests/eval/data/basic_with_pii.yaml)
   — same 8 case IDs and rubrics, but each prompt has incidental PII
   injected
3. Clear caches between runs (otherwise the cache would confound the
   experiment), send both sets, score each response against the SAME
   rubric
4. Per-case Δ + aggregate

### Real result (Groq 8B candidate, Groq 70B judge)

```text
clean mean:    1.000  (8/8 correct)
redacted mean: 0.875  (7/8 correct — one case 1.00 → 0.00)
max |Δ|:       1.0000

→ Redaction safe for incidental PII (7/8 cases).
→ Redaction broke 1 case: TCP/UDP question with office IPs as setup.
  The redacted [REDACTED-IP] tokens in a networking context caused
  Groq 8B to over-refuse — interpreting the redaction itself as a
  signal that the user was asking about something sensitive.
```

### Why this matters

Most teams ship redaction and assume quality is preserved. We **proved
otherwise on one out of eight cases**. The failure mode is honest:
redaction can over-fire when the redacted token is *topically* relevant
to the question (IPs in a networking prompt; SSNs in a tax-policy
prompt; etc.). Without an experiment like this, the failure is
invisible until a user complains.

The mitigation belongs in a follow-up phase — per-tenant policy that
can disable specific rules for specific endpoints, or a rephrase pass
that replaces redacted tokens with type-aware placeholders. The point
isn't that redaction is broken; it's that **quality regressions need
measurement, not assumption**.
