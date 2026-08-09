"""Etsy Open API v3 client — read only.

This platform never writes to Etsy. ShipStation's own Etsy store connection is
what pushes tracking back to the buyer (§4.1, §4.4).
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PROVIDER_ETSY
from ..services import credentials
from .base import AuthExpiredError, IntegrationError, new_client, request

CONNECT_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"
SCOPES = ("transactions_r", "shops_r")

# Refresh a little before expiry so a poll never fails on a stale token.
REFRESH_MARGIN_SECONDS = 300


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for an S256 PKCE handshake."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(*, keystring: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": keystring,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{CONNECT_URL}?{query}"


async def exchange_code(
    *, keystring: str, redirect_uri: str, code: str, code_verifier: str
) -> dict[str, Any]:
    async with new_client() as client:
        response = await request(
            client,
            "POST",
            TOKEN_URL,
            provider=PROVIDER_ETSY,
            json={
                "grant_type": "authorization_code",
                "client_id": keystring,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
        )
    return _token_payload(response.json())


async def refresh_tokens(*, keystring: str, refresh_token: str) -> dict[str, Any]:
    async with new_client() as client:
        response = await request(
            client,
            "POST",
            TOKEN_URL,
            provider=PROVIDER_ETSY,
            json={
                "grant_type": "refresh_token",
                "client_id": keystring,
                "refresh_token": refresh_token,
            },
        )
    return _token_payload(response.json())


def _token_payload(data: dict[str, Any]) -> dict[str, Any]:
    if "access_token" not in data:
        raise IntegrationError(PROVIDER_ETSY, f"Token response missing access_token: {data}")
    return {
        "access_token": data["access_token"],
        # Etsy rotates refresh tokens: always persist the new one (§4.1).
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + float(data.get("expires_in", 3600)),
    }


def user_id_from_token(access_token: str | None) -> str | None:
    """Etsy access tokens are `<user_id>.<random>`.

    Reading the id straight off the token avoids a call to /users/me, which is
    one more endpoint that can 403 on a scope we do not otherwise need.
    """
    if not access_token:
        return None
    prefix = str(access_token).split(".", 1)[0]
    return prefix if prefix.isdigit() else None


class EtsyClient:
    def __init__(self, session: AsyncSession, payload: dict[str, Any]) -> None:
        self.session = session
        self.payload = payload

    @property
    def shop_id(self) -> int | None:
        shop_id = self.payload.get("shop_id")
        return int(shop_id) if shop_id else None

    async def _access_token(self) -> str:
        expires_at = float(self.payload.get("expires_at") or 0)
        if expires_at - REFRESH_MARGIN_SECONDS <= time.time():
            await self._refresh()
        token = self.payload.get("access_token")
        if not token:
            raise AuthExpiredError(PROVIDER_ETSY, "No Etsy access token stored")
        return str(token)

    async def _refresh(self) -> None:
        keystring = self.payload.get("keystring")
        refresh_token = self.payload.get("refresh_token")
        if not keystring or not refresh_token:
            raise AuthExpiredError(PROVIDER_ETSY, "Etsy refresh token missing; reconnect Etsy")
        tokens = await refresh_tokens(keystring=str(keystring), refresh_token=str(refresh_token))
        self.payload["access_token"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            self.payload["refresh_token"] = tokens["refresh_token"]
        self.payload["expires_at"] = tokens["expires_at"]
        await credentials.merge(
            self.session,
            PROVIDER_ETSY,
            {
                "access_token": self.payload["access_token"],
                "refresh_token": self.payload["refresh_token"],
                "expires_at": self.payload["expires_at"],
            },
        )

    async def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": str(self.payload.get("keystring", "")),
            "Authorization": f"Bearer {await self._access_token()}",
            "Accept": "application/json",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = await self._headers()
        async with new_client(base_url=API_BASE) as client:
            response = await request(
                client, "GET", path, provider=PROVIDER_ETSY, headers=headers, params=params
            )
        return response.json()

    async def me(self) -> dict[str, Any]:
        return await self._get("/users/me")

    async def shops_for_user(self, user_id: int | str) -> list[dict[str, Any]]:
        data = await self._get(f"/users/{user_id}/shops")
        # Etsy returns either a bare shop object or a paginated results list.
        if isinstance(data, dict) and "results" in data:
            return list(data.get("results") or [])
        return [data] if data else []

    async def get_shop(self, shop_id: int | str) -> dict[str, Any]:
        return await self._get(f"/shops/{shop_id}")

    async def iter_receipts(
        self,
        *,
        shop_id: int | str,
        was_shipped: bool | None = False,
        min_created: int | None = None,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch receipts, following Etsy's offset pagination to the end."""
        receipts: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": offset,
                "sort_on": "created",
                "sort_order": "desc",
            }
            if was_shipped is not None:
                params["was_shipped"] = str(was_shipped).lower()
            if min_created:
                params["min_created"] = int(min_created)
            data = await self._get(f"/shops/{shop_id}/receipts", params)
            page = list(data.get("results") or [])
            receipts.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return receipts


async def client_for(session: AsyncSession) -> EtsyClient:
    payload = await credentials.require(session, PROVIDER_ETSY)
    return EtsyClient(session, payload)
