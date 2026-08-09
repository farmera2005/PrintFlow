"""Made-items sheets, and the one place PrintFlow writes to someone's books.

The stakes here are different from the rest of the suite. A wrong receipt import
is a wrong row on a board; a wrong Purchase is a wrong number in an accounting
system that someone files taxes from. These tests pin the arithmetic, the shape
of the payload, and — most of all — the conditions under which nothing is sent.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.integrations import qbo as qbo_api
from app.models import (
    SHEET_DRAFT,
    SHEET_POSTED,
    SHEET_VOIDED,
    BomLine,
    MadeSheet,
    MadeSheetLine,
    Product,
    PROVIDER_QBO,
)
from app.services import credentials, manufacturing, settings_store
from app.services.manufacturing import SheetError, build_purchase, explode_components, money

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


async def _product(session, sku, name="Thing", qbo_id="100", fulfillment="stocked"):
    product = Product(
        sku=sku, name=name, fulfillment=fulfillment, qbo_item_id=qbo_id, qbo_item_name=name
    )
    session.add(product)
    await session.flush()
    # A freshly added object has no loaded relationships, and touching one
    # outside a greenlet is an error rather than a lazy load. Refresh applies
    # the mapper's selectin strategies, which is what a real query would do.
    await session.refresh(product)
    return product


async def _bundle(session, sku, components):
    """components: [(product, qty), ...]"""
    bundle = await _product(session, sku, name=f"{sku} bundle", fulfillment="bundle")
    for component, quantity in components:
        session.add(
            BomLine(bundle_id=bundle.id, component_id=component.id, quantity=quantity)
        )
    await session.flush()
    await session.refresh(bundle)
    return bundle


async def _sheet(session, lines=(), reference="MI-TEST-01"):
    sheet = MadeSheet(
        reference=reference,
        made_on=datetime(2026, 8, 9, tzinfo=timezone.utc),
        status=SHEET_DRAFT,
        idempotency_key=uuid.uuid4(),
    )
    session.add(sheet)
    await session.flush()
    for product, quantity, cost in lines:
        session.add(
            MadeSheetLine(
                sheet_id=sheet.id,
                product_id=product.id,
                quantity=quantity,
                unit_cost=Decimal(str(cost)),
            )
        )
    await session.flush()
    await session.refresh(sheet)
    return sheet


async def _qbo_connected(session):
    await credentials.save(
        session,
        PROVIDER_QBO,
        {
            "client_id": "c",
            "client_secret": "s",
            "access_token": "t",
            "refresh_token": "r",
            "realm_id": "4620816365",
            "expires_at": 9_999_999_999,
            "environment": "sandbox",
        },
    )
    await session.commit()


async def _configured(session, **overrides):
    await settings_store.set_manufacturing_settings(
        session,
        {
            "account_id": "42",
            "account_name": "Cost of Goods Sold",
            "payment_type": "Cash",
            **overrides,
        },
    )
    await session.commit()


def _patch_qbo(monkeypatch, handler):
    from app.integrations import base as base_module

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_module.new_client(**kwargs)

    monkeypatch.setattr(qbo_api, "new_client", fake)


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


class TestMoneyIsNotFloat:
    async def test_cents_are_exact(self):
        """0.1 + 0.2 must be 0.30 in a number that reaches someone's ledger."""
        assert money(Decimal("0.1") + Decimal("0.2")) == Decimal("0.30")

    async def test_rounding_is_half_up_not_bankers(self):
        # Python's round() would give 0.02 here; an invoice would say 0.03.
        assert money("0.025") == Decimal("0.03")
        assert money("0.015") == Decimal("0.02")

    async def test_none_is_zero(self):
        assert money(None) == Decimal("0.00")


# --------------------------------------------------------------------------
# BOM explosion
# --------------------------------------------------------------------------


