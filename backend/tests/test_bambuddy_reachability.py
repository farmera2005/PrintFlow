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
        # The machine is what validation is checking for; the live readings a
        # printer row also carries are the farm screen's business, not this one.
        assert [(row["id"], row["name"]) for row in result["printers"]] == [(1, "P1S")]
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


class TestErrorsNameTheUrlThatFailed:
    """A bare "404" is unactionable when the base URL and the path are set
    separately and either could be the wrong half."""

    async def test_the_joined_url_is_reported(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(IntegrationError) as excinfo:
            await _client().list_printers(retries=0)
        assert "http://printer-host:8080/api/printers" in str(excinfo.value)
        # Etsy's own words are still carried through.
        assert "Not found" in str(excinfo.value)

    async def test_a_base_url_that_already_has_a_path_shows_the_doubling(
        self, monkeypatch
    ):
        """base_url ending in /api silently produces /api/api/printers."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        client = _client(base_url="http://printer-host:8080/api")
        with pytest.raises(IntegrationError) as excinfo:
            await client.list_printers(retries=0)
        assert "/api/api/printers" in str(excinfo.value)

    async def test_url_for_matches_where_the_request_actually_goes(self, monkeypatch):
        """The reported URL is worthless if it is not the one httpx built."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=[])

        _patch_transport(monkeypatch, handler)
        for base in ("http://printer-host:8080", "http://printer-host:8080/api"):
            client = _client(base_url=base)
            await client.list_printers(retries=0)
            assert seen[-1] == client.url_for("/api/printers")


class TestEndpointsAreReadFromTheInstance:
    """Bambuddy publishes an OpenAPI document and ships hundreds of endpoints
    that move between releases. Guessing the paths is what produced the 404."""

    SPEC = {
        "/api/printers": {"get": {}},
        "/api/printers/{printer_id}": {"get": {}},
        "/api/printers/{printer_id}/queue": {"get": {}, "post": {}},
        "/api/archives": {"get": {}},
        "/api/queue": {"get": {}, "post": {}},
        "/api/settings": {"get": {}},
    }

    async def test_each_endpoint_is_found(self):
        found = bambuddy_api.discover_paths(self.SPEC)
        assert found["printers"]["path"] == "/api/printers"
        assert found["archives"]["path"] == "/api/archives"
        assert found["queue"]["path"] == "/api/queue"

    async def test_item_endpoints_are_not_mistaken_for_collections(self):
        found = bambuddy_api.discover_paths({"/api/printers/{id}": {"get": {}}})
        assert found["printers"]["path"] is None

    async def test_the_farm_queue_beats_a_per_printer_queue(self):
        """/api/printers/{id}/queue is a different thing from the farm queue."""
        found = bambuddy_api.discover_paths(self.SPEC)
        assert found["queue"]["path"] == "/api/queue"

    async def test_a_queue_that_cannot_be_posted_to_is_not_the_queue(self):
        found = bambuddy_api.discover_paths({"/api/queue": {"get": {}}})
        assert found["queue"]["path"] is None

    async def test_a_versioned_prefix_is_handled(self):
        found = bambuddy_api.discover_paths(
            {"/api/v1/printers": {"get": {}}, "/api/v1/queue": {"get": {}, "post": {}}}
        )
        assert found["printers"]["path"] == "/api/v1/printers"
        assert found["queue"]["path"] == "/api/v1/queue"

    async def test_runners_up_are_reported_so_a_wrong_pick_can_be_corrected(self):
        found = bambuddy_api.discover_paths(
            {"/api/models": {"get": {}}, "/api/archives": {"get": {}}}
        )
        assert found["archives"]["path"] == "/api/archives"
        assert "/api/models" in found["archives"]["alternatives"]

    async def test_listable_endpoints_are_offered_for_manual_choice(self):
        rows = bambuddy_api.collection_paths(self.SPEC)
        paths = [row["path"] for row in rows]
        assert "/api/settings" in paths
        # Templated paths cannot be listed, so they are not offered.
        assert not any("{" in p for p in paths)

    async def test_discovered_paths_are_adopted_over_our_defaults(self):
        client = _client()
        adopted = client.adopt_discovered({"printers": {"path": "/api/v1/printers"}})
        assert adopted == {"printers": "/api/v1/printers"}
        assert client.paths["printers"] == "/api/v1/printers"

    async def test_a_hand_set_path_outranks_discovery(self):
        """An explicit choice in Advanced settings must not be overwritten."""
        client = _client(paths={"printers": "/custom/printers"})
        adopted = client.adopt_discovered({"printers": {"path": "/api/v1/printers"}})
        assert adopted == {}
        assert client.paths["printers"] == "/custom/printers"

    async def test_validation_uses_the_discovered_path(self, monkeypatch):
        """The whole point: a build whose printers live somewhere else works."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/openapi.json":
                return httpx.Response(
                    200,
                    json={
                        "openapi": "3.1.0",
                        "info": {"version": "0.1.6"},
                        "paths": {
                            "/api/v1/printers": {"get": {}},
                            "/api/v1/queue": {"get": {}, "post": {}},
                        },
                    },
                )
            if request.url.path == "/api/v1/printers":
                return httpx.Response(200, json=[{"id": 3, "name": "A1"}])
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        result = await _client().validate()
        assert result["printers"][0]["id"] == 3
        assert result["adopted_paths"]["printers"] == "/api/v1/printers"
        # The default was never tried; the spec was believed.
        assert "/api/printers" not in calls

    async def test_the_adopted_paths_are_persisted(self, signed_in, db, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi.json":
                return httpx.Response(
                    200,
                    json={
                        "openapi": "3.1.0",
                        "info": {"version": "0.1.6"},
                        "paths": {
                            "/api/v1/printers": {"get": {}},
                            "/api/v1/queue": {"get": {}, "post": {}},
                            "/api/v1/archives": {"get": {}},
                        },
                    },
                )
            if request.url.path == "/api/v1/printers":
                return httpx.Response(200, json=[{"id": 3}])
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        response = await signed_in.post(
            "/api/integrations/bambuddy/config",
            json={"base_url": "http://printer-host:8080", "api_key": "k"},
        )
        assert response.status_code == 200

        # Polling and queueing must use what validation proved, not the defaults.
        stored = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert stored["discovered_paths"]["printers"] == "/api/v1/printers"
        assert stored["discovered_paths"]["queue"] == "/api/v1/queue"
        client = bambuddy_api.BambuddyClient(stored)
        assert client.paths["printers"] == "/api/v1/printers"
        # Not filed as an operator choice, or a later upgrade could never move it.
        assert "printers" not in (stored.get("paths") or {})

    async def test_re_validating_after_an_upgrade_moves_the_paths(
        self, signed_in, db, monkeypatch
    ):
        """A discovered path must not outlive the release that published it."""
        await credentials.save(
            db,
            PROVIDER_BAMBUDDY,
            {
                "base_url": "http://printer-host:8080",
                "api_key": "k",
                "discovered_paths": {"printers": "/api/v1/printers"},
            },
        )
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi.json":
                return httpx.Response(
                    200,
                    json={
                        "openapi": "3.1.0",
                        "info": {"version": "0.2.0"},
                        "paths": {"/api/v2/printers": {"get": {}}},
                    },
                )
            if request.url.path == "/api/v2/printers":
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        response = await signed_in.post(
            "/api/integrations/bambuddy/config",
            json={"base_url": "http://printer-host:8080", "api_key": "k"},
        )
        assert response.status_code == 200
        stored = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert stored["discovered_paths"]["printers"] == "/api/v2/printers"


class TestA404IsExplained:
    async def test_it_says_the_host_is_fine_and_the_path_is_not(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi.json":
                return httpx.Response(
                    200,
                    json={"openapi": "3.1.0", "info": {}, "paths": {"/api/thing": {"get": {}}}},
                )
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(IntegrationError) as excinfo:
            await _client().validate()
        text = str(excinfo.value)
        assert "address is reachable" in text
        assert "nothing at that path" in text
        assert "http://printer-host:8080/api/printers" in text

    async def test_a_missing_spec_is_named_as_the_reason_discovery_could_not_help(
        self, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(IntegrationError) as excinfo:
            await _client().validate()
        assert "could not read this instance's OpenAPI document" in str(excinfo.value)

    async def test_the_endpoints_route_reports_what_is_there(self, signed_in, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi.json":
                return httpx.Response(
                    200,
                    json={
                        "openapi": "3.1.0",
                        "info": {"version": "0.1.6"},
                        "paths": {"/api/v1/printers": {"get": {}}},
                    },
                )
            return httpx.Response(404)

        _patch_transport(monkeypatch, handler)
        body = (
            await signed_in.post(
                "/api/integrations/bambuddy/endpoints",
                json={"base_url": "http://printer-host:8080", "api_key": "k"},
            )
        ).json()
        assert body["error"] is None
        assert body["discovered"]["printers"]["path"] == "/api/v1/printers"
        assert {"path": "/api/v1/printers", "methods": ["get"]} in body["collections"]

    async def test_the_endpoints_route_never_fails_the_request(
        self, signed_in, monkeypatch
    ):
        """Someone staring at a 404 needs the endpoint list, not a second error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        _patch_transport(monkeypatch, handler)
        response = await signed_in.post(
            "/api/integrations/bambuddy/endpoints",
            json={"base_url": "http://printer-host:8080", "api_key": "k"},
        )
        assert response.status_code == 200
        assert response.json()["error"]
        assert response.json()["collections"] == []


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
