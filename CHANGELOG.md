# Changelog

## v1.3.0 — 2026-08-28

**AGPL-3.0-or-later, and the first release meant to be read by someone who is
not standing in the author's home directory.**

- **Licence.** `LICENSE` is the canonical GNU AGPL-3.0 text, byte-identical to
  the copy the sibling projects ship and already recognised as `agpl-3.0` by
  GitHub's own classifier. `NOTICE` carries what the licence deliberately does
  not grant: per-source data terms for Klook, Airbnb and Viator, the basis for
  the `--ignore-robots` decision being the *operator's* and not this project's,
  and what `tests/fixtures/` actually contains.
- **The install command was wrong for everyone but one machine.** The only
  documented route was `ln -sf /Users/<owner>/Documents/A-coding/…`, and no
  check in this repo could see it, because every check ran on the machine where
  it worked. README now leads with `git clone` + `pip install .`, keeps the
  launcher symlink as the zero-build route, and states the `PATH` shim
  explicitly — that last step is outside what any gate here can reach, so it is
  written down rather than left to be discovered as `command not found`.
- **Packaging.** `pyproject.toml` (`license = "AGPL-3.0-or-later"`,
  `license-files = ["LICENSE", "NOTICE"]`, console script `activity-intel`), a
  `tls` extra that installs certifi for interpreters with an empty CA store,
  and `__version__` single-sourced in `activityintel/__init__.py` so the wheel
  and the CLI cannot disagree. Added `activityintel/__main__.py` so
  `python3 -m activityintel` works alongside the longer form the docs use.
- **`tests/test_license_and_packaging.py` (11 tests).** Pins the licence digest,
  asserts the Affero-specific network clause so a GPL-3.0 copy cannot pass as
  this one, checks `pyproject`/`NOTICE`/package agree, requires the CHANGELOG's
  newest version to equal `__version__` (it caught this entry being missing),
  and — ranging over every shipped file, not the one README where it happened —
  fails on any absolute `/Users/…` or `/home/…` path.
- **A source can no longer be merged without its data terms.** The new test
  walks `activityintel/sources/`, reads each adapter's `HOST`, and fails if that
  domain is absent from `NOTICE`. AGENTS.md rule 9 as a gate rather than a
  request.

Two of those guards were wrong first, and the harness is what said so. The
home-path check originally globbed six hand-written patterns and reached
neither `tests/` nor `tools/` — where all three absolute paths in the repo
actually live. It reported 22 files and passed; no count could have revealed
that, because a selector which never looks somewhere returns a confident
number, not a smaller one. Walking the tree took it to 40. Then the
placeholder allowlist added to accommodate the harness's own fake paths turned
the README mutant from CAUGHT to ESCAPED, so the exemption is now scoped to
`tests/` and `tools/`: `/Users/<name>/…` in a README is not a milder bug, it
is a command that works for nobody.

Nothing about fetching, ranking, or compliance behaviour changed in this
release. 175 tests, both interpreters; 29/29 mutants caught.

## v1.2.0 — 2026-08-28

An adversarial review plus a run from the owner's own terminal found six
defects, **none of which raised anything**. Four of them answered "allowed",
"kept" or "cheaper" — confidently, and wrongly.

**Compliance — the project's whole guarantee, broken two ways**
- **Path matching moved out of `urllib.robotparser` into `rfc9309.py`.** That
  module ignores a mid-path `*` on Python 3.12 and honours it on 3.14. Measured:
  `Disallow: /s/*/*` against Airbnb's search path returns **False on 3.14 and
  True on 3.12** — and `bin/activity-intel` runs whichever `python3` is first on
  PATH, which on this machine is 3.12. The gate permitted the exact path an
  earlier version of this tool fetched by mistake, while every check ran under
  the other interpreter and passed. The suite now runs under both.
- **The robots gate moved ahead of the cache.** With the check below the cache
  read, one `--ignore-robots` run wrote disallowed bodies into the store and
  every later strict run served them back without consulting robots.txt.
  `doctor` said "klook search was NOT refused", which was true and had nothing
  to do with robots.txt changing. An override that persists in a cache cannot
  be seen or revoked. Cost: `--cache-only` may still fetch robots.txt, once per
  host per process — a policy read, not a data read.

