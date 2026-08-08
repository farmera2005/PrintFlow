"""Connect, validate, and re-authorise the four external platforms (§2, §4)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..integrations import bambuddy as bambuddy_api
from ..integrations import etsy as etsy_api
from ..integrations import qbo as qbo_api
from ..integrations import shipstation as ss_api
from ..integrations.base import IntegrationError
from ..models import (
    PROVIDER_BAMBUDDY,
    PROVIDER_ETSY,
    PROVIDER_QBO,
    PROVIDER_SHIPSTATION,
    PROVIDERS,
    OAuthState,
    User,
)
from ..services import credentials
from ..services.credentials import IntegrationNotConfigured
from ..services.settings_store import KEY_PUBLIC_BASE_URL, get_setting

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

OAUTH_STATE_TTL = timedelta(minutes=30)


async def _base_url(request: Request, session: AsyncSession) -> str:
    configured = await get_setting(session, KEY_PUBLIC_BASE_URL)
    if configured:
        return str(configured).rstrip("/")
    return str(request.base_url).rstrip("/")


async def _redirect_uri(request: Request, session: AsyncSession, provider: str) -> str:
    return f"{await _base_url(request, session)}/api/integrations/{provider}/callback"


async def _new_state(
    session: AsyncSession, provider: str, redirect_uri: str, code_verifier: str | None
) -> str:
    await session.execute(
        delete(OAuthState).where(
            OAuthState.created_at < datetime.now(timezone.utc) - OAUTH_STATE_TTL
        )
    )
    state = secrets.token_urlsafe(32)
    session.add(
        OAuthState(
            state=state,
            provider=provider,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
    )
    await session.flush()
    return state


async def _consume_state(session: AsyncSession, provider: str, state: str) -> OAuthState:
    row = await session.get(OAuthState, state)
    if row is None or row.provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired OAuth state")
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > OAUTH_STATE_TTL:
        await session.delete(row)
        await session.flush()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth state expired; start again")
    await session.delete(row)
    await session.flush()
    return row


def _ui_redirect(base_url: str, provider: str, ok: bool, message: str = "") -> RedirectResponse:
    status_word = "connected" if ok else "error"
    target = f"{base_url}/#/settings?provider={provider}&result={status_word}"
    if message:
        from urllib.parse import quote

        target += f"&message={quote(message[:300])}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


@router.get("")
async def list_integrations(
    request: Request,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {
        "integrations": await credentials.status_summary(session),
        "redirect_uris": {
            provider: await _redirect_uri(request, session, provider)
            for provider in (PROVIDER_ETSY, PROVIDER_QBO)
        },
        "base_url": await _base_url(request, session),
    }


@router.delete("/{provider}")
async def disconnect(
    provider: str,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown provider")
    await credentials.delete(session, provider)
    await session.commit()
    return {"ok": True}


@router.post("/{provider}/retry")
async def retry_now(
    provider: str,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """"Retry now" button on the failure banner — runs that provider's job immediately."""
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown provider")
    from ..scheduler import run_job_now

    result = await run_job_now(provider)
    return result


# --------------------------------------------------------------------------
# Etsy
# --------------------------------------------------------------------------


class EtsyStartRequest(BaseModel):
    keystring: str = Field(min_length=4)
    shared_secret: str = Field(min_length=4)


