"""Where a parcel leaves from, and making sure the screen and the carrier agree.

Choosing the wrong origin is not a cosmetic mistake: it is a carrier sent to
collect from a building nobody is standing in, and it is discovered when the
parcel does not turn up. So the rules about which address wins are pinned here
rather than left to be inferred from the call order.

ShipStation's `createlabelfororder` has no `shipFrom` field — an address is only
accepted by `shipments/createlabel`, which produces a label not attached to the
order, and ShipStation pushes tracking back to Etsy and Wix *per order*. So the
origin is named the only way that endpoint understands: by warehouse.
"""

from __future__ import annotations

import pytest

from app.integrations import shipstation as ss_api
from app.models import PROVIDER_SHIPSTATION
from app.services import credentials, shipping

pytestmark = pytest.mark.asyncio


def warehouse(wid, name, postal, *, default=False, city="Kilnford"):
    return {
        "warehouseId": wid,
        "warehouseName": name,
        "isDefault": default,
        "originAddress": {
            "street1": "12 Kiln Lane",
            "city": city,
            "state": "OH",
            "postalCode": postal,
        },
    }


HOME = warehouse(1, "Kilnford Workshop", "45011", default=True)
SECOND = warehouse(2, "Second Unit", "45050", city="Farhaven")


class FakeShipStation:
    """Just enough of the client for the origin ladder."""

    def __init__(self, warehouses, remote=None):
        self.warehouses = warehouses
        self.remote = remote or {}
        self.label_calls: list[dict] = []

    async def list_warehouses(self):
        return self.warehouses

    async def get_order(self, _order_id):
        return self.remote

    async def create_label_for_order(self, **kwargs):
        self.label_calls.append(kwargs)
        return {"trackingNumber": "9400111899223", "shipmentCost": 4.21}


# --------------------------------------------------------------------------
# Reading a warehouse
# --------------------------------------------------------------------------


async def test_a_location_is_described_by_its_address_not_its_id():
    """A warehouse id is a number nobody recognises."""
    summary = ss_api.warehouse_summary(HOME)

    assert summary["warehouse_id"] == 1
    assert summary["name"] == "Kilnford Workshop"
    assert summary["address"] == "12 Kiln Lane, Kilnford, OH 45011"
    assert summary["label"] == "Kilnford Workshop — 12 Kiln Lane, Kilnford, OH 45011"
    assert summary["postal_code"] == "45011"
    assert summary["is_default"] is True


async def test_a_location_with_no_name_still_has_something_to_show():
    summary = ss_api.warehouse_summary(warehouse(7, "", "45011"))

    assert summary["name"] is None
    assert summary["label"].startswith("12 Kiln Lane")


# --------------------------------------------------------------------------
# Which one wins
# --------------------------------------------------------------------------


async def test_the_label_s_own_choice_beats_everything(db):
    await credentials.save(
        db, PROVIDER_SHIPSTATION, {"api_key": "k", shipping.KEY_SHIP_FROM: "1"}
    )
    client = FakeShipStation([HOME, SECOND], {"advancedOptions": {"warehouseId": 1}})

    origin, _all = await shipping.resolve_origin(db, client, client.remote, "2")
    assert origin["warehouseId"] == 2


async def test_the_shop_default_beats_the_order_s_own_warehouse(db):
    """Deliberate, and the one rule worth arguing about.

    A shop that has chosen a ship-from has said where it packs parcels *now*.
    An order imported weeks ago carries whatever warehouse ShipStation gave it
    then, and letting that quietly win would mean the setting appears to do
    nothing for exactly the orders somebody is trying to fix.
    """
    await credentials.save(
        db, PROVIDER_SHIPSTATION, {"api_key": "k", shipping.KEY_SHIP_FROM: "2"}
    )
    client = FakeShipStation([HOME, SECOND], {"advancedOptions": {"warehouseId": 1}})

    origin, _all = await shipping.resolve_origin(db, client, client.remote)
    assert origin["warehouseId"] == 2


async def test_with_no_default_the_order_s_own_warehouse_is_used(db):
    await credentials.save(db, PROVIDER_SHIPSTATION, {"api_key": "k"})
    client = FakeShipStation([HOME, SECOND], {"advancedOptions": {"warehouseId": 2}})

    origin, _all = await shipping.resolve_origin(db, client, client.remote)
    assert origin["warehouseId"] == 2