**The tool did not run in the owner's terminal at all**
- `/opt/homebrew/bin/python3` loads **193** CA certificates;
  `/usr/local/bin/python3` — first on a login shell's PATH — loads **0**. Every
  https call died with CERTIFICATE_VERIFY_FAILED, and the tool correctly
  reported an honest, complete, entirely empty catalogue. `config.ssl_context()`
  now falls back to certifi, `doctor` gained a `tls trust store` check, and
  `config.tls_remedy()` gives the error a fix instead of a diagnosis.
- `robots.default_fetcher` retries transient failures (one dropped packet used
  to forfeit a 100-second sweep) and treats 4xx as RFC 9309 "no rules
  published" rather than "unreadable". A cert failure is not retried.
- `RobotsUnavailable` now names the cause it used to discard.

**Correctness (from the review)**
- `Place.scope_of` matches whole words. Raw `in` matched `tan lac` inside
  "rat**tan lac**quer", `quan ba` inside "**Quan Ba**r", `ba vi` inside
  "**Ba Vi**en", `sapa` inside "**Sapa**way" — four out-of-scope listings from
  four different cities, all counted as Hanoi day trips.
- `--match` no longer gives a server-filtered source a blanket pass. Klook
  answers every query with something, so a row sharing zero words with the
  query sailed through — while `relevance_filter`, written for exactly that
  trap, was called by nothing but its own test. Now
  `model.query_relevance_filter`, applied via the extracted
  `cli.apply_match_filter`.
- Cross-source groups are validated **pairwise**, not against the anchor only.
  A 3-member group reported the anchor pair's 0.80 while two members scored
  0.4286 against each other — below the threshold every other pair is held to —
  and their prices still fed the spread. Reported similarity is now the group's
  weakest pair and `shared_terms` is intersected across all members.
- Pricing unit is three-state. Only Airbnb writes a `priced / guest|group`
  qualifier; reading a bare "group" substring collides that with any source's
  marketing badge, and 1,025 of 1,256 rows were labelled `/pp` on the strength
  of nothing. Unstated now renders blank, with a stderr note.

**Testing**
- 164 tests (was 110), green under **both** interpreters. `tools/mutate.py`
  grades **25/25** red. Two mutants escaped first and both were the harness
  working: one exposed a redundant dead guard in `rfc9309.parse`, the other
  showed that testing the two components either side of a wire does not test
  the wire.
- The docs extractor narrowed twice and the printed denominator caught both —
  once by count (11 -> 9), once by a subject silently truncated at a quote
  while the count held. It now has a direct truncation guard.

## v1.1.0 — 2026-08-27

Klook enabled behind an explicit operator override; cross-platform price
comparison; currency normalization; a launcher that runs from anywhere.

**Policy**
- `--ignore-robots` is now a documented capability rather than a suppressed
  flag. robots.txt is a voluntary crawl directive and the operator may decide
  it does not bind their own low-volume research. It is a parameter of the
  gate, never a second fetch path, and announces itself on stderr once per
  host — a silent override is the failure the gate exists to prevent.
- **The override is per-host, not a global switch.** `cli.override_hosts()`
  exempts only sources declaring `REQUIRES_ROBOTS_OVERRIDE`. The first cut
  flipped the whole gate off, so a flag passed to reach Klook also stopped
  checking Airbnb — retiring the guard on Airbnb's disallowed `/s/*/*`, the
  exact path an earlier version of this tool fetched by mistake. Caught by
  reading a live run's stderr, which announced an override for a host that
  never needed one. `coverage.robots_override_hosts` now names the set.
- **The Akamai 403 on Klook's activity pages is explicitly out of scope of that
  override** and no code here may attempt it. Consequence: Klook cannot answer
  "is this taught in Chinese?", because guided languages live on the detail
  page. Airbnb's `--language` is the route for that.
- Klook `AVAILABLE` stays `False` (default policy) and `available(ignore_robots=)`
  is the only way in — a mutable constant would be exactly the second entry
  point the chokepoint rule forbids. `doctor` builds its own default-policy gate
  so `doctor --ignore-robots` cannot satisfy the check by adopting the setting
  it verifies.

