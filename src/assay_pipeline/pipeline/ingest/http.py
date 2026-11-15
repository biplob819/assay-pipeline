"""HTTP fetching for ingest: rate limit, the §2.5 retry rules, an injectable transport.

Only the ingest stage touches the network. Requests carry a user agent that names the project,
never the user or the machine.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from assay_pipeline import __version__

USER_AGENT = f"assay-pipeline/{__version__} (+https://github.com/biplob819/assay-pipeline)"
RETRY_STATUSES = frozenset({429})  # plus every 5xx (§2.5)


class FetchError(RuntimeError):
    """A request failed for good: retries exhausted, a non-retryable status, or a bad body."""


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    body: bytes
    data: Any
    date: str | None  # the server's Date header, ISO 8601, when it sent one


@dataclass
class Fetcher:
    transport: httpx.BaseTransport | None = None
    max_requests_per_second: float = 4.0
    attempts: int = 6
    base_delay: float = 0.5
    max_delay: float = 30.0
    timeout: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    requests_made: int = 0
    _last: float | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    def _pace(self) -> None:
        gap = 1.0 / self.max_requests_per_second
        now = self.clock()
        if self._last is not None and now - self._last < gap:
            self.sleep(gap - (now - self._last))
        self._last = self.clock()

    def _backoff(self, attempt: int) -> float:
        cap = min(self.max_delay, self.base_delay * 2**attempt)
        return cap * (0.5 + 0.5 * self.rng.random())  # jitter in [cap/2, cap]

    def get_json(
        self,
        url: str,
        params: dict[str, str | int],
        ok_statuses: frozenset[int] = frozenset(),
    ) -> Fetched:
        """GET JSON with the §2.5 retry rules. `ok_statuses` are non-2xx answers the caller
        expects and parses itself (PubChem's `PUGREST.NotFound` 404), returned, not raised."""
        where = f"{url} {dict(sorted(params.items()))}" if params else url
        last_error = ""
        with self._client() as client:
            for attempt in range(self.attempts):
                self._pace()
                self.requests_made += 1
                retry_after: float | None = None
                try:
                    resp = client.get(url, params=params)
                except httpx.TransportError as exc:
                    last_error = f"transport error: {exc.__class__.__name__}: {exc}"
                except httpx.HTTPError as exc:  # redirects, decoding: not transient
                    raise FetchError(f"{where}: {exc.__class__.__name__}: {exc}") from exc
                else:
                    if resp.status_code in ok_statuses:
                        return _parse(where, resp)
                    if resp.status_code in RETRY_STATUSES or resp.status_code >= 500:
                        last_error = f"HTTP {resp.status_code}"
                        retry_after = _retry_after(resp)
                    elif not 200 <= resp.status_code < 300:
                        raise FetchError(f"{where}: HTTP {resp.status_code}, not retried")
                    else:
                        return _parse(where, resp)
                if attempt + 1 < self.attempts:
                    delay = self._backoff(attempt)
                    if retry_after is not None:
                        delay = min(self.max_delay, max(delay, retry_after))
                    self.sleep(delay)
        raise FetchError(f"{where}: gave up after {self.attempts} attempts ({last_error})")


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _parse(url: str, resp: httpx.Response) -> Fetched:
    body = resp.content
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FetchError(f"{url}: response is not JSON ({exc}), not retried") from exc
    date = resp.headers.get("Date")
    iso = None
    if date:
        try:
            when = parsedate_to_datetime(date)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:  # RFC 2822 "-0000" means UTC with unknown origin
                when = when.replace(tzinfo=UTC)
            iso = when.isoformat()
    return Fetched(url=str(resp.url), status=resp.status_code, body=body, data=data, date=iso)
