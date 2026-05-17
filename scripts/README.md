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