**Reach**
- Hanoi: **1,245 listings** (Klook 1,023 + Airbnb 222), up from 219.
- Klook keyword search is server-side; `search` hands the query down instead of
  sweeping the city and grepping it. The local `--match` filter now skips rows
  a source already filtered, which was silently discarding correct matches.

**Correctness**
- `Place.scope_of` replaces filtering on the source's own `city_name`, which
  discarded **350 of 1,068** Klook rows including "Hoa Lu … Day Tour *from
  Hanoi*" and "Hanoi to Lao Cai Sleeper Train" — a day trip's `city_name` is
  its destination, not the traveller's city. Now 38 dropped, split reported as
  `in_city` / `day_trip` / `dropped_out_of_scope`.
- `model.to_usd` + `config.FX_TO_USD`: Klook is hard-pinned to HKD (`k_currency`,
  `currency`, `X-Klook-Currency` and two cookies all measured, all ignored), so
  a mixed table sorted by currency rather than cost. Native price is never
  overwritten; `price_usd` is `null` when no honest rate exists; the rate table
  carries an `as_of` and warns when unpegged pins go stale.
- `cheapest_source` and `spread_usd` both require *two* comparable prices. A
  test caught the first version naming the only priced side as cheapest.

**Features**
- `compare <city>` — same experience on two platforms, priced. Measured live:
  "Ninh Binh Day Tour from Ha Noi" $33.46 on Klook (4.80★/2,862) vs $161 on
  Airbnb (5.00★/1). Matches are reported, never merged.
- `bin/activity-intel` launcher: resolves through symlinks, works from any
  directory. Added after a documented command was quoted without its `cd` and
  produced `ModuleNotFoundError` for its first user, while every check in the
  repo passed because every check ran from the repo root.

**Testing**
- 110 tests (was 57). `tools/mutate.py` moved into the repo and grades 11/11
  guards red, plus a launcher-breakage check. One mutant escaped first: both
  `override_hosts()` and `RobotsGate` were tested and correct while `_client`
  could revert to a global kill switch with every test green — testing two
  components does not test the wire between them.
- The docs-parity extractor now grades both invocation forms; matching only one
  would have let the graded share of the docs shrink silently.

## v1.0.0 — 2026-08-27

First release. Airbnb Experiences enabled; Klook disabled on compliance
grounds; Viator written pending an owner key.

**Compliance**
- `robots.py` gate in front of every socket, failing closed. Added after
  discovering the predecessor skill fetched two robots-disallowed paths
  (Klook `*/search/*`, Airbnb `/s/*/*`) silently on every call.
- Klook disabled: search endpoint robots-disallowed, activity pages HTTP 403,
  permitted paths carry no structured data. `doctor` asserts it stays disabled.
- `docs/SOURCES.md` records the access-vs-licence distinction and the
  personal-low-volume scope boundary.

**Correctness**
- Three-state rating (`rated` / `unrated` / `unknown`). Airbnb's genuine `0`
  for new listings no longer becomes a `0.0` that sorts below one-star.
- Default ranking is a lower confidence bound: posterior mean shrunk toward the
  population mean, minus an uncertainty penalty decaying with √n. Plain
  shrinkage was tried first and a test caught it failing when the population
  mean sits above the proven listing's rating.
- `review_count` read from `card.track_info`, not `card.data` — caught by
  parsing a real captured payload rather than a hand-written fixture.
- Currency read from the price string; Klook accepts `k_currency` and ignores it.
- Per-guest vs per-group price qualifier preserved and shown in output.
- Coverage honesty: capped/truncated/failed sweeps exit `7` with a note.
- Airbnb category union — the unfiltered grid is a ranked feed that drops
  listings (173 of Hanoi's 219 via pagination alone).

**Features**
- Server-side language filter (`--language zh`) — 13 Chinese-guided Hanoi
  experiences found; the field the original request actually needed.
- `doctor` live contract check; `cache` stats/purge; `--cache-only`.

**Testing**
- 57 tests, hermetic via `tests/_sandbox.py`. Six mutations confirmed to turn
  the suite red. `test_docs_parity.py` asserts every documented command parses
  and reports how many it graded.
