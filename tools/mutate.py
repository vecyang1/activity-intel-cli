"""Mutation harness — proves each guard's test can actually go red.

    python3 tools/mutate.py

Lives in the repo, not a scratch directory, for two reasons. It is verification
infrastructure the next agent must be able to re-run, and a harness kept in a
temp folder is one restart away from being a claim nobody can reproduce.

Three implementation details are load-bearing; each was a real defect here:

1. **Bytecode caching is disabled** (`-B` + PYTHONDONTWRITEBYTECODE). A mutation
   loop rewrites a module several times a second, and CPython decides a `.pyc`
   is stale on whole-second mtime + size, so a cached mutant can be executed
   during the *restore* run and the verdicts become noise.
2. **The verdict is read as `^OK$`, not from a line offset.** `tail -3 | head -1`
   on unittest output lands on "Ran N tests", which is present whether the run
   passed or failed — i.e. it grades nothing.
3. **Restore survives a signal, not just an exception.** `finally:` unwinds on
   an exception and CPython's default SIGTERM handler does not unwind at all,
   so a harness killed by a timeout or a `kill` leaves the mutant it was
   holding in the source tree. Measured 2026-08-28: a 120s tool timeout killed
   a run mid-mutation and left `transport.py` moving the robots check *after*
   the cache lookup -- a live policy bypass, in the working tree, announced by
   nothing but one unrelated-looking red test in the next run. The handlers
   below turn those signals into an exception so the `finally` still fires.

4. **Patterns are line-anchored and must match exactly once.** An unanchored
   pattern once hit a *docstring* mention of `AVAILABLE = False` instead of the
   assignment, producing a mutant that changed no behaviour and a false CAUGHT.
   A pattern that matches 0 or 2+ times is reported as NOT GRADED, never
   silently skipped.
"""
from __future__ import annotations

