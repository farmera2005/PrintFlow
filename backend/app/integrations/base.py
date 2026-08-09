"""Shared HTTP plumbing: timeouts, bounded retries with backoff, rate limiting."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger("printflow.integrations")

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class IntegrationError(RuntimeError):
    """Any failure talking to a third-party API, normalised for the UI."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.body = body

    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code:
            base = f"{base} (HTTP {self.status_code})"
        # Include what the provider actually said. Swallowing it leaves the
        # operator with "rejected" and nothing to act on, when the body usually
        # names the reason outright — a missing scope, an unknown API key, an
        # expired grant.
        detail = _compact(self.body)
        if detail:
            base = f"{base} — {provider_said(self.provider)}: {detail}"
        return base


class AuthExpiredError(IntegrationError):
    """Credentials were rejected — the operator must re-connect in Settings."""


def new_client(**kwargs: Any) -> httpx.AsyncClient:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    expected: tuple[int, ...] = (200, 201, 202, 204),
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying transient failures with exponential backoff + jitter."""
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= retries:
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = IntegrationError(provider, f"{type(exc).__name__}: {exc}")
            if attempt == retries:
                raise last_exc from exc
        else:
            if response.status_code in expected:
                return response
            if response.status_code in (401, 403):
                raise AuthExpiredError(
                    provider,
                    "Authorisation was rejected",
                    status_code=response.status_code,
                    body=_snippet(response),
                )
            if response.status_code not in RETRY_STATUSES or attempt == retries:
                raise IntegrationError(
                    provider,
                    f"Unexpected response from {provider}",
                    status_code=response.status_code,
                    body=_snippet(response),
                )
            retry_after = _retry_after(response)
            if retry_after is not None:
                await asyncio.sleep(min(retry_after, max_delay))
                attempt += 1
                continue

        delay = min(base_delay * (2**attempt), max_delay)
        await asyncio.sleep(delay * (0.5 + random.random() / 2))
        attempt += 1

    raise last_exc or IntegrationError(provider, "Request failed")


def provider_said(provider: str) -> str:
    return f"{provider} said"


def _compact(body: str | None, limit: int = 300) -> str:
    """One-line, readable summary of a response body.

    Error pages from an edge proxy are megabytes of markup; the useful part of
    an API error is a short JSON document. Collapse whitespace, and say plainly
    when the body was an HTML page rather than dumping tags into a toast.
    """
    if not body:
        return ""
    text = " ".join(body.split())
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("<!doctype") or lowered.startswith("<html"):
        title = ""
        start = lowered.find("<title>")
        if start != -1:
            end = lowered.find("</title>", start)
            if end != -1:
                title = text[start + 7 : end].strip()
        return f"an HTML error page{f' ({title})' if title else ''}"
    return text[:limit] + ("…" if len(text) > limit else "")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _snippet(response: httpx.Response, limit: int = 500) -> str:
    try:
        return response.text[:limit]
    except Exception:  # pragma: no cover - defensive
        return ""


class RateLimiter:
    """Simple token-bucket, enough for ShipStation's 40 requests/minute cap."""

    def __init__(self, max_calls: int, period: float) -> None:
        self.max_calls = max_calls
        self.period = period
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self.period]
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0]) + 0.01
                await asyncio.sleep(max(sleep_for, 0.01))
