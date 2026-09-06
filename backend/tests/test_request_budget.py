"""Every API request answers before the proxy in front of PrintFlow stops waiting.

Three times running, a newly added integration call turned out not to be
bounded, the request ran past Cloudflare's 100-second limit, and the operator
got Cloudflare's own 502 page — which names PrintFlow's hostname rather than
whatever was actually unreachable, and sends them looking in the wrong place
while the real error never gets rendered.

Each of those was fixed where it happened. This is the backstop for the ones
nobody has written yet.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import main

pytestmark = pytest.mark.asyncio


def _app_with_budget(budget: float, handler) -> FastAPI:
    """A miniature app carrying the real middleware class, so this tests the
    shipped code rather than a copy of its logic."""
    app = FastAPI()
    app.add_middleware(main.RequestBudget, budget=budget)
    app.add_api_route("/api/slow", handler, methods=["GET"])
    app.add_api_route("/api/backup/download", handler, methods=["GET"])
    app.add_api_route("/health", handler, methods=["GET"])
    return app


async def _forever():
    await asyncio.sleep(3600)
    return {"never": True}


async def _quick():
    return {"ok": True}


async def _call(app, path):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get(path)


async def test_a_request_that_runs_long_is_answered_not_left_hanging():
    response = await _call(_app_with_budget(0.2, _forever), "/api/slow")

    assert response.status_code == 504
    body = response.json()
    assert body["timed_out"] is True
    assert body["path"] == "/api/slow"
    # Actionable, and pointing away from PrintFlow, because that is where the
    # trouble almost always is.
    assert "connected services" in body["detail"]


async def test_504_not_502():
    """502 is the status the proxy itself serves.

    Answering with the same one would leave the two indistinguishable, which is
    the whole confusion this exists to end.
    """
    response = await _call(_app_with_budget(0.2, _forever), "/api/slow")
    assert response.status_code == 504


async def test_an_ordinary_request_is_untouched():
    response = await _call(_app_with_budget(30.0, _quick), "/api/slow")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_moving_a_database_is_allowed_to_take_as_long_as_it_takes():
    """Cutting a restore off half way is far worse than making somebody wait."""
    app = _app_with_budget(0.2, _quick)
    response = await _call(app, "/api/backup/download")

    assert response.status_code == 200


async def test_pages_and_assets_are_not_the_middleware_s_business():
    """Only /api/. The frontend is served from the same origin."""
    response = await _call(_app_with_budget(0.2, _quick), "/health")
    assert response.status_code == 200


async def test_the_budget_leaves_room_for_the_proxy():
    """Cloudflare gives up at 100s, so answering at 99 would be no answer.

    The margin is for the tunnel and the network between them; if somebody ever
    raises this, that is the thing to keep.
    """
    assert main.REQUEST_BUDGET_SECONDS <= 80.0
