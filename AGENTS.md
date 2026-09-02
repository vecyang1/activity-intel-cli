# AGENTS.md — activity-intel-cli

Runtime and behaviour owner for this repo. Read `docs/SOURCES.md` before
touching anything that fetches.

## Hard rules

0. **Policy is checked BEFORE the cache, and matching is ours, not the
   stdlib's.** These two are the project's whole guarantee and both were broken
   until 2026-08-28, each answering "allowed" with no error:
   - `Client.get` consults `robots.check` *before* `cache_get`. With the check
     below the cache, one `--ignore-robots` run wrote disallowed bodies into
     the store and every later strict run served them back untested. An
     override that persists in a cache cannot be seen or revoked. Cost:
     `--cache-only` may still fetch robots.txt, once per host per process.
   - Path matching lives in `rfc9309.py`, **never `urllib.robotparser`**. That
     module ignores a mid-path `*` on Python 3.12 and honours it on 3.14, and
     `bin/activity-intel` runs whichever `python3` is first on PATH — 3.12 on
     this machine. A guarantee that changes with the runtime's minor version is
     not a guarantee. Run the suite under **both** interpreters.

1. **`transport.py` is the only code that opens a socket.** Source adapters
   build URLs and parse bodies. A request made anywhere else bypasses the
   robots gate, the cache, the pacing, and the request log — all four at once.
2. **`robots.py` gates every fetch and fails closed.** The one way past it is
   `--ignore-robots`, which the *operator* passes for their own low-volume
   research. Three properties, all load-bearing:
   - It is a **parameter of the gate**, never a second fetch path around it.
   - It is **per-host**, never a global switch. `cli.override_hosts()` exempts
     only sources declaring `REQUIRES_ROBOTS_OVERRIDE`. The first version
     flipped the whole gate off, so a flag passed to reach Klook also stopped
     checking Airbnb — retiring the guard on Airbnb's disallowed `/s/*/*`,
     the exact path an earlier version of this tool fetched by mistake. **An
     override wider than its need disables the check that catches the next
     unrelated bug.**
   - It **announces every exempted host** on stderr, once each, and
     `coverage.robots_override_hosts` names them.

   Do not add a way to enable it from config, an env var, or a default — a
   silent override is the failure this whole module exists to prevent.
3. **Klook is off by default and only `--ignore-robots` turns it on.** Its
   search endpoint is robots-disallowed; its activity pages return **403
   (Akamai)** and that block is **not** overridden by the flag and must never
   be worked around. `doctor` asserts both halves: off under default policy,
   reachable under the override. Klook's partner API remains the route that
   needs no override at all.
4. **No credentials in this repo.** The enabled sources need none. Viator reads
   `VIATOR_API_KEY` from the environment through its own resolver and never from
   a file here. Never add a key to a fixture.
4b. **Not everything a source sells is an activity, and only the source's own
   vertical says which.** Klook answers a things-to-do query with hotel rooms
   (441 of them in one Hanoi sweep), rated 5.00 and priced per night. Read
   `card_name`, never `data.category` — the category is a localized display
   label, the vertical is structural. `klook.split_verticals` has **three**
   outcomes and all three are load-bearing: known activity kept, known
   non-activity dropped **with its count reported**, unknown vertical **kept
   and named**. Do not turn the third into either of the other two: dropping it
   loses real listings, and waving it through silently is precisely how the
   hotels arrived unnoticed.

   Run it **after** the sweep, never inside `parse_search`. `sweep.sweep` stops
   paging on a page shorter than the page size; a parser that filters makes a
   full page look like the last page and truncates the union while still
   reporting complete. Verticals are also filtered **before** the geographic
   scope, so `dropped_out_of_scope` keeps meaning "a real activity, wrong city".
5. **Never let a short answer look like a complete one.** Capped, truncated, or
   partially-failed sweeps set `coverage.complete = false` and exit `7`.
5a. **An incident stops the sweep; a dead keyword does not.** `transport.INCIDENTS`
   (`RateLimited`, `robots.Disallowed`, `robots.RobotsUnavailable`) are facts
   about the host and re-raise out of both sweep loops to exit `5` / `3`. Until
   2026-09-02 the per-query `except Exception` in `sweep.sweep` and
   `airbnb.sweep_place` swallowed them: measured with a fake client, a 429 was
   followed by 26 more requests to the same host and exit `7`. Only an
   ordinary `UpstreamError` / parser failure is recorded against its own query
   and stepped over — and `coverage` then names `failed_queries` and
   `first_error` verbatim, because "N queries failed" made a 429, a schema
   change and a DNS blip identical.
