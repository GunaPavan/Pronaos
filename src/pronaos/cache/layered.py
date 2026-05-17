"""Two-tier cache composition.

Read path
---------
L1 (exact) → hit? return.
L1 miss → L2 (semantic) → hit? populate L1 with the response, return.
L2 miss → return miss.

The L1-after-L2-hit write is a deliberate optimisation: if a paraphrase
finds a stored response in L2, the same paraphrase the second time
should hit the cheaper L1 path. Cache locality compounds.

Write path
----------
``put`` writes to **both** tiers concurrently with ``asyncio.gather``.
Writing both keeps the layers consistent: a future identical request
hits L1; a future paraphrase hits L2.

Fail-open
---------
Every backend already fails open internally — this composer just
combines their results. If L1 raises (it shouldn't — its impl swallows
errors) we still try L2. The only failure mode that reaches the caller
is a miss, never an exception.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pronaos.cache.base import Cache, CacheLookup
from pronaos.logging import get_logger

log = get_logger(__name__)


class LayeredCache(Cache):
    """Compose L1 + L2 caches with promotion-on-L2-hit semantics."""

    def __init__(self, l1: Cache, l2: Cache) -> None:
        self._l1 = l1
        self._l2 = l2

    async def get(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
    ) -> CacheLookup:
        # L1 first — cheap, exact match.
        try:
            l1_result = await self._l1.get(
                tenant_id=tenant_id, model=model, key_payload=key_payload
            )
        except Exception as e:
            # Should never happen (backends fail-open), but if it does
            # we don't let it propagate.
            log.warning("cache.layered.l1_get_failed", error=str(e))
            l1_result = CacheLookup(hit=False)

        if l1_result.hit:
            return l1_result

        # L2 fallthrough — semantic match.
        try:
            l2_result = await self._l2.get(
                tenant_id=tenant_id, model=model, key_payload=key_payload
            )
        except Exception as e:
            log.warning("cache.layered.l2_get_failed", error=str(e))
            return CacheLookup(hit=False)

        if l2_result.hit and l2_result.response is not None:
            # Promote into L1 so the same paraphrase hits the cheaper
            # path next time. Best-effort — promotion failure is just a
            # lost optimisation, not a correctness issue.
            try:
                await self._l1.put(
                    tenant_id=tenant_id,
                    model=model,
                    key_payload=key_payload,
                    response=l2_result.response,
                )
            except Exception:  # noqa: BLE001
                pass
            return l2_result

        return CacheLookup(hit=False)

    async def put(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        # Both writes can run in parallel — they touch independent
        # backends. ``return_exceptions=True`` so one slow/failing
        # backend doesn't take down the other.
        await asyncio.gather(
            self._l1.put(
                tenant_id=tenant_id, model=model, key_payload=key_payload, response=response
            ),
            self._l2.put(
                tenant_id=tenant_id, model=model, key_payload=key_payload, response=response
            ),
            return_exceptions=True,
        )

    async def aclose(self) -> None:
        await asyncio.gather(self._l1.aclose(), self._l2.aclose(), return_exceptions=True)
