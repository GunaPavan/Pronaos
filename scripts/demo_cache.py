"""Live cache-effectiveness demo.

Fires a configurable mix of exact-duplicate, paraphrased, and unique
prompts at a running Pronaos gateway and prints the cache hit rate in
real time. Designed to be the answer to "does the semantic cache
actually work?" — open Grafana, run this script, watch the hit-rate
panel climb from 0% to ~70% over 60 seconds.

Usage
-----

    # mint a key once:
    pronaos-cli tenant create demo
    pronaos-cli team create eng --tenant <tenant-id>
    pronaos-cli key issue --team <team-id> --label demo

    # then run the demo (against the local gateway):
    python scripts/demo_cache.py --api-key pn_live_...

    # tweak the traffic mix:
    python scripts/demo_cache.py --runs 200 --paraphrase-rate 0.5

    # point at a non-local gateway:
    PRONAOS_DEMO_API_KEY=... python scripts/demo_cache.py \\
        --base-url https://pronaos.example.com

What the output means
---------------------

Each request lands in one of three buckets:
    hit       — served from cache (L1 or L2; tier visible in --verbose)
    miss      — went upstream
    skip      — bypassed (shouldn't happen here; we send temperature=0)

The "savings" estimate divides cached requests by the cost-per-call of
the model in use (rough — Pronaos' actual cost tracker is authoritative).

The script is provider-agnostic: whatever model you pass through
``--model`` must be reachable by the gateway (set the matching API key
in the gateway's .env). For zero-cost runs use Groq's free tier
(``--model groq/llama-3.1-8b-instant``) or a local Ollama
(``--model ollama/llama3.2``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

# --------------------------------------------------------------------------- #
# Traffic generator                                                           #
# --------------------------------------------------------------------------- #

# Question buckets — each (anchor, paraphrases) tuple. The anchor is what
# we cache; paraphrases of the same anchor SHOULD hit the L2 semantic cache
# even though they're not exact-match. Unique prompts always miss.
QUESTIONS: list[tuple[str, list[str]]] = [
    (
        "What is the capital of France?",
        [
            "Tell me the capital of France.",
            "Which city is France's capital?",
            "France's capital — what is it?",
        ],
    ),
    (
        "Explain dynamic programming in one sentence.",
        [
            "Define dynamic programming briefly.",
            "Give me a one-line description of dynamic programming.",
        ],
    ),
    (
        "How does TCP differ from UDP?",
        [
            "What's the difference between TCP and UDP?",
            "Compare TCP and UDP for me.",
        ],
    ),
    (
        "Summarize what a transformer model is.",
        [
            "Briefly describe a transformer architecture.",
            "What is a transformer neural network?",
        ],
    ),
    (
        "What's the time complexity of quicksort?",
        [
            "Tell me quicksort's complexity.",
            "How fast is quicksort?",
        ],
    ),
]


@dataclass(slots=True)
class TrafficPlan:
    """Decision rule for what each request will look like."""

    duplicate_rate: float
    paraphrase_rate: float
    # remainder is unique prompts (incrementing counter)

    @property
    def unique_rate(self) -> float:
        return max(0.0, 1.0 - self.duplicate_rate - self.paraphrase_rate)

    def __post_init__(self) -> None:
        total = self.duplicate_rate + self.paraphrase_rate
        if total > 1.0:
            raise ValueError(
                f"duplicate_rate + paraphrase_rate must be <= 1.0 (got {total})"
            )


def next_prompt(plan: TrafficPlan, *, unique_counter: int, rng: random.Random) -> str:
    """Pick a prompt according to the traffic plan.

    - With probability ``duplicate_rate``: pick a known anchor verbatim.
    - With probability ``paraphrase_rate``: pick an anchor's paraphrase.
    - Otherwise: emit a unique sequential question (always a miss).
    """
    roll = rng.random()
    if roll < plan.duplicate_rate:
        anchor, _ = rng.choice(QUESTIONS)
        return anchor
    if roll < plan.duplicate_rate + plan.paraphrase_rate:
        anchor, paraphrases = rng.choice(QUESTIONS)
        return rng.choice(paraphrases)
    return f"What is the {unique_counter}-th prime number?"


# --------------------------------------------------------------------------- #
# HTTP runner                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Stats:
    total: int = 0
    l1_hits: int = 0  # exact-match cache
    l2_hits: int = 0  # semantic cache
    misses: int = 0
    skips: int = 0
    errors: int = 0
    # Total time spent in the gateway (seconds). Useful to compare cached
    # vs uncached latency at a glance.
    cumulative_seconds: float = 0.0
    error_messages: list[str] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return self.l1_hits + self.l2_hits

    @property
    def hit_rate(self) -> float:
        served = self.hits + self.misses
        return self.hits / served if served else 0.0


async def one_request(
    client: httpx.AsyncClient,
    *,
    model: str,
    prompt: str,
    api_key: str,
) -> tuple[str, float, str | None]:
    """Send one chat-completion request. Returns (tier, duration_s, error).

    ``tier`` ∈ {"miss", "exact", "semantic", "skip"} as reported by the
    gateway's ``X-Pronaos-Cache`` response header. We rely on the header
    rather than a latency heuristic — even cached responses go through
    the full ASGI stack at 100-300 ms on a typical laptop, which would
    fool a "<50 ms = hit" rule.
    """
    start = time.monotonic()
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,  # cache-eligible
                "max_tokens": 64,
            },
            timeout=30.0,
        )
    except Exception as e:
        return "ERR", time.monotonic() - start, f"network: {e}"

    duration = time.monotonic() - start
    if resp.status_code != 200:
        # Truncate the body so a verbose 401/500 doesn't overflow the
        # console. The status code alone usually says enough.
        return "ERR", duration, f"http {resp.status_code}: {resp.text[:200]}"

    # Header format: "hit:exact", "hit:semantic:0.97", "miss", "skip", or absent.
    raw = resp.headers.get("x-pronaos-cache", "miss")
    if raw.startswith("hit:semantic"):
        return "semantic", duration, None
    if raw.startswith("hit:"):
        return "exact", duration, None
    if raw == "skip":
        return "skip", duration, None
    return "miss", duration, None


async def run_demo(
    *,
    base_url: str,
    api_key: str,
    model: str,
    runs: int,
    plan: TrafficPlan,
    seed: int,
    verbose: bool,
) -> Stats:
    stats = Stats()
    rng = random.Random(seed)

    print(f"target:    {base_url}")
    print(f"model:     {model}")
    print(f"runs:      {runs}")
    print(
        f"mix:       {plan.duplicate_rate:.0%} exact duplicates / "
        f"{plan.paraphrase_rate:.0%} paraphrases / {plan.unique_rate:.0%} unique"
    )
    print()
    print(
        f"{'i':>4} {'tier':>8} {'lat_ms':>7} {'hit_rate':>8}   prompt"
    )
    print("-" * 80)

    async with httpx.AsyncClient(base_url=base_url) as client:
        for i in range(1, runs + 1):
            prompt = next_prompt(plan, unique_counter=i, rng=rng)
            tier, duration, error = await one_request(
                client, model=model, prompt=prompt, api_key=api_key
            )
            stats.total += 1
            stats.cumulative_seconds += duration

            if error is not None:
                stats.errors += 1
                if len(stats.error_messages) < 5:
                    stats.error_messages.append(error)
            elif tier == "exact":
                stats.l1_hits += 1
            elif tier == "semantic":
                stats.l2_hits += 1
            elif tier == "skip":
                stats.skips += 1
            else:  # "miss"
                stats.misses += 1

            if verbose or i <= 5 or i % 10 == 0 or i == runs:
                shown = prompt if len(prompt) <= 42 else prompt[:39] + "..."
                print(
                    f"{i:>4} {tier:>8} {int(duration * 1000):>7} "
                    f"{stats.hit_rate:>8.1%}   {shown}"
                )

    return stats


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive synthetic traffic at a Pronaos gateway and "
        "print live cache hit rate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument(
        "--api-key",
        default=os.environ.get("PRONAOS_DEMO_API_KEY"),
        help="API key (or set PRONAOS_DEMO_API_KEY). Mint one with "
        "`pronaos-cli key issue`.",
    )
    p.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help="Model id understood by the gateway's router. Default is "
        "Groq's free-tier 8b model so a fresh clone runs at zero cost.",
    )
    p.add_argument("--runs", type=int, default=60)
    p.add_argument(
        "--duplicate-rate",
        type=float,
        default=0.4,
        help="Fraction of requests that repeat a known anchor verbatim "
        "(L1 hits after the first).",
    )
    p.add_argument(
        "--paraphrase-rate",
        type=float,
        default=0.3,
        help="Fraction of requests that paraphrase a known anchor (L2 hits).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed — same seed produces the same traffic pattern, "
        "useful for screencast reproducibility.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Print every request, not just every 10th."
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.api_key:
        print(
            "error: --api-key is required (or set PRONAOS_DEMO_API_KEY).\n"
            "       mint one with: pronaos-cli key issue --team <id>",
            file=sys.stderr,
        )
        return 2

    plan = TrafficPlan(
        duplicate_rate=args.duplicate_rate, paraphrase_rate=args.paraphrase_rate
    )

    try:
        stats = asyncio.run(
            run_demo(
                base_url=args.base_url,
                api_key=args.api_key,
                model=args.model,
                runs=args.runs,
                plan=plan,
                seed=args.seed,
                verbose=args.verbose,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    print()
    print("=" * 80)
    print(f"total:       {stats.total}")
    print(f"L1 hits:     {stats.l1_hits}  (exact)")
    print(f"L2 hits:     {stats.l2_hits}  (semantic paraphrase)")
    print(f"misses:      {stats.misses}  (forwarded to provider)")
    if stats.skips:
        print(f"skips:       {stats.skips}  (temperature>0 or streaming)")
    print(f"errors:      {stats.errors}")
    print(f"hit rate:    {stats.hit_rate:.1%}")
    print(f"avg lat:     {stats.cumulative_seconds / max(stats.total, 1) * 1000:.0f} ms")
    if stats.error_messages:
        print()
        print("first few errors:")
        for e in stats.error_messages:
            print(f"  • {e}")
    print()
    print("Open Grafana → Pronaos → FinOps to see the cache panels move.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
