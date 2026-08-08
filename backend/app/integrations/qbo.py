"""QuickBooks Online client — read only.

QBO is the inventory system of record. This platform only ever reads QtyOnHand;
it never posts inventory adjustments (§4.2).
"""

from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PROVIDER_QBO
from ..services import credentials
from .base import AuthExpiredError, IntegrationError, new_client, request

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"

API_BASES = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}

# §6: refresh proactively at <10 minutes remaining.
REFRESH_MARGIN_SECONDS = 600


def authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def _token_request(
    *, client_id: str, client_secret: str, form: dict[str, str]
) -> dict[str, Any]:
    async with new_client() as client:
        response = await request(
            client,
            "POST",
            TOKEN_URL,
            provider=PROVIDER_QBO,
            headers={
                "Authorization": _basic_auth(client_id, client_secret),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=form,
        )
    data = response.json()
    if "access_token" not in data:
        raise IntegrationError(PROVIDER_QBO, f"Token response missing access_token: {data}")
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + float(data.get("expires_in", 3600)),
        "refresh_expires_at": time.time()
        + float(data.get("x_refresh_token_expires_in", 8726400)),
    }


async def exchange_code(
    *, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict[str, Any]:
    return await _token_request(
        client_id=client_id,
        client_secret=client_secret,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )


async def refresh_tokens(
    *, client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    return await _token_request(
        client_id=client_id,
        client_secret=client_secret,
        form={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )


def escape_literal(value: str) -> str:
    """Escape a value for a QBO query string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class QboClient:
    def __init__(self, session: AsyncSession, payload: dict[str, Any]) -> None:
        self.session = session
        self.payload = payload

    @property
    def realm_id(self) -> str | None:
        realm = self.payload.get("realm_id")
        return str(realm) if realm else None

    @property
    def api_base(self) -> str:
        env = str(self.payload.get("environment") or "production")
        return API_BASES.get(env, API_BASES["production"])

    async def _access_token(self) -> str:
        if float(self.payload.get("expires_at") or 0) - REFRESH_MARGIN_SECONDS <= time.time():
            await self.refresh()
        token = self.payload.get("access_token")
        if not token:
            raise AuthExpiredError(PROVIDER_QBO, "No QuickBooks access token stored")
        return str(token)

    async def refresh(self) -> None:
        client_id = self.payload.get("client_id")
        client_secret = self.payload.get("client_secret")
        refresh_token = self.payload.get("refresh_token")
        if not (client_id and client_secret and refresh_token):
            raise AuthExpiredError(
                PROVIDER_QBO, "QuickBooks refresh token missing; reconnect QuickBooks"
            )
        tokens = await refresh_tokens(
            client_id=str(client_id),
            client_secret=str(client_secret),
            refresh_token=str(refresh_token),
        )
        self.payload.update(
            {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token") or refresh_token,
                "expires_at": tokens["expires_at"],
                "refresh_expires_at": tokens["refresh_expires_at"],
            }
        )
        await credentials.merge(
            self.session,
            PROVIDER_QBO,
            {
                "access_token": self.payload["access_token"],
                "refresh_token": self.payload["refresh_token"],
                "expires_at": self.payload["expires_at"],
                "refresh_expires_at": self.payload["refresh_expires_at"],
            },
        )

    def seconds_until_expiry(self) -> float:
        return float(self.payload.get("expires_at") or 0) - time.time()

    async def query(self, statement: str) -> dict[str, Any]:
        if not self.realm_id:
            raise AuthExpiredError(PROVIDER_QBO, "No QuickBooks company (realm) stored")
        headers = {
            "Authorization": f"Bearer {await self._access_token()}",
            "Accept": "application/json",
        }
        async with new_client(base_url=self.api_base) as client:
            response = await request(
                client,
                "GET",
                f"/v3/company/{self.realm_id}/query",
                provider=PROVIDER_QBO,
                headers=headers,
                params={"query": statement, "minorversion": MINOR_VERSION},
            )
        return response.json().get("QueryResponse", {})

    async def company_info(self) -> dict[str, Any]:
        result = await self.query("select * from CompanyInfo")
        rows = result.get("CompanyInfo") or []
        return rows[0] if rows else {}

    async def get_items(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch items by Id, batched. Returns {item_id: item}."""
        out: dict[str, dict[str, Any]] = {}
        unique = [i for i in dict.fromkeys(str(i) for i in item_ids if i)]
        # QBO caps a query at 1000 results; 100 ids per batch keeps URLs sane.
        for start in range(0, len(unique), 100):
            batch = unique[start : start + 100]
            joined = ",".join(f"'{escape_literal(i)}'" for i in batch)
            result = await self.query(f"select * from Item where Id in ({joined})")
            for item in result.get("Item") or []:
                out[str(item.get("Id"))] = item
        return out

    async def search_items(self, term: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Item picker backing query."""
        limit = max(1, min(limit, 200))
        if term.strip():
            needle = escape_literal(term.strip())
            statement = (
                f"select * from Item where Name like '%{needle}%' "
                f"maxresults {limit}"
            )
        else:
            statement = f"select * from Item maxresults {limit}"
        result = await self.query(statement)
        return list(result.get("Item") or [])


def item_qty_on_hand(item: dict[str, Any]) -> float | None:
    """QtyOnHand is only meaningful for inventory-tracked items."""
    if not item.get("TrackQtyOnHand", False) and "QtyOnHand" not in item:
        return None
    raw = item.get("QtyOnHand")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def client_for(session: AsyncSession) -> QboClient:
    payload = await credentials.require(session, PROVIDER_QBO)
    return QboClient(session, payload)
