"""A wrong Bambuddy address must fail fast and say what is wrong.

Bambuddy is on the LAN, and the usual mistakes are a wrong port, a container
that cannot see the host, or a firewall dropping the connection. The default
internet-API timeouts turned all of those into a three-and-a-half minute wait:
four OpenAPI probes plus three printer attempts, each willing to sit through a
30s read timeout. Any reverse proxy in front of PrintFlow gives up long before
that and answers with its own error page, so the operator was shown a Cloudflare
502 naming *PrintFlow's* hostname for a fault in a machine on their own bench.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.integrations import bambuddy as bambuddy_api
from app.integrations import base as base_api
from app.integrations.base import (
    DeadlineExceeded,
    IntegrationError,
    TransportFailed,
)
from app.models import PROVIDER_BAMBUDDY
from app.services import credentials

pytestmark = pytest.mark.asyncio


def _patch_transport(monkeypatch, handler):
    """Point the Bambuddy client's HTTP client at a canned handler."""

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_api.new_client(**kwargs)

    monkeypatch.setattr(bambuddy_api, "new_client", fake)


def _client(**overrides):
    return bambuddy_api.BambuddyClient(
        {"base_url": "http://printer-host:8080", "api_key": "k", **overrides}
    )


class TestFailureMessagesNameTheAddress:
    """`str(httpx.ReadTimeout())` is empty, so the obvious formatting produced
    the message "ReadTimeout: " — a Python class name and nothing else."""

    def _reason(self, exc):
        client = base_api.new_client(base_url="http://printer-host:8080")
        return base_api.transport_reason(exc, client, "/api/printers")

    async def test_nothing_listening(self):
        text = self._reason(httpx.ConnectError("refused"))
        assert "http://printer-host:8080/api/printers" in text
        assert "nothing is listening" in text

    async def test_connection_dropped(self):
        text = self._reason(httpx.ConnectTimeout("timed out"))
        assert "http://printer-host:8080/api/printers" in text
        assert "firewall" in text

    async def test_connected_but_silent(self):
        text = self._reason(httpx.ReadTimeout(""))
        assert "http://printer-host:8080/api/printers" in text
        assert "never sent a reply" in text
        # The empty str() of the exception must not be the whole message.
        assert text.strip() != "ReadTimeout:"

    async def test_unknown_transport_errors_still_name_the_target(self):
        text = self._reason(httpx.TransportError("something odd"))
        assert "http://printer-host:8080/api/printers" in text
        assert "something odd" in text


