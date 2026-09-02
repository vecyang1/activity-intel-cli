"""The only code in this package that opens a socket.

Source adapters build URLs and parse bodies; they never fetch. That is what
makes the pacing promise auditable instead of aspirational — there is exactly
one place to check, and one place to add a header, a retry, or a new host.

Contract, in order, for every call:

    cache hit (fresh)  -> return it, touch no network
    otherwise          -> reserve a per-host slot, send, record, cache

The property that matters most: **this module never returns a partial or
degraded answer.** Every failure raises. A sweep that stops early because of a
429 must be distinguishable from a city that genuinely has twelve activities,
and the only way to guarantee that is to refuse to paper over the difference.
"""
from __future__ import annotations

import gzip
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import config, robots, store


class TransportError(RuntimeError):
    """Base for every failure that must stop a sweep rather than shorten it."""


class RateLimited(TransportError):
    """The source returned 429/403 and retries did not clear it. An incident."""


class UpstreamError(TransportError):
    """The source returned an error we cannot interpret."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CacheMiss(TransportError):
    """--cache-only was requested and this URL is not cached."""


# Failures that must STOP a sweep, not be recorded against one query and
# stepped over. `sweep.sweep` and `airbnb.sweep_place` both catch `Exception`
# per query so that one dead keyword does not discard the other nine — and
# until 2026-09-02 that catch also swallowed these. Measured with a fake
# client: after the first 429 the Klook loop sent 26 more requests to the
# throttling host (each already three retries deep) and the run exited 7,
# PARTIAL — "the market is small" — instead of 5, RATE_LIMIT — "we were told
# to stop". A robots verdict is the same shape: a policy answer, not a flaky
# keyword. Both loops re-raise this tuple before their generic catch; the
# CLI maps it to the exit code the docstring in `exit_codes` promises.
INCIDENTS = (RateLimited, robots.Disallowed, robots.RobotsUnavailable)


class Client:
    """Cache-first, pace-governed reader for public OTA endpoints.

    ``sleep``/``clock``/``opener`` are injectable so tests can prove retry and
    pacing behaviour without spending wall-clock time or touching the network.
    """

    def __init__(self, conn: sqlite3.Connection, *, sleep=time.sleep, clock=time.time,
                 opener=None, max_retries: int = config.MAX_RETRIES,
                 gap_s: float | None = None, allow_network: bool = True,
                 robots_gate=None):
        self.conn = conn
        self._sleep = sleep
        self._clock = clock
        self._opener = opener or self._urlopen
        self.max_retries = max_retries
        self.gap_s = config.request_gap_s() if gap_s is None else gap_s
        self.allow_network = allow_network
        # Default-on. A caller must pass an explicitly disabled gate to opt out,
        # so "we forgot to check robots" is not reachable by omission.
        self.robots = robots_gate if robots_gate is not None else robots.RobotsGate(
            robots.default_fetcher)
        # Per-run counters, so a caller can prove cache-vs-network from outside.
        self.requests_sent = 0
        self.cache_hits = 0

    def get(self, url: str, *, ttl_s: float = config.TTL_SEARCH_S,
            headers: dict | None = None) -> str:
        # POLICY BEFORE CACHE. Measured 2026-08-28: with the check below the
        # cache read, one `--ignore-robots` run wrote Klook's disallowed search
        # responses into the store, and every later run — strict policy, no
        # flag — served them straight back without consulting robots.txt at all.
        # `doctor` reported "klook search was NOT refused", which was true and
        # was not about robots.txt changing. An override that persists in a
        # cache is an override nobody can see or revoke.
        #
        # Consequence for --cache-only: robots.txt itself may still be fetched,
        # once per host per process. That is a policy read, not a data read, and
        # the alternative is a flag that silently disables the gate.
        self.robots.check(url)

        cached = store.cache_get(self.conn, url, now=self._clock())
        if cached is not None:
            self.cache_hits += 1
            return cached
        if not self.allow_network:
            raise CacheMiss(f"not cached and --cache-only was requested: {url}")
        return self._fetch(url, ttl_s, headers or {})

    # -- internals -------------------------------------------------------------

    def _fetch(self, url: str, ttl_s: float, headers: dict) -> str:
        host = urllib.parse.urlsplit(url).netloc
        last: Exception | None = None

        for attempt in range(self.max_retries + 1):
            wait = store.reserve_slot(self.conn, host, self.gap_s, now=self._clock())
            if wait > 0:
                self._sleep(wait)

            self.requests_sent += 1
            try:
                body, status = self._opener(url, headers)
            except urllib.error.HTTPError as exc:
                status = exc.code
                store.record_request(self.conn, host, url, status, now=self._clock())
                detail = self._detail(exc)

                if status in (429, 403):
                    if attempt < self.max_retries:
                        self._sleep(self._retry_after(exc, attempt))
                        last = RateLimited(f"HTTP {status} from {host}: {detail}")
                        continue
                    raise RateLimited(
                        f"{host} returned HTTP {status} for {url} after "
                        f"{self.max_retries} retries: {detail}. Stopping rather than "
                        f"returning a short result — a throttled sweep and a small "
                        f"market must not look the same."
                    ) from exc

                if 500 <= status < 600 and attempt < self.max_retries:
                    self._sleep(min(0.5 * (2 ** attempt), 10.0))
                    last = UpstreamError(f"HTTP {status} from {host}: {detail}", status)
                    continue

                raise UpstreamError(
                    f"{host} returned HTTP {status} for {url}: {detail}", status
                ) from exc

            except ssl.SSLCertVerificationError as exc:
                # Not retryable and not the site's fault. Fail immediately with
                # the fix rather than three timeouts and a generic message.
                store.record_request(self.conn, host, url, None, now=self._clock())
                raise UpstreamError(
                    f"TLS verification failed reaching {host}.\n{config.tls_remedy()}"
                ) from exc

            except Exception as exc:  # transport-level; never swallow the reason
                store.record_request(self.conn, host, url, None, now=self._clock())
                if attempt < self.max_retries:
                    self._sleep(min(0.5 * (2 ** attempt), 10.0))
                    last = UpstreamError(f"{type(exc).__name__}: {exc}")
                    continue
                raise UpstreamError(
                    f"{type(exc).__name__} reaching {host} for {url}: {exc}"
                ) from exc

            store.record_request(self.conn, host, url, status, now=self._clock())
            store.cache_put(self.conn, url, host, body, ttl_s, now=self._clock())
            return body

        raise last or UpstreamError(f"{url} failed for an unrecorded reason")

    def _retry_after(self, exc, attempt: int) -> float:
        headers = getattr(exc, "headers", None)
        if headers is not None:
            raw = headers.get("Retry-After")
            if raw is not None:
                try:
                    return min(float(raw), 30.0)
                except (TypeError, ValueError):
                    pass
        return min(1.0 * (2 ** attempt), 20.0)

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            return "(no body)"

    @staticmethod
    def _urlopen(url: str, headers: dict) -> tuple[str, int]:
        """Decode explicitly at the boundary.

        `response.read()` plus a guessed charset is how a UTF-8 body with a BOM
        turns into a mangled first key. Decode as utf-8-sig from bytes; never
        trust a server that declined to declare a charset.
        """
        base = {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        base.update(headers)
        req = urllib.request.Request(url, headers=base)
        with urllib.request.urlopen(req, timeout=30,
                                    context=config.ssl_context()) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw.decode("utf-8-sig", errors="replace"), resp.status