class TestComponentsConsumed:
    async def test_a_bom_is_exploded_by_the_quantity_made(self, db):
        screw = await _product(db, "SCREW", qbo_id="1")
        body = await _product(db, "BODY", qbo_id="2")
        widget = await _bundle(db, "WIDGET", [(screw, 4), (body, 1)])
        sheet = await _sheet(db, [(widget, 10, "3.00")])

        consumed = {p.sku: qty for p, qty in explode_components(sheet.lines).values()}
        assert consumed == {"SCREW": 40, "BODY": 10}

    async def test_a_component_shared_across_lines_is_summed_once(self, db):
        screw = await _product(db, "SCREW", qbo_id="1")
        a = await _bundle(db, "A", [(screw, 2)])
        b = await _bundle(db, "B", [(screw, 3)])
        sheet = await _sheet(db, [(a, 10, "1.00"), (b, 10, "1.00")])

        consumed = {p.sku: qty for p, qty in explode_components(sheet.lines).values()}
        assert consumed == {"SCREW": 50}

    async def test_a_product_without_a_bom_consumes_nothing(self, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        sheet = await _sheet(db, [(plain, 5, "2.00")])
        assert explode_components(sheet.lines) == {}


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------


class TestThePurchasePayload:
    SETTINGS = {
        "account_id": "42",
        "account_name": "Cost of Goods Sold",
        "payment_type": "Cash",
        "vendor_id": None,
        "doc_number_prefix": "",
    }

    async def test_made_items_get_a_positive_quantity(self, db):
        """A positive item line is what raises quantity on hand."""
        plain = await _product(db, "PLAIN", qbo_id="77")
        sheet = await _sheet(db, [(plain, 5, "2.50")])

        body = build_purchase(sheet, settings=self.SETTINGS, component_unit_costs={})
        assert len(body["Line"]) == 1
        detail = body["Line"][0]["ItemBasedExpenseLineDetail"]
        assert detail["ItemRef"]["value"] == "77"
        assert detail["Qty"] == 5
        assert body["Line"][0]["Amount"] == 12.50

    async def test_components_get_a_negative_quantity(self, db):
        """A negative item line is what lowers it."""
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        sheet = await _sheet(db, [(widget, 10, "5.00")])

        body = build_purchase(
            sheet, settings=self.SETTINGS, component_unit_costs={"1": Decimal("0.25")}
        )
        component = [
            line
            for line in body["Line"]
            if line["ItemBasedExpenseLineDetail"]["ItemRef"]["value"] == "1"
        ][0]
        assert component["ItemBasedExpenseLineDetail"]["Qty"] == -40
        assert component["Amount"] == -10.00

    async def test_a_pure_transfer_nets_to_zero(self, db):
        """Made at the cost of its components, the Purchase totals nothing.

        That is the point: value moves from components into finished goods and
        the expense account is untouched.
        """
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        # 4 screws at 0.25 = 1.00 per widget.
        sheet = await _sheet(db, [(widget, 10, "1.00")])

        body = build_purchase(
            sheet, settings=self.SETTINGS, component_unit_costs={"1": Decimal("0.25")}
        )
        assert sum(line["Amount"] for line in body["Line"]) == 0.0

    async def test_added_value_lands_in_the_chosen_account(self, db):
        """Costed above the components — the difference is labour and overhead."""
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        sheet = await _sheet(db, [(widget, 10, "1.50")])

        body = build_purchase(
            sheet, settings=self.SETTINGS, component_unit_costs={"1": Decimal("0.25")}
        )
        assert sum(line["Amount"] for line in body["Line"]) == 5.00
        assert body["AccountRef"]["value"] == "42"

    async def test_an_unpriced_component_still_moves_its_quantity(self, db):
        """Value unknown is not the same as quantity unknown.

        Skipping the line would leave the component's stock overstated forever;
        posting it at zero keeps the count right and understates value by an
        amount the operator can see in the preview.
        """
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        sheet = await _sheet(db, [(widget, 10, "1.00")])

        body = build_purchase(sheet, settings=self.SETTINGS, component_unit_costs={})
        component = [
            line
            for line in body["Line"]
            if line["ItemBasedExpenseLineDetail"]["ItemRef"]["value"] == "1"
        ][0]
        assert component["ItemBasedExpenseLineDetail"]["Qty"] == -40
        assert component["Amount"] == 0.0

    async def test_the_transaction_date_is_the_made_date(self, db):
        plain = await _product(db, "PLAIN", qbo_id="77")
        sheet = await _sheet(db, [(plain, 1, "1.00")])
        body = build_purchase(sheet, settings=self.SETTINGS, component_unit_costs={})
        assert body["TxnDate"] == "2026-08-09"

    async def test_the_sheet_reference_travels_into_quickbooks(self, db):
        plain = await _product(db, "PLAIN", qbo_id="77")
        sheet = await _sheet(db, [(plain, 1, "1.00")], reference="MI-20260809-03")
        body = build_purchase(sheet, settings=self.SETTINGS, component_unit_costs={})
        assert "MI-20260809-03" in body["PrivateNote"]

    async def test_the_doc_number_stays_within_quickbooks_limit(self, db):
        plain = await _product(db, "PLAIN", qbo_id="77")
        sheet = await _sheet(db, [(plain, 1, "1.00")], reference="X" * 30)
        body = build_purchase(
            sheet,
            settings={**self.SETTINGS, "doc_number_prefix": "MADE-"},
            component_unit_costs={},
        )
        assert len(body["DocNumber"]) <= 21


# --------------------------------------------------------------------------
# Refusing to post
# --------------------------------------------------------------------------


class TestNothingIsPostedWhenItWouldBeWrong:
    async def test_an_unlinked_product_blocks_posting(self, db):
        """No QuickBooks item means no quantity to move."""
        await _qbo_connected(db)
        await _configured(db)
        orphan = await _product(db, "ORPHAN", qbo_id=None)
        sheet = await _sheet(db, [(orphan, 1, "1.00")])

        found = await manufacturing.problems(db, sheet)
        assert any("ORPHAN" in p and "not linked" in p for p in found)

    async def test_an_unlinked_component_blocks_posting(self, db):
        await _qbo_connected(db)
        await _configured(db)
        screw = await _product(db, "SCREW", qbo_id=None)
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        sheet = await _sheet(db, [(widget, 10, "1.00")])

        found = await manufacturing.problems(db, sheet)
        assert any("SCREW" in p and "component" in p for p in found)

    async def test_no_account_blocks_posting(self, db):
        await _qbo_connected(db)
        plain = await _product(db, "PLAIN", qbo_id="77")
        sheet = await _sheet(db, [(plain, 1, "1.00")])

        found = await manufacturing.problems(db, sheet)
        assert any("No account is set" in p for p in found)

    async def test_an_empty_sheet_blocks_posting(self, db):
        await _qbo_connected(db)
        await _configured(db)
        sheet = await _sheet(db, [])
        assert any("no lines" in p for p in await manufacturing.problems(db, sheet))

    async def test_every_problem_is_reported_at_once(self, db):
        """One failed post per problem is a miserable way to learn them."""
        await _qbo_connected(db)
        a = await _product(db, "A", qbo_id=None)
        b = await _product(db, "B", qbo_id=None)
        sheet = await _sheet(db, [(a, 1, "1.00"), (b, 1, "1.00")])

        found = await manufacturing.problems(db, sheet)
        assert any("A is not linked" in p for p in found)
        assert any("B is not linked" in p for p in found)
        assert any("No account is set" in p for p in found)

    async def test_posting_a_blocked_sheet_sends_nothing(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        orphan = await _product(db, "ORPHAN", qbo_id=None)
        sheet = await _sheet(db, [(orphan, 1, "1.00")])

        calls: list[str] = []
        _patch_qbo(monkeypatch, lambda r: (calls.append(r.method), httpx.Response(200, json={}))[1])
        with pytest.raises(SheetError):
            await manufacturing.post(db, sheet, actor="admin")
        assert "POST" not in calls
        assert sheet.status == SHEET_DRAFT

    async def test_a_posted_sheet_cannot_be_edited(self, db):
        sheet = await _sheet(db, [])
        sheet.status = SHEET_POSTED
        with pytest.raises(SheetError) as excinfo:
            manufacturing.assert_editable(sheet)
        assert "Void it" in str(excinfo.value)


# --------------------------------------------------------------------------
# Posting for real
# --------------------------------------------------------------------------


class TestPosting:
    async def _ready(self, db):
        await _qbo_connected(db)
        await _configured(db)
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        return await _sheet(db, [(widget, 10, "1.00")])

    async def test_a_successful_post_records_the_quickbooks_document(
        self, db, monkeypatch
    ):
        sheet = await self._ready(db)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"QueryResponse": {"Item": []}})
            return httpx.Response(
                200,
                json={"Purchase": {"Id": "180", "DocNumber": "1042", "SyncToken": "0"}},
            )

        _patch_qbo(monkeypatch, handler)
        await manufacturing.post(db, sheet, actor="admin")

        assert sheet.status == SHEET_POSTED
        assert sheet.qbo_purchase_id == "180"
        assert sheet.qbo_doc_number == "1042"
        assert sheet.posted_by == "admin"
        # The payload is kept so a question months later has an answer.
        assert sheet.qbo_request["Line"]
        assert sheet.qbo_response["Id"] == "180"

    async def test_the_idempotency_key_is_sent(self, db, monkeypatch):
        """A timeout then a retry must not post the transaction twice."""
        sheet = await self._ready(db)
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"QueryResponse": {"Item": []}})
            seen.append(request.url.params.get("requestid"))
            return httpx.Response(200, json={"Purchase": {"Id": "1", "SyncToken": "0"}})

        _patch_qbo(monkeypatch, handler)
        await manufacturing.post(db, sheet, actor="admin")
        assert seen == [str(sheet.idempotency_key)]

    async def test_a_write_is_never_retried(self, db, monkeypatch):
        """A create that times out may have succeeded; retrying would duplicate it."""
        sheet = await self._ready(db)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"QueryResponse": {"Item": []}})
            attempts["n"] += 1
            # 503 is in the retry set for reads.
            return httpx.Response(503, json={"Fault": {"Error": [{"Message": "busy"}]}})

        _patch_qbo(monkeypatch, handler)
        with pytest.raises(SheetError):
            await manufacturing.post(db, sheet, actor="admin")
        assert attempts["n"] == 1

    async def test_a_rejected_post_leaves_the_sheet_a_draft(self, db, monkeypatch):
        sheet = await self._ready(db)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"QueryResponse": {"Item": []}})
            return httpx.Response(
                400, json={"Fault": {"Error": [{"Message": "Invalid account"}]}}
            )

        _patch_qbo(monkeypatch, handler)
        with pytest.raises(SheetError) as excinfo:
            await manufacturing.post(db, sheet, actor="admin")
        assert sheet.status == SHEET_DRAFT
        assert sheet.qbo_purchase_id is None
        # The reason survives, and so does the advice to look before retrying.
        assert "check QuickBooks" in str(excinfo.value)

    async def test_voiding_deletes_the_transaction(self, db, monkeypatch):
        sheet = await self._ready(db)
        sheet.status = SHEET_POSTED
        sheet.qbo_purchase_id = "180"
        sheet.qbo_sync_token = "0"
        await db.flush()

        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["operation"] = request.url.params.get("operation")
            return httpx.Response(200, json={"Purchase": {"Id": "180", "status": "Deleted"}})

        _patch_qbo(monkeypatch, handler)
        await manufacturing.void(db, sheet, actor="admin")
        assert seen["operation"] == "delete"
        assert sheet.status == SHEET_VOIDED
        assert sheet.voided_by == "admin"

    async def test_a_draft_cannot_be_voided(self, db):
        sheet = await self._ready(db)
        with pytest.raises(SheetError):
            await manufacturing.void(db, sheet, actor="admin")


