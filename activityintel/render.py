"""Rendering: table, CSV and JSON for catalogue rows and compare groups.

Split out of ``cli`` on 2026-09-02 when the entry point passed 1,100 lines.
Nothing here decides anything — the payload arrives with every three-state
field already settled — but it is where those states are most easily lost, so
the rules are restated at the point of loss:

* ``rating: None`` is an EMPTY cell, never ``0`` and never ``"None"``.
* ``price_usd: None`` is an empty cell: no honest rate, not free.
* Coverage warnings go to **stderr** in every format, so a pipe stays clean
  and a human still sees that the sweep was partial.
"""
from __future__ import annotations

import csv
import json
import sys

from . import model
from .sources import REGISTRY


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
    order = list(REGISTRY)
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
    found = payload.get("match_count", len(groups))
    shown = (f"showing {len(groups)} of {found}" if len(groups) < found
             else f"{found}")
    print(f"scanned {payload.get('scanned', 0)} listings in "
          f"{payload.get('city')} — {shown} likely cross-platform match(es)\n")
    if not groups:
        print("none found. Titles differ enough between platforms that no pair "
              "cleared the similarity bar; lower it with --threshold to see more, "
              "and expect false pairs when you do.")
    else:
        _print_groups(groups)
    # The one renderer that said nothing about a partial sweep. `compare
    # hanoi --ignore-robots` on a capped catalogue exited 7 with no note on
    # either stream (measured 2026-09-02); the CSV and table paths both warn.
    cov = payload.get("coverage") or {}
    note = cov.get("note")
    if note:
        print(f"\n[coverage] {note}", file=sys.stderr)
    if cov.get("complete") is False:
        print("[coverage] the catalogue under this comparison is a SAMPLE, "
              "not complete.", file=sys.stderr)


def _print_groups(groups: list) -> None:
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
