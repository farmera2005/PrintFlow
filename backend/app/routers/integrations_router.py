"""Connect, validate, and re-authorise the four external platforms (§2, §4)."""

from __future__ import annotations

import logging
import secrets
import time
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
from ..integrations import base as base_api
from ..integrations import etsy as etsy_api
from ..integrations import qbo as qbo_api
from ..integrations import shipstation as ss_api
from ..integrations import wix as wix_api
from ..integrations.base import IntegrationError
from ..models import (
    PROVIDER_BAMBUDDY,
    PROVIDER_ETSY,
    PROVIDER_QBO,
    PROVIDER_SHIPSTATION,
    PROVIDER_WIX,
    PROVIDERS,
    EtsyProductLink,
    OAuthState,
    User,
)
from ..services import audit, catalog, credentials, intake, public_url, settings_store
from ..services.credentials import IntegrationNotConfigured

log = logging.getLogger("printflow.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

OAUTH_STATE_TTL = timedelta(minutes=30)


async def _base_url(request: Request, session: AsyncSession) -> str:
    """The one public address, with the tunnel winning (see services/public_url)."""
    base, _source = await public_url.resolve_base_url(session, request)
    return base


async def _redirect_uri(request: Request, session: AsyncSession, provider: str) -> str:
    return public_url.redirect_uri(await _base_url(request, session), provider)


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
        await session.commit()

        # Everything past this point is convenience: the tokens are already
        # stored, so Etsy IS connected. Pre-selecting the shop must not be able
        # to undo that — a 403 on a lookup used to fail the whole handshake and
        # report it as "authorisation was rejected" when authorisation had in
        # fact succeeded.
        await _try_preselect_etsy_shop(session, payload)
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
    """List the account's shops.

    Returns an `error` instead of failing the request: if Etsy will not list
    shops, the operator still needs to be able to enter the shop id by hand
    rather than being stuck at an empty dropdown.
    """
    client = await _etsy_client(session)
    payload = client.payload
    result: dict[str, Any] = {
        "shops": [],
        "selected_shop_id": payload.get("shop_id"),
        "selected_shop_name": payload.get("shop_name"),
        "orders_since": payload.get("orders_since"),
        "error": None,
    }

    user_id = etsy_api.user_id_from_token(payload.get("access_token")) or payload.get(
        "user_id"
    )
    try:
        if not user_id:
            me = await client.me()
            user_id = me.get("user_id") or me.get("shop_id")
        if user_id and payload.get("user_id") != user_id:
            await credentials.merge(session, PROVIDER_ETSY, {"user_id": user_id})
            await session.commit()
        if not user_id:
            result["error"] = "Could not determine your Etsy user id."
            return result
        shops = await client.shops_for_user(user_id)
        result["shops"] = [
            {"shop_id": s.get("shop_id"), "shop_name": s.get("shop_name")}
            for s in shops
            if s.get("shop_id")
        ]
        if not result["shops"]:
            result["error"] = "Etsy returned no shops for this account."
    except IntegrationError as exc:
        await session.rollback()
        result["error"] = str(exc)
    return result


@router.get("/etsy/catalog")
async def etsy_catalog(
    state: str = "active",
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The shop's listings lined up against the product table.

    Returns 200 with an `error` rather than failing: the point of this screen is
    to be told what is wrong, and a token issued before the listings scope
    existed will land here — which is worth explaining, not just refusing.
    """
    client = await _etsy_client(session)
    empty = {"rows": [], "unused_products": [], "counts": {}, "notes": []}
    if not client.shop_id:
        return {**empty, "error": "No Etsy shop selected yet.", "needs_reconnect": False}
    try:
        listings, notes = await catalog.fetch_listings(client, state=state)
    except IntegrationError as exc:
        needs_reconnect = etsy_api.wants_listing_scope(exc)
        message = (
            "Etsy will not release your listings with the access this connection "
            "was granted. Reconnect Etsy — the authorisation screen now asks for "
            "permission to read listings as well as orders."
            if needs_reconnect
            else str(exc)
        )
        await session.rollback()
        return {**empty, "error": message, "needs_reconnect": needs_reconnect}

    result = await catalog.reconcile(session, listings)
    return {**result, "notes": notes, "error": None, "needs_reconnect": False}


class ImportListingsRequest(BaseModel):
    listing_ids: list[int] = Field(min_length=1, max_length=500)
    fulfillment: str = Field(pattern="^(printed|stocked)$")
    state: str = "active"


@router.post("/etsy/catalog/import")
async def etsy_catalog_import(
    body: ImportListingsRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Build products straight from Etsy listings, each already linked.

    The setup path for a shop that has never used SKUs: rather than inventing a
    code per listing and linking each one by hand, take the listings Etsy
    already describes and make the products from them.
    """
    client = await _etsy_client(session)
    if not client.shop_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "No Etsy shop selected yet.")
    try:
        listings, _notes = await catalog.fetch_listings(client, state=body.state)
    except IntegrationError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    result = await catalog.import_listings(
        session, listings, body.listing_ids, fulfillment=body.fulfillment
    )

    # Orders that stalled on these listings are the reason the products are
    # being made at all, so clear them in the same pass.
    fixed = 0
    for entry in result["created"]:
        link = (
            await session.execute(
                select(EtsyProductLink).where(
                    EtsyProductLink.etsy_listing_id == entry["listing_id"],
                    EtsyProductLink.etsy_product_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if link is not None:
            fixed += await intake.apply_etsy_link(session, link)

    await audit.record(
        session,
        entity_type="integration",
        entity_id=None,
        action="etsy_catalog_import",
        detail={
            "created": len(result["created"]),
            "skipped": len(result["skipped"]),
            "fulfillment": body.fulfillment,
            "also_fixed": fixed,
        },
        actor=user.username,
    )
    await session.commit()
    return {**result, "also_fixed": fixed}


@router.post("/etsy/test")
async def etsy_test(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Try the one Etsy call PrintFlow actually depends on: reading receipts.

    Listing shops is a convenience; fetching receipts is the integration. If
    this works the connection is good, whatever the shops endpoint says.
    """
    client = await _etsy_client(session)
    if not client.shop_id:
        return {
            "ok": False,
            "detail": "No shop selected yet — set the shop ID first.",
        }
    try:
        receipts = await client.iter_receipts(
            shop_id=client.shop_id, page_size=1, max_pages=1
        )
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_ETSY, str(exc))
        await session.commit()
        return {"ok": False, "detail": str(exc)}
    await credentials.mark_ok(session, PROVIDER_ETSY)
    await session.commit()
    return {
        "ok": True,
        "detail": (
            f"Read the receipt feed for shop {client.shop_id} successfully "
            f"({len(receipts)} unshipped receipt(s) on the first page). "
            "Order polling will work."
        ),
    }


def _hint(value: str | None) -> str:
    """Enough of a credential to identify it, not enough to use it."""
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return f"{value[:2]}…({len(value)} chars)"
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


@router.post("/etsy/diagnose")
async def etsy_diagnose(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Try each plausible header combination and report what Etsy answers.

    Etsy's 403s here have been contradictory — one endpoint asking for the
    shared secret in x-api-key, and the shared secret then being rejected as an
    unknown API key. Rather than keep guessing, ask Etsy directly and show the
    whole matrix.
    """
    client = await _etsy_client(session)
    payload = client.payload
    keystring = str(payload.get("keystring") or "")
    secret = str(payload.get("shared_secret") or "")
    user_id = etsy_api.user_id_from_token(payload.get("access_token")) or payload.get(
        "user_id"
    )

    endpoints: list[tuple[str, str]] = []
    if client.shop_id:
        # The one that matters: order polling.
        endpoints.append(("receipts", f"/shops/{client.shop_id}/receipts"))
        endpoints.append(("shop", f"/shops/{client.shop_id}"))
    if user_id:
        endpoints.append(("shops list", f"/users/{user_id}/shops"))
    endpoints.append(("me", "/users/me"))

    variants: list[tuple[str, str | None, bool]] = [
        ("keystring + bearer", keystring, True),
        ("shared secret + bearer", secret, True),
        ("keystring:secret + bearer", f"{keystring}:{secret}", True),
        ("keystring, no bearer", keystring, False),
    ]

    attempts: list[dict[str, Any]] = []
    for endpoint_label, path in endpoints:
        for variant_label, api_key, with_bearer in variants:
            result = await client.probe(path, api_key=api_key, with_bearer=with_bearer)
            attempts.append(
                {
                    "endpoint": endpoint_label,
                    "path": path,
                    "variant": variant_label,
                    "status": result["status"],
                    "body": result["body"],
                    "ok": result["status"] == 200,
                }
            )

    working = [a for a in attempts if a["ok"]]
    return {
        "credentials": {
            "keystring": _hint(keystring),
            "shared_secret": _hint(secret),
            "access_token": _hint(str(payload.get("access_token") or "")),
            "user_id": user_id,
            "shop_id": client.shop_id,
        },
        "attempts": attempts,
        "working": [f"{a['endpoint']} — {a['variant']}" for a in working],
        "summary": (
            f"{len(working)} of {len(attempts)} combinations worked."
            if working
            else "Etsy rejected every combination. The credentials or the app "
            "itself are the problem, not the header."
        ),
    }


class EtsyShopLookupRequest(BaseModel):
    shop_id: int


@router.post("/etsy/shop/lookup")
async def etsy_shop_lookup(
    body: EtsyShopLookupRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Check a hand-entered shop id, without making it a prerequisite."""
    client = await _etsy_client(session)
    try:
        shop = await client.get_shop(body.shop_id)
    except IntegrationError as exc:
        return {"found": False, "shop_name": None, "error": str(exc)}
    return {
        "found": True,
        "shop_name": shop.get("shop_name"),
        "error": None,
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
    payload = await credentials.load(session, PROVIDER_ETSY)
    updates: dict[str, Any] = {"shop_id": body.shop_id, "shop_name": body.shop_name}
    if not payload.get("orders_since"):
        # An established shop can have hundreds of open receipts. Without a
        # cutoff the first poll would import the lot, fill the board with
        # historical orders and plan prints for all of them. Start from now;
        # the operator can move it back deliberately to backfill.
        updates["orders_since"] = int(time.time())
    await credentials.merge(session, PROVIDER_ETSY, updates)
    await credentials.mark_ok(session, PROVIDER_ETSY)
    await session.commit()
    return {"shop_id": body.shop_id, "orders_since": updates.get("orders_since")}


class EtsyOrdersSinceRequest(BaseModel):
    # Epoch seconds; null means "import everything Etsy still lists as open".
    orders_since: int | None = None


@router.post("/etsy/orders-since")
async def etsy_set_orders_since(
    body: EtsyOrdersSinceRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await credentials.merge(
        session, PROVIDER_ETSY, {"orders_since": body.orders_since}
    )
    await audit.record(
        session,
        entity_type="settings",
        entity_id=None,
        action="etsy_orders_since_changed",
        detail={"orders_since": body.orders_since},
        actor=user.username,
    )
    await session.commit()
    return {"orders_since": body.orders_since}


async def _try_preselect_etsy_shop(session: AsyncSession, payload: dict) -> None:
    """Best effort: fill in the shop when the account has exactly one."""
    user_id = etsy_api.user_id_from_token(payload.get("access_token"))
    client = etsy_api.EtsyClient(session, payload)
    try:
        if not user_id:
            me = await client.me()
            user_id = me.get("user_id") or me.get("shop_id")
        if not user_id:
            return
        payload["user_id"] = user_id
        shops = await client.shops_for_user(user_id)
        if len(shops) == 1:
            payload["shop_id"] = shops[0].get("shop_id")
            payload["shop_name"] = shops[0].get("shop_name")
            if not payload.get("orders_since"):
                payload["orders_since"] = int(time.time())
        await credentials.save(session, PROVIDER_ETSY, payload)
        await session.commit()
    except (IntegrationError, Exception) as exc:  # noqa: B014 - never fail the connect
        await session.rollback()
        log.warning("Etsy connected, but the shop lookup failed: %s", exc)
        # Surface it without blocking: the Settings screen shows a shop picker.
        await credentials.mark_error(
            session,
            PROVIDER_ETSY,
            f"Connected, but could not list your shops automatically: {exc}. "
            "Pick the shop manually below.",
        )
        await session.commit()


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
    kind: str = "",
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Searchable QBO item picker used by the Products screen.

    `kind=non_stock` narrows it to items that can carry an invoice line without
    moving stock, which is what the income item on an invoice has to be.
    """
    if kind not in ("", "non_stock"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown item kind '{kind}'. Use 'non_stock' or leave it out.",
        )
    types = settings_store.NON_STOCK_ITEM_TYPES if kind == "non_stock" else ()
    try:
        client = await qbo_api.client_for(session)
        items = await client.search_items(q, limit, item_types=types)
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
                # Used to prefill a made-items line and to grey out the items a
                # Purchase cannot move.
                "purchase_cost": (
                    str(cost) if (cost := qbo_api.item_purchase_cost(item)) is not None else None
                ),
                "inventory": qbo_api.item_is_inventory(item),
                # Whether an invoice line naming this item would change quantity
                # on hand. The income-item picker refuses one that would.
                "moves_stock": qbo_api.item_moves_stock(item),
            }
            for item in items
        ]
    }


# A Purchase's AccountRef is the account the money came *out of*, so only these
# are valid there. Offering an expense account is what produced QuickBooks
# error 6430, "Invalid account type used".
PAYMENT_ACCOUNT_TYPES = ("Bank", "Credit Card")

# Where value added beyond the components goes — labour, machine time.
OFFSET_ACCOUNT_TYPES = (
    "Cost of Goods Sold",
    "Expense",
    "Other Expense",
    "Other Current Asset",
    "Other Current Liability",
)

ACCOUNT_ROLES = {
    "payment": PAYMENT_ACCOUNT_TYPES,
    "offset": OFFSET_ACCOUNT_TYPES,
    # Where the cost of a unit goes when a printed line takes it out of stock.
    "cogs": settings_store.COGS_ACCOUNT_TYPES,
    # Where a discount Etsy took off an order lands. Income, because a discount
    # is revenue not earned rather than a cost incurred — QuickBooks' own
    # default discount account is an income account for the same reason.
    "discount": ("Income", "Other Income"),
}


@router.get("/qbo/accounts")
async def qbo_accounts(
    role: str = "payment",
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Chart of accounts, filtered to what the chosen role can legally use."""
    types = ACCOUNT_ROLES.get(role)
    if types is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown account role '{role}'. Use one of {', '.join(ACCOUNT_ROLES)}.",
        )
    try:
        client = await qbo_api.client_for(session)
        accounts = await client.get_accounts(types)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {
        "accounts": [
            {
                "id": account.get("Id"),
                "name": account.get("FullyQualifiedName") or account.get("Name"),
                "type": account.get("AccountType"),
                "subtype": account.get("AccountSubType"),
            }
            for account in accounts
        ]
    }


@router.get("/qbo/vendors")
async def qbo_vendors(
    q: str = "",
    limit: int = 50,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Optional payee on a manufacturing posting."""
    try:
        client = await qbo_api.client_for(session)
        vendors = await client.search_vendors(q, limit)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {
        "vendors": [
            {"id": v.get("Id"), "name": v.get("DisplayName")} for v in vendors
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
    try:
        result = await client.validate()
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    spec: dict[str, Any] = result["openapi"]
    printers = result["printers"]
    adopted: dict[str, str] = result["adopted_paths"]

    # Persist what the instance's own spec said, so polling and queueing use the
    # same endpoints the validation just proved. Kept separate from the operator's
    # own `paths` so a later re-validation can move them.
    payload["discovered_paths"] = client.discovered_paths
    # "This build has no camera" was remembered so the farm screen would stop
    # asking. Re-validating is exactly the moment an upgrade would have added
    # one, so the question is allowed to be asked again.
    payload.pop("camera_checked", None)
    # The same for controls: an upgrade that added a Pause endpoint should show
    # up as a Pause button without anyone knowing to ask for it.
    payload.pop("controls_checked", None)
    payload["api_version"] = spec.get("version")
    payload["openapi"] = spec
    payload["printers"] = printers
    await credentials.save(session, PROVIDER_BAMBUDDY, payload)
    await session.commit()
    return {
        "printers": printers,
        "openapi": spec,
        "adopted_paths": adopted,
        "paths": client.paths,
    }


class BambuddyEndpointsRequest(BaseModel):
    base_url: str = Field(min_length=4)
    api_key: str = ""


@router.post("/bambuddy/endpoints")
async def bambuddy_endpoints(
    body: BambuddyEndpointsRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What this instance says it serves, for when our default paths 404.

    Reads the OpenAPI document only. Never fails the request: an operator
    staring at a 404 needs the list of real endpoints more than they need
    another error.
    """
    existing = await credentials.load(session, PROVIDER_BAMBUDDY)
    client = bambuddy_api.BambuddyClient(
        {
            **existing,
            "base_url": body.base_url.strip().rstrip("/"),
            "api_key": body.api_key.strip() or existing.get("api_key", ""),
        }
    )
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Reading the API description"):
            spec = await client.fetch_openapi()
    except IntegrationError as exc:
        return {"error": str(exc), "discovered": {}, "collections": [], "current": client.paths}
    return {
        "error": None,
        "spec_path": spec.get("path"),
        "version": spec.get("version"),
        "discovered": spec.get("discovered") or {},
        "collections": spec.get("collections") or [],
        "current": client.paths,
    }


@router.get("/bambuddy/printers")
async def bambuddy_printers(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    client = await _bambuddy_client(session)
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Listing printers"):
            printers = await bambuddy_api.with_healing(
                session, client, ("printers",), client.list_printers
            )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await session.commit()
    return {"printers": printers}


@router.get("/bambuddy/archives")
async def bambuddy_archives(
    search: str = "",
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every archived print file, so the picker is a list and not a guess.

    It used to return one page of fifty. A shop with more files than that could
    only reach the rest by typing a name it already knew, which is not a picker.
    """
    client = await _bambuddy_client(session)
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Listing archives"):
            archives, truncated = await bambuddy_api.with_healing(
                session, client, ("archives",), lambda: client.iter_archives(search=search)
            )
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    archives.sort(key=lambda row: str(row.get("name") or "").lower())
    return {"archives": archives, "truncated": truncated}


@router.get("/bambuddy/files")
async def bambuddy_files(
    printer_id: int | None = None,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bambuddy's file manager, folders and all.

    The whole structure in one call rather than a folder at a time: the picker
    needs to search across it, and a shop's library is a few thousand entries at
    most. `printer_id` reads one machine's files instead of the farm's.
    """
    client = await _bambuddy_client(session)
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Reading the file manager"):
            tree = await bambuddy_api.read_file_manager(
                session, client, printer_id=printer_id
            )
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    # read_file_manager may have adopted a corrected endpoint on the way.
    await session.commit()
    return {"printer_id": printer_id, **tree}


@router.get("/bambuddy/files/raw")
async def bambuddy_files_raw(
    printer_id: int | None = None,
    path: str = "",
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What Bambuddy actually replied, when what PrintFlow made of it was nothing.

    Every instance of this is self-hosted and none of them are quite the same,
    so an empty file manager cannot be diagnosed from here. It can be shown.
    """
    client = await _bambuddy_client(session)
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Reading the file manager"):
            return await client.raw_listing(path=path, printer_id=printer_id)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/bambuddy/printer-models")
async def bambuddy_printer_models(
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The distinct printer models on the farm, with how many of each."""
    client = await _bambuddy_client(session)
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Listing printers"):
            models = await bambuddy_api.with_healing(
                session, client, ("printers",), client.list_printer_models
            )
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"models": models}


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
    # ShipStation's newer API, on its own host with its own credential. The
    # only thing that will say whether a parcel arrived — the key/secret above
    # cannot — and entirely optional: without it PrintFlow simply never moves a
    # card to Complete on its own. Empty string clears a stored one.
    tracking_api_key: str | None = None


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
    # None means "leave whatever is stored alone", so saving the key/secret
    # from a form that never shows the tracking key cannot wipe it.
    if body.tracking_api_key is not None:
        payload["tracking_api_key"] = body.tracking_api_key.strip()
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
    return {"stores": stores, "tracking": bool(payload.get("tracking_api_key"))}


class WixConfigRequest(BaseModel):
    """An API key and the site it belongs to.

    No OAuth and so no callback URL, which is deliberate: PrintFlow is one
    shop's software on one shop's machine, and the Etsy connection's need for a
    public callback is the fiddliest part of setting the whole thing up.
    """

    api_key: str = Field(min_length=10)
    site_id: str = Field(min_length=4)
    # How far back to import on the first poll. Left unset, PrintFlow takes
    # everything the site has — which for an established shop is years of
    # history nobody wants on the board.
    orders_since: str | None = None


@router.post("/wix/config")
async def wix_config(
    body: WixConfigRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Save the key and prove it before saying it is connected.

    The check is the same call the poll makes, so a key that passes here is a
    key that has been shown to read this site's orders — rather than one that
    merely exists.
    """
    existing = await credentials.load(session, PROVIDER_WIX)
    payload = {
        **existing,
        "api_key": body.api_key.strip(),
        "site_id": body.site_id.strip(),
    }
    if body.orders_since is not None:
        payload["orders_since"] = body.orders_since.strip() or None
    client = wix_api.WixClient(payload)
    try:
        found = await client.validate()
    except IntegrationError as exc:
        await credentials.mark_error(session, PROVIDER_WIX, str(exc))
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await credentials.save(session, PROVIDER_WIX, payload)
    await session.commit()
    return found


class WixOrdersSinceRequest(BaseModel):
    # An ISO 8601 instant, as Wix's filter wants. Null means no cutoff.
    orders_since: str | None = None


@router.post("/wix/orders-since")
async def wix_set_orders_since(
    body: WixOrdersSinceRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Move the cutoff without re-pasting the key.

    Its own endpoint for the same reason Etsy's is: winding the date back to
    backfill is a thing done later, and asking for the API key again to do it
    would mean going and finding it in the Wix dashboard a second time.
    """
    value = (body.orders_since or "").strip() or None
    await credentials.merge(session, PROVIDER_WIX, {"orders_since": value})
    await audit.record(
        session,
        entity_type="settings",
        entity_id=None,
        action="wix_orders_since_changed",
        detail={"orders_since": value},
        actor=user.username,
    )
    await session.commit()
    return {"orders_since": value}


@router.post("/wix/import")
async def wix_import(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Fetch orders now rather than waiting for the next poll.

    The same job the scheduler runs, on demand — which is what somebody wants
    the moment after they connect it.
    """
    from ..scheduler import poll_wix

    return await poll_wix()


class TrackingKeyRequest(BaseModel):
    # Empty clears it, which is how delivery detection is switched back off.
    tracking_api_key: str = Field(default="")


@router.post("/shipstation/tracking-key")
async def shipstation_tracking_key(
    body: TrackingKeyRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Turn delivery detection on, by pasting the key that can answer.

    Checked against the instance before it is stored, because a key that is
    quietly wrong looks exactly like a shop where nothing ever gets delivered —
    and that is a fault nobody would think to go looking for.
    """
    payload = await credentials.load(session, PROVIDER_SHIPSTATION)
    if payload is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Connect ShipStation before adding a tracking key."
        )
    key = body.tracking_api_key.strip()

    # The key is stored either way, and the check only reports. An earlier
    # version refused to save a key its own probe disliked, which is the wrong
    # way round: the operator is holding the key and PrintFlow is guessing at
    # what a refusal means. The cost of storing a key that turns out not to
    # work is that deliveries are not detected — which is exactly what happens
    # if it is not stored, except now the reason is visible on this screen.
    checked: dict[str, Any] = {"ok": None, "detail": None, "carriers": []}
    if key:
        client = ss_api.ShipStationClient({**payload, "tracking_api_key": key})
        try:
            carriers = await client.tracking_carriers()
            checked["ok"] = True
            checked["carriers"] = [
                {
                    "code": row.get("carrier_code") or row.get("carrierCode"),
                    "name": row.get("friendly_name")
                    or row.get("nickname")
                    or row.get("carrier_code"),
                }
                for row in carriers
            ]
            checked["detail"] = (
                f"ShipStation accepted it — {len(carriers)} carrier"
                f"{'' if len(carriers) == 1 else 's'} visible."
            )
        except IntegrationError as exc:
            checked["ok"] = False
            # Verbatim, including the status and whatever ShipStation wrote.
            # Every guess PrintFlow makes about what a refusal means is a guess
            # that can be wrong, and this one already was.
            checked["detail"] = str(exc)

    payload["tracking_api_key"] = key
    await credentials.save(session, PROVIDER_SHIPSTATION, payload, mark_connected=False)
    await session.commit()
    return {"tracking": bool(key), "checked": checked}


@router.post("/shipstation/tracking-key/check")
async def shipstation_tracking_check(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Ask ShipStation about the key already stored, and say what it answered.

    Separate from saving because the two questions arrive at different moments:
    the key was accepted months ago and deliveries have stopped, and the first
    thing worth knowing is whether the key still works.
    """
    payload = await credentials.load(session, PROVIDER_SHIPSTATION)
    if not (payload or {}).get("tracking_api_key"):
        raise HTTPException(status.HTTP_409_CONFLICT, "No tracking key is stored.")
    client = ss_api.ShipStationClient(payload)
    try:
        carriers = await client.tracking_carriers()
    except IntegrationError as exc:
        return {"tracking": True, "checked": {"ok": False, "detail": str(exc), "carriers": []}}
    return {
        "tracking": True,
        "checked": {
            "ok": True,
            "detail": (
                f"ShipStation accepted it — {len(carriers)} carrier"
                f"{'' if len(carriers) == 1 else 's'} visible."
            ),
            "carriers": [
                {
                    "code": row.get("carrier_code") or row.get("carrierCode"),
                    "name": row.get("friendly_name") or row.get("nickname"),
                }
                for row in carriers
            ],
        },
    }


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
