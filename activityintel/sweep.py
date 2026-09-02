"""Walk a source's pages and union the results, reporting coverage honestly.

The one thing this module exists to prevent: **a truncated pool looking
complete.** Klook caps any query at 1000 results while still reporting
``total: 1000``, so "I fetched everything the API offered" and "I hit the
ceiling and there is more out there" produce identical-looking output unless
something says otherwise. Every return value here carries that distinction.

Reach therefore comes from *partitioning the query*, not from paging deeper:
several narrower keywords, unioned by id. `SweepReport.capped_queries` names
the sub-queries that hit the ceiling so the caller can narrow those further
rather than believing the union is exhaustive.
"""
from __future__ import annotations

import dataclasses

from . import config, transport
from .model import Activity


@dataclasses.dataclass
class QueryResult:
    query: str
    reported_total: int | None
    pages_walked: int
    returned: int
    capped: bool
    error: str | None = None


@dataclasses.dataclass
class SweepReport:
    source: str
    queries: list[QueryResult]
    activities: list[Activity]
    requests_sent: int
    cache_hits: int

    @property
    def capped_queries(self) -> list[str]:
        return [q.query for q in self.queries if q.capped]

    @property
    def failed_queries(self) -> list[str]:
        return [q.query for q in self.queries if q.error]

    @property
    def first_error(self) -> str | None:
        """The first recorded failure, verbatim. A note that names the keywords
        that failed and not the fault ("CacheMiss: not cached and --cache-only
        was requested") sends the reader to the wrong remedy."""
        return next((q.error for q in self.queries if q.error), None)

    @property
    def is_complete(self) -> bool:
        """True only when every sub-query was fully walked without hitting a cap.

        Deliberately conservative: a single capped or failed sub-query makes the
        whole union a sample, and callers must label it that way.
        """
        return not self.capped_queries and not self.failed_queries

    def coverage_note(self) -> str | None:
        """Human-readable warning, or None when the sweep really was exhaustive."""
        bits = []
        if self.capped_queries:
            bits.append(
                f"{len(self.capped_queries)} quer{'y' if len(self.capped_queries) == 1 else 'ies'} "
                f"hit the source's result ceiling ({', '.join(self.capped_queries[:5])}"
                f"{', …' if len(self.capped_queries) > 5 else ''}) — more listings exist "
                f"than were returned. Narrow those keywords to reach further."
            )
        if self.failed_queries:
            bits.append(
                f"{len(self.failed_queries)} quer{'y' if len(self.failed_queries) == 1 else 'ies'} "
                f"failed ({', '.join(self.failed_queries[:5])}) — this union is missing "
                f"whatever they would have contributed. First error: {self.first_error}"
            )
        return " ".join(bits) if bits else None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "returned": len(self.activities),
            "complete": self.is_complete,
            "coverage_note": self.coverage_note(),
            "requests_sent": self.requests_sent,
            "cache_hits": self.cache_hits,
            "queries": [dataclasses.asdict(q) for q in self.queries],
            "activities": [a.to_dict() for a in self.activities],
        }


def sweep(client, source, queries, *, page_size: int | None = None,
          max_pages: int = config.MAX_SWEEP_PAGES, lang: str = "en_US",
          on_progress=None) -> SweepReport:
    """Union ``source``'s results across ``queries``.

    A failing query is recorded and the sweep continues — one dead keyword must
    not discard the other nine — but it is surfaced in ``failed_queries`` and
    flips ``is_complete`` to False. Silence is exactly what this refuses to do.
    """
    size = page_size or getattr(source, "MAX_PAGE_SIZE", 50)
    ceiling = min(max_pages, getattr(source, "MAX_PAGE", max_pages))

    seen: dict[str, Activity] = {}
    results: list[QueryResult] = []

    for query in queries:
        pages = 0
        got = 0
        total = None
        capped = False
        error = None
        try:
            for page in range(1, ceiling + 1):
                batch = source.fetch_search(client, query, page=page, size=size,
                                            lang=lang)
                pages += 1
                total = batch.get("total", total)
                capped = capped or bool(batch.get("capped"))
                items = batch.get("activities") or []
                if not items:
                    break
                got += len(items)
                for a in items:
                    seen.setdefault(a.key, a)
                if len(items) < size:
                    break  # short page: the pool ended before the ceiling
            else:
                # Loop finished without breaking -> we stopped because we ran out
                # of allowed pages, not because the source ran out of results.
                capped = True
        except transport.INCIDENTS:
            # A rate limit or a robots verdict is an incident for the whole
            # host, not a fact about this keyword. Recording it and moving to
            # the next query would keep sending to a host that said stop, and
            # exit PARTIAL for what was a refusal. See transport.INCIDENTS.
            raise
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            error = f"{type(exc).__name__}: {exc}"

        results.append(QueryResult(query=query, reported_total=total,
                                   pages_walked=pages, returned=got,
                                   capped=capped, error=error))
        if on_progress:
            on_progress(results[-1], len(seen))

    return SweepReport(
        source=getattr(source, "NAME", "unknown"),
        queries=results,
        activities=list(seen.values()),
        requests_sent=client.requests_sent,
        cache_hits=client.cache_hits,
    )