@router.post("/etsy/start")
async def etsy_start(
    body: EtsyStartRequest,
    request: Request,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    redirect_uri = await _redirect_uri(request, session, PROVIDER_ETSY)
    verifier, challenge = etsy_api.make_pkce_pair()
    state = await _new_state(session, PROVIDER_ETSY, redirect_uri, verifier)
    payload = await credentials.load(session, PROVIDER_ETSY)
    payload.update(
        {"keystring": body.keystring.strip(), "shared_secret": body.shared_secret.strip()}
    )
    await credentials.save(session, PROVIDER_ETSY, payload, mark_connected=False)
    await session.commit()
    return {
        "authorize_url": etsy_api.authorize_url(
            keystring=body.keystring.strip(),
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
        ),
        "redirect_uri": redirect_uri,
    }


@router.get("/etsy/callback")
async def etsy_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    base_url = await _base_url(request, session)
    if error or not code or not state:
        return _ui_redirect(base_url, PROVIDER_ETSY, False, error or "Authorisation cancelled")
    try:
        row = await _consume_state(session, PROVIDER_ETSY, state)
        payload = await credentials.load(session, PROVIDER_ETSY)
        tokens = await etsy_api.exchange_code(
            keystring=str(payload.get("keystring")),
            redirect_uri=row.redirect_uri,
            code=code,
            code_verifier=str(row.code_verifier),
        )
        payload.update(tokens)
        await credentials.save(session, PROVIDER_ETSY, payload)

        # Pre-select the shop when the account only has one.
        client = etsy_api.EtsyClient(session, payload)
        me = await client.me()
        user_id = me.get("user_id") or me.get("shop_id")
        payload["user_id"] = user_id
        shops = await client.shops_for_user(user_id) if user_id else []
        if len(shops) == 1:
            payload["shop_id"] = shops[0].get("shop_id")
            payload["shop_name"] = shops[0].get("shop_name")
        await credentials.save(session, PROVIDER_ETSY, payload)
        await session.commit()
    except (IntegrationError, HTTPException) as exc:
        await session.rollback()
        await credentials.mark_error(session, PROVIDER_ETSY, str(exc))
        await session.commit()
        return _ui_redirect(base_url, PROVIDER_ETSY, False, str(exc))
    return _ui_redirect(base_url, PROVIDER_ETSY, True)


@router.get("/etsy/shops")
async def etsy_shops(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    client = await _etsy_client(session)
    payload = client.payload
    user_id = payload.get("user_id")
    if not user_id:
        me = await client.me()
        user_id = me.get("user_id") or me.get("shop_id")
        await credentials.merge(session, PROVIDER_ETSY, {"user_id": user_id})
        await session.commit()
    shops = await client.shops_for_user(user_id)
    return {
        "shops": [
            {"shop_id": s.get("shop_id"), "shop_name": s.get("shop_name")} for s in shops
        ],
        "selected_shop_id": payload.get("shop_id"),
    }


class EtsyShopRequest(BaseModel):
    shop_id: int
    shop_name: str | None = None


@router.post("/etsy/shop")
async def etsy_select_shop(
    body: EtsyShopRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await credentials.merge(
        session,
        PROVIDER_ETSY,
        {"shop_id": body.shop_id, "shop_name": body.shop_name},
    )
    await credentials.mark_ok(session, PROVIDER_ETSY)
    await session.commit()
    return {"shop_id": body.shop_id}


async def _etsy_client(session: AsyncSession) -> etsy_api.EtsyClient:
    try:
        return await etsy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# --------------------------------------------------------------------------
# QuickBooks Online
# --------------------------------------------------------------------------


class QboStartRequest(BaseModel):
    client_id: str = Field(min_length=4)
    client_secret: str = Field(min_length=4)
    environment: str = Field(default="production", pattern="^(production|sandbox)$")


@router.post("/qbo/start")
async def qbo_start(
    body: QboStartRequest,
    request: Request,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    redirect_uri = await _redirect_uri(request, session, PROVIDER_QBO)
    state = await _new_state(session, PROVIDER_QBO, redirect_uri, None)
    payload = await credentials.load(session, PROVIDER_QBO)
    payload.update(
        {
            "client_id": body.client_id.strip(),
            "client_secret": body.client_secret.strip(),
            "environment": body.environment,
        }
    )
    await credentials.save(session, PROVIDER_QBO, payload, mark_connected=False)
    await session.commit()
    return {
        "authorize_url": qbo_api.authorize_url(
            client_id=body.client_id.strip(), redirect_uri=redirect_uri, state=state
        ),
        "redirect_uri": redirect_uri,
    }


@router.get("/qbo/callback")
async def qbo_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    realmId: str | None = None,  # noqa: N803 - Intuit's parameter name
    error: str | None = None,
):
    base_url = await _base_url(request, session)
    if error or not code or not state:
        return _ui_redirect(base_url, PROVIDER_QBO, False, error or "Authorisation cancelled")
    try:
        row = await _consume_state(session, PROVIDER_QBO, state)
        payload = await credentials.load(session, PROVIDER_QBO)
        tokens = await qbo_api.exchange_code(
            client_id=str(payload.get("client_id")),
            client_secret=str(payload.get("client_secret")),
            redirect_uri=row.redirect_uri,
            code=code,
        )
        payload.update(tokens)
        if realmId:
            payload["realm_id"] = realmId
        await credentials.save(session, PROVIDER_QBO, payload)

        client = qbo_api.QboClient(session, payload)
        try:
            info = await client.company_info()
            payload["company_name"] = info.get("CompanyName")
            await credentials.save(session, PROVIDER_QBO, payload)
        except IntegrationError:
            pass  # connection still valid; company name is cosmetic
        await session.commit()
    except (IntegrationError, HTTPException) as exc:
        await session.rollback()
        await credentials.mark_error(session, PROVIDER_QBO, str(exc))
        await session.commit()
        return _ui_redirect(base_url, PROVIDER_QBO, False, str(exc))
    return _ui_redirect(base_url, PROVIDER_QBO, True)


@router.get("/qbo/items")
async def qbo_items(
    q: str = "",
    limit: int = 50,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Searchable QBO item picker used by the Products screen."""
    try:
        client = await qbo_api.client_for(session)
        items = await client.search_items(q, limit)
        await credentials.mark_ok(session, PROVIDER_QBO)
        await session.commit()
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_QBO, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {
        "items": [
            {
                "id": item.get("Id"),
                "name": item.get("Name"),
                "sku": item.get("Sku"),
                "type": item.get("Type"),
                "qty_on_hand": qbo_api.item_qty_on_hand(item),
                "tracked": bool(item.get("TrackQtyOnHand")),
            }
            for item in items
        ]
    }


# --------------------------------------------------------------------------
# Bambuddy
# --------------------------------------------------------------------------


class BambuddyConfigRequest(BaseModel):
    base_url: str = Field(min_length=4)
    api_key: str = ""
    auth_header: str | None = None
    paths: dict[str, str] | None = None
    fields: dict[str, str] | None = None


@router.post("/bambuddy/config")
async def bambuddy_config(
    body: BambuddyConfigRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Validate with a ping to the API and list the printers found (§2)."""
    existing = await credentials.load(session, PROVIDER_BAMBUDDY)
    payload: dict[str, Any] = {
        **existing,
        "base_url": body.base_url.strip().rstrip("/"),
        "api_key": body.api_key.strip() or existing.get("api_key", ""),
    }
    if body.auth_header:
        payload["auth_header"] = body.auth_header.strip()
    if body.paths:
        payload["paths"] = {**(existing.get("paths") or {}), **body.paths}
    if body.fields:
        payload["fields"] = {**(existing.get("fields") or {}), **body.fields}

    client = bambuddy_api.BambuddyClient(payload)
    spec: dict[str, Any] = {}
    try:
        spec = await client.fetch_openapi()
    except IntegrationError as exc:
        # The spec is informational; a working printers endpoint is the real test.
        spec = {"warning": str(exc)}
    try:
        printers = await client.list_printers()
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    payload["api_version"] = spec.get("version")
    payload["openapi"] = spec
    payload["printers"] = printers
    await credentials.save(session, PROVIDER_BAMBUDDY, payload)
    await session.commit()
    return {"printers": printers, "openapi": spec}


@router.get("/bambuddy/printers")
async def bambuddy_printers(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    client = await _bambuddy_client(session)
    try:
        printers = await client.list_printers()
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"printers": printers}


@router.get("/bambuddy/archives")
async def bambuddy_archives(
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Archive browser behind the mapping helper — no manual ID entry (§4.3)."""
    client = await _bambuddy_client(session)
    try:
        archives = await client.list_archives(
            search=search, limit=max(1, min(limit, 200)), offset=max(0, offset)
        )
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"archives": archives}


async def _bambuddy_client(session: AsyncSession) -> bambuddy_api.BambuddyClient:
    try:
        return await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# --------------------------------------------------------------------------
# ShipStation
# --------------------------------------------------------------------------


class ShipStationConfigRequest(BaseModel):
    api_key: str = Field(min_length=4)
    api_secret: str = Field(min_length=4)


@router.post("/shipstation/config")
async def shipstation_config(
    body: ShipStationConfigRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    existing = await credentials.load(session, PROVIDER_SHIPSTATION)
    payload = {
        **existing,
        "api_key": body.api_key.strip(),
        "api_secret": body.api_secret.strip(),
    }
    client = ss_api.ShipStationClient(payload)
    try:
        stores = await client.list_stores()
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_SHIPSTATION, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    payload["stores"] = stores
    await credentials.save(session, PROVIDER_SHIPSTATION, payload)
    await session.commit()
    return {"stores": stores}


@router.get("/shipstation/stores")
async def shipstation_stores(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    payload = await credentials.load(session, PROVIDER_SHIPSTATION)
    if not payload:
        raise HTTPException(status.HTTP_409_CONFLICT, "ShipStation is not connected")
    client = ss_api.ShipStationClient(payload)
    try:
        stores = await client.list_stores()
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"stores": stores, "selected_store_id": payload.get("store_id")}


@router.get("/shipstation/services")
async def shipstation_services(
    carrier: str,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    client = await _shipstation_client(session)
    try:
        services = await client.list_services(carrier)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {
        "services": [
            {"code": s.get("code"), "name": s.get("name")}
            for s in services
            if isinstance(s, dict)
        ]
    }


@router.get("/shipstation/packages")
async def shipstation_packages(
    carrier: str,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    client = await _shipstation_client(session)
    try:
        packages = await client.list_packages(carrier)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {
        "packages": [
            {"code": p.get("code"), "name": p.get("name")}
            for p in packages
            if isinstance(p, dict)
        ]
    }


async def _shipstation_client(session: AsyncSession) -> ss_api.ShipStationClient:
    try:
        return await ss_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


class ShipStationStoreRequest(BaseModel):
    store_id: int
    store_name: str | None = None


@router.post("/shipstation/store")
async def shipstation_select_store(
    body: ShipStationStoreRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Pick which ShipStation store is the Etsy one (§2)."""
    await credentials.merge(
        session,
        PROVIDER_SHIPSTATION,
        {"store_id": body.store_id, "store_name": body.store_name},
    )
    await credentials.mark_ok(session, PROVIDER_SHIPSTATION)
    await session.commit()
    return {"store_id": body.store_id}


@router.get("/oauth-states/count")
async def oauth_state_count(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    total = len((await session.execute(select(OAuthState.state))).all())
    return {"pending": total}