async def test_with_nothing_chosen_shipstation_s_own_default_is_used(db):
    await credentials.save(db, PROVIDER_SHIPSTATION, {"api_key": "k"})
    client = FakeShipStation([SECOND, HOME], {})

    origin, _all = await shipping.resolve_origin(db, client, client.remote)
    assert origin["warehouseId"] == 1  # HOME is isDefault


async def test_no_warehouses_at_all_is_none_rather_than_a_guess(db):
    await credentials.save(db, PROVIDER_SHIPSTATION, {"api_key": "k"})
    client = FakeShipStation([], {})

    origin, options = await shipping.resolve_origin(db, client, client.remote)
    assert origin is None
    assert options == []


async def test_clearing_the_default_hands_the_choice_back(db):
    """An empty setting must mean "ShipStation decides", not "warehouse ''"."""
    await credentials.save(
        db, PROVIDER_SHIPSTATION, {"api_key": "k", shipping.KEY_SHIP_FROM: ""}
    )
    assert await shipping.default_warehouse_id(db) is None


# --------------------------------------------------------------------------
# What the carrier is actually told
# --------------------------------------------------------------------------


async def test_a_shop_that_never_chose_one_sends_no_origin_at_all():
    """The behaviour every label before this feature was bought with.

    Sending warehouseId: null is not the same as sending nothing, and this is
    the money-spending call — a shop that has not opted in must get back
    byte-for-byte what it had.
    """
    client = FakeShipStation([HOME])
    await client.create_label_for_order(
        order_id=1,
        carrier_code="stamps_com",
        service_code="usps_ground_advantage",
        package_code="package",
        weight={"value": 6.5, "units": "ounces"},
        warehouse_id=None,
    )
    assert "advancedOptions" not in client.label_calls[0]


async def test_a_chosen_origin_reaches_shipstation_as_a_number():
    """The form hands us a string; ShipStation wants an integer."""
    real = ss_api.ShipStationClient({"api_key": "k", "api_secret": "s"})
    sent: dict = {}

    async def capture(_method, _path, **kwargs):
        sent.update(kwargs.get("json") or {})
        return {"trackingNumber": "1Z"}

    real._call = capture  # type: ignore[method-assign]
    await real.create_label_for_order(
        order_id=1,
        carrier_code="stamps_com",
        service_code="usps_ground_advantage",
        package_code="package",
        weight={"value": 6.5, "units": "ounces"},
        warehouse_id="2",
    )
    assert sent["advancedOptions"] == {"warehouseId": 2}


async def test_something_that_is_not_a_number_is_passed_on_untouched():
    """ShipStation says what is wrong with it far better than we can."""
    real = ss_api.ShipStationClient({"api_key": "k", "api_secret": "s"})
    sent: dict = {}

    async def capture(_method, _path, **kwargs):
        sent.update(kwargs.get("json") or {})
        return {"trackingNumber": "1Z"}

    real._call = capture  # type: ignore[method-assign]
    await real.create_label_for_order(
        order_id=1,
        carrier_code="stamps_com",
        service_code="usps_ground_advantage",
        package_code="package",
        weight={"value": 6.5, "units": "ounces"},
        warehouse_id="not-a-number",
    )
    assert sent["advancedOptions"] == {"warehouseId": "not-a-number"}


# --------------------------------------------------------------------------
# The quote and the purchase must agree
# --------------------------------------------------------------------------


async def test_the_quote_prices_from_the_same_ladder_the_label_ships_from(db):
    """A price from one building and a parcel from another is the failure here.

    Both go through resolve_origin for exactly this reason, so this test is
    about the two staying joined rather than about either one alone.
    """
    await credentials.save(
        db, PROVIDER_SHIPSTATION, {"api_key": "k", shipping.KEY_SHIP_FROM: "2"}
    )
    client = FakeShipStation([HOME, SECOND], {"advancedOptions": {"warehouseId": 1}})

    quoted, _all = await shipping.resolve_origin(db, client, client.remote)
    bought = ss_api.origin_warehouse(
        await client.list_warehouses(), await shipping.default_warehouse_id(db)
    )
    assert quoted["warehouseId"] == bought["warehouseId"] == 2
    assert ss_api.warehouse_summary(quoted)["postal_code"] == "45050"
