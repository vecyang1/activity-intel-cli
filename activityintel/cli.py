"""One entry point: `python3 -m activityintel.cli`.

Design rule for every command here: **never let a short answer look like a
complete one.** Any result that was capped, truncated, or partially failed
carries a `coverage` block and exits non-zero (PARTIAL), so a caller that only
checks the exit code still cannot mistake a sample for a catalogue.
"""
from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import json
import os
import sys

from . import config, exit_codes, model, robots, store, sweep, transport
from . import places
from .places import PLACES, resolve_place
from .sources import airbnb, klook, viator

ALL_SOURCES = {klook.NAME: klook, airbnb.NAME: airbnb, viator.NAME: viator}
SOURCES = {n: m for n, m in ALL_SOURCES.items() if getattr(m, "AVAILABLE", True)}


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


def _emit(payload: dict, args) -> None:
    if getattr(args, "csv", False):
        _render_csv(payload)
        return
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    _render_table(payload)


# The flat columns, in the order a reader wants them. Deliberately explicit
# rather than "whatever keys the first row happens to have": a dict-derived
# header silently changes shape when a source stops setting a field, and the
# column that vanishes is the one nobody was watching.
CSV_COLUMNS = (
    "source", "vertical", "source_id", "title", "category", "city",
    "score", "rating", "rating_state", "review_count",
    "price_usd", "price_amount", "price_currency", "price_display", "price_unit",
    "duration_text", "booked_count", "languages", "tags",
    "lat", "lng", "url", "image_url",
)


# Excel, Sheets and LibreOffice evaluate a cell that begins with any of these.
# Listing titles are written by third-party sellers, and `--csv` exists to be
# opened in a spreadsheet, so a title of `=HYPERLINK("http://x")` is a live
# formula on open (CWE-1236). `QUOTE_MINIMAL` does not help: it quotes on
# delimiters and has no concept of a formula.
_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value, counter: list) -> object:
    """Neuter a spreadsheet formula trigger, and count that we did.

    Only in CSV. `--json` stays byte-faithful — it is the lossless channel and
    the hazard lives entirely in the spreadsheet. This project does not rewrite
    source values silently anywhere else (`price_amount` is never converted in
    place), so the count is printed to stderr rather than the change being
    smuggled into a data file.
    """
    if isinstance(value, str) and value[:1] in _CSV_TRIGGERS:
        counter.append(value)
        return "'" + value
    return value


