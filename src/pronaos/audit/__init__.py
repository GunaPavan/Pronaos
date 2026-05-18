"""Hash-chained audit log (Phase 10).

A tamper-evident record of every successful chat call, designed for
compliance use-cases where you need to prove "this exact request got
this exact response and nothing has been altered since."

Module layout
-------------
- ``AuditLogger.append()`` — writes one record, chained to the
  tenant's previous record by SHA-256.
- ``AuditVerifier.verify()`` — walks a tenant's chain in time order,
  recomputes each ``this_hash``, reports the first break (or "intact").
- ``hash_inputs()`` — pure helper that produces the canonical input
  bytes for the hash. Same function on both the write and verify sides
  so any drift fails both — there's no "you got the verify wrong" mode.

What's NOT in the audit log
---------------------------
Raw request / response BODIES. Storing them would re-introduce the PII
problem the gateway's guardrails exist to solve, AND would bloat the
table by an order of magnitude. We store hashes only — enough to
prove "this request had this content" against a separately-kept copy
(application logs, S3 export, etc.).
"""

from pronaos.audit.logger import AuditLogger, hash_body, hash_inputs
from pronaos.audit.verifier import AuditVerifier, ChainBreak, VerifyResult

__all__ = [
    "AuditLogger",
    "AuditVerifier",
    "ChainBreak",
    "VerifyResult",
    "hash_body",
    "hash_inputs",
]
