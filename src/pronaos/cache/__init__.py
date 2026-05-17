"""Response caching layer.

Two-tier design:

- **L1: exact-match** (Redis) — canonical hash of (model, messages,
  temperature, max_tokens). The fast path for repeat queries.
- **L2: semantic** (Qdrant) — embedding similarity above a configurable
  threshold. Catches paraphrases that L1 misses ("hi there" vs "hello").

L1 lookups run first; if they miss, L2 is tried; if both miss the request
hits the provider. Writes go to BOTH tiers so a future exact match also
benefits from the semantic-match insertion path.

Tenant isolation is enforced at the cache key (L1) and via a payload
filter (L2). There is no cross-tenant lookup path.
"""

from pronaos.cache.base import Cache, CacheLookup
from pronaos.cache.null import NullCache

__all__ = ["Cache", "CacheLookup", "NullCache"]