def _render_csv(payload: dict) -> None:
    """Flatten to CSV on stdout, warnings to stderr.

    Two things this must not do, both of which a naive `csv.DictWriter` over
    `to_dict()` does by default:

    * **Print a coverage warning into the data.** `--csv` exists to be piped
      into a spreadsheet; a note in the stream becomes a row. It goes to stderr,
      where a human still sees it and a pipe does not.
    * **Let a three-state field become two-state.** `rating: None` must render
      as an *empty cell*, never `0` and never the string `"None"` — a reader
      sorts that column, and a new listing scored 0 sorts below a one-star one.
      Same for `price_usd`, which is null precisely when we have no honest rate.
    """
    rows = payload.get("activities") or []
    neutered: list = []
    writer = csv.DictWriter(sys.stdout, fieldnames=CSV_COLUMNS,
                            extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for a in rows:
        out = {}
        for col in CSV_COLUMNS:
            v = a.get(col)
            if col == "price_unit":
                v = model.price_unit(a.get("tags"))
            if isinstance(v, (list, tuple)):
                v = ";".join(str(x) for x in v)
            # None -> "" is the whole point. csv would write "" for None anyway,
            # but going through str() first (which some refactor will add) would
            # write the literal "None", so the branch is explicit and tested.
            out[col] = "" if v is None else _csv_safe(v, neutered)
        writer.writerow(out)

    _warn_neutered(neutered)
    cov = payload.get("coverage") or {}
    note = cov.get("note")
    if note or cov.get("complete") is False:
        print(f"[coverage] {note or 'this sweep is not complete'}", file=sys.stderr)


def _warn_neutered(neutered: list) -> None:
    if not neutered:
        return
    print(f"[csv] {len(neutered)} cell(s) began with a spreadsheet formula "
          f"character and were prefixed with an apostrophe so they open as "
          f"text; --json is unmodified. First: {neutered[0][:60]!r}",
          file=sys.stderr)


def _render_table(payload: dict) -> None:
    rows = payload.get("activities") or []
    cov = payload.get("coverage") or {}

    if not rows:
        print("no activities returned")
    else:
        print(f"{'SCORE':>6}  {'RATING':>6}  {'REVIEWS':>7}  {'PRICE':>16}  "
              f"{'DUR':>8}  SOURCE   TITLE")
        print("-" * 100)
        for a in rows:
            if a["rating_state"] == model.RATED:
                rating = f"{a['rating']:.2f}"
                reviews = str(a["review_count"] if a["review_count"] is not None else "?")
            elif a["rating_state"] == model.UNRATED:
                rating, reviews = "new", "0"
            else:
                rating, reviews = "?", "?"
            # Show the unit AND one currency. A per-group $197 next to a
            # per-guest $23 is not expensive, it is a different unit; and a
            # Klook HK$236 next to an Airbnb $33 is not 7x pricier, it is a
            # different currency. Truncating either away is how a price table
            # starts lying. USD is the comparison column when we have an honest
            # rate; otherwise the native price is shown, never a guess.
            amt = a.get("price_amount")
            usd = a.get("price_usd")
            cur = a.get("price_currency") or ""
            # Three states, like rating. Only Airbnb states a pricing unit; a
            # bare "/pp" on a Klook row would assert per-person on the strength
            # of nothing, and a per-group total shown as a per-person rate
            # understates the cost by roughly the group size. Blank = unstated.
            unit = model.price_unit(a.get("tags"))
            suffix = {"group": "/grp", "guest": "/pp"}.get(unit, "")
            if usd is not None:
                price = f"${usd:g}{suffix}"
                if cur and cur != "USD":
                    price += f" ({cur})"
            elif amt is not None:
                price = f"{cur} {amt:g}{suffix} ?"   # '?' = not comparable
            else:
                price = "—"
            dur = a.get("duration_text") or "—"
            sc = a.get("score")
            score = f"{sc:.3f}" if isinstance(sc, float) else "—"
            print(f"{score:>6}  {rating:>6}  {reviews:>7}  {price[:16]:>16}  "
                  f"{dur[:8]:>8}  {a['source']:<8} {a['title'][:42]}")
        print("-" * 100)
        print(f"{len(rows)} activities")

    unstated = sum(1 for a in rows
                   if a.get("price_amount") is not None
                   and model.price_unit(a.get("tags")) is None)
    if unstated:
        print(f"[price] {unstated} of {len(rows)} rows carry no pricing unit "
              f"(no /pp or /grp) — that source does not state one. Do not read "
              f"a blank unit as per-person.", file=sys.stderr)

    note = cov.get("note")
    if note:
        # stderr so --json / piped consumers stay clean while a human still sees it
        print(f"\n[coverage] {note}", file=sys.stderr)
    if cov.get("complete") is False:
        print("[coverage] this result is a SAMPLE, not a complete catalogue.",
              file=sys.stderr)


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
                page_size=min(args.size or klook.MAX_PAGE_SIZE, klook.MAX_PAGE_SIZE),
                max_pages=args.max_pages, lang=args.lang)

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
                                         size=args.size or airbnb.PAGE_SIZE,
                                         language=getattr(args, "language", None))
                combined.extend(res["activities"])
                note = None
                if res["incomplete_passes"]:
                    note = ("passes that did not reach the end: "
                            + ", ".join(res["incomplete_passes"][:6]))
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
    args.limit = 0
    args.sort = "none"

    live = enabled_sources(bool(getattr(args, "ignore_robots", False)))
    if len(live) < 2:
        print(f"error: compare needs at least two enabled sources, have "
              f"{sorted(live) or 'none'}. Klook is off until --ignore-robots; "
              f"Viator needs a key. Nothing to compare against.", file=sys.stderr)
        return exit_codes.CONFIG

    rc, captured = cmd_catalog(args, emit=False)

    rows = [model.Activity(**{k: v for k, v in a.items()
                              if k in _ACTIVITY_FIELDS})
            for a in captured.get("activities", [])]
    groups = model.find_cross_source_matches(rows, threshold=args.threshold)

    payload = {"city": captured.get("city"), "matches": groups,
               "match_count": len(groups),
               "scanned": len(rows),
               "coverage": captured.get("coverage")}
    _emit_compare(payload, args)
    return rc


