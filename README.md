# activity-intel-cli

Bookable activity/experience intelligence across OTA platforms, for trip
research. Cache-first, robots-gated, and built so a truncated result can never
look like a complete one.

## Install

Python 3.10+, standard library only. No credentials for the enabled sources.

```bash
git clone https://github.com/vecyang1/activity-intel-cli.git
cd activity-intel-cli
pip install .
```

Or run it straight from the checkout with no build step at all — the launcher
resolves its own path, so the symlink works from anywhere:

```bash
ln -sf "$PWD/bin/activity-intel" ~/.local/bin/activity-intel
```

If you get `activity-intel: command not found` after either route, the install
directory is not on your `PATH` — that step is outside what any check in this
repo can reach, so it is called out here rather than left to be discovered.
`~/.local/bin` is the usual answer; `pipx` users want `pipx ensurepath`.

If `activity-intel doctor` reports the TLS trust store as unusable, your
interpreter has an empty CA bundle and no https call can succeed from it —
`pip install 'activity-intel[tls]'` supplies one. `doctor` prints the fix.

## Use

```bash
activity-intel sources                              # what is on, what is off, and why
activity-intel doctor                               # live: are the pinned endpoints real?
activity-intel catalog hanoi                        # full city catalogue
activity-intel catalog hanoi --language zh          # Chinese-guided only (Airbnb)
activity-intel search "cooking class" hanoi
activity-intel compare hanoi --ignore-robots        # same class, both platforms, price gap
activity-intel cache                                # stats; --purge to clear

activity-intel catalog hanoi --ignore-robots --csv   # flat CSV for a spreadsheet
activity-intel compare hanoi --ignore-robots --csv   # both platforms side by side
```

`--csv` writes rows to stdout and coverage warnings to **stderr**, so a pipe
stays clean while a human still sees that the sweep was partial. Empty cells are
real absences: a blank `rating` is a listing nobody has rated, a blank
`price_usd` is a currency with no honest rate, a blank `cheapest_source` means
only one side had a price. Reading a blank as `0` sorts every new listing below
every one-star one, which is the bug the JSON three-state model exists to
prevent and the one a spreadsheet re-introduces for free.

A cell that would start with `=`, `+`, `-`, `@`, a tab or a CR is prefixed with
an apostrophe so a spreadsheet opens it as text — listing titles are written by
third-party sellers and `--csv` exists to be double-clicked. The count is
announced on stderr; **`--json` is untouched** and remains the lossless channel.

`coverage.sources[*].returned` counts what the caller actually received, after
`--match`; the rows that filter removed are `matched_out`. Those are different
facts with opposite remedies — "this platform is thin" versus "my keyword was
narrow" — and reporting one as the other made a 59-row answer claim 862.

Everything works from any directory. That is deliberate: on 2026-08-27 a
documented command was quoted without its `cd` prefix and produced
`ModuleNotFoundError` for the first person who ran it, while every check in this
repo passed — because every check ran from the repo root. `bin/activity-intel`
resolves its own path through symlinks, and `tests/test_docs_parity.py` runs it
from a temp directory to keep that true.

## What it returns

Hanoi, verified 2026-08-28:

| | listings | notes |
|---|---|---|
| Airbnb Experiences | 232 | 19-category union; server-side language filter |
| Klook | 630 | needs `--ignore-robots`; 391 in-city + 239 day trips |
| **combined** | **862** | 668 rated, 194 new, 861 with USD prices |

**That Klook number used to read 1,025, and 411 of those were hotel rooms.**
Klook answers a things-to-do query with whatever it sells — rooms at
`/hotels/detail/`, priced per night, rated 5.00, outranking real experiences —
and marks the difference only in its card type. See *Verticals* below.

```
 SCORE  RATING  REVIEWS             PRICE       DUR  SOURCE   TITLE
 4.972    5.00      429   $30.26/pp (HKD)  Up to 3   klook    Hanoi Coffee Workshop: Taste Salt, Coconut
 4.946    4.97      557            $32/pp   4.25 hr  airbnb   Hanoi Cooking Class with Local Market Tour
```

## Five design decisions worth knowing before you trust the output

**Ranking is a lower confidence bound, not the raw average.** Sorting by rating
put a 5.00-from-2-reviews above a 4.98-from-1,387 on the live Hanoi set. Those
are not comparable claims. `--sort score` (the default) shrinks toward the
population mean *and* subtracts an uncertainty penalty that decays with √n.
`--sort rating` still gives the raw order.

**A new listing is `new`, not `0`.** Airbnb reports `ratingAverage: 0` for
unreviewed experiences and that zero is genuine — but writing it through as a
number sorts every new listing below every one-star listing. Unrated rows carry
`rating: null`, `rating_state: "unrated"`, and rank in their own band after
everything scored. `"unknown"` (the source said nothing) stays distinct from
`"unrated"` (the source said none).

**Prices are compared in USD, and never converted in place.** Klook hard-pins
its search prices to HKD — `k_currency`, a `currency` param, an
`X-Klook-Currency` header, two currency cookies and a different locale were all
measured and all ignored. So `price_amount`/`price_currency` keep exactly what
the source said, and `price_usd` is a separate derived column that is `null`
whenever there is no honest rate. Sorting the raw numbers together would order
by currency rather than by cost.

**Coverage is reported, and a short answer exits non-zero.** Any capped,
truncated, or partially-failed sweep sets `coverage.complete = false`, prints a
note to stderr, and exits `7` (PARTIAL). The unfiltered Airbnb grid is a *ranked
feed that silently drops listings*: paginating it to exhaustion returned 173 of
Hanoi's 222, and the category sweep recovers the rest. Klook's `total` caps at
exactly 1000 for any broad query, so a broad Hanoi sweep is always a sample and
says so.

