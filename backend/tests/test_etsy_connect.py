"""Etsy connect: report what Etsy actually said, and do not lose a good token.

The handshake used to fail as a whole if a *convenience* lookup 403'd after the
token exchange had already succeeded — reported to the operator as
"Authorisation was rejected", which was the opposite of what happened.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations import etsy as etsy_api
from app.integrations.base import AuthExpiredError, IntegrationError, _compact
from app.models import PROVIDER_ETSY
from app.services import credentials

pytestmark = pytest.mark.asyncio


def _patch_transport(monkeypatch, handler):
    """Point the Etsy client's HTTP client at a canned response.

    Always wraps the pristine factory: re-patching an already-patched
    etsy_api.new_client would nest, and the innermost transport would win.
    """
    from app.integrations import base as base_module

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_module.new_client(**kwargs)

    monkeypatch.setattr(etsy_api, "new_client", fake)


class TestErrorMessagesCarryTheReason:
    async def test_body_is_included_so_the_reason_is_visible(self):
        exc = IntegrationError(
            "etsy",
            "Unexpected response from etsy",
            status_code=403,
            body='{"error":"insufficient_scope: transactions_r required"}',
        )
        text = str(exc)
        assert "403" in text
        assert "insufficient_scope" in text
        assert "etsy said" in text

    async def test_auth_failures_say_what_was_rejected(self):
        exc = AuthExpiredError(
            "etsy", "Authorisation was rejected", status_code=403, body='{"error":"invalid_api_key"}'
        )
        assert "invalid_api_key" in str(exc)

    async def test_html_error_pages_are_summarised_not_dumped(self):
        page = "<!DOCTYPE html><html><head><title>502: Bad gateway</title></head><body>" + (
            "<div>x</div>" * 500
        )
        summary = _compact(page)
        assert summary == "an HTML error page (502: Bad gateway)"
        assert "<div>" not in summary

    async def test_long_json_bodies_are_truncated(self):
        assert len(_compact('{"error":"' + "y" * 5000 + '"}')) < 320

    async def test_empty_body_adds_nothing(self):
        exc = IntegrationError("etsy", "Boom", status_code=403, body="")
        assert str(exc) == "Boom (HTTP 403)"


class TestUserIdFromToken:
    """Etsy tokens are `<user_id>.<random>`, so /users/me is avoidable."""

    async def test_extracts_the_user_id(self):
        assert etsy_api.user_id_from_token("123456789.abcdefghijklmnop") == "123456789"

    async def test_rejects_a_token_without_the_prefix(self):
        assert etsy_api.user_id_from_token("abcdefghijklmnop") is None

    async def test_handles_missing_tokens(self):
        assert etsy_api.user_id_from_token(None) is None
        assert etsy_api.user_id_from_token("") is None


class TestCallbackKeepsAGoodToken:
    async def _start(self, client):
        response = await client.post(
            "/api/integrations/etsy/start",
            json={"keystring": "keystring-value", "shared_secret": "shared-secret"},
        )
        assert response.status_code == 200
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(response.json()["authorize_url"]).query)["state"][0]

    async def test_a_403_on_the_shop_lookup_does_not_undo_the_connection(
        self, signed_in, db, monkeypatch
    ):
        state = await self._start(signed_in)

        async def fake_exchange(**kwargs):
            return {
                "access_token": "987654321.tokenvalue",
                "refresh_token": "refresh-value",
                "expires_at": 9_999_999_999,
            }

        monkeypatch.setattr(etsy_api, "exchange_code", fake_exchange)

        async def forbidden(self, path, params=None):
            raise AuthExpiredError(
                "etsy",
                "Authorisation was rejected",
                status_code=403,
                body='{"error":"insufficient_scope"}',
            )

        monkeypatch.setattr(etsy_api.EtsyClient, "_get", forbidden)

        response = await signed_in.get(
            f"/api/integrations/etsy/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        # The UI is told the connection succeeded, because it did.
        assert response.status_code == 303
        assert "result=connected" in response.headers["location"]

        stored = await credentials.load(db, PROVIDER_ETSY)
        assert stored["access_token"] == "987654321.tokenvalue"
        assert stored["refresh_token"] == "refresh-value"

        # ...and the lookup failure is still surfaced, not silently dropped.
        record = await credentials.get_record(db, PROVIDER_ETSY)
        assert "could not list your shops" in (record.last_error or "")
        assert "insufficient_scope" in record.last_error

    async def test_a_failed_token_exchange_still_fails_loudly(
        self, signed_in, db, monkeypatch
    ):
        state = await self._start(signed_in)

        async def refuse(**kwargs):
            raise AuthExpiredError(
                "etsy",
                "Authorisation was rejected",
                status_code=403,
                body='{"error":"invalid_grant"}',
            )

        monkeypatch.setattr(etsy_api, "exchange_code", refuse)

        response = await signed_in.get(
            f"/api/integrations/etsy/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        assert "result=error" in location
        # The reason travels to the UI rather than a bare "rejected".
        assert "invalid_grant" in location

        stored = await credentials.load(db, PROVIDER_ETSY)
        assert "access_token" not in stored

    async def test_shop_is_preselected_when_there_is_exactly_one(
        self, signed_in, db, monkeypatch
    ):
        state = await self._start(signed_in)

        async def fake_exchange(**kwargs):
            return {
                "access_token": "555.tokenvalue",
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
            }

        monkeypatch.setattr(etsy_api, "exchange_code", fake_exchange)

        seen = {}

        async def fake_get(self, path, params=None):
            seen["path"] = path
            return {"results": [{"shop_id": 4242, "shop_name": "Dragon Forge"}]}

        monkeypatch.setattr(etsy_api.EtsyClient, "_get", fake_get)

        response = await signed_in.get(
            f"/api/integrations/etsy/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert "result=connected" in response.headers["location"]
        # The user id came off the token, so /users/me was never called.
        assert seen["path"] == "/users/555/shops"

        stored = await credentials.load(db, PROVIDER_ETSY)
        assert stored["shop_id"] == 4242
        assert stored["shop_name"] == "Dragon Forge"


class TestApiHeaders:
    async def test_both_etsy_auth_headers_are_sent(self, db):
        """Etsy needs the app key *and* the bearer token; one alone is a 403."""
        await credentials.save(
            db,
            PROVIDER_ETSY,
            {
                "keystring": "my-keystring",
                "access_token": "1.token",
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
            },
        )
        await db.commit()
        client = await etsy_api.client_for(db)
        headers = await client._headers()
        assert headers["x-api-key"] == "my-keystring"
        assert headers["Authorization"] == "Bearer 1.token"

    async def test_scopes_cover_receipts_and_shops(self):
        url = etsy_api.authorize_url(
            keystring="k", redirect_uri="https://x/cb", state="s", code_challenge="c"
        )
        assert "transactions_r" in url
        assert "shops_r" in url


class TestRetryBehaviour:
    async def test_403_is_not_retried(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(403, json={"error": "insufficient_scope"})

        from app.integrations.base import request as do_request

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            with pytest.raises(AuthExpiredError) as excinfo:
                await do_request(client, "GET", "/thing", provider="etsy", base_delay=0.01)
        assert attempts["n"] == 1
        assert "insufficient_scope" in str(excinfo.value)


class TestSharedSecretErrorIsExplained:
    """Etsy answers some endpoints with:

        {"error":"Shared secret is required in x-api-key header."}

    Sending the shared secret instead was tried against the live API and Etsy
    rejected *that* with "API key not found or not active", so the secret is
    not an API key and swapping it only produces a more misleading error. Keep
    the keystring and explain what the error usually means.
    """

    async def test_the_error_is_recognised(self):
        assert etsy_api.wants_shared_secret(
            '{"error":"Shared secret is required in x-api-key header."}'
        )

    async def test_unrelated_errors_are_not(self):
        assert not etsy_api.wants_shared_secret('{"error":"insufficient_scope"}')
        assert not etsy_api.wants_shared_secret(None)

    async def _connected(self, db):
        await credentials.save(
            db,
            PROVIDER_ETSY,
            {
                "keystring": "the-keystring",
                "shared_secret": "the-shared-secret",
                "access_token": "42.token",
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
            },
        )
        await db.commit()
        return await etsy_api.client_for(db)

    async def test_the_keystring_is_always_what_is_sent(self, db, monkeypatch):
        client = await self._connected(db)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-api-key"])
            return httpx.Response(
                403, json={"error": "Shared secret is required in x-api-key header."}
            )

        _patch_transport(monkeypatch, handler)
        with pytest.raises(AuthExpiredError):
            await client.shops_for_user(42)
        # Exactly one attempt, with the keystring. No credential shuffling.
        assert seen == ["the-keystring"]

    async def test_the_message_explains_the_likely_cause(self, db, monkeypatch):
        client = await self._connected(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, json={"error": "Shared secret is required in x-api-key header."}
            )

        _patch_transport(monkeypatch, handler)
        with pytest.raises(AuthExpiredError) as excinfo:
            await client.shops_for_user(42)
        text = str(excinfo.value)
        assert "not approved" in text
        # Etsy's own words are still carried through.
        assert "Shared secret is required" in text

    async def test_other_403s_pass_through_unchanged(self, db, monkeypatch):
        client = await self._connected(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "insufficient_scope"})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(AuthExpiredError) as excinfo:
            await client.shops_for_user(42)
        assert "insufficient_scope" in str(excinfo.value)
        assert "not approved" not in str(excinfo.value)


class TestShopSelectionNeverDeadEnds:
    async def test_shop_listing_reports_errors_instead_of_failing(
        self, signed_in, db, monkeypatch
    ):
        await credentials.save(
            db,
            PROVIDER_ETSY,
            {
                "keystring": "k",
                "shared_secret": "s",
                "access_token": "42.token",
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
            },
        )
        await db.commit()

        async def forbidden(self, path, params=None):
            raise AuthExpiredError(
                "etsy", "Authorisation was rejected", status_code=403, body='{"error":"nope"}'
            )

        monkeypatch.setattr(etsy_api.EtsyClient, "_get", forbidden)

        response = await signed_in.get("/api/integrations/etsy/shops")
        # 200 with an error field: the UI needs to render the manual entry path.
        assert response.status_code == 200
        body = response.json()
        assert body["shops"] == []
        assert "nope" in body["error"]

    async def test_a_shop_id_can_be_set_by_hand(self, signed_in, db):
        await credentials.save(
            db, PROVIDER_ETSY, {"keystring": "k", "access_token": "42.t", "expires_at": 9e9}
        )
        await db.commit()
        response = await signed_in.post(
            "/api/integrations/etsy/shop", json={"shop_id": 12345678, "shop_name": None}
        )
        assert response.status_code == 200
        stored = await credentials.load(db, PROVIDER_ETSY)
        assert stored["shop_id"] == 12345678

    async def test_lookup_failure_does_not_block_the_choice(self, signed_in, db, monkeypatch):
        await credentials.save(
            db, PROVIDER_ETSY, {"keystring": "k", "access_token": "42.t", "expires_at": 9e9}
        )
        await db.commit()

        async def forbidden(self, path, params=None):
            raise AuthExpiredError("etsy", "rejected", status_code=403, body="{}")

        monkeypatch.setattr(etsy_api.EtsyClient, "_get", forbidden)
        response = await signed_in.post(
            "/api/integrations/etsy/shop/lookup", json={"shop_id": 12345678}
        )
        assert response.status_code == 200
        assert response.json()["found"] is False


class TestConnectionTest:
    """The receipts feed is the only Etsy call the product depends on."""

    async def _connected(self, db, shop_id=None):
        payload = {
            "keystring": "k",
            "shared_secret": "s",
            "access_token": "42.token",
            "refresh_token": "r",
            "expires_at": 9_999_999_999,
        }
        if shop_id:
            payload["shop_id"] = shop_id
        await credentials.save(db, PROVIDER_ETSY, payload)
        await db.commit()

    async def test_it_says_so_when_no_shop_is_set(self, signed_in, db):
        await self._connected(db)
        body = (await signed_in.post("/api/integrations/etsy/test")).json()
        assert body["ok"] is False
        assert "No shop selected" in body["detail"]

    async def test_a_working_receipt_feed_reports_success(self, signed_in, db, monkeypatch):
        await self._connected(db, shop_id=4242)

        async def receipts(self, **kwargs):
            assert kwargs["shop_id"] == 4242
            return [{"receipt_id": 1}]

        monkeypatch.setattr(etsy_api.EtsyClient, "iter_receipts", receipts)
        body = (await signed_in.post("/api/integrations/etsy/test")).json()
        assert body["ok"] is True
        assert "4242" in body["detail"]

        # Success clears any stale error banner.
        record = await credentials.get_record(db, PROVIDER_ETSY)
        assert record.last_error is None

    async def test_a_failing_feed_reports_the_reason(self, signed_in, db, monkeypatch):
        await self._connected(db, shop_id=4242)

        async def receipts(self, **kwargs):
            raise AuthExpiredError(
                "etsy",
                "Authorisation was rejected",
                status_code=403,
                body='{"error":"insufficient_scope"}',
            )

        monkeypatch.setattr(etsy_api.EtsyClient, "iter_receipts", receipts)
        body = (await signed_in.post("/api/integrations/etsy/test")).json()
        assert body["ok"] is False
        assert "insufficient_scope" in body["detail"]

        record = await credentials.get_record(db, PROVIDER_ETSY)
        assert "insufficient_scope" in record.last_error


class TestDiagnostics:
    """When Etsy's 403s contradict each other, ask Etsy rather than guess."""

    async def _connected(self, db):
        await credentials.save(
            db,
            PROVIDER_ETSY,
            {
                "keystring": "keystring-abcdefgh",
                "shared_secret": "secret-12345678",
                "access_token": "42.tokenvalue",
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
                "shop_id": 4242,
            },
        )
        await db.commit()

    async def test_it_tries_each_header_combination(self, signed_in, db, monkeypatch):
        await self._connected(db)
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.headers.get("x-api-key", "")))
            return httpx.Response(403, json={"error": "nope"})

        _patch_transport(monkeypatch, handler)
        body = (await signed_in.post("/api/integrations/etsy/diagnose")).json()

        keys_tried = {key for _, key in seen}
        assert "keystring-abcdefgh" in keys_tried
        assert "secret-12345678" in keys_tried
        assert "keystring-abcdefgh:secret-12345678" in keys_tried
        # Receipts is the endpoint that matters, so it must be covered.
        assert any("/receipts" in path for path, _ in seen)
        assert "Etsy rejected every combination" in body["summary"]

    async def test_a_working_combination_is_identified(self, signed_in, db, monkeypatch):
        await self._connected(db)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("x-api-key") == "keystring-abcdefgh:secret-12345678":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(403, json={"error": "nope"})

        _patch_transport(monkeypatch, handler)
        body = (await signed_in.post("/api/integrations/etsy/diagnose")).json()
        assert body["working"]
        assert all("keystring:secret" in w for w in body["working"])
        assert "combinations worked" in body["summary"]

    async def test_credentials_are_only_ever_shown_truncated(self, signed_in, db, monkeypatch):
        await self._connected(db)
        _patch_transport(monkeypatch, lambda r: httpx.Response(403, json={"error": "x"}))
        response = await signed_in.post("/api/integrations/etsy/diagnose")
        text = response.text
        assert "keystring-abcdefgh" not in text
        assert "secret-12345678" not in text
        creds = response.json()["credentials"]
        assert creds["keystring"].startswith("keys")
        assert "18 chars" in creds["keystring"]

    async def test_errors_name_the_failing_endpoint(self, db, monkeypatch):
        await self._connected(db)
        client = await etsy_api.client_for(db)
        _patch_transport(
            monkeypatch, lambda r: httpx.Response(403, json={"error": "insufficient_scope"})
        )
        with pytest.raises(AuthExpiredError) as excinfo:
            await client.shops_for_user(42)
        assert "/users/42/shops" in str(excinfo.value)
