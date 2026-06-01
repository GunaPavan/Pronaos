"""Per-team model allowlist matching.

A team's ``allowed_models`` field is a list of glob patterns (or NULL
for "unrestricted"). The chat handler asks ``is_model_allowed`` for
every request; if it returns False the request is denied with 403
before any provider call or quota deduction happens.

Pattern semantics
-----------------
We use the stdlib ``fnmatch`` semantics, which match shell globs:

- ``*``  matches any sequence of characters (greedy)
- ``?``  matches any single character
- ``[seq]`` matches any character in the sequence
- ``[!seq]`` matches any character NOT in the sequence

Examples (the ones operators actually want):

- ``"groq/*"``                       — every model under the Groq prefix
- ``"anthropic/claude-opus-*"``      — only Opus models on Anthropic
- ``"groq/llama-3.1-8b-instant"``    — exact pin (most restrictive)
- ``"*"``                            — everything (matches NULL semantically)

Note that ``fnmatch.fnmatch`` treats ``/`` as a normal character, so
``"groq/*"`` matches ``"groq/llama-3.1-8b-instant"`` AND
``"groq/meta-llama/llama-4-scout-17b-16e-instruct"`` — providers like
Groq sometimes namespace models within their prefix and we want the
intuitive "everything under this provider" behaviour.

Why fnmatch, not regex
----------------------
Operators set these by hand from a CLI or admin endpoint. Glob is the
syntax they already know (gitignore, .npmignore, shell). A regex with
``^groq/.*$`` reads worse than ``groq/*`` and adds a surface area
(catastrophic-backtracking patterns) we don't want exposed to tenant
input. fnmatch is simple, predictable, stdlib.

Validation
----------
``validate_allowed_models`` is called by the CLI / admin endpoint
before write. It enforces:

- Must be a list (NOT a dict, NOT a string)
- Every element must be a non-empty string
- Empty list ``[]`` is allowed — explicit "deny everything"

Pattern syntax is NOT validated for correctness because fnmatch
accepts arbitrary strings (any string is a valid pattern, even if
it never matches). A pattern that's a typo silently denies — which
is the SAFE default. The admin endpoint can offer a "test" affordance
in the future to preview matches.
"""

from __future__ import annotations

import fnmatch
from typing import Final

# Sentinel returned by ``validate_allowed_models`` for the unrestricted
# case. Distinguishing "None" (no policy) from "[]" (deny-everything)
# is important — see module docstring.
UNRESTRICTED: Final = None


def is_model_allowed(model: str, allowed_models: list[str] | None) -> bool:
    """Return True if ``model`` matches at least one pattern in ``allowed_models``.

    ``None`` allowlist → unrestricted (backwards-compat for teams that
    never had a policy set). ``[]`` → deny everything (intentional
    pause). Otherwise: standard fnmatch any-of semantics.
    """
    if allowed_models is None:
        # Unrestricted — no team policy set.
        return True
    # Empty list is an explicit deny-everything (paused team).
    if not allowed_models:
        return False
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in allowed_models)


def validate_allowed_models(value: object) -> list[str]:
    """Validate user-supplied ``allowed_models`` and return the canonical form.

    Raises ``ValueError`` with a human-readable reason on bad input —
    the CLI and admin endpoint surface this directly to the operator.

    Returns the input list of strings unchanged. An empty list is
    returned as-is (it's a valid "deny everything" policy). NULL (the
    "unrestricted" sentinel) is handled separately by the caller — this
    function only validates the LIST form.
    """
    if not isinstance(value, list):
        raise ValueError(
            "allowed_models must be a JSON list (or null to clear the policy); "
            f"got {type(value).__name__}"
        )
    for i, entry in enumerate(value):
        if not isinstance(entry, str):
            raise ValueError(
                f"allowed_models[{i}] must be a string pattern; got {type(entry).__name__}"
            )
        if not entry.strip():
            raise ValueError(f"allowed_models[{i}] is empty — patterns must be non-empty strings")
    return value
