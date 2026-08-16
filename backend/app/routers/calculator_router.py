"""What a printed part costs to make.

The arithmetic lives in services/costing.py rather than in the browser for the
same reason every other figure in PrintFlow does: it is money, it is Decimal
here and a float there, and a cost worked out two different ways in two places
eventually disagrees.

Nothing here writes to QuickBooks or to an order. It is a calculator.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..models import User
from ..services import audit, costing, settings_store

router = APIRouter(prefix="/api/calculator", tags=["calculator"])


class Filament(BaseModel):
    name: str = Field(default="", max_length=120)
    price_per_kg: str | float | int | None = None


class CalculatorSettings(BaseModel):
    machine_per_hour: str | float | int | None = None
    printer_watts: str | float | int | None = None
    electricity_per_kwh: str | float | int | None = None
    labour_per_hour: str | float | int | None = None
    failure_percent: str | float | int | None = None
    markup_percent: str | float | int | None = None
    filaments: list[Filament] | None = Field(default=None, max_length=50)


class EstimateRequest(BaseModel):
    """One part, and any rate the caller wants to differ from the saved one.

    Overrides rather than a full set of rates: the usual case is "what would
    this cost", not "what would this cost if everything about the shop were
    different", and making the page send six rates it did not change is how the
    saved ones and the used ones drift apart.
    """

    grams: str | float | int | None = None
    filament_per_kg: str | float | int | None = None
    print_hours: str | float | int | None = None
    print_minutes: str | float | int | None = None
    labour_minutes: str | float | int | None = None
    extras_each: str | float | int | None = None
    quantity: int | None = Field(default=1, ge=1, le=100_000)

    machine_per_hour: str | float | int | None = None
    printer_watts: str | float | int | None = None
    electricity_per_kwh: str | float | int | None = None
    labour_per_hour: str | float | int | None = None
    failure_percent: str | float | int | None = None
    markup_percent: str | float | int | None = None


@router.get("")
async def read_settings(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    return {"settings": await settings_store.get_calculator_settings(session)}


@router.put("")
async def write_settings(
    body: CalculatorSettings,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    values: dict[str, Any] = body.model_dump(exclude_unset=True)
    if body.filaments is not None:
        values["filaments"] = [
            {"name": row.name.strip(), "price_per_kg": row.price_per_kg}
            for row in body.filaments
            if row.name.strip()
        ]
    saved = await settings_store.set_calculator_settings(session, values)
    await audit.record(
        session,
        entity_type="settings",
        entity_id=None,
        action="calculator_settings_changed",
        detail={
            "machine_per_hour": saved.get("machine_per_hour"),
            "labour_per_hour": saved.get("labour_per_hour"),
            "filaments": len(saved.get("filaments") or []),
        },
        actor=user.username,
    )
    await session.commit()
    return {"settings": saved}


@router.post("/estimate")
async def estimate(
    body: EstimateRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cost this part. Read-only: nothing is stored, nothing is posted."""
    stored = await settings_store.get_calculator_settings(session)
    sent = body.model_dump(exclude_unset=True)
    # Anything the caller named wins, anything it did not comes from Settings.
    rates = costing.Rates.from_settings({**stored, **sent})
    return costing.estimate(costing.Part.from_request(sent), rates)