5b. **A source in the registry is not a source a command can ask.** Viator is
   "available" the moment a key is set and no command has a sweep for it; that
   collapsed into `activities: [], complete: true, exit 0`. `cli.SWEEPABLE`
   names what can be asked, `compare` counts only those, `doctor` says "key
   present, not wired", and `cmd_catalog` writes a `not wired` coverage entry
   for any wanted source that reaches the end without one — keyed on the
   *absence of an entry*, so the next source registered and forgotten is
   caught by the same line.
6. **Absent is not zero.** `rating: null` + `rating_state` — never a `0.0`
   rating, never a `0.0` price for an unknown price. Same rule for derived
   fields: `price_usd` is `null` for a currency we have no honest rate for, and
   `cheapest_source` is `null` unless *two* comparable prices exist.
7. **`price_amount`/`price_currency` are never converted in place.** Klook is
   hard-pinned to HKD (every currency param, header and cookie is ignored), so
   comparison needs a *separate derived* `price_usd` column. Rates live in
   `config.FX_TO_USD` with an `as_of` date and a staleness warning.
6b. **`returned` means "what the caller got", after every filter.** It was
   computed before the client-side `--match` filter, so a 59-row answer
   reported 862. Report the removed rows separately as `matched_out`: "this
   source is thin" and "my keyword was narrow" have opposite remedies, and a
   reader cannot tell them apart from one number.
7a. **A source is a SET of listings in a match group, never one.** Keying a
   dict on `m.source` lets the last member win, and 9 of 44 live Hanoi groups
   had a same-source sibling. Reduce with `min` over the *priced* members —
   `compare` answers "where should I book this" — and report
   `members_by_source` so a row distilled from five listings cannot read as a
   1:1 comparison. An unpriced sibling must never erase a priced one.
7b. **A three-state field stays three-state on the way out.** `--csv` is where
   `rating: null` becomes a `0` a reader sorts on. Blank cells are the contract:
   blank rating = unrated, blank `price_usd` = no honest rate, blank
   `cheapest_source` = fewer than two comparable prices. Coverage notes go to
   **stderr**, never into the rows.
8. **Every documented invocation must run from an arbitrary directory —
   including the one the program prints about itself.** Docs lead with
   `bin/activity-intel`, not a `cd` plus `python3 -m`, and so does every usage
   and error line argparse emits. Never hardcode `prog`. See below.
8a. **A flag the parser accepts must be consumed, and a value it cannot use is
   refused at parse time.** `build_parser` has four option blocks — output,
   list, sweep, network — and each subcommand takes only the blocks it reads.
   One shared block gave `doctor` a `--limit` it ignored and `compare` a
   `--sort` it overwrote. Validators (`_int_at_least`, `_unit_interval`,
   `_language`, …) refuse with the flag's own name: `--limit -1` used to become
   `rows[:-1]`, `--size 0` became the default, `--language xx` failed every
   Airbnb pass with the cause visible only in JSON. `--lang` was removed
   outright: Klook's `k_lang` is measured to change nothing and it never
   reached Airbnb. Do not add a flag to the shared blocks without tracing it to
   the code that reads it in every command that gets the block.

## Adding a source

Follow the checklist at the bottom of `docs/SOURCES.md`. In short: check
robots.txt with `urllib.robotparser` first, confirm plain-curl reachability,
write the adapter with no socket, capture an **unedited** real response into
`tests/fixtures/`, and add a `doctor` check that can actually fail.

## The launcher, and why it is a hard rule

`bin/activity-intel` resolves its own path through symlinks and puts the repo on
`PYTHONPATH`, so it works from any directory. This exists because of a measured
failure on 2026-08-27: a documented command was quoted without its `cd` prefix
and the first person to run it got `ModuleNotFoundError`. Every check in this
repo passed, because every check ran from the repo root.

**And the interpreter it picks is not the one you tested under.** The launcher
runs `python3` from PATH. On this machine a login shell resolves that to
`/usr/local/bin/python3` (3.12, python.org build) which loads **0 CA
certificates** — every https call died with CERTIFICATE_VERIFY_FAILED while the
suite, run under Homebrew's 3.14 with 193, was green and a `/tmp` run returned
1,252 listings. `config.ssl_context()` now falls back to certifi, `doctor` has a
`tls trust store` check, and `config.tls_remedy()` gives the error a fix instead
of a diagnosis.