import pathlib
import re
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (label, file, line-anchored pattern, replacement)
MUTANTS = [
    ("klook available ignores the override",
     "activityintel/sources/klook.py",
     r"(?m)^    return bool\(ignore_robots\)$",
     "    return True"),

    ("robots override stops announcing itself",
     "activityintel/robots.py",
     r"(?m)^                self\._warn\($",
     "                _ = ("),

    ("override widens from per-host back to a global switch",
     "activityintel/cli.py",
     r"(?m)^        exempt_hosts=override_hosts\(bool\(getattr\(args, \"ignore_robots\", False\)\)\)\)$",
     "        enabled=not bool(getattr(args, 'ignore_robots', False)))"),

    ("override_hosts exempts every source, not just those needing it",
     "activityintel/cli.py",
     r"(?m)^        if getattr\(m, \"REQUIRES_ROBOTS_OVERRIDE\", False\) and getattr\(m, \"HOST\", None\)$",
     "        if getattr(m, \"HOST\", None)"),

    ("to_usd falls back to the raw amount for an unknown currency",
     "activityintel/model.py",
     r"(?m)^    entry = config\.FX_TO_USD\.get\(currency\.upper\(\)\)$",
     "    entry = config.FX_TO_USD.get(currency.upper()) or (1.0, False)"),

    ("to_usd zero-fills instead of refusing",
     "activityintel/model.py",
     r"(?m)^        return None\n    per_usd, _pegged = entry$",
     "        return 0.0\n    per_usd, _pegged = entry"),

    ("day-trip scope only looks at the city field, not the title",
     "activityintel/places.py",
     r"(?m)^        if _contains_phrase\(hay, self\.day_trip_cities\):$",
     "        if _contains_phrase((getattr(activity, 'city', None) or '').lower(),\n"
     "                            self.day_trip_cities):"),

    ("place scope stops refusing anything",
     "activityintel/places.py",
     r"(?m)^            return IN_CITY$",
     "            return IN_CITY\n        return IN_CITY"),

    ("cheapest_source named from a single price",
     "activityintel/model.py",
     r"(?m)^                                if len\(known\) > 1 else None\),$",
     "                                if known else None),"),

    ("one shared word is enough to call it the same product",
     "activityintel/model.py",
     r"(?m)^MIN_SHARED_TOKENS = 2$",
     "MIN_SHARED_TOKENS = 1"),

    # -- 2026-08-28 adversarial-review fixes ----------------------------------
    ("scope matching reverts to raw substring (no word boundaries)",
     "activityintel/places.py",
     r"(?m)^        if pattern\.search\(haystack\):$",
     "        if phrase in haystack:"),

    ("server_filtered becomes a blanket pass again",
     "activityintel/cli.py",
     r"(?m)^            if \(a\.key in loose_keys if a\.source in server_filtered$",
     "            if (True if a.source in server_filtered"),

    ("group membership validated against the anchor only",
     "activityintel/model.py",
     r"(?m)^                   for m in members\):$",
     "                   for m in members[:1]):"),

    ("group reports its strongest pair instead of its weakest",
     "activityintel/model.py",
     r'(?m)^            "similarity": round\(min\(pair_sims\), 3\) if pair_sims else 0\.0,$',
     '            "similarity": round(max(pair_sims), 3) if pair_sims else 0.0,'),

    ("price unit defaults to per-guest when the source stated none",
     "activityintel/model.py",
     r"(?m)^            if rest\.startswith\(UNIT_GUEST\):\n                return UNIT_GUEST\n    return None$",
     "            if rest.startswith(UNIT_GUEST):\n                return UNIT_GUEST\n    return UNIT_GUEST"),

    ("price unit read from any tag containing 'group'",
     "activityintel/model.py",
     r"(?m)^        if isinstance\(tag, str\) and tag\.startswith\(PRICED_TAG_PREFIX\):$",
     "        if isinstance(tag, str) and 'group' in tag.lower():"),

    ("a 404 robots.txt is treated as unreadable and fails closed",
     "activityintel/robots.py",
     r"(?m)^            return rfc9309\.Rules\(\[\]\)          # empty ruleset == allow all$",
     "            return None"),

    ("robots.txt fetch stops retrying transient failures",
     "activityintel/robots.py",
     r"(?m)^ROBOTS_FETCH_ATTEMPTS = 3$",
     "ROBOTS_FETCH_ATTEMPTS = 1"),

    ("TLS falls back to no trust store instead of certifi",
     "activityintel/config.py",
     r"(?m)^        if not ctx\.get_ca_certs\(\):$",
     "        if False:"),

    ("a TLS cert failure is retried like a timeout",
     "activityintel/robots.py",
     r"(?m)^        except ssl\.SSLCertVerificationError as exc:$",
     "        except ssl.SSLZeroReturnError as exc:"),

    ("cache is read before the robots gate (an override becomes sticky)",
     "activityintel/transport.py",
     r"(?m)^        self\.robots\.check\(url\)\n\n        cached = store\.cache_get\(self\.conn, url, now=self\._clock\(\)\)$",
     "        cached = store.cache_get(self.conn, url, now=self._clock())\n"
     "        if cached is not None:\n            self.cache_hits += 1\n            return cached\n"
     "        self.robots.check(url)\n        cached = None"),

    ("path matching falls back to the version-dependent stdlib parser",
     "activityintel/rfc9309.py",
     r'(?m)^        out\.append\("\.\*" if ch == "\*" else re\.escape\(ch\)\)$',
     '        out.append(re.escape(ch))'),

    ("longest-match loses to first-match",
     "activityintel/rfc9309.py",
     r"(?m)^            if n > best_len or \(n == best_len and kind == ALLOW\):$",
     "            if best_len < 0:"),

    ("an empty rule value is recorded, so the pattern '' blocks everything",
     "activityintel/rfc9309.py",
     r'(?m)^            if not value:$',
     "            if False:"),

    ("price sort falls back to the native amount",
     "activityintel/cli.py",
     r"(?m)^            usd = model\.to_usd\(a\.price_amount, a\.price_currency\)$",
     "            usd = a.price_amount"),

    # --- licence and packaging (v1.3.0) ------------------------------------
    # These four mutate *documents*, not code, which is the point: the licence
    # claims are the ones nothing else in the suite would notice going wrong.

    ("the documented install points back into one machine's home directory",
     "README.md",
     r'(?m)^ln -sf "\$PWD/bin/activity-intel" ~/\.local/bin/activity-intel$',
     'ln -sf "/Users/someone/Documents/A-coding/26.08.26-activity-intel-cli/bin/activity-intel" ~/.local/bin/activity-intel'),

    ("a source loses its data terms in NOTICE",
     "NOTICE",
     r"(?m)^  klook\.com    Listing data is Klook's\. This project redistributes no Klook$",
     "  (removed)   Listing data is Klook's. This project redistributes no Klook"),

    ("the wheel ships the code licence without the data terms",
     "pyproject.toml",
     r'(?m)^license-files = \["LICENSE", "NOTICE"\]$',
     'license-files = ["LICENSE"]'),

    ("LICENSE loses the clause that makes it Affero rather than GPL",
     "LICENSE",
     r"(?m)^  13\. Remote Network Interaction; Use with the GNU General Public License\.$",
     "  13. Use with the GNU Affero General Public License."),


    # --- vertical classification (2026-08-28) --------------------------------
    ("hotel rooms stop being excluded from the activity catalogue",
     "activityintel/sources/klook.py",
     r"(?m)^        if v in NON_ACTIVITY_VERTICALS:$",
     "        if False:"),

    ("a vertical we have never seen is dropped silently instead of named",
     "activityintel/sources/klook.py",
     r"(?m)^        if v not in ACTIVITY_VERTICALS:$",
     "        if False:"),

    ("the classifier stops classifying and every card becomes unlabelled",
     "activityintel/sources/klook.py",
     r"(?m)^    return named or typed$",
     "    return None"),

    ("coverage stops reporting what the vertical filter removed",
     "activityintel/cli.py",
     r'(?m)^                "dropped_not_activity": dict\(off_vertical\),$',
     '                "dropped_not_activity": {},'),

    # Mutates a TEST file on purpose: the subject of this guard is the test
    # files themselves, so the only honest mutant is a class that a direct
    # `python3 tests/test_x.py` would never reach.
    ("a test class hides below the entry block where direct invocation cannot see it",
     "tests/test_sweep.py",
     r"(?m)^    unittest\.main\(\)$",
     "    unittest.main()\n\n\nclass MutantBelowEntryBlock(unittest.TestCase):\n"
     "    def test_never_reached(self):\n        self.fail('unreachable')"),

    # --- CSV flattening (2026-08-28) -----------------------------------------
    ("CSV writes a 0 rating for a listing that has never been rated",
     "activityintel/render.py",
     r'(?m)^            out\[col\] = "" if v is None else _csv_safe\(v, neutered\)$',
     '            out[col] = 0 if v is None else _csv_safe(v, neutered)'),

    ("CSV prints the coverage warning into the data instead of stderr",
     "activityintel/render.py",
     r'(?m)^        print\(f"\[coverage\] \{note or \'this sweep is not complete\'\}", file=sys\.stderr\)$',
     '        print(f"[coverage] {note or \'this sweep is not complete\'}")'),

    ("CSV columns are taken from whatever the first row happens to have",
     "activityintel/render.py",
     r"(?m)^                            extrasaction=\"ignore\", lineterminator=\"\\n\"\)$",
     '                            extrasaction="ignore", lineterminator="\\n",\n'
     '                            restval="0")'),

    # --- one source, several listings (2026-08-28) ---------------------------
    ("a source is represented by its LAST listing instead of its cheapest",
     "activityintel/model.py",
     r"(?m)^        prices = \{src: \(min\(by_source\[src\]\) if by_source\.get\(src\) else None\)$",
     "        prices = {src: (by_source[src][-1] if by_source.get(src) else None)"),

    ("an unpriced sibling erases a priced one",
     "activityintel/model.py",
     r"(?m)^            if usd is not None:$",
     "            if True:"),

    ("the collapsed member count stops being reported",
     "activityintel/model.py",
     r'(?m)^            "members_by_source": counts,$',
     '            "members_by_source": {},'),

    ("the CSV's n_sources column counts listings instead of platforms",
     "activityintel/render.py",
     r'(?m)^               "n_sources": len\(g\.get\("members_by_source"\) or \{\}\),$',
     '               "n_sources": g.get("members_count"),'),

    # --- review round 2 (2026-08-28) -----------------------------------------
    ("coverage.returned reverts to the count from before the --match filter",
     "activityintel/cli.py",
     r'(?m)^        entry\["returned"\] = after\.get\(name, 0\)$',
     '        entry["returned"] = before.get(name, 0)'),

    ("the rows --match removed stop being reported",
     "activityintel/cli.py",
     r'(?m)^        entry\["matched_out"\] = before\.get\(name, 0\) - after\.get\(name, 0\)$',
     '        entry["matched_out"] = 0'),

    ("a spreadsheet formula in a listing title survives into the CSV",
     "activityintel/render.py",
     r'(?m)^    if isinstance\(value, str\) and value\[:1\] in _CSV_TRIGGERS:$',
     '    if False:'),

    ("doctor accepts --csv again and silently prints JSON",
     "activityintel/cli.py",
     r'(?m)^        print\("error: doctor has no --csv output[^\n]*$',
     '        pass  # mutant: doctor stops refusing'),

    ("card_name is trusted even when vertical_type contradicts it",
     "activityintel/sources/klook.py",
     r'(?m)^    if named and typed and named != typed:$',
     '    if False:'),

    ("the numeric vertical map loses the code that identifies a hotel",
     "activityintel/sources/klook.py",
     r'(?m)^VERTICAL_BY_TYPE = \{100: "ttd", 102: "hotel", 103: "carrental", 104: "ttd",$',
     'VERTICAL_BY_TYPE = {100: "ttd", 103: "carrental", 104: "ttd",'),

    ("usage is hardcoded back to the module form that fails outside the repo",
     "activityintel/cli.py",
     r'(?m)^        prog=os\.environ\.get\("ACTIVITY_INTEL_PROG"\) or None,$',
     '        prog="python3 -m activityintel.cli",'),

    ("the launcher stops telling argparse the name the caller typed",
     "bin/activity-intel",
     r'(?m)^export ACTIVITY_INTEL_PROG$',
     ': # mutant: override dropped'),

    ("docs-parity grades a documented --help as a command that does not parse",
     "tests/test_docs_parity.py",
     r'(?m)^                if \(exc\.code or 0\) != 0:$',
     '                if True:'),

    # -- 2026-09-02 debug pass: incidents, flags, the unwired source --------
    ("a rate limit is recorded against one keyword instead of stopping the sweep",
     "activityintel/sweep.py",
     r"(?m)^        except transport\.INCIDENTS:$",
     "        except ():"),

    ("the Airbnb sweep keeps paging a host that answered 429",
     "activityintel/sources/airbnb.py",
     r"(?m)^        except transport\.INCIDENTS:$",
     "        except ():"),

    ("a source that was enabled and never asked reports complete again",
     "activityintel/cli.py",
     r"(?m)^        if name not in coverage\[\"sources\"\]:$",
     "        if False:"),

    ("compare counts an unwired source as a platform to compare against",
     "activityintel/cli.py",
     r"(?m)^    askable = sorted\(n for n in live if n in SWEEPABLE\)$",
     "    askable = sorted(live)"),

    ("doctor calls a keyed Viator ready",
     "activityintel/doctor.py",
     r"(?m)^    return \(f\"key present in \$\{viator\.KEY_ENV\}, but this build has no sweep \"$",
     "    return (f\"ready (key present in ${viator.KEY_ENV}) \""),

    ("compare --limit is overwritten with 0 again",
     "activityintel/cli.py",
     r"(?m)^    limit = getattr\(args, \"limit\", 0\) or 0$",
     "    limit = 0"),

    ("the compare table goes quiet about a partial sweep",
     "activityintel/render.py",
     r"(?m)^        print\(\"\[coverage\] the catalogue under this comparison is a SAMPLE, \"$",
     "        print(\"\""),

    ("a negative --limit parses again and drops the last row",
     "activityintel/cli.py",
     r"(?m)^        sp\.add_argument\(\"--limit\", type=_int_at_least\(0\), default=0,$",
     "        sp.add_argument(\"--limit\", type=int, default=0,"),

    ("--max-pages stops reaching the Airbnb sweep",
     "activityintel/cli.py",
     r"(?m)^                                         max_pages=args\.max_pages\)$",
     "                                         )"),

    ("the Airbnb note stops naming the fault behind an incomplete pass",
     "activityintel/cli.py",
     r"(?m)^                        note \+= f\"\. First error: \{first_err\}\"$",
     "                        pass"),

    ("the sweep note stops naming the first error",
     "activityintel/sweep.py",
     r"(?m)^                f\"whatever they would have contributed\. First error: \{self\.first_error\}\"$",
     "                f\"whatever they would have contributed.\""),

    ("a missing Airbnb cursor key reads as the last page again",
     "activityintel/sources/airbnb.py",
     r"(?m)^    if not isinstance\(pagination, dict\) or \"nextPageCursor\" not in pagination:$",
     "    if False:"),

    ("an unopenable store escapes as a traceback again",
     "activityintel/cli.py",
     r"(?m)^    except store\.StoreUnavailable as exc:$",
     "    except () as exc:"),

    ("a request that never got a status stops counting as an error",
     "activityintel/store.py",
     r"(?m)^        \"SUM\(CASE WHEN status IS NULL OR status >= 400 THEN 1 ELSE 0 END\) AS errors \"$",
     "        \"SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors \""),

    ("the sandbox stops scrubbing the one credential",
     "tests/_sandbox.py",
     r"(?m)^SCRUBBED = \(\"ACTIVITY_INTEL_REQUEST_GAP_S\", \"VIATOR_API_KEY\"\)$",
     "SCRUBBED = (\"ACTIVITY_INTEL_REQUEST_GAP_S\",)"),
]


