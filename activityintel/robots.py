"""robots.txt enforcement, wired into the transport chokepoint.

This is a *gate*, not a document. The rule "we respect robots.txt" written in a
README is a rule nobody runs; the same rule in front of `urlopen` cannot be
forgotten by the next adapter, and fails loudly when a URL is not permitted.

It earns its place because a real violation shipped here once: measured
2026-08-26, Airbnb's robots.txt disallows `/s/*/*` under the `*` group, and the
first version of this capability fetched `/s/Hanoi--Vietnam/experiences` on
every call. Nothing errored — a disallowed fetch looks exactly like an allowed
one from the client side, which is precisely why it needs a machine check
rather than a careful reader.

robots.txt is fetched once per host per process, cached in the store like any
other body, and **fails closed**: if we cannot read a host's rules we do not
assume permission.

Matching is done by ``rfc9309``, not ``urllib.robotparser``. That module's
answer depends on the interpreter's minor version — measured 2026-08-28,
``Disallow: /s/*/*`` against Airbnb's search path returns False on 3.14 and
**True on 3.12** — and `bin/activity-intel` runs whichever ``python3`` is first
on the user's PATH. A compliance guarantee that changes with the runtime is not
one.

Scope note: robots.txt is not a licence. A path being allowed here says only
that the site's crawl file does not object; a site's Terms of Service are a
separate document and a separate decision, recorded in docs/SOURCES.md.
"""
from __future__ import annotations

import urllib.parse

from . import config, rfc9309


class Disallowed(RuntimeError):
    """robots.txt forbids this path for our user-agent. Not retryable."""


class RobotsUnavailable(RuntimeError):
    """We could not read the host's robots.txt, so we decline to guess."""


# Group we evaluate against. We are an ordinary client here, not a declared
# crawler, so the wildcard group is the honest one to obey.
AGENT = "*"


class RobotsGate:
    def __init__(self, fetcher, *, agent: str = AGENT, enabled: bool = True,
                 exempt_hosts=None, warn=None):
        """``fetcher(url) -> str`` returns a robots.txt body; injected for tests.

        ``exempt_hosts`` is the operator override, and it is deliberately
        **per-host** rather than a global switch. That is not tidiness: the
        first version disabled the whole gate, so `--ignore-robots` — passed to
        reach Klook — also stopped checking Airbnb, whose `/api/v3/` path is
        *allowed* and needs no exemption. Worse, it silently retired the guard
        on Airbnb's disallowed `/s/*/*`, which is the exact path an earlier
        version of this tool fetched by mistake. An override must be no wider
        than the need, or it disables the check that would have caught the next
        unrelated bug.

        ``enabled=False`` remains a total bypass for tests only. The CLI never
        constructs one; it passes ``exempt_hosts``.

        Either way the skip must never be *quiet* — the whole reason this gate
        exists is that a disallowed fetch is invisible from the client side. So
        every exempted host is announced once on stderr, and
        ``overridden_hosts`` lets a caller report it too.
        """
        self._fetch = fetcher
        self.agent = agent
        self.enabled = enabled
        self.exempt_hosts = frozenset(exempt_hosts or ())
        self._warn = warn if warn is not None else _default_warn
        self.overridden_hosts: set[str] = set()
        self._parsers: dict[str, rfc9309.Rules | None] = {}
        # Why each origin failed, so the refusal can name a cause. Discarding it
        # turns every network fault into the same unactionable sentence.
        self._failures: dict[str, str] = {}

    def check(self, url: str) -> None:
        """Raise unless ``url`` is permitted. Silent success is the normal path."""
        parts = urllib.parse.urlsplit(url)
        if not self.enabled or parts.netloc in self.exempt_hosts:
            if parts.netloc not in self.overridden_hosts:
                self.overridden_hosts.add(parts.netloc)
                self._warn(
                    f"[robots] OVERRIDE ACTIVE for {parts.netloc} — its robots.txt "
                    f"is not being consulted. This was an explicit --ignore-robots "
                    f"choice, not a default. Keep the volume low and do not "
                    f"redistribute what you read."
                )
            return
        origin = f"{parts.scheme}://{parts.netloc}"

        if origin not in self._parsers:
            self._parsers[origin] = self._load(origin)

        parser = self._parsers[origin]
        if parser is None:
            raise RobotsUnavailable(
                f"could not read {origin}/robots.txt ({self._failures.get(origin, 'no reason recorded')}), "
                f"so {parts.path} is not known to be permitted. Failing closed "
                f"rather than assuming consent."
            )
        if not rfc9309.can_fetch(parser, url):
            raise Disallowed(
                f"{origin}/robots.txt disallows {parts.path or '/'} for user-agent "
                f"'{self.agent}'. This path will not be fetched. If a sibling path "
                f"serves the same data and is allowed, use that one instead."
            )

    def _load(self, origin: str):
        try:
            body = self._fetch(f"{origin}/robots.txt")
        except NoRobotsFile:
            # RFC 9309 s2.3.1.3: 4xx means "no restrictions". Treating a 404 as
            # unreadable and failing closed would refuse a site that has
            # explicitly published no rules — over-strict, and wrong.
            return rfc9309.Rules([])          # empty ruleset == allow all
        except Exception as exc:
            self._failures[origin] = f"{type(exc).__name__}: {exc}"
            return None
        return rfc9309.parse(body.splitlines(), self.agent)


def _default_warn(message: str) -> None:
    import sys
    print(message, file=sys.stderr)


# One transient failure here used to kill an entire sweep, because the gate
# fails closed and nothing retried. Measured 2026-08-28: a cold-cache Hanoi run
# returned 0 of ~1,250 listings after a single unreadable
# `https://www.airbnb.com/robots.txt`. The refusal was *correct* — coverage said
# so and the exit code was 7 — but a 100-second sweep should not be forfeited to
# one dropped packet.
ROBOTS_FETCH_ATTEMPTS = 3
ROBOTS_BACKOFF_S = 1.5


class NoRobotsFile(Exception):
    """The host answered 4xx: it publishes no rules. Not a failure."""


def default_fetcher(url: str, *, sleep=None) -> str:
    """Plain fetch that deliberately bypasses the gate — it IS the gate's input.

    Retries transient failures. A 4xx is NOT transient and NOT an error: it
    raises ``NoRobotsFile`` so the caller can apply the RFC's allow-all rule
    rather than confusing "no rules published" with "we could not look".
    """
    import ssl
    import time
    import urllib.error
    import urllib.request
    sleep = sleep or time.sleep

    last: Exception | None = None
    for attempt in range(ROBOTS_FETCH_ATTEMPTS):
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20,
                                        context=config.ssl_context()) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise NoRobotsFile(f"{url} -> HTTP {exc.code}") from exc
            last = exc
        except ssl.SSLCertVerificationError as exc:
            # An empty trust store is not transient. Retrying it three times
            # just delays a message that must name the fix.
            raise RuntimeError(
                f"TLS verification failed fetching {url}.\n{config.tls_remedy()}"
            ) from exc
        except Exception as exc:      # timeout, DNS, reset — all retryable
            last = exc
        if attempt < ROBOTS_FETCH_ATTEMPTS - 1:
            sleep(ROBOTS_BACKOFF_S * (2 ** attempt))
    raise RuntimeError(
        f"could not fetch {url} after {ROBOTS_FETCH_ATTEMPTS} attempts: {last}"
    ) from last