# --------------------------------------------------------------------------
# Costing
# --------------------------------------------------------------------------


class TestCostRollup:
    async def test_a_bom_rolls_up_to_a_unit_cost(self, db):
        screw = await _product(db, "SCREW", qbo_id="1")
        panel = await _product(db, "PANEL", qbo_id="2")
        widget = await _bundle(db, "WIDGET", [(screw, 4), (panel, 2)])

        cost = await manufacturing.bom_unit_cost(
            db, widget, {"1": Decimal("0.25"), "2": Decimal("1.10")}
        )
        assert cost == Decimal("3.20")

    async def test_a_partly_priced_bom_gives_no_answer(self, db):
        """Half a roll-up looks like a real figure and is worse than none."""
        screw = await _product(db, "SCREW", qbo_id="1")
        panel = await _product(db, "PANEL", qbo_id="2")
        widget = await _bundle(db, "WIDGET", [(screw, 4), (panel, 2)])

        assert await manufacturing.bom_unit_cost(db, widget, {"1": Decimal("0.25")}) is None

    async def test_a_product_without_a_bom_has_no_rollup(self, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        assert await manufacturing.bom_unit_cost(db, plain, {}) is None

    async def test_a_quickbooks_outage_does_not_block_editing(self, db, monkeypatch):
        """Costing is a convenience; losing it must not stop the sheet."""
        await _qbo_connected(db)
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        _patch_qbo(monkeypatch, handler)
        assert await manufacturing.suggest_unit_cost(db, widget) is None


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


class TestTheSheetApi:
    async def test_a_sheet_gets_a_readable_reference(self, signed_in, db):
        body = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        assert body["reference"].startswith("MI-")
        assert body["status"] == "draft"

    async def test_references_do_not_collide(self, signed_in, db):
        first = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        second = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        assert first["reference"] != second["reference"]

    async def test_a_line_is_priced_from_the_bom_when_no_cost_is_given(
        self, signed_in, db, monkeypatch
    ):
        await _qbo_connected(db)
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"QueryResponse": {"Item": [{"Id": "1", "PurchaseCost": 0.25}]}},
            )

        _patch_qbo(monkeypatch, handler)
        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        body = (
            await signed_in.post(
                f"/api/manufacturing/sheets/{sheet['id']}/lines",
                json={"product_id": str(widget.id), "quantity": 10},
            )
        ).json()
        line = body["lines"][0]
        assert line["unit_cost"] == "1.00"
        assert line["cost_from_bom"] is True
        assert line["amount"] == "10.00"

    async def test_typing_a_cost_stops_it_being_treated_as_the_bom_figure(
        self, signed_in, db
    ):
        plain = await _product(db, "PLAIN", qbo_id="9")
        await db.commit()
        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        added = (
            await signed_in.post(
                f"/api/manufacturing/sheets/{sheet['id']}/lines",
                json={"product_id": str(plain.id), "quantity": 2, "unit_cost": "4.5"},
            )
        ).json()
        assert added["lines"][0]["unit_cost"] == "4.50"
        assert added["lines"][0]["cost_from_bom"] is False

    async def test_the_same_product_cannot_be_listed_twice(self, signed_in, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        await db.commit()
        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        payload = {"product_id": str(plain.id), "quantity": 1, "unit_cost": "1"}
        assert (
            await signed_in.post(f"/api/manufacturing/sheets/{sheet['id']}/lines", json=payload)
        ).status_code == 200
        second = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines", json=payload
        )
        assert second.status_code == 409
        assert "already on this sheet" in second.json()["detail"]

    async def test_a_negative_cost_is_refused(self, signed_in, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        await db.commit()
        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        response = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"product_id": str(plain.id), "quantity": 1, "unit_cost": "-5"},
        )
        assert response.status_code == 422

    async def test_a_zero_quantity_is_refused(self, signed_in, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        await db.commit()
        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        response = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"product_id": str(plain.id), "quantity": 0, "unit_cost": "1"},
        )
        assert response.status_code == 422

    async def test_a_posted_sheet_refuses_edits(self, signed_in, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        sheet_row = await _sheet(db, [(plain, 1, "1.00")])
        sheet_row.status = SHEET_POSTED
        await db.commit()

        response = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet_row.id}/lines",
            json={"product_id": str(plain.id), "quantity": 1, "unit_cost": "1"},
        )
        assert response.status_code == 409

    async def test_a_posted_sheet_cannot_be_deleted(self, signed_in, db):
        sheet_row = await _sheet(db, [])
        sheet_row.status = SHEET_POSTED
        await db.commit()
        response = await signed_in.delete(f"/api/manufacturing/sheets/{sheet_row.id}")
        assert response.status_code == 409
        assert "Void it first" in response.json()["detail"]

    async def test_the_preview_shows_both_sides_and_the_difference(
        self, signed_in, db, monkeypatch
    ):
        await _qbo_connected(db)
        await _configured(db)
        screw = await _product(db, "SCREW", qbo_id="1")
        widget = await _bundle(db, "WIDGET", [(screw, 4)])
        sheet_row = await _sheet(db, [(widget, 10, "1.50")])
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"QueryResponse": {"Item": [{"Id": "1", "PurchaseCost": 0.25}]}},
            )

        _patch_qbo(monkeypatch, handler)
        body = (
            await signed_in.get(f"/api/manufacturing/sheets/{sheet_row.id}/preview")
        ).json()
        assert body["made_total"] == "15.00"
        assert body["consumed_total"] == "10.00"
        assert body["net_to_account"] == "5.00"
        assert body["account_name"] == "Cost of Goods Sold"
        assert body["problems"] == []

    async def test_the_preview_writes_nothing(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        plain = await _product(db, "PLAIN", qbo_id="9")
        sheet_row = await _sheet(db, [(plain, 1, "1.00")])
        await db.commit()

        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(200, json={"QueryResponse": {"Item": []}})

        _patch_qbo(monkeypatch, handler)
        await signed_in.get(f"/api/manufacturing/sheets/{sheet_row.id}/preview")
        assert "POST" not in methods

    async def test_posting_through_the_api(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        plain = await _product(db, "PLAIN", qbo_id="9")
        sheet_row = await _sheet(db, [(plain, 3, "2.00")])
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"QueryResponse": {"Item": []}})
            return httpx.Response(
                200, json={"Purchase": {"Id": "9", "DocNumber": "77", "SyncToken": "0"}}
            )

        _patch_qbo(monkeypatch, handler)
        body = (
            await signed_in.post(f"/api/manufacturing/sheets/{sheet_row.id}/post")
        ).json()
        assert body["status"] == "posted"
        assert body["qbo_doc_number"] == "77"

    async def test_a_blocked_post_explains_itself(self, signed_in, db):
        await _qbo_connected(db)
        orphan = await _product(db, "ORPHAN", qbo_id=None)
        sheet_row = await _sheet(db, [(orphan, 1, "1.00")])
        await db.commit()

        response = await signed_in.post(f"/api/manufacturing/sheets/{sheet_row.id}/post")
        assert response.status_code == 400
        assert "ORPHAN" in response.json()["detail"]