def run_suite() -> bool:
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "tests"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
             "HOME": str(pathlib.Path.home())},
    )
    return bool(re.search(r"(?m)^OK$", proc.stdout + proc.stderr))


def _die_on_signal(signum, _frame):
    """Turn a kill into an exception so the `finally` restore actually runs."""
    raise KeyboardInterrupt(f"signal {signum}")


def _dirty_targets() -> list[str]:
    """Mutant target files git says are modified.

    A red baseline has two very different causes and one message used to cover
    both: your tests are broken, or a previous run died holding a mutant. Only
    the second one is silently dangerous, so name it with evidence.
    """
    rel = sorted({m[1] for m in MUTANTS})
    proc = subprocess.run(["git", "status", "--porcelain", "--"] + rel,
                          cwd=ROOT, capture_output=True, text=True)
    return [ln[3:] for ln in proc.stdout.splitlines() if ln.strip()]


def main() -> int:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _die_on_signal)

    if not run_suite():
        print("BASELINE IS RED — fix that before trusting any verdict below")
        dirty = _dirty_targets()
        if dirty:
            print("  a previous run may have died holding a mutant; these "
                  "mutant targets are modified:")
            for d in dirty:
                print(f"    {d}")
            print("  check `git diff` on them before assuming the tests broke.")
        return 1
    print("baseline: OK\n")

    escaped = []
    for label, rel, pattern, repl in MUTANTS:
        path = ROOT / rel
        original = path.read_text()
        mutated, n = re.subn(pattern, repl, original, count=1)
        if n != 1:
            print(f"  !! {label}: pattern matched {n} times, not 1 — NOT GRADED")
            escaped.append(f"{label} (pattern miss)")
            continue
        try:
            path.write_text(mutated)
            caught = not run_suite()
        finally:
            path.write_text(original)
        print(f"  {'CAUGHT ' if caught else 'ESCAPED'} {label}")
        if not caught:
            escaped.append(label)

    if not run_suite():
        print("\nRESTORE FAILED — the tree is not back to baseline")
        return 1
    print("\nbaseline restored: OK")
    print(f"{len(MUTANTS) - len(escaped)}/{len(MUTANTS)} mutants caught")
    if escaped:
        print("escaped:", *escaped, sep="\n  - ")
    return 1 if escaped else 0


if __name__ == "__main__":
    sys.exit(main())