class TestTransportFailuresAreDistinguishable:
    async def test_a_transport_failure_is_typed(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _patch_transport(monkeypatch, handler)
        with pytest.raises(TransportFailed):
            await _client().list_printers(retries=0)

    async def test_an_http_error_is_not(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(IntegrationError) as excinfo:
            await _client().list_printers(retries=0)
        assert not isinstance(excinfo.value, TransportFailed)


class TestTheOpenApiProbeStopsWhenNothingAnswers:
    async def test_an_unreachable_host_is_probed_once(self, monkeypatch):
        """Every candidate path is on the same host, so the rest can only fail
        the same way — and each costs a full timeout the operator sits through."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise httpx.ConnectError("refused")

        _patch_transport(monkeypatch, handler)
        with pytest.raises(TransportFailed):
            await _client().fetch_openapi()
        assert len(calls) == 1

    async def test_a_host_that_answers_gets_every_candidate_tried(self, monkeypatch):
        """A 404 says the host is there and the path is wrong — keep looking."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(404, json={"detail": "not found"})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(IntegrationError) as excinfo:
            await _client().fetch_openapi()
        assert len(calls) == len(set(bambuddy_api.OPENAPI_CANDIDATES))
        assert "Could not find an OpenAPI document" in str(excinfo.value)

    async def test_the_spec_is_returned_when_found(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/api/openapi.json":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "info": {"title": "Bambuddy", "version": "2.4.0"},
                    "paths": {"/api/printers": {"get": {}}},
                },
            )

        _patch_transport(monkeypatch, handler)
        spec = await _client().fetch_openapi()
        assert spec["version"] == "2.4.0"
        assert spec["path"] == "/api/openapi.json"


class TestValidationIsBounded:
    async def test_lan_timeouts_are_used_not_the_internet_ones(self):
        """30s reads are sized for a far-away API, not a box on the bench."""
        assert base_api.LAN_TIMEOUT.read < base_api.DEFAULT_TIMEOUT.read
        assert base_api.LAN_TIMEOUT.connect < base_api.DEFAULT_TIMEOUT.connect

    async def test_the_budget_leaves_room_before_a_proxy_gives_up(self):
        """Cloudflare answers with its own 502 at 100s; ours must land first."""
        assert base_api.INTERACTIVE_BUDGET_SECONDS <= 30

    async def test_a_slow_host_hits_the_deadline(self):
        async def crawl():
            await asyncio.sleep(5)

        with pytest.raises(DeadlineExceeded) as excinfo:
            async with base_api.deadline(PROVIDER_BAMBUDDY, "Checking", seconds=0.05):
                await crawl()
        text = str(excinfo.value)
        assert "Checking" in text
        assert "did not finish" in text

    async def test_a_prompt_host_is_left_alone(self):
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Checking", seconds=5):
            pass

    async def test_validate_reports_printers_when_the_spec_is_missing(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/printers":
                return httpx.Response(200, json=[{"id": 1, "name": "P1S"}])
            return httpx.Response(404)

        _patch_transport(monkeypatch, handler)
        result = await _client().validate()
        assert result["printers"] == [
            {"id": 1, "name": "P1S", "model": None, "status": None, "online": None}
        ]
        # A missing spec is a warning, not a failure: printers is the real test.
        assert "warning" in result["openapi"]

    async def test_validate_does_not_retry_the_printers_call(self, monkeypatch):
        """The operator is watching a form; retrying just burns their budget."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/api/printers":
                return httpx.Response(503, json={"error": "busy"})
            return httpx.Response(404)

        _patch_transport(monkeypatch, handler)
        with pytest.raises(IntegrationError):
            await _client().validate()
        assert calls.count("/api/printers") == 1


class TestTheOperatorSeesOurErrorNotTheProxysPage:
    async def _configure(self, signed_in, base_url="http://printer-host:8080"):
        return await signed_in.post(
            "/api/integrations/bambuddy/config",
            json={"base_url": base_url, "api_key": "k"},
        )

    async def test_an_unreachable_host_is_reported_plainly(
        self, signed_in, db, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _patch_transport(monkeypatch, handler)
        response = await self._configure(signed_in)
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "printer-host:8080" in detail
        assert "nothing is listening" in detail
        # Not an HTML page from whatever sits in front of PrintFlow.
        assert "<html" not in detail.lower()

    async def test_a_previously_working_instance_going_dark_sets_the_banner(
        self, signed_in, db, monkeypatch
    ):
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://printer-host:8080", "api_key": "k"}
        )
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _patch_transport(monkeypatch, handler)
        assert (await self._configure(signed_in)).status_code == 502
        record = await credentials.get_record(db, PROVIDER_BAMBUDDY)
        assert "nothing is listening" in (record.last_error or "")

    async def test_a_working_instance_is_saved(self, signed_in, db, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi.json":
                return httpx.Response(
                    200, json={"openapi": "3.1.0", "info": {"version": "2.4.0"}, "paths": {}}
                )
            if request.url.path == "/api/printers":
                return httpx.Response(200, json=[{"id": 7, "name": "X1C"}])
            return httpx.Response(404)

        _patch_transport(monkeypatch, handler)
        response = await self._configure(signed_in)
        assert response.status_code == 200
        assert response.json()["printers"][0]["id"] == 7

        stored = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert stored["base_url"] == "http://printer-host:8080"
        assert stored["api_version"] == "2.4.0"
