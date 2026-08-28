"""RFC 9309 robots.txt group parsing and path matching, implemented here.

**Why this is not `urllib.robotparser`.** Measured 2026-08-28 on the same
machine, same rules, same URL:

    Disallow: /s/*/*      vs  https://www.airbnb.com/s/Hanoi--Vietnam/experiences

    Python 3.14.7  ->  can_fetch = False   (correct)
    Python 3.12.8  ->  can_fetch = True    (wrong)

3.12's parser does not honour a `*` in the middle of a path. That single
difference silently permits the exact path this project's compliance gate was
built to refuse, and which an earlier version of this tool actually fetched.
Nothing errors; the gate simply answers "allowed".

The interpreter is not ours to choose — `bin/activity-intel` runs whichever
`python3` is first on the user's PATH, and on this machine that is 3.12. A
guarantee that depends on the runtime's minor version is not a guarantee, so
the matching is done here where it is deterministic.

Semantics implemented (RFC 9309 §2.2):

* Rules are matched against the URL's path plus query string.
* ``*`` matches any sequence of characters, ``/`` included.
* ``$`` at the end of a pattern anchors the match to the end of the path.
* The **longest** matching rule wins; on an equal-length tie, ``Allow`` wins.
* An empty ``Disallow:`` value grants access and is not a rule.
* Group selection is exact-token, case-insensitive on the user-agent.
"""
from __future__ import annotations

import re
import urllib.parse

ALLOW = "allow"
DISALLOW = "disallow"


class Rules:
    """The allow/disallow rules that apply to one user-agent."""

    __slots__ = ("rules",)

    def __init__(self, rules: list[tuple[str, str]] | None = None):
        # (kind, pattern), in file order.
        self.rules: list[tuple[str, str]] = list(rules or ())

    def __bool__(self) -> bool:
        return bool(self.rules)

    def allows(self, path: str) -> bool:
        """RFC 9309 §2.2.2: longest match wins, Allow wins a tie."""
        best_len = -1
        best_kind = ALLOW
        for kind, pattern in self.rules:
            if not _matches(pattern, path):
                continue
            # Length is measured on the pattern, per the spec's "most specific".
            n = len(pattern)
            if n > best_len or (n == best_len and kind == ALLOW):
                best_len, best_kind = n, kind
        return best_kind == ALLOW


def _matches(pattern: str, path: str) -> bool:
    return _compile(pattern).match(path) is not None


_CACHE: dict[str, re.Pattern] = {}


def _compile(pattern: str) -> re.Pattern:
    """Translate a robots path pattern to a regex.

    Only ``*`` and a trailing ``$`` are special; everything else is literal.
    Building this by hand rather than with fnmatch, because fnmatch's ``*``
    does not cross ``/`` in some implementations and its ``[]`` class would
    make bracket characters in a real URL path behave as syntax.
    """
    cached = _CACHE.get(pattern)
    if cached is not None:
        return cached

    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    out = []
    for ch in body:
        out.append(".*" if ch == "*" else re.escape(ch))
    regex = "".join(out) + ("$" if anchored else "")
    compiled = re.compile(regex)
    _CACHE[pattern] = compiled
    return compiled


def parse(lines, agent: str = "*") -> Rules:
    """Collect the rules for ``agent`` (falling back to the ``*`` group).

    Consecutive ``User-agent:`` lines share the rules that follow them; a group
    ends at the next ``User-agent:`` seen *after* at least one rule.
    """
    agent = agent.lower()
    groups: dict[str, list[tuple[str, str]]] = {}
    current: list[str] = []
    seen_rule = False

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if seen_rule:
                current = []
                seen_rule = False
            current.append(value.lower())
            groups.setdefault(value.lower(), [])
            continue

        if field in (ALLOW, DISALLOW) and current:
            seen_rule = True
            # RFC 9309 2.2.2: an empty value is not a rule. `Disallow:` with
            # nothing after it *grants* access, and an empty `Allow:` says
            # nothing — recording either would append the pattern "", which
            # matches every path and would block or permit the entire site.
            #
            # One guard, not two: an earlier version had a `field == DISALLOW
            # and value == ""` check above an identical `not value` check, so
            # the first was dead. A mutation run graded it ESCAPED, which is
            # the harness reporting redundancy rather than a missing test.
            if not value:
                continue
            for name in current:
                groups.setdefault(name, []).append((field, value))

    if agent in groups:
        return Rules(groups[agent])
    return Rules(groups.get("*", []))


def can_fetch(rules: Rules, url: str) -> bool:
    """True when ``url``'s path (plus query) is permitted by ``rules``."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    if not rules:
        return True          # no rules published == no restrictions
    return rules.allows(path)
