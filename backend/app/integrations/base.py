"""Shared HTTP plumbing: timeouts, bounded retries with backoff, rate limiting."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("printflow.integrations")

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# Bambuddy is on the same LAN, so a slow answer means something is wrong rather
# than far away. The default timeouts are sized for the internet APIs and are
# far too patient here: four OpenAPI probes plus three printer attempts at a
# 30s read timeout is over three minutes, which is longer than any reverse
# proxy in front of PrintFlow will wait for a reply (Cloudflare gives up at
# 100s and serves its own 502 page, which is what the operator then sees
# instead of our error).
LAN_TIMEOUT = httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=4.0)

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# What an interactive "save and validate" is allowed to take end to end. Well
# under any proxy's patience, and well over a healthy LAN round trip.
INTERACTIVE_BUDGET_SECONDS = 20.0


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


class DeadlineExceeded(IntegrationError):
    """A whole operation ran out of its time budget, not just one request."""


class TransportFailed(IntegrationError):
    """The request never got an HTTP reply — wrong address, or nothing there.

    Distinct from an HTTP error because it says something about the *host*
    rather than the path: when this happens there is no point trying the same
    host again on a different path.
    """


# What to suggest when an operation runs out of time. The default suits a
# service the operator configured the address of; a public API they cannot
# have typed wrong needs different advice, hence the override.
LOCAL_SERVICE_HINT = (
    "check the address and port, and that the service is running"
)


@asynccontextmanager
async def deadline(
    provider: str,
    what: str,
    seconds: float = INTERACTIVE_BUDGET_SECONDS,
    *,
    hint: str = LOCAL_SERVICE_HINT,
) -> AsyncIterator[None]:
    """Cap a multi-request operation so a reply always beats the proxy.

    Without this the operator sees the proxy's own error page — which names
    PrintFlow's hostname, not the service that was actually unreachable, and
    sends them looking in entirely the wrong place.
    """
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError as exc:
        raise DeadlineExceeded(
            provider,
            f"{what} did not finish within {int(seconds)}s. "
            f"{provider} accepted the connection but never answered — {hint}",
        ) from exc


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
            last_exc = TransportFailed(provider, transport_reason(exc, client, url))
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


def transport_reason(exc: Exception, client: httpx.AsyncClient, url: str) -> str:
    """Plain-language reason a request never got an HTTP reply.

    `str(httpx.ReadTimeout())` is the empty string, so the obvious
    f"{type(exc).__name__}: {exc}" renders as "ReadTimeout: " — the operator is
    told the name of a Python class and nothing about which address failed.
    """
    try:
        target = str(client.build_request("GET", url).url)
    except Exception:  # pragma: no cover - defensive
        target = url

    if isinstance(exc, httpx.ConnectTimeout):
        return f"No answer from {target} — the address is wrong, or a firewall is dropping the connection"
    if isinstance(exc, httpx.ConnectError):
        return f"Could not connect to {target} — nothing is listening on that address and port"
    if isinstance(exc, httpx.ReadTimeout):
        return (
            f"Connected to {target}, but it never sent a reply. "
            "That is usually a different service on the port, or one that is wedged"
        )
    detail = str(exc).strip()
    return f"Could not reach {target} ({type(exc).__name__}{f': {detail}' if detail else ''})"


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
