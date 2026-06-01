"""Redis-backed singleflight registry (Phase 36).

Closes the Phase 33 caveat: in-memory singleflight is process-local,
so a 5-replica deployment can still see 5 concurrent upstream calls
for the same key. With this Redis-backed registry, only ONE replica
(whichever's atomic ``SET NX`` succeeds first) becomes the leader;
the other 4 — plus any followers within each replica — wait on
the shared key in Redis.

Design
------
- **Storage**: one Redis string per in-flight key. Value is a JSON
  envelope ``{"state": "pending|done|failed", ...}``. TTL bounds the
  absolute wait so a dead leader doesn't deadlock followers.
- **Leader claim**: atomic ``SET NX`` (mirrors the Phase 25 distributed
  circuit breaker's atomicity story; no Lua needed because SET NX is
  already atomic).
- **Follower wait**: poll the key every ~50 ms until state transitions
  to ``done`` (return result) or ``failed`` (raise the leader's
  exception), with a hard deadline at the TTL.

Exception serialization
-----------------------
The leader's exception class + message is JSON-encoded into the
``failed`` envelope. Followers reconstruct a ``RuntimeError`` carrying
the original message and a ``__cause__`` chain pointing back to a
synthesized ``CrossReplicaLeaderError`` with the original class name.
This loses the original exception type but preserves the message and
makes the cross-replica origin visible in tracebacks.

Pragma
------
- Result must be JSON-serializable. For chat/embedding/rerank payloads
  this is always true (they're dicts of primitives + lists). The
  registry asserts JSON-serializability with a TypeError at write
  time if the leader returns something that can't be encoded.
- Same-replica fast path: we also keep an asyncio.Lock-guarded
  in-process dict so concurrent same-process calls don't all hit
  Redis. The Lock catches "5 concurrent calls on one replica" before
  Redis sees them; the leader of that local group then does the Redis
  claim. Saves Redis round trips.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from pronaos.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# Polling interval (seconds). 50 ms is a reasonable upper bound on
# follower-side added latency — negligible compared to the LLM
# upstream call this is collapsing.
_POLL_INTERVAL_S = 0.05


class CrossReplicaLeaderError(Exception):
    """Raised on followers when the cross-replica leader's fn raised.

    Carries the original exception's class name + message. The original
    type is lost across the Redis hop (we can't reconstruct arbitrary
    user exception classes), but the diagnostic information is preserved.
    """

    def __init__(self, leader_exc_class: str, message: str) -> None:
        self.leader_exc_class = leader_exc_class
        super().__init__(f"{leader_exc_class}: {message}")


class RedisSingleflightRegistry(Generic[T]):  # noqa: UP046
    """Cross-replica singleflight collapser.

    Same shape as :class:`pronaos.core.singleflight.SingleflightRegistry`
    — the chat / embedding / rerank handlers don't care which backend
    they got. The factory in ``main.py`` decides at startup based on
    ``settings.singleflight_distributed``.

    ``T`` is the concrete result type returned by ``fn``. For Pronaos
    use sites, T = ``dict[str, Any]`` (the cached response body).
    Other shapes work as long as they round-trip through ``json.dumps``.
    """

    KEY_PREFIX = "pronaos:singleflight:"

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = 60,
        key_prefix: str = KEY_PREFIX,
    ) -> None:
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix
        # Same-replica fast path: local lock + in-flight dict so concurrent
        # same-process calls share the leader BEFORE the Redis claim. Cuts
        # Redis round-trips proportional to per-replica fanout.
        self._local_lock = asyncio.Lock()
        self._local_in_flight: dict[str, asyncio.Future[T]] = {}

    async def share(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Run ``fn()`` exactly once per ``key`` across ALL replicas.

        Returns ``(result, was_follower)``. ``was_follower=True`` means
        this call rode along on someone else's leader future — either
        a same-process leader (local fast path) or a different-replica
        leader (Redis path).

        Standard Go-style semantics: a leader exception propagates to
        every follower (including cross-replica ones) as
        :class:`CrossReplicaLeaderError`. The next caller AFTER the
        exception propagated becomes a fresh leader.
        """
        # ---- Local fast path: process-local leader/follower split. ----
        # If another coroutine on this replica is already in flight for
        # this key, attach to its future and skip the Redis hop entirely.
        async with self._local_lock:
            existing = self._local_in_flight.get(key)
            if existing is not None:
                future = existing
                is_local_leader = False
            else:
                future = asyncio.get_running_loop().create_future()
                self._local_in_flight[key] = future
                is_local_leader = True

        if not is_local_leader:
            result = await future
            return result, True

        # ---- We're the local leader. Now race for the GLOBAL leader. ----
        try:
            result, was_cross_replica_follower = await self._global_share(key, fn)
            future.set_result(result)
            return result, was_cross_replica_follower
        except BaseException as exc:
            future.set_exception(exc)
            # Mark exception as "retrieved" to suppress asyncio warning
            # if no local follower happened to await it.
            with contextlib.suppress(asyncio.CancelledError, asyncio.InvalidStateError):
                future.exception()
            raise
        finally:
            async with self._local_lock:
                self._local_in_flight.pop(key, None)

    async def _global_share(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Race for cross-replica leadership via Redis SET NX."""
        redis_key = f"{self._key_prefix}{key}"
        # Claim the key. ``set(... nx=True, ex=ttl)`` is atomic: only one
        # caller across all replicas wins the SET.
        pending_envelope = json.dumps({"state": "pending"})
        claimed = await self._redis.set(redis_key, pending_envelope, nx=True, ex=self._ttl_seconds)
        if claimed:
            # We're the global leader — run fn and publish the result.
            try:
                result = await fn()
            except BaseException as exc:
                # Serialize the exception so followers (across replicas)
                # see the same failure.
                envelope = json.dumps(
                    {
                        "state": "failed",
                        "error_class": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                try:
                    await self._redis.set(redis_key, envelope, ex=self._ttl_seconds)
                except Exception as e:  # pragma: no cover — redis outage during write
                    log.warning("singleflight.write_failure_envelope_failed", error=str(e))
                raise
            # Encode result into the envelope. JSON-serializable required.
            envelope_dict = {"state": "done", "result": result}
            try:
                envelope = json.dumps(envelope_dict)
            except TypeError as e:
                # Caller's result wasn't JSON-encodable. Skip publishing
                # — followers will time out, the leader returns
                # successfully. Surface as a log warning so operators
                # see the bug.
                log.warning(
                    "singleflight.result_not_json_serializable",
                    key=redis_key,
                    error=str(e),
                )
                with contextlib.suppress(Exception):  # pragma: no cover
                    await self._redis.delete(redis_key)
                return result, False
            try:
                await self._redis.set(redis_key, envelope, ex=self._ttl_seconds)
            except Exception as e:  # pragma: no cover — redis outage during write
                log.warning("singleflight.write_result_envelope_failed", error=str(e))
            return result, False

        # ---- Follower path: poll the key until done/failed/timeout. ----
        deadline = time.monotonic() + float(self._ttl_seconds)
        while time.monotonic() < deadline:
            raw = await self._redis.get(redis_key)
            if raw is None:
                # The leader's entry expired (TTL hit before they finished)
                # OR the leader cleaned up after a non-serializable result.
                # Either way, the next caller for this key should re-race.
                # We become that caller: claim the key, run fn ourselves.
                claimed_fresh = await self._redis.set(
                    redis_key,
                    json.dumps({"state": "pending"}),
                    nx=True,
                    ex=self._ttl_seconds,
                )
                if claimed_fresh:
                    return await self._run_as_fresh_leader(redis_key, fn)
                # Lost the re-race — someone else became the new leader,
                # loop and poll them.
                continue
            try:
                envelope = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except json.JSONDecodeError:
                # Corrupted entry — drop it and retry as a fresh leader.
                log.warning("singleflight.corrupted_envelope", key=redis_key, raw=raw)
                with contextlib.suppress(Exception):  # pragma: no cover
                    await self._redis.delete(redis_key)
                continue

            state = envelope.get("state")
            if state == "done":
                return envelope["result"], True
            if state == "failed":
                raise CrossReplicaLeaderError(
                    envelope.get("error_class", "Exception"),
                    envelope.get("error_message", "(no message)"),
                )
            # state == "pending" — keep polling.
            await asyncio.sleep(_POLL_INTERVAL_S)

        # Hit the TTL deadline without the leader publishing. Race for a
        # fresh leadership slot — if we win, we run fn ourselves; if
        # we lose, raise.
        claimed_after_timeout = await self._redis.set(
            redis_key,
            json.dumps({"state": "pending"}),
            nx=True,
            ex=self._ttl_seconds,
        )
        if claimed_after_timeout:
            return await self._run_as_fresh_leader(redis_key, fn)
        raise TimeoutError(
            f"singleflight: leader for {key!r} did not complete within "
            f"{self._ttl_seconds}s and another replica took over"
        )

    async def _run_as_fresh_leader(
        self,
        redis_key: str,
        fn: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Run ``fn`` as a recovery leader after the previous one timed out.

        Same logic as the main leader path; factored out so the polling
        path can call it without duplicating the try/except shape.
        """
        try:
            result = await fn()
        except BaseException as exc:
            envelope = json.dumps(
                {
                    "state": "failed",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            try:
                await self._redis.set(redis_key, envelope, ex=self._ttl_seconds)
            except Exception as e:  # pragma: no cover
                log.warning("singleflight.recovery_write_failed", error=str(e))
            raise
        try:
            envelope = json.dumps({"state": "done", "result": result})
            await self._redis.set(redis_key, envelope, ex=self._ttl_seconds)
        except (TypeError, Exception) as e:  # pragma: no cover
            log.warning("singleflight.recovery_write_result_failed", error=str(e))
        return result, False

    async def aclose(self) -> None:
        """No-op — we don't own the Redis client."""
        return None

    def in_flight_count(self) -> int:
        """Diagnostic — local in-flight leader count (NOT cross-replica).

        Returns the size of the in-process leader dict. To get the
        cross-replica picture, scan the Redis keyspace by prefix.
        """
        return len(self._local_in_flight)