class TestQuickBooksItemsCanBeAddedDirectly:
    """Plenty of stock is worth counting into QuickBooks without being a product
    here — supplies, sub-assemblies, anything not sold on Etsy."""

    def _items(self, extra=None):
        rows = [
            {"Id": "50", "Name": "Packing Box", "Type": "Inventory",
             "PurchaseCost": 0.85, "TrackQtyOnHand": True, "QtyOnHand": 100},
            {"Id": "60", "Name": "Design Time", "Type": "Service", "PurchaseCost": 75.00},
        ]
        return rows + (extra or [])

    def _handler(self, extra=None, posted=None):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                q = request.url.params.get("query", "")
                ids = re.findall(r"'([^']+)'", q)
                rows = [r for r in self._items(extra) if not ids or r["Id"] in ids]
                return httpx.Response(200, json={"QueryResponse": {"Item": rows}})
            if posted is not None:
                posted.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                200, json={"Purchase": {"Id": "5", "DocNumber": "88", "SyncToken": "0"}}
            )

        return handler

    async def test_an_item_with_no_product_can_be_added(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, self._handler())

        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        body = (
            await signed_in.post(
                f"/api/manufacturing/sheets/{sheet['id']}/lines",
                json={"qbo_item_id": "50", "qbo_item_name": "Packing Box", "quantity": 40},
            )
        ).json()
        line = body["lines"][0]
        assert line["source"] == "quickbooks"
        assert line["sku"] == "Packing Box"
        assert line["qbo_item_id"] == "50"
        # Prefilled from the item's own cost in QuickBooks.
        assert line["unit_cost"] == "0.85"
        assert line["amount"] == "34.00"

    async def test_it_posts_the_item_and_consumes_nothing(
        self, signed_in, db, monkeypatch
    ):
        await _qbo_connected(db)
        await _configured(db)
        posted: list[dict] = []
        _patch_qbo(monkeypatch, self._handler(posted=posted))

        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"qbo_item_id": "50", "qbo_item_name": "Packing Box", "quantity": 40},
        )
        result = (
            await signed_in.post(f"/api/manufacturing/sheets/{sheet['id']}/post")
        ).json()
        assert result["status"] == "posted"

        lines = posted[0]["Line"]
        assert len(lines) == 1
        detail = lines[0]["ItemBasedExpenseLineDetail"]
        assert detail["ItemRef"]["value"] == "50"
        assert detail["Qty"] == 40

    async def test_picking_an_item_that_maps_to_a_product_keeps_its_bom(
        self, signed_in, db, monkeypatch
    ):
        """Otherwise the same physical act posts two different ways depending on
        which picker was used — no BOM, so no components consumed."""
        await _qbo_connected(db)
        await _configured(db)
        screw = await _product(db, "SCREW", qbo_id="1")
        await _bundle(db, "WIDGET", [(screw, 4)])
        await db.commit()

        extra = [
            {"Id": "1", "Name": "Screw", "Type": "Inventory", "PurchaseCost": 0.25,
             "TrackQtyOnHand": True},
            {"Id": "100", "Name": "Widget", "Type": "Inventory", "PurchaseCost": 1.00,
             "TrackQtyOnHand": True},
        ]
        _patch_qbo(monkeypatch, self._handler(extra))

        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        body = (
            await signed_in.post(
                f"/api/manufacturing/sheets/{sheet['id']}/lines",
                # Item 100 is WIDGET's linked item, chosen from the QuickBooks list.
                json={"qbo_item_id": "100", "qbo_item_name": "Widget", "quantity": 10},
            )
        ).json()
        line = body["lines"][0]
        assert line["source"] == "product"
        assert line["sku"] == "WIDGET"
        assert line["has_bom"] is True

        preview = (
            await signed_in.get(f"/api/manufacturing/sheets/{sheet['id']}/preview")
        ).json()
        assert [row["sku"] for row in preview["consumed"]] == ["SCREW"]
        assert preview["consumed"][0]["quantity"] == 40

    async def test_a_service_item_is_refused(self, signed_in, db, monkeypatch):
        """A Service line would book the expense and move no stock at all."""
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, self._handler())

        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"qbo_item_id": "60", "qbo_item_name": "Design Time", "quantity": 2},
        )
        preview = (
            await signed_in.get(f"/api/manufacturing/sheets/{sheet['id']}/preview")
        ).json()
        assert any("does not track quantity" in p for p in preview["problems"])

        response = await signed_in.post(f"/api/manufacturing/sheets/{sheet['id']}/post")
        assert response.status_code == 400

    async def test_the_same_item_cannot_be_listed_twice(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, self._handler())

        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        payload = {"qbo_item_id": "50", "qbo_item_name": "Packing Box", "quantity": 1}
        first = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines", json=payload
        )
        assert first.status_code == 200
        second = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines", json=payload
        )
        assert second.status_code == 409

    async def test_a_line_must_name_exactly_one_thing(self, signed_in, db):
        plain = await _product(db, "PLAIN", qbo_id="9")
        await db.commit()
        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()

        neither = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines", json={"quantity": 1}
        )
        assert neither.status_code == 422

        both = await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"product_id": str(plain.id), "qbo_item_id": "50", "quantity": 1},
        )
        assert both.status_code == 422

    async def test_a_product_line_and_an_item_line_coexist(
        self, signed_in, db, monkeypatch
    ):
        await _qbo_connected(db)
        await _configured(db)
        plain = await _product(db, "PLAIN", qbo_id="9")
        await db.commit()
        extra = [{"Id": "9", "Name": "Plain", "Type": "Inventory", "TrackQtyOnHand": True}]
        posted: list[dict] = []
        _patch_qbo(monkeypatch, self._handler(extra, posted))

        sheet = (await signed_in.post("/api/manufacturing/sheets", json={})).json()
        await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"product_id": str(plain.id), "quantity": 2, "unit_cost": "1.00"},
        )
        await signed_in.post(
            f"/api/manufacturing/sheets/{sheet['id']}/lines",
            json={"qbo_item_id": "50", "qbo_item_name": "Packing Box", "quantity": 3},
        )
        result = (
            await signed_in.post(f"/api/manufacturing/sheets/{sheet['id']}/post")
        ).json()
        assert result["status"] == "posted"
        moved = {
            line["ItemBasedExpenseLineDetail"]["ItemRef"]["value"]:
                line["ItemBasedExpenseLineDetail"]["Qty"]
            for line in posted[0]["Line"]
        }
        assert moved == {"9": 2, "50": 3}


class TestPostingSettings:
    async def test_the_account_round_trips(self, signed_in, db):
        await signed_in.put(
            "/api/manufacturing/settings",
            json={"account_id": "42", "account_name": "Cost of Goods Sold"},
        )
        body = (await signed_in.get("/api/manufacturing/settings")).json()
        assert body["settings"]["account_id"] == "42"
        assert body["settings"]["account_name"] == "Cost of Goods Sold"

    async def test_an_unknown_payment_type_falls_back(self, signed_in, db):
        await signed_in.put(
            "/api/manufacturing/settings", json={"payment_type": "Barter"}
        )
        body = (await signed_in.get("/api/manufacturing/settings")).json()
        assert body["settings"]["payment_type"] == "Cash"

    async def test_no_account_is_configured_by_default(self, db):
        """Posting stays blocked until someone chooses where the cost lands."""
        settings = await settings_store.get_manufacturing_settings(db)
        assert settings["account_id"] is None
