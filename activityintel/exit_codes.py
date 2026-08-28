"""Typed exit codes so an agent can branch on *why* a run stopped.

Kept in its own module to avoid an import cycle between the CLI and the layers
that raise these conditions.

The distinction that matters most operationally is PARTIAL (we reached the
source, but could not cover the whole pool) versus UPSTREAM (the source refused
us). Both produce fewer rows than expected; only one of them means the market is
actually that small. Collapsing them is how a rate-limited sweep gets read as
"this city has 12 cooking classes".
"""
from __future__ import annotations

OK = 0
USAGE = 2
CONFIG = 3
RATE_LIMIT = 5     # the source returned 429/403 — an incident, never a page to skip
UPSTREAM = 6       # the source returned an error we cannot interpret
PARTIAL = 7        # a pool could only be partly covered; the result is not authoritative
CONTRACT = 8       # a source's response no longer matches the shape we parse
