"""One entry point: `python3 -m activityintel.cli`.

Design rule for every command here: **never let a short answer look like a
complete one.** Any result that was capped, truncated, or partially failed
carries a `coverage` block and exits non-zero (PARTIAL), so a caller that only
checks the exit code still cannot mistake a sample for a catalogue.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import sys

from . import config, doctor, exit_codes, model, robots, store, sweep, transport
from .render import _emit, _emit_compare
from . import places
from .places import PLACES, resolve_place
from .sources import REGISTRY, airbnb, klook, viator

ALL_SOURCES = REGISTRY

# The sources a command can actually SWEEP. Viator has a parser and a key
# resolver but no fetch: its search is a POST, the transport has no POST, and
# no `cmd_*` block asks it anything. Being *available* (key present) and being
# *askable* are different facts, and until 2026-09-02 they were collapsed —
# `catalog hanoi --sources viator` with a key returned zero rows,
# `complete: true`, exit 0. This set is what `compare` counts and what `doctor`
# reports; `cmd_catalog` additionally refuses to finish without a coverage
# entry for every wanted source, so a source added to ALL_SOURCES and
# forgotten below lands as "not wired", never as silence.
SWEEPABLE = frozenset({klook.NAME, airbnb.NAME})
NOT_WIRED_NOTE = ("not wired: this build has no sweep for this source, so it "
                  "was enabled but never asked and nothing from it is in this "
                  "result. Viator needs a POST transport and a fetch_search "
                  "before a key makes any difference.")


def source_available(mod, *, ignore_robots: bool = False) -> bool:
    """Is this source usable under the caller's policy?

    A source may expose ``available(ignore_robots=...)`` when its answer depends
    on an operator choice (Klook), or a plain ``AVAILABLE`` constant when it does
    not. Asking through one helper keeps every command's idea of "enabled"
    identical — a per-command reimplementation is how a source ends up on in one
    code path and off in another.
    """
    fn = getattr(mod, "available", None)
    if callable(fn):
        try:
            return bool(fn(ignore_robots=ignore_robots))
        except TypeError:
            return bool(fn())
    return bool(getattr(mod, "AVAILABLE", True))


def enabled_sources(ignore_robots: bool = False) -> dict:
    return {n: m for n, m in ALL_SOURCES.items()
            if source_available(m, ignore_robots=ignore_robots)}


def override_hosts(ignore_robots: bool) -> frozenset[str]:
    """Which hosts --ignore-robots actually exempts. Never "all of them".

    Only sources that declare `REQUIRES_ROBOTS_OVERRIDE` are exempted. Airbnb's
    `/api/v3/` path is robots-*allowed*, so passing the flag to reach Klook must
    not also retire the guard on Airbnb's disallowed `/s/*/*` — the exact path an
    earlier version of this tool fetched by mistake. An override wider than its
    need silently disables the check that would catch the next unrelated bug.
    """
    if not ignore_robots:
        return frozenset()
    return frozenset(
        m.HOST for m in ALL_SOURCES.values()
        if getattr(m, "REQUIRES_ROBOTS_OVERRIDE", False) and getattr(m, "HOST", None)
    )


def apply_match_filter(activities, match: str, server_filtered: set[str]):
    """Filter by the user's keyword, differently per source. Extracted so it is
    testable without a network sweep — the blanket-pass bug below lived here and
    survived a mutation run precisely because nothing drove this code path.

    Two filters, because the two halves were matched differently:

    * A source with no keyword API (Airbnb) gets the user's phrase as a
      substring — that IS the filter.
    * A source that matched server-side (Klook) does not get the phrase
      re-applied, because it legitimately returns "Hanoi Cooking Experience"
      for "cooking class" and a substring test would delete it — a second
      filter silently undoing the first.

    But the second does NOT get a blanket pass, which is what the first version
    gave it. Klook answers EVERY query with something, so a row sharing zero
    words with the query sailed through under "the server already filtered
    this" — while `relevance_filter`, written for exactly this trap, was called
    by nothing but its own test.
    """
    if not match:
        return list(activities)
    needle = match.lower()

    def substring_hit(a) -> bool:
        return needle in (a.title + " " + (a.description or "")
                          + " " + (a.category or "")).lower()

    loose_keys = {a.key for a in model.query_relevance_filter(
        [a for a in activities if a.source in server_filtered], match)}
    return [a for a in activities
            if (a.key in loose_keys if a.source in server_filtered
                else substring_hit(a))]


def _client(args) -> tuple[transport.Client, object]:
    conn = store.connect()
    gate = robots.RobotsGate(
        robots.default_fetcher,
        exempt_hosts=override_hosts(bool(getattr(args, "ignore_robots", False))))
    client = transport.Client(conn, allow_network=not getattr(args, "cache_only", False),
                             gap_s=getattr(args, "gap", None), robots_gate=gate)
    return client, conn


def _sorted(activities, order: str):
    if order == "score":
        return sorted(activities, key=model.sort_key_score(activities))
    if order == "rating":
        return sorted(activities, key=model.sort_key_rating)
    if order == "reviews":
        return sorted(activities, key=lambda a: -(a.review_count or 0))
    if order == "price":
        # Sort on the USD column, not the native amount: sorting HKD and USD
        # numbers together orders by currency, not by cost. Unknown price sorts
        # last rather than first — a missing price is not free — and so does an
        # unconvertible currency, because it cannot be placed honestly.
        def price_key(a):
            usd = model.to_usd(a.price_amount, a.price_currency)
            return (usd is None, usd or 0.0)
        return sorted(activities, key=price_key)
    return list(activities)


# -- commands -----------------------------------------------------------------

def cmd_search(args) -> int:
    """Keyword search within a city.

    Airbnb's experiences search has **no keyword filter**, so its half is always
    a city sweep filtered locally. That is slower but it is honest: the
    alternative is sending a keyword to an endpoint that ignores it and
    reporting the whole city as if it matched.

    Klook *does* index free text, so under ``--ignore-robots`` the keyword goes
    to the server and only that query is walked. The two halves therefore have
    genuinely different coverage, which is why `coverage.sources` reports them
    separately rather than as one number.
    """
    args.match = args.query
    args.categories = None
    args.klook_query_text = args.query
    return cmd_catalog(args)


def cmd_catalog(args, *, emit: bool = True) -> tuple[int, dict] | int:
    """Full city catalogue across every enabled source.

    ``emit=False`` returns ``(exit_code, payload)`` instead of printing, so a
    command that needs the rows (``compare``) reuses this path exactly rather
    than growing a second sweep that could drift from it.
    """
    place = resolve_place(args.city)
    if place is None:
        print(f"error: unknown city {args.city!r}. Known: {', '.join(sorted(PLACES))}\n"
              f"Add one to activityintel/places.py — a place needs a Klook keyword "
              f"and (for Airbnb) a Google place_id.", file=sys.stderr)
        return (exit_codes.USAGE, {}) if not emit else exit_codes.USAGE

    ignore_robots = bool(getattr(args, "ignore_robots", False))
    live = enabled_sources(ignore_robots)
    wanted = args.sources or list(live)
    client, conn = _client(args)
    combined, coverage = [], {"complete": True, "sources": {}}
    if ignore_robots:
        # Name the hosts. "an override was active" and "which sites it covered"
        # are different facts, and only the second is auditable.
        coverage["robots_override_hosts"] = sorted(override_hosts(True))

    # A source that cannot run is reported, never quietly omitted: "we did not
    # ask Klook" and "Klook has nothing" must not produce the same output.
    for name, mod in ALL_SOURCES.items():
        if name in wanted and not source_available(mod, ignore_robots=ignore_robots):
            coverage["sources"][name] = {
                "returned": 0, "complete": False, "skipped": True,
                "note": getattr(mod, "UNAVAILABLE_REASON", "source unavailable"),
            }
            coverage["complete"] = False

    try:
        if klook.NAME in wanted and source_available(klook, ignore_robots=ignore_robots):
            # `search` hands its keyword down so Klook does the filtering
            # server-side instead of us sweeping the whole city and grepping it.
            # The city is prepended when the keyword does not already carry it —
            # Klook indexes free text with no separate location parameter, so a
            # bare "cooking class" returns the planet.
            raw_q = getattr(args, "klook_query_text", None)
            if raw_q:
                queries = (raw_q if place.klook_query.lower() in raw_q.lower()
                           else f"{place.klook_query} {raw_q}",)
            else:
                queries = (place.klook_query,) + tuple(place.extra_queries)
            report = sweep.sweep(
                client, klook, queries,
                page_size=min(args.size, klook.MAX_PAGE_SIZE),
                max_pages=args.max_pages)

            # Klook answers EVERY query with something — a nonsense string
            # returns a confident page of Taipei listings, and the Hanoi union
            # carried Seoul and Ho Chi Minh rows. So the union is scoped, and
            # both the kept split and the drop count are reported: a silent
            # filter is the same failure as no filter, because the caller could
            # not tell a small market from an aggressive filter.
            # Klook sells more than experiences and marks the difference only
            # in `card_name`. Rooms first, geography second: it keeps
            # `dropped_out_of_scope` meaning "a real activity, wrong city"
            # instead of quietly mixing two unrelated reasons into one number.
            activities, off_vertical, unknown_verticals = klook.split_verticals(
                report.activities)

            kept, day_trips, dropped_rows = [], 0, []
            for a in activities:
                scope = place.scope_of(a)
                if scope is None:
                    dropped_rows.append(a)
                    continue
                if scope == places.DAY_TRIP:
                    day_trips += 1
                kept.append(a)
            dropped = len(dropped_rows)
            combined.extend(kept)

            health = klook.tag_health(kept)
            notes = [report.coverage_note()]
            if off_vertical:
                notes.append(
                    f"{sum(off_vertical.values())} row(s) dropped as not activities ("
                    + ", ".join(f"{v}: {n}" for v, n in off_vertical.most_common())
                    + ") — Klook answers a things-to-do query with more than "
                    "activities, and says which only in its card type.")
            if unknown_verticals:
                notes.append(
                    "verticals this build has never seen were KEPT rather than "
                    f"judged: {', '.join(unknown_verticals)}. Check whether they "
                    f"belong in an activity catalogue.")
            if dropped:
                notes.append(
                    f"{dropped} row(s) dropped as outside {place.name}'s scope "
                    f"(neither in-city nor a declared day trip) — Klook returns "
                    f"unrelated listings for any query and flags none of them.")
            if health["suspect_degraded"]:
                notes.append("Klook's tag service looks degraded (<=2 distinct tags "
                             "across the union); duration/language fields are "
                             "unreliable for this run.")
            coverage["sources"][klook.NAME] = {
                "returned": len(kept),
                "in_city": len(kept) - day_trips,
                "day_trip": day_trips,
                "dropped_out_of_scope": dropped,
                "dropped_not_activity": dict(off_vertical),
                "unknown_verticals": unknown_verticals,
                "server_filtered": bool(raw_q),
                "complete": report.is_complete,
                "note": " ".join(n for n in notes if n) or None,
                "queries": len(report.queries),
                "capped_queries": report.capped_queries,
                # Which keywords failed and the first fault verbatim. A count
                # alone made a 429, a schema change and a DNS blip identical.
                "failed_queries": report.failed_queries,
                "first_error": report.first_error,
                "tag_health": health,
            }
            coverage["complete"] &= report.is_complete

        if airbnb.NAME in wanted:
            if not place.airbnb_place_id:
                coverage["sources"][airbnb.NAME] = {
                    "returned": 0, "complete": False,
                    "note": f"no Airbnb place_id known for {place.name}; skipped."}
                coverage["complete"] = False
            else:
                res = airbnb.sweep_place(client, place.airbnb_place_id,
                                         place.airbnb_query,
                                         categories=args.categories,
                                         size=args.size,
                                         language=getattr(args, "language", None),
                                         max_pages=args.max_pages)
                combined.extend(res["activities"])
                note = None
                if res["incomplete_passes"]:
                    note = ("passes that did not reach the end: "
                            + ", ".join(res["incomplete_passes"][:6]))
                    # Name the fault, not just the passes. "(unfiltered),
                    # Cooking, …" was the whole message for a cold cache under
                    # --cache-only; the cause lived only in passes[].error,
                    # which the table and CSV renderers never show.
                    first_err = next((p["error"] for p in res["passes"]
                                      if p.get("error")), None)
                    if first_err:
                        note += f". First error: {first_err}"
                coverage["sources"][airbnb.NAME] = {
                    "returned": len(res["activities"]),
                    "complete": res["complete"], "note": note,
                    "passes": res["passes"],
                }
                coverage["complete"] &= res["complete"]
    except (transport.RateLimited,) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return (exit_codes.RATE_LIMIT, {}) if not emit else exit_codes.RATE_LIMIT
    except (robots.Disallowed, robots.RobotsUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return (exit_codes.CONFIG, {}) if not emit else exit_codes.CONFIG
    except transport.TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return (exit_codes.UPSTREAM, {}) if not emit else exit_codes.UPSTREAM
    finally:
        conn.close()

    # Every wanted source must own a coverage entry by now. One that does not
    # was enabled and never asked — the Viator shape — and "we did not ask"
    # must never render as "nothing there". Structural on purpose: it keys on
    # the absence of an entry, not on a list of names, so the next source
    # registered and forgotten above is caught by the same line.
    for name in wanted:
        if name not in coverage["sources"]:
            coverage["sources"][name] = {"returned": 0, "complete": False,
                                         "skipped": True, "note": NOT_WIRED_NOTE}
            coverage["complete"] = False

    # `returned` is read as "how much did this source give me". It was computed
    # before this filter ran, so `--match cooking` on Hanoi handed the caller 59
    # rows while coverage claimed 630 + 232 = 862. Re-derive it from what the
    # caller actually receives, and report what the filter took, because
    # "this source is thin" and "my keyword was narrow" have opposite remedies.
    coverage["match"] = args.match or None
    before = collections.Counter(a.source for a in combined)
    if args.match:
        server_filtered = {s for s, v in coverage["sources"].items()
                           if v.get("server_filtered")}
        combined = apply_match_filter(combined, args.match, server_filtered)
    after = collections.Counter(a.source for a in combined)
    for name, entry in coverage["sources"].items():
        if entry.get("skipped"):
            continue
        entry["returned"] = after.get(name, 0)
        entry["matched_out"] = before.get(name, 0) - after.get(name, 0)
        if entry["matched_out"]:
            entry["note"] = " ".join(filter(None, [
                entry.get("note"),
                f"{entry['matched_out']} row(s) removed by --match "
                f"{args.match!r}; this is a keyword filter, not the size of "
                f"what {name} holds."]))

    notes = [f"{s}: {v['note']}" for s, v in coverage["sources"].items() if v.get("note")]
    # Currencies mix the moment two sources are on, so say which rates the USD
    # column used and complain when the unpegged ones have gone unreviewed.
    fx_warning = model.fx_note()
    coverage["fx"] = {"as_of": config.FX_AS_OF, "stale_note": fx_warning}
    if fx_warning:
        notes.append(f"fx: {fx_warning}")
    coverage["note"] = " | ".join(notes) if notes else None

    mean = model.population_mean_rating(combined)
    rows = _sorted(combined, args.sort)[: args.limit or None]
    out = []
    for a in rows:
        d = a.to_dict()
        d["score"] = model.bayesian_score(a.rating, a.review_count, mean)
        out.append(d)
    coverage["population_mean_rating"] = round(mean, 4)
    payload = {"city": place.name, "activities": out, "coverage": coverage}
    rc = exit_codes.OK if coverage["complete"] else exit_codes.PARTIAL
    if not emit:
        return rc, payload
    _emit(payload, args)
    return rc


def cmd_compare(args) -> int:
    """Find the same experience on more than one platform and price the gap.

    This is the only question that needs two sources to answer, so it is the
    one command that refuses to run on one: with a single source enabled every
    group would be empty and "no cross-listings found" would be indistinguishable
    from "we only looked at one place".
    """
    args.match = None
    args.categories = None
    # The cap applies to GROUPS, so the catalogue underneath must be whole:
    # capping rows before matching would hide pairs, not rank them.
    limit = getattr(args, "limit", 0) or 0
    args.limit = 0
    args.sort = "none"

    live = enabled_sources(bool(getattr(args, "ignore_robots", False)))
    # Count sources that can be ASKED, not merely enabled. A keyed Viator
    # passed this gate as a second platform and was then compared against
    # nothing, so "no cross-listings found" read as a finding.
    askable = sorted(n for n in live if n in SWEEPABLE)
    unwired = sorted(set(live) - set(askable))
    if len(askable) < 2:
        print(f"error: compare needs at least two enabled sources with a sweep "
              f"wired, have {askable or 'none'}."
              + (f" Enabled but not wired in this build: {', '.join(unwired)}."
                 if unwired else "")
              + " Klook is off until --ignore-robots; Viator needs a key AND a "
              "sweep. Nothing to compare against.", file=sys.stderr)
        return exit_codes.CONFIG

    rc, captured = cmd_catalog(args, emit=False)

    rows = [model.Activity(**{k: v for k, v in a.items()
                              if k in _ACTIVITY_FIELDS})
            for a in captured.get("activities", [])]
    groups = model.find_cross_source_matches(rows, threshold=args.threshold)
    # `--limit` used to be accepted here and overwritten with 0, so
    # `compare hanoi --limit 1` returned all 45 groups (measured 2026-09-02).
    # `match_count` stays what was FOUND; `shown` is what survived the cap.
    shown = groups[:limit] if limit else groups

    payload = {"city": captured.get("city"), "matches": shown,
               "match_count": len(groups), "shown": len(shown),
               "scanned": len(rows),
               "coverage": captured.get("coverage")}
    _emit_compare(payload, args)
    return rc


_ACTIVITY_FIELDS = {f.name for f in dataclasses.fields(model.Activity)}


def cmd_sources(args) -> int:
    out = []
    for name, mod in ALL_SOURCES.items():
        by_default = source_available(mod, ignore_robots=False)
        with_override = source_available(mod, ignore_robots=True)
        out.append({
            "name": name,
            "host": mod.HOST,
            "auth": "none (public endpoint)",
            "max_page_size": getattr(mod, "MAX_PAGE_SIZE", None),
            "result_cap": getattr(mod, "RESULT_CAP", None),
            # Only Klook indexes free text, and only when the override is on.
            "keyword_search": bool(getattr(mod, "REQUIRES_ROBOTS_OVERRIDE", False)),
            "available": by_default,
            "available_with_ignore_robots": with_override,
            "requires_robots_override": bool(with_override and not by_default),
            "unavailable_reason": (None if by_default
                                   else getattr(mod, "UNAVAILABLE_REASON", None)),
            "notes": (mod.__doc__ or "").strip().splitlines()[0],
        })
    print(json.dumps({"sources": out, "places": sorted(PLACES)},
                     ensure_ascii=False, indent=2))
    return exit_codes.OK


def cmd_doctor(args) -> int:
    """Live contract check: are the pinned endpoints still the real ones?

    The checks themselves live in ``doctor.py``; this is the exit-code and
    output-shape half.
    """
    # `doctor` reports a check list, not rows, so `--csv` has nothing to render.
    # It used to parse, exit 0 and print JSON — the same "you asked for CSV and
    # got something else" failure that `compare --csv` shipped with. Refusing is
    # the honest answer. The parser no longer offers the flag; this guard stays
    # for a caller that builds the Namespace by hand.
    if getattr(args, "csv", False):
        print("error: doctor has no --csv output — it reports named checks, not "
              "rows. Use --json (the default shape) or drop the flag.",
              file=sys.stderr)
        return exit_codes.USAGE

    client, conn = _client(args)
    try:
        checks = doctor.run_checks(
            client, ignore_robots=bool(getattr(args, "ignore_robots", False)),
            source_available=source_available)
    finally:
        conn.close()

    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    return exit_codes.OK if ok else exit_codes.CONTRACT


def cmd_cache(args) -> int:
    conn = store.connect()
    now = store.now()
    if args.purge:
        n = store.purge_all(conn)
        print(json.dumps({"purged": n}))
    else:
        expired = store.purge_expired(conn, now=now)
        print(json.dumps({
            "expired_removed": expired,
            "cache": store.cache_stats(conn, now=now),
            "requests_last_24h": store.request_stats(conn, since=now - 86400),
            "db": str(config.db_path()),
        }, ensure_ascii=False, indent=2))
    conn.close()
    return exit_codes.OK


# -- argument validation ------------------------------------------------------
#
# argparse's `type=` is the one place a bad value can be refused BEFORE any
# command runs, with the flag's own name in the message. Each validator below
# replaces a silent misreading measured on 2026-09-02: `--limit -1` became
# `rows[:-1]` and dropped the last row; `--size 0` was falsy and became the
# default; `--max-pages 0` walked nothing and reported "hit the ceiling";
# `--threshold 2` found nothing and blamed the titles; `--language xx` failed
# every Airbnb pass with the cause buried in JSON.

def _int_at_least(floor: int):
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}")
        if value < floor:
            raise argparse.ArgumentTypeError(f"must be >= {floor}, got {value}")
        return value
    return parse


def _float_at_least(floor: float):
    def parse(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a number, got {raw!r}")
        if value < floor:
            raise argparse.ArgumentTypeError(f"must be >= {floor:g}, got {value:g}")
        return value
    return parse


def _unit_interval(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number in 0..1, got {raw!r}")
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {value:g}")
    return value


def _non_empty(raw: str) -> str:
    if not raw.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return raw


def _language(raw: str) -> str:
    # The vocabulary belongs to the source that filters on it; the parser only
    # consults it so the refusal carries the list instead of a shrug.
    if raw not in airbnb.LANGUAGE_CODES:
        raise argparse.ArgumentTypeError(
            f"unknown language {raw!r}. Known: {', '.join(sorted(airbnb.LANGUAGE_CODES))}")
    return raw


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        # Never hardcode prog. It was pinned to "python3 -m activityintel.cli"
        # until v1.4.1, so BOTH install routes printed a usage line naming the
        # one form bin/activity-intel exists to replace — and that form raises
        # ModuleNotFoundError from any directory but the checkout (measured
        # from `/`, 2026-08-28). The help of a working command named a broken
        # one. argparse derives this from sys.argv[0], which is right for the
        # console script and for a real `python3 -m` run; the sh launcher
        # hands off through `-m`, so it exports the name the caller typed.
        prog=os.environ.get("ACTIVITY_INTEL_PROG") or None,
        description="Search bookable activities/experiences across OTA platforms.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Four option blocks, and every subcommand takes ONLY the blocks it reads.
    # One shared block gave `doctor` a `--limit` and `--sort` it ignored and
    # `compare` a `--sort` it overwrote — an options bag that silently drops
    # whatever it does not destructure. argparse refusing is the honest answer,
    # and it costs a reader nothing they would not have paid at exit 0.

    def output_opts(sp, *, csv: bool = True):
        fmt = sp.add_mutually_exclusive_group()
        fmt.add_argument("--json", action="store_true", help="machine-readable output")
        if csv:
            fmt.add_argument("--csv", action="store_true",
                             help="flat CSV on stdout for a spreadsheet; coverage "
                                  "warnings go to stderr so the pipe stays clean")

    def list_opts(sp, *, sort: bool = True):
        sp.add_argument("--limit", type=_int_at_least(0), default=0,
                        help="max rows (0 = all)")
        if sort:
            sp.add_argument("--sort",
                            choices=("score", "rating", "reviews", "price", "none"),
                            default="score",
                            help="score = rating shrunk toward the population mean "
                                 "by review volume (default); rating = raw average")

    def sweep_opts(sp):
        sp.add_argument("--size", type=_int_at_least(1), default=50,
                        help="page size (clamped per source)")
        sp.add_argument("--max-pages", type=_int_at_least(1), default=20,
                        help="pages to walk per query (Klook) or per pass (Airbnb) "
                             "before the sweep reports PARTIAL")
        sp.add_argument("--language", type=_language, default=None,
                        help="only experiences offered in this language "
                             "(e.g. zh, en, ko, ja) — server-side filter (Airbnb)")

    def network_opts(sp):
        sp.add_argument("--gap", type=_float_at_least(0.0), default=None,
                        help="seconds between requests to one host")
        sp.add_argument("--cache-only", action="store_true",
                        help="fail instead of touching the network")
        sp.add_argument("--ignore-robots", action="store_true",
                        help="do not consult robots.txt, and enable sources that "
                             "are off because of it (klook). An explicit operator "
                             "choice for low-volume personal research; every "
                             "override is announced on stderr. Does NOT defeat "
                             "bot-protection blocks, which stay refused.")

    s = sub.add_parser("search", help="keyword search within a city")
    s.add_argument("query", type=_non_empty)
    s.add_argument("city")
    s.add_argument("--sources", nargs="*", choices=list(ALL_SOURCES),
                   help="default: sources enabled under current policy")
    output_opts(s); list_opts(s); sweep_opts(s); network_opts(s)
    s.set_defaults(func=cmd_search)

    c = sub.add_parser("catalog", help="full city catalogue across sources")
    c.add_argument("city")
    c.add_argument("--sources", nargs="*", choices=list(ALL_SOURCES),
                   help="default: sources enabled under current policy")
    c.add_argument("--categories", nargs="*", help="Airbnb category names to sweep")
    c.add_argument("--match", help="client-side substring filter on title/description")
    output_opts(c); list_opts(c); sweep_opts(c); network_opts(c)
    c.set_defaults(func=cmd_catalog)

    cp = sub.add_parser("compare",
                        help="find the same experience on two platforms and "
                             "price the gap")
    cp.add_argument("city")
    cp.add_argument("--sources", nargs="*", choices=list(ALL_SOURCES))
    cp.add_argument("--threshold", type=_unit_interval,
                    default=model.DEFAULT_MATCH_THRESHOLD,
                    help="title similarity floor (0-1). Lower finds more pairs "
                         "and more false ones")
    # Groups are ordered by price spread, so there is no --sort to offer; the
    # --limit caps groups, and `shown` says how many survived it.
    output_opts(cp); list_opts(cp, sort=False); sweep_opts(cp); network_opts(cp)
    cp.set_defaults(func=cmd_compare)

    v = sub.add_parser("sources", help="list sources, limits, and known places")
    v.set_defaults(func=cmd_sources)

    d = sub.add_parser("doctor", help="live check that pinned endpoints still work")
    # `doctor` reports named checks, not rows: no rows to limit, sort, page
    # or flatten, so it takes none of those flags. `--json` is its native shape.
    output_opts(d, csv=False); network_opts(d)
    d.set_defaults(func=cmd_doctor)

    ca = sub.add_parser("cache", help="cache stats, or purge")
    ca.add_argument("--purge", action="store_true")
    ca.set_defaults(func=cmd_cache)

    return p

def _force_utf8_stdout() -> None:
    """A Vietnamese title must not truncate the output file.

    When stdout is redirected rather than a console, Python falls back to the
    locale encoding (cp1252 on a default Windows install). Writing "Phở" then
    raises UnicodeEncodeError *mid-stream*, after the header and some rows have
    already flushed — a half-written CSV with no note saying why. Same class as
    the empty-CA-bundle interpreter this project already learned about: the code
    is fine and the runtime a user's shell picks is not.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") \
                    not in ("utf8",):
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable stream; nothing to do and nothing to say


def main(argv=None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except store.StoreUnavailable as exc:
        # Every command opens the store first. A read-only or missing state
        # directory used to escape as a bare traceback and exit 1 — from a
        # tool whose exit codes exist so a caller can branch on the cause.
        print(f"error: {exc}", file=sys.stderr)
        return exit_codes.CONFIG


if __name__ == "__main__":
    sys.exit(main())