A command travels as a string and leaves its working directory behind. So:
docs lead with `activity-intel …`, and `tests/test_docs_parity.py` runs the
launcher from a temp directory, through a symlink, and asserts a detached copy
refuses with a remedy. Its command extractor grades **both** invocation forms —
matching only one would let the graded share of the docs shrink silently.

**The usage line is a documented invocation too, and it was the last one
nobody graded.** `docs/` was swept, the launcher was tested through a symlink
from a temp directory — and `argparse.ArgumentParser(prog=...)` was pinned to
the literal `"python3 -m activityintel.cli"`, so every route printed that form
anyway: the sh launcher, which exists precisely because that form loses its
`cd`, and the pip console script, which is not a module invocation at all.
Measured from `/` on 2026-08-28: the program printed its own usage and exited
0, and the command it named raised `ModuleNotFoundError`. The docs-parity
extractor could not see it, because it reads Markdown and this string lives in
Python.

The fix is to state nothing: `prog` comes from `sys.argv[0]`, which is already
right for the console script and for a real `python3 -m` run, and
`bin/activity-intel` exports `ACTIVITY_INTEL_PROG` because it hands off through
`-m` and would otherwise lose the name the caller typed. The guard asserts the
*property* — the token in the usage line runs from `/` — not a spelling, since
a different wrong constant would satisfy a string comparison just as well.
`tools/mutate.py` grades both halves.

Install: `ln -sf "$PWD/bin/activity-intel" ~/.local/bin/activity-intel`

## Verification

```bash
# Run under BOTH interpreters — the compliance verdict used to differ between
# them, and only one of them is the one a human's shell actually picks.
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  PYTHONDONTWRITEBYTECODE=1 $py -B -m unittest discover -s tests -t tests   # 273
done
python3 tools/mutate.py                    # 65 guards confirmed able to go red
zsh -lc 'cd / && activity-intel doctor'                  # from where a user stands
zsh -lc 'cd / && activity-intel doctor --ignore-robots'  # exercises Klook too
```

Nothing may be defined **below** `if __name__ == "__main__":` in a test file.
`unittest.main()` calls `sys.exit()`, so `discover` sees those classes and
`python3 tests/test_x.py` does not — measured here as 38 vs 33 tests, OK both
times. Appending is what puts them there, and appending is what a tool does by
default. `TestFilesRunWhicheverWayTheyAreInvoked` grades every test file.

`tests/_sandbox.py` must be the **first import** in every test module — it
points `ACTIVITY_INTEL_HOME` at a disposable directory and asserts afterwards
that the real database was untouched. A location variable is *set*, never
unset: unset means "use the default", and the default is the user's real store.
Credential and behaviour variables are the opposite: `_sandbox.SCRUBBED` pops
them, and a test asserts `VIATOR_API_KEY` is on that list — it was read by the
code and not scrubbed until 2026-09-02, so a developer with the key exported
ran a different suite from one without it.

Mutants restore from the harness's own snapshot of each target file. **Do not
edit a target — source, `README.md`, `tests/_sandbox.py` — while
`tools/mutate.py` is running**; the restore would overwrite the edit. It aborts
on a red baseline, and names the targets `git` reports as modified.

Do not trust a green suite that has never been seen red. When you change a
guard, add it to `tools/mutate.py` and confirm the suite fails.

The harness earns its keep on the *wiring*, not the parts. It caught a case
where `override_hosts()` and `RobotsGate` were both tested and both correct
while `_client` could silently revert to a global kill switch with every test
still green — testing two components does not test the wire between them, and
the wire is where that defect lived.

## Licence

AGPL-3.0-or-later, from the first public release. Two rules follow from it:

9. **The licence covers this code and grants nothing about the data.** `NOTICE`
   states the per-source terms. When you add a source, add its terms there in
   the same change — a source adapter merged without them silently implies the
   AGPL covers whatever it fetches, which it cannot.
10. **Do not commit this tool's output.** `.gitignore` refuses the obvious
    shapes; the judgement is still yours. Fixtures are the one exception and
    they are documented as samples in `NOTICE`.

Operator-private routing (capability card, router skill, machine specifics)
lives in `progress.md`, which is deliberately not published. Keep it out of
this file — this one describes the repository, not the estate around it.