_ACTIVITY_FIELDS = {f.name for f in dataclasses.fields(model.Activity)}


def _emit_compare(payload: dict, args) -> None:
    if getattr(args, "csv", False):
        _render_compare_csv(payload)
        return
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    _render_compare(payload)


def _render_compare_csv(payload: dict) -> None:
    """One row per matched group, both platforms side by side.

    The per-source columns come from the SOURCE REGISTRY, not from whichever
    sources happen to appear in the first match. A header that changes shape
    with the data is unusable as a spreadsheet and hides the case that matters:
    a run where one platform contributed nothing at all.
    """
    order = list(ALL_SOURCES)
    # `n_sources` counts PLATFORMS, `n_listings` counts rows. They differ
    # whenever one platform lists the same experience twice, which is 9 of 44
    # live Hanoi groups — and a column named `n_sources` reading 6 for a
    # two-platform group is worse than no column.
    cols = ["group", "n_sources", "n_listings", "similarity", "spread_usd",
            "cheapest_source"]
    for src in order:
        cols += [f"{src}_n", f"{src}_title", f"{src}_price_usd", f"{src}_rating",
                 f"{src}_reviews", f"{src}_url"]
    neutered: list = []
    w = csv.DictWriter(sys.stdout, fieldnames=cols, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    for i, g in enumerate(payload.get("matches") or [], start=1):
        # `spread_usd` and `cheapest_source` are null whenever fewer than two
        # comparable prices exist. Writing a 0 spread there would assert the two
        # platforms charge the same, which is the opposite of "we could not
        # compare" — so they stay empty, exactly like an unrated rating.
        row = {"group": i,
               "n_sources": len(g.get("members_by_source") or {}),
               "n_listings": g.get("members_count"),
               "similarity": g.get("similarity"),
               "spread_usd": "" if g.get("spread_usd") is None else g["spread_usd"],
               "cheapest_source": g.get("cheapest_source") or ""}
        # A source can contribute several listings, and the group's own
        # `price_usd_by_source` already reduced them to the cheapest. Render the
        # member that price came from, not whichever one iterates last, or the
        # row's title and its price describe two different listings.
        chosen = g.get("price_usd_by_source") or {}
        counts = g.get("members_by_source") or {}
        picked: dict[str, dict] = {}
        for m in g.get("members") or []:
            src = m.get("source")
            if src not in order:
                continue
            want = chosen.get(src)
            cur = picked.get(src)
            if cur is None:
                picked[src] = m
            elif want is not None and m.get("price_usd") == want \
                    and cur.get("price_usd") != want:
                picked[src] = m
        for src, m in picked.items():
            row[f"{src}_n"] = counts.get(src, 1)
            row[f"{src}_title"] = m.get("title") or ""
            usd = m.get("price_usd")
            row[f"{src}_price_usd"] = "" if usd is None else usd
            r = m.get("rating")
            row[f"{src}_rating"] = "" if r is None else r
            n = m.get("review_count")
            row[f"{src}_reviews"] = "" if n is None else n
            row[f"{src}_url"] = m.get("url") or ""
        w.writerow({c: _csv_safe(row.get(c, ""), neutered) for c in cols})

    _warn_neutered(neutered)
    cov = payload.get("coverage") or {}
    if cov.get("note") or cov.get("complete") is False:
        print(f"[coverage] {cov.get('note') or 'this sweep is not complete'}",
              file=sys.stderr)


def _render_compare(payload: dict) -> None:
    groups = payload.get("matches") or []
    print(f"scanned {payload.get('scanned', 0)} listings in "
          f"{payload.get('city')} — {len(groups)} likely cross-platform match(es)\n")
    if not groups:
        print("none found. Titles differ enough between platforms that no pair "
              "cleared the similarity bar; lower it with --threshold to see more, "
              "and expect false pairs when you do.")
        return
    for g in groups:
        prices = g["price_usd_by_source"]
        spread = g["spread_usd"]
        head = ", ".join(g["shared_terms"][:5])
        if spread is None:
            verdict = "price gap unknown (a side has no comparable price)"
        elif spread == 0:
            verdict = "same price on both"
        else:
            verdict = f"${spread:g} cheaper on {g['cheapest_source']}"
        print(f"~{g['similarity']:.2f} [{head}] — {verdict}")
        for m in g["members"]:
            usd = m.get("price_usd")
            p = f"${usd:g}" if usd is not None else "—"
            if m.get("rating") is not None:
                q = f"{m['rating']:.2f}/{m.get('review_count', '?')}"
            else:
                q = "new"
            print(f"    {m['source']:<8} {p:>8}  {q:>10}  {m['title'][:58]}")
            if m.get("url"):
                print(f"             {m['url']}")
        print()


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

    This is the command that turns 'the tool returned nothing' into a specific
    diagnosis. It is separate from the unit suite on purpose: the suite runs
    against fixtures and must stay hermetic, so only something that deliberately
    touches the network can notice that an endpoint moved.
    """
    # `doctor` reports a check list, not rows, so `--csv` has nothing to render.
    # It used to parse, exit 0 and print JSON — the same "you asked for CSV and
    # got something else" failure that `compare --csv` shipped with. Refusing is
    # the honest answer; the flag reaches here only because `doctor` shares the
    # common option block.
    if getattr(args, "csv", False):
        print("error: doctor has no --csv output — it reports named checks, not "
              "rows. Use --json (the default shape) or drop the flag.",
              file=sys.stderr)
        return exit_codes.USAGE

    ignore_robots = bool(getattr(args, "ignore_robots", False))
    client, conn = _client(args)
    checks = []

    def record(name, fn):
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 — the point is to report it
            checks.append({"check": name, "ok": False,
                           "detail": f"{type(exc).__name__}: {exc}"})

    def klook_default_off_check():
        """Klook must be OFF unless the operator explicitly overrides robots.

        Deliberately built with its own default-policy gate rather than the
        caller's: running ``doctor --ignore-robots`` must not make this check
        pass by adopting the very setting it is here to verify. A check that
        cannot go red under the condition it guards is not evidence.
        """
        if source_available(klook, ignore_robots=False):
            raise RuntimeError(
                "klook is available under DEFAULT policy — it is robots-disallowed "
                "and must require an explicit --ignore-robots")
        if not source_available(klook, ignore_robots=True):
            raise RuntimeError(
                "klook is unavailable even WITH --ignore-robots — the override no "
                "longer reaches the source, so the documented flag is a no-op")

        strict = transport.Client(
            store.connect(),
            robots_gate=robots.RobotsGate(robots.default_fetcher, enabled=True))
        try:
            klook.fetch_search(strict, "Hanoi cooking class", page=1, size=5)
        except robots.Disallowed:
            return ("default policy: klook off and refused by the gate; "
                    "--ignore-robots enables it")
        except transport.TransportError as exc:
            return f"klook unreachable ({type(exc).__name__}); still off by default"
        finally:
            strict.conn.close()
        raise RuntimeError(
            "klook search was NOT refused under default policy — robots.txt may have "
            "changed, or the gate regressed. It was Disallow: */search/* on 2026-08-27.")

    def klook_endpoint_check():
        """Under override, is the pinned search endpoint still the real one?

        Reported separately from the policy check above so 'we choose not to
        read it' and 'it stopped working' never collapse into one status.
        """
        if not ignore_robots:
            return "skipped — run doctor --ignore-robots to exercise the endpoint"
        r = klook.fetch_search(client, "Hanoi cooking class", page=1, size=5)
        n = len(r["activities"])
        if n == 0:
            raise RuntimeError("0 activities for a query known to have ~29 — the "
                               "payload shape or the endpoint moved")
        rated = sum(1 for a in r["activities"] if a.rating_state == model.RATED)
        return (f"{n} activities, {rated} rated, total={r['total']}, "
                f"capped={r['capped']}")

    def airbnb_check():
        place = PLACES["hanoi"]
        r = airbnb.fetch_search(client, place.airbnb_query, size=5,
                                place_id=place.airbnb_place_id)
        n = len(r["activities"])
        if n == 0:
            raise RuntimeError("0 activities — persisted-query hash or key may have rotated")
        priced = sum(1 for a in r["activities"] if a.price_amount is not None)
        return f"{n} activities, {priced} priced, cursor={'yes' if r['next_cursor'] else 'no'}"

    def robots_check():
        gate = robots.RobotsGate(robots.default_fetcher)
        gate.check("https://www.airbnb.com/api/v3/ExperiencesSearch/x")
        try:
            gate.check("https://www.airbnb.com/s/Hanoi--Vietnam/experiences")
        except robots.Disallowed:
            return "gate live: /api/v3 allowed, /s/*/* correctly refused"
        raise RuntimeError(
            "/s/*/* was NOT refused — robots.txt changed or the gate regressed. "
            "That path was disallowed on 2026-08-26 and this tool must not fetch it.")

    def tls_check():
        """Does THIS interpreter trust anything? Measured, not assumed.

        Added because the answer differed between the interpreter every check
        ran under (/opt/homebrew/bin/python3, 193 CAs) and the one first on a
        login shell's PATH (/usr/local/bin/python3, 0 CAs). Under the second,
        every https call failed and the tool returned an honest, complete,
        entirely empty catalogue.
        """
        ctx = config.ssl_context()
        n = len(ctx.get_ca_certs())
        if n == 0:
            raise RuntimeError(config.tls_remedy())
        return f"{n} CA certs available to {sys.executable}"

    record("tls trust store", tls_check)
    record("robots gate", robots_check)
    record("klook off by default", klook_default_off_check)
    record("klook search contract", klook_endpoint_check)
    record("airbnb search contract", airbnb_check)
    record("viator key", lambda: (
        f"ready (key present in ${viator.KEY_ENV})" if viator.available()
        else f"needs-key: {viator.UNAVAILABLE_REASON.splitlines()[0]}"))
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

    def common(sp):
        fmt = sp.add_mutually_exclusive_group()
        fmt.add_argument("--json", action="store_true", help="machine-readable output")
        fmt.add_argument("--csv", action="store_true",
                         help="flat CSV on stdout for a spreadsheet; coverage "
                              "warnings go to stderr so the pipe stays clean")
        sp.add_argument("--limit", type=int, default=0, help="max rows (0 = all)")
        sp.add_argument("--sort",
                        choices=("score", "rating", "reviews", "price", "none"),
                        default="score",
                        help="score = rating shrunk toward the population mean by "
                             "review volume (default); rating = raw average")
        sp.add_argument("--size", type=int, default=50, help="page size (clamped per source)")
        sp.add_argument("--max-pages", type=int, default=20)
        sp.add_argument("--lang", default="en_US")
        sp.add_argument("--gap", type=float, default=None,
                        help="seconds between requests to one host")
        sp.add_argument("--language", default=None,
                        help="only experiences offered in this language "
                             "(e.g. zh, en, ko, ja) — server-side filter")
        sp.add_argument("--cache-only", action="store_true",
                        help="fail instead of touching the network")
        sp.add_argument("--ignore-robots", action="store_true",
                        help="do not consult robots.txt, and enable sources that "
                             "are off because of it (klook). An explicit operator "
                             "choice for low-volume personal research; every "
                             "override is announced on stderr. Does NOT defeat "
                             "bot-protection blocks, which stay refused.")

    s = sub.add_parser("search", help="keyword search within a city")
    s.add_argument("query")
    s.add_argument("city")
    s.add_argument("--sources", nargs="*", choices=list(ALL_SOURCES),
                   help="default: sources enabled under current policy")
    common(s)
    s.set_defaults(func=cmd_search)

    c = sub.add_parser("catalog", help="full city catalogue across sources")
    c.add_argument("city")
    c.add_argument("--sources", nargs="*", choices=list(ALL_SOURCES),
                   help="default: sources enabled under current policy")
    c.add_argument("--categories", nargs="*", help="Airbnb category names to sweep")
    c.add_argument("--match", help="client-side substring filter on title/description")
    common(c)
    c.set_defaults(func=cmd_catalog)

    cp = sub.add_parser("compare",
                        help="find the same experience on two platforms and "
                             "price the gap")
    cp.add_argument("city")
    cp.add_argument("--sources", nargs="*", choices=list(ALL_SOURCES))
    cp.add_argument("--threshold", type=float, default=model.DEFAULT_MATCH_THRESHOLD,
                    help="title similarity floor (0-1). Lower finds more pairs "
                         "and more false ones")
    common(cp)
    cp.set_defaults(func=cmd_compare)

    v = sub.add_parser("sources", help="list sources, limits, and known places")
    v.set_defaults(func=cmd_sources)

    d = sub.add_parser("doctor", help="live check that pinned endpoints still work")
    common(d)
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
