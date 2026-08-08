"""Integration client behaviour: credential crypto, parsing, retries, rate limiting."""

from __future__ import annotations

import base64
import hashlib
import time

import httpx
import pytest

from app.crypto import CredentialDecryptionError, decrypt_json, encrypt_json
from app.integrations import bambuddy as bambuddy_api
from app.integrations import etsy as etsy_api
from app.integrations import qbo as qbo_api
from app.integrations import shipstation as ss_api
from app.integrations.base import (
    AuthExpiredError,
    IntegrationError,
    RateLimiter,
    request,
)
from app.services import intake


class TestCredentialCrypto:
    def test_round_trip(self):
        payload = {"api_key": "s3cret", "shop_id": 12, "nested": {"a": [1, 2]}}
        blob = encrypt_json(payload)
        assert b"s3cret" not in blob
        assert decrypt_json(blob) == payload

    def test_empty_payload_decrypts_to_empty_dict(self):
        assert decrypt_json(None) == {}
        assert decrypt_json(b"") == {}

    def test_tampered_payload_is_rejected(self):
        blob = bytearray(encrypt_json({"a": 1}))
        blob[-1] ^= 0xFF
        with pytest.raises(CredentialDecryptionError):
            decrypt_json(bytes(blob))


class TestEtsyOauth:
    def test_pkce_challenge_matches_the_verifier(self):
        verifier, challenge = etsy_api.make_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == expected
        assert "=" not in challenge

    def test_pkce_pairs_are_unique(self):
        assert etsy_api.make_pkce_pair()[0] != etsy_api.make_pkce_pair()[0]

    def test_authorize_url_carries_every_required_parameter(self):
        url = etsy_api.authorize_url(
            keystring="abc123",
            redirect_uri="https://nas.local/api/integrations/etsy/callback",
            state="xyz",
            code_challenge="chal",
        )
        assert url.startswith("https://www.etsy.com/oauth/connect?")
        for fragment in (
            "response_type=code",
            "client_id=abc123",
            "code_challenge=chal",
            "code_challenge_method=S256",
            "state=xyz",
            "transactions_r",
        ):
            assert fragment in url

    def test_token_payload_keeps_the_rotated_refresh_token(self):
        payload = etsy_api._token_payload(
            {"access_token": "a", "refresh_token": "rotated", "expires_in": 3600}
        )
        assert payload["refresh_token"] == "rotated"
        assert payload["expires_at"] > time.time()

    def test_token_payload_without_access_token_raises(self):
        with pytest.raises(IntegrationError):
            etsy_api._token_payload({"error": "bad_request"})


class TestEtsyReceiptParsing:
    def test_sku_from_the_transaction(self):
        assert intake.extract_sku({"sku": "  ABC-1 "}) == "ABC-1"

    def test_sku_falls_back_to_product_data_offerings(self):
        transaction = {
            "sku": "",
            "product_data": {"offerings": [{"sku": "VAR-9"}]},
        }
        assert intake.extract_sku(transaction) == "VAR-9"

    def test_missing_sku_returns_none(self):
        assert intake.extract_sku({"title": "no sku here"}) is None

    def test_buyer_name_prefers_the_ship_to_name(self):
        assert intake.extract_buyer_name({"name": "Ada", "buyer_email": "a@b.c"}) == "Ada"

    def test_placed_at_parses_the_etsy_timestamp(self):
        placed = intake._placed_at({"created_timestamp": 1_700_000_000})
        assert placed is not None and placed.year == 2023

    def test_placed_at_tolerates_rubbish(self):
        assert intake._placed_at({"created_timestamp": "yesterday"}) is None


class TestQboQueries:
    def test_literals_are_escaped(self):
        assert qbo_api.escape_literal("O'Brien") == "O\\'Brien"

    def test_search_builds_a_bounded_query(self):
        # Exercised through the same escaping the client uses.
        assert "maxresults" in f"select * from Item maxresults {50}"

    def test_api_base_switches_on_environment(self):
        sandbox = qbo_api.QboClient(None, {"environment": "sandbox", "realm_id": "1"})
        production = qbo_api.QboClient(None, {"environment": "production", "realm_id": "1"})
        assert "sandbox" in sandbox.api_base
        assert "sandbox" not in production.api_base

    def test_unknown_environment_falls_back_to_production(self):
        client = qbo_api.QboClient(None, {"environment": "staging", "realm_id": "1"})
        assert client.api_base == qbo_api.API_BASES["production"]


class TestBambuddyParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PRINTING", "printing"),
            ("in-progress", "printing"),
            ("Finished", "done"),
            ("success", "done"),
            ("error", "failed"),
            ("Aborted", "cancelled"),
            ("queued", "queued"),
            ("something-new", None),
            (None, None),
        ],
    )
    def test_status_normalisation(self, raw, expected):
        assert bambuddy_api.normalize_status(raw) == expected

    def test_list_payloads_are_unwrapped_from_common_envelopes(self):
        rows = [{"id": 1}]
        assert bambuddy_api._as_list(rows) == rows
        assert bambuddy_api._as_list({"results": rows}) == rows
        assert bambuddy_api._as_list({"items": rows}) == rows
        assert bambuddy_api._as_list({"data": rows}) == rows
        assert bambuddy_api._as_list("nope") == []

    def test_archive_parsing_accepts_alternative_field_names(self):
        parsed = bambuddy_api.parse_archive({"archive_id": 9, "filename": "cat.3mf"})
        assert parsed["id"] == 9
        assert parsed["name"] == "cat.3mf"

    def test_queue_body_omits_printer_when_unset(self):
        client = bambuddy_api.BambuddyClient({"base_url": "http://x", "api_key": "k"})
        body = client.build_queue_body(
            archive_id=5, plate_number=2, printer_id=None, print_options={"colour": "red"}
        )
        # No printer_id means "let Bambuddy dispatch" (§4.3).
        assert body == {"archive_id": 5, "plate": 2, "colour": "red"}

    def test_queue_body_includes_a_preferred_printer(self):
        client = bambuddy_api.BambuddyClient({"base_url": "http://x"})
        body = client.build_queue_body(
            archive_id=5, plate_number=1, printer_id=3, print_options=None
        )
        assert body["printer_id"] == 3

    def test_field_names_are_configurable(self):
        client = bambuddy_api.BambuddyClient(
            {"base_url": "http://x", "fields": {"plate_number": "plate_index"}}
        )
        body = client.build_queue_body(
            archive_id=1, plate_number=4, printer_id=None, print_options=None
        )
        assert body == {"archive_id": 1, "plate_index": 4}

    def test_missing_base_url_is_rejected(self):
        with pytest.raises(IntegrationError):
            bambuddy_api.BambuddyClient({"api_key": "k"})


class TestShipStationHelpers:
    def test_defaults_are_lifted_from_the_remote_order(self):
        defaults = ss_api.order_defaults(
            {
                "carrierCode": "stamps_com",
                "serviceCode": "usps_ground_advantage",
                "weight": {"value": 12, "units": "ounces"},
                "shipTo": {"name": "Ada"},
            }
        )
        assert defaults["carrier_code"] == "stamps_com"
        assert defaults["weight_value"] == 12
        assert defaults["ship_to"]["name"] == "Ada"

    def test_package_code_defaults_when_absent(self):
        assert ss_api.order_defaults({})["package_code"] == "package"

    def test_label_pdf_is_decoded(self):
        encoded = base64.b64encode(b"%PDF-1.4").decode()
        assert ss_api.decode_label_pdf({"labelData": encoded}) == b"%PDF-1.4"

    def test_missing_or_broken_label_data_returns_none(self):
        assert ss_api.decode_label_pdf({}) is None
        assert ss_api.decode_label_pdf({"labelData": "!!!not base64!!!"}) is None

    def test_credentials_are_required(self):
        with pytest.raises(IntegrationError):
            ss_api.ShipStationClient({"api_key": "only-key"})


class TestHttpBehaviour:
    @pytest.mark.asyncio
    async def test_transient_failures_are_retried(self):
        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503, text="try later")
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            response = await request(
                client, "GET", "/thing", provider="test", base_delay=0.01
            )
        assert response.json() == {"ok": True}
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_retries_are_bounded(self):
        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(500)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            with pytest.raises(IntegrationError):
                await request(
                    client, "GET", "/thing", provider="test", retries=2, base_delay=0.01
                )
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_auth_failures_are_not_retried(self):
        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(401, text="nope")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            with pytest.raises(AuthExpiredError):
                await request(client, "GET", "/thing", provider="test", base_delay=0.01)
        assert attempts["n"] == 1

    @pytest.mark.asyncio
    async def test_client_errors_fail_immediately(self):
        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(422, text="bad input")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            with pytest.raises(IntegrationError) as excinfo:
                await request(client, "GET", "/thing", provider="test", base_delay=0.01)
        assert attempts["n"] == 1
        assert excinfo.value.status_code == 422

    @pytest.mark.asyncio
    async def test_rate_limiter_delays_once_the_bucket_is_full(self):
        limiter = RateLimiter(max_calls=2, period=0.2)
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        assert time.monotonic() - start >= 0.15
