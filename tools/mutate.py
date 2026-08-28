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
3. **Patterns are line-anchored and must match exactly once.** An unanchored
   pattern once hit a *docstring* mention of `AVAILABLE = False` instead of the
   assignment, producing a mutant that changed no behaviour and a false CAUGHT.
   A pattern that matches 0 or 2+ times is reported as NOT GRADED, never
   silently skipped.
"""
from __future__ import annotations

import pathlib
import re
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

]


def run_suite() -> bool:
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "tests"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
             "HOME": str(pathlib.Path.home())},
    )
    return bool(re.search(r"(?m)^OK$", proc.stdout + proc.stderr))


def main() -> int:
    if not run_suite():
        print("BASELINE IS RED — fix that before trusting any verdict below")
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