**Not everything a source sells is an activity.** Klook's search mixes
verticals and names them only in `card_name`: `ttd` (things to do) and `fnd`
(food & dining) are activities, `hotel` and `carrental` are not. A Hanoi sweep
drops 441 rooms and 1 rental form, and `coverage.dropped_not_activity` reports
both counts by vertical — a smaller number with no explanation is
indistinguishable from a small market. A vertical this build has never seen is
**kept, not judged**, and named in `coverage.unknown_verticals`: dropping it
would lose real listings, and waving it through silently is exactly how 441
hotel rooms got into an activity catalogue unnoticed.

**Cross-platform matches are reported, never merged.** `compare` finds the same
experience on two platforms and prices the gap — "Ninh Binh Day Tour from Ha
Noi" was $33.46 on Klook (4.80★, 2,862 reviews) and $161 on Airbnb (5.00★, 1
review). Merging them would collapse the two fields that make the finding
useful, and a false merge would silently delete a real listing, so the tool
groups and leaves the judgement to you. When one platform lists the same
experience several times — 9 of 44 Hanoi groups do — the row shows that
platform's **cheapest** priced listing and `members_by_source` (CSV: `<src>_n`)
says how many were collapsed.

## Sources and the robots override

**Airbnb Experiences is on by default** and uses only its robots-allowed
`/api/v3/` path.

**Klook is off by default** because its search endpoint matches
`Disallow: */search/*` in its own robots.txt. `--ignore-robots` turns it on:
robots.txt is a voluntary crawl directive, and overriding it for low-volume
personal research is a call the operator is entitled to make.

The override is **per-host, not a global switch** — it exempts `www.klook.com`
and nothing else, and `coverage.robots_override_hosts` names exactly what it
covered. Airbnb's rules keep being enforced while the flag is on, including its
disallowed `/s/*/*` path. That matters because an override wider than its need
retires the guard that would catch the next unrelated bug. Every exempted host
is announced on stderr, once each.

The flag does **not** defeat bot protection. Klook's activity detail pages
return 403 (Akamai) and stay unread — which is why Klook cannot tell you whether
a class is taught in Chinese. Use `--language zh` on Airbnb for that.

**Viator** is written and unit-tested but needs a free self-serve key you must
create yourself (`VIATOR_API_KEY`). An agent must not create the account.

Full table and reasoning in [docs/SOURCES.md](docs/SOURCES.md).

## Data, licence, and what you may do with the output

The **code** is AGPL-3.0-or-later. The **data it fetches is not ours to
license**, and the two are easy to conflate:

- Listings, prices and photographs belong to Klook, Airbnb and their hosts. This
  project ships no harvested catalogue and redistributes no dataset. Don't
  commit its output to a public repository.
- `--ignore-robots` is a decision *you* make about *your* traffic, per
  invocation, for one host. It is off by default and announced on stderr every
  time precisely so it never becomes a decision this project made for you. If
  you want Klook data at volume or for anything commercial, its partner API
  needs no override at all.
- The flag stops at bot protection. Klook's 403 pages stay unread and no code
  here tries them.
- `tests/fixtures/` holds four unedited captured responses (~800 KB) so the
  parsers are graded against what the servers really send. They are listing
  records and photo URLs — no host names, accounts or personal data — kept as
  test samples, not as a dataset for use.

Full terms in [NOTICE](NOTICE); the licence itself is in [LICENSE](LICENSE).

## Layout

```
bin/activity-intel  location-independent launcher — the shipped entry point
activityintel/
  transport.py   the ONLY code that opens a socket — policy, cache, pacing
  robots.py      robots.txt gate, enforced in front of every request AND cache
  rfc9309.py     robots path matching — ours, because the stdlib's differs by
                 Python version and the launcher does not choose the version
  model.py       normalized Activity; three-state rating; scoring; USD; matching
  places.py      per-city identifiers, aliases, and day-trip scope
  sweep.py       page walking + union + coverage honesty
  store.py       SQLite cache + cross-process pace + request log
  sources/       adapters: build URLs, parse bodies, never fetch
  cli.py         the single entry point
docs/SOURCES.md  what we may read and what we may not — read before adding a source
```

## Verification

```bash
# Both interpreters: the compliance verdict used to differ between them.
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  PYTHONDONTWRITEBYTECODE=1 $py -B -m unittest discover -s tests -t tests   # 227 tests
done
python3 tools/mutate.py                   # 47/47 guards confirmed able to go red
activity-intel doctor                     # live contract, default policy
activity-intel doctor --ignore-robots     # also exercises the Klook endpoint
```

The suite is hermetic — `tests/_sandbox.py` must be the first import in every
test module, and it asserts the real database is untouched afterwards. The
fixtures are unedited captured responses; a hand-written one hid a real bug once
(`review_count` lives on `card.track_info`, not `card.data`).

`tools/mutate.py` confirms all 47 guards turn the suite red — including reading
the cache before the robots gate, falling back to the version-dependent stdlib
path matcher, widening the per-host override to a global switch, letting a
stated zero become a `0.0` rating, and naming a cheapest platform when only one
side had a price.

**Run it under both interpreters.** Until 2026-08-28 the gate's verdict depended
on which `python3` was first on PATH: `urllib.robotparser` ignores a mid-path
`*` on 3.12 and honours it on 3.14, so the same rules permitted a disallowed
path under the interpreter a human's shell actually picks. Matching now lives in
`rfc9309.py` and is identical on both.
