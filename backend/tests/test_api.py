"""HTTP-level tests: setup wizard, auth, catalogue rules, board and overrides."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, Order, OrderLine, Product
from app.services import intake

pytestmark = pytest.mark.asyncio


class FakeQbo:
    async def get_items(self, item_ids):
        return {str(i): {"Id": str(i), "TrackQtyOnHand": True, "QtyOnHand": 0} for i in item_ids}


@pytest.fixture(autouse=True)
def stub_qbo(monkeypatch):
    async def client_for(_session):
        return FakeQbo()

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)


class TestSetupAndAuth:
    async def test_first_run_reports_no_admin(self, client):
        response = await client.get("/api/setup/status")
        assert response.status_code == 200
        body = response.json()
        assert body["admin_exists"] is False
        assert body["setup_complete"] is False
        assert body["poll_intervals"] == {
            "etsy_minutes": 5,
            "bambuddy_minutes": 2,
            "shipstation_minutes": 10,
        }

    async def test_creating_the_admin_signs_you_in(self, client):
        response = await client.post(
            "/api/setup/admin", json={"username": "adam", "password": "correct horse b"}
        )
        assert response.status_code == 200
        me = await client.get("/api/auth/me")
        assert me.json() == {"authenticated": True, "username": "adam"}

    async def test_admin_endpoint_closes_after_the_first_account(self, client):
        await client.post(
            "/api/setup/admin", json={"username": "adam", "password": "correct horse b"}
        )
        second = await client.post(
            "/api/setup/admin", json={"username": "mallory", "password": "correct horse b"}
        )
        assert second.status_code == 409

    async def test_protected_endpoints_require_a_session(self, client):
        assert (await client.get("/api/products")).status_code == 401
        assert (await client.get("/api/board")).status_code == 401

    async def test_login_rejects_a_bad_password(self, signed_in):
        await signed_in.post("/api/auth/logout")
        response = await signed_in.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert response.status_code == 401
        assert "Incorrect username or password" in response.json()["detail"]

    async def test_logout_then_login_round_trip(self, signed_in):
        await signed_in.post("/api/auth/logout")
        assert (await signed_in.get("/api/auth/me")).json()["authenticated"] is False
        response = await signed_in.post(
            "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"}
        )
        assert response.status_code == 200
        assert (await signed_in.get("/api/auth/me")).json()["authenticated"] is True

    async def test_poll_intervals_are_clamped_and_persisted(self, signed_in):
        response = await signed_in.post(
            "/api/setup/intervals",
            json={"etsy_minutes": 9999, "bambuddy_minutes": 0, "shipstation_minutes": 15},
        )
        assert response.status_code == 200
        assert response.json()["poll_intervals"] == {
            "etsy_minutes": 240,
            "bambuddy_minutes": 1,
            "shipstation_minutes": 15,
        }

    async def test_finishing_setup_flips_the_flag(self, signed_in):
        assert (await signed_in.post("/api/setup/complete")).json() == {
            "setup_complete": True
        }
        assert (await signed_in.get("/api/setup/status")).json()["setup_complete"] is True

    async def test_integration_status_lists_all_four_platforms(self, signed_in):
        body = (await signed_in.get("/api/settings")).json()
        providers = {row["provider"] for row in body["integrations"]}
        assert providers == {"etsy", "qbo", "bambuddy", "shipstation"}
        assert all(row["connected"] is False for row in body["integrations"])


async def make_product(client, **overrides):
    body = {"sku": "SKU-1", "name": "Thing", "fulfillment": "printed"}
    body.update(overrides)
    response = await client.post("/api/products", json=body)
    assert response.status_code == 201, response.text
    return response.json()


class TestCatalogue:
    async def test_create_and_list(self, signed_in):
        await make_product(signed_in, sku="WIDGET", name="Widget")
        listing = (await signed_in.get("/api/products")).json()["products"]
        assert [p["sku"] for p in listing] == ["WIDGET"]

    async def test_duplicate_sku_is_rejected(self, signed_in):
        await make_product(signed_in, sku="WIDGET")
        response = await signed_in.post(
            "/api/products", json={"sku": "WIDGET", "name": "Dup", "fulfillment": "printed"}
        )
        assert response.status_code == 409

    async def test_invalid_fulfillment_is_rejected(self, signed_in):
        response = await signed_in.post(
            "/api/products", json={"sku": "X", "name": "X", "fulfillment": "conjured"}
        )
        assert response.status_code == 400

    async def test_bom_rejects_nested_bundles(self, signed_in):
        outer = await make_product(signed_in, sku="OUTER", fulfillment="bundle")
        inner = await make_product(signed_in, sku="INNER", fulfillment="bundle")
        response = await signed_in.post(
            f"/api/products/{outer['id']}/bom",
            json={"component_id": inner["id"], "quantity": 1},
        )
        assert response.status_code == 400
        assert "single-level" in response.json()["detail"]

    async def test_bom_add_update_remove(self, signed_in):
        bundle = await make_product(signed_in, sku="KIT", fulfillment="bundle")
        part = await make_product(signed_in, sku="PART", fulfillment="printed")

        added = (
            await signed_in.post(
                f"/api/products/{bundle['id']}/bom",
                json={"component_id": part["id"], "quantity": 3},
            )
        ).json()
        assert added["bom"][0]["quantity"] == 3
        assert added["bom"][0]["component_sku"] == "PART"

        bom_id = added["bom"][0]["id"]
        updated = (
            await signed_in.put(
                f"/api/products/{bundle['id']}/bom/{bom_id}",
                json={"component_id": part["id"], "quantity": 5},
            )
        ).json()
        assert updated["bom"][0]["quantity"] == 5

        removed = (
            await signed_in.delete(f"/api/products/{bundle['id']}/bom/{bom_id}")
        ).json()
        assert removed["bom"] == []

    async def test_duplicate_bom_component_is_rejected(self, signed_in):
        bundle = await make_product(signed_in, sku="KIT", fulfillment="bundle")
        part = await make_product(signed_in, sku="PART")
        payload = {"component_id": part["id"], "quantity": 1}
        await signed_in.post(f"/api/products/{bundle['id']}/bom", json=payload)
        again = await signed_in.post(f"/api/products/{bundle['id']}/bom", json=payload)
        assert again.status_code == 409

    async def test_bom_only_on_bundles(self, signed_in):
        printed = await make_product(signed_in, sku="P1")
        other = await make_product(signed_in, sku="P2")
        response = await signed_in.post(
            f"/api/products/{printed['id']}/bom",
            json={"component_id": other["id"], "quantity": 1},
        )
        assert response.status_code == 400

    async def test_a_component_cannot_become_a_bundle(self, signed_in):
        bundle = await make_product(signed_in, sku="KIT", fulfillment="bundle")
        part = await make_product(signed_in, sku="PART")
        await signed_in.post(
            f"/api/products/{bundle['id']}/bom",
            json={"component_id": part["id"], "quantity": 1},
        )
        response = await signed_in.put(
            f"/api/products/{part['id']}",
            json={"sku": "PART", "name": "Part", "fulfillment": "bundle"},
        )
        assert response.status_code == 400

    async def test_print_mapping_requires_printed_fulfillment(self, signed_in):
        stocked = await make_product(signed_in, sku="STOCKED", fulfillment="stocked")
        response = await signed_in.put(
            f"/api/products/{stocked['id']}/print-mapping",
            json={"bambuddy_archive_id": 12, "units_per_plate": 2},
        )
        assert response.status_code == 400

    async def test_print_mapping_round_trip(self, signed_in):
        product = await make_product(signed_in, sku="PRINTED")
        saved = (
            await signed_in.put(
                f"/api/products/{product['id']}/print-mapping",
                json={
                    "bambuddy_archive_id": 77,
                    "bambuddy_archive_name": "dragon.3mf",
                    "plate_number": 3,
                    "units_per_plate": 6,
                    "print_options": {"filament": "PETG"},
                    "preferred_printer_id": 2,
                },
            )
        ).json()
        mapping = saved["print_mapping"]
        assert mapping["bambuddy_archive_id"] == 77
        assert mapping["units_per_plate"] == 6
        assert mapping["print_options"] == {"filament": "PETG"}

        cleared = (
            await signed_in.delete(f"/api/products/{product['id']}/print-mapping")
        ).json()
        assert cleared["print_mapping"] is None

    async def test_product_in_use_cannot_be_deleted(self, signed_in, db):
        product = await make_product(signed_in, sku="USED")
        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 5,
                "transactions": [{"transaction_id": 1, "sku": "USED", "quantity": 1}],
            },
        )
        await db.commit()
        response = await signed_in.delete(f"/api/products/{product['id']}")
        assert response.status_code == 409
        assert "Deactivate it instead" in response.json()["detail"]


class TestBoardAndOverrides:
    @pytest.fixture
    async def seeded(self, signed_in, db):
        await make_product(signed_in, sku="PART-A", name="Part A")
        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 4242,
                "name": "Jo Buyer",
                "transactions": [
                    {"transaction_id": 1, "sku": "PART-A", "quantity": 2},
                    {"transaction_id": 2, "sku": "GHOST", "quantity": 1},
                ],
            },
        )
        await db.commit()
        return signed_in

    async def test_board_groups_orders_into_columns(self, seeded):
        body = (await seeded.get("/api/board")).json()
        keys = [column["key"] for column in body["columns"]]
        assert keys == ["new", "in_production", "assembly", "ready_to_ship", "shipped"]
        new_column = body["columns"][0]
        assert new_column["count"] == 1
        card = new_column["orders"][0]
        assert card["order_number"] == "4242"
        assert card["buyer_name"] == "Jo Buyer"
        assert card["summary"]["needs_attention"] is True
        assert card["summary"]["unmatched_count"] == 1

    async def test_order_detail_returns_the_line_tree(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        body = (await seeded.get(f"/api/orders/{order.id}")).json()
        skus = {line["sku_raw"] for line in body["lines"]}
        assert skus == {"PART-A", "GHOST"}

    async def test_linking_a_product_clears_the_unmatched_flag(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        ghost = (
            (await db.execute(select(OrderLine).where(OrderLine.sku_raw == "GHOST")))
            .scalars()
            .one()
        )
        product = (
            (await db.execute(select(Product).where(Product.sku == "PART-A")))
            .scalars()
            .one()
        )

        response = await seeded.post(
            f"/api/orders/{order.id}/lines/{ghost.id}/link-product",
            json={"product_id": str(product.id)},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["unmatched_count"] == 0

        entries = (await db.execute(select(AuditLog))).scalars().all()
        assert any(entry.action == "link_product" for entry in entries)

    async def test_mark_printed_override_is_audited(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        line = (
            (await db.execute(select(OrderLine).where(OrderLine.sku_raw == "PART-A")))
            .scalars()
            .one()
        )
        response = await seeded.post(
            f"/api/orders/{order.id}/lines/{line.id}/override",
            json={"action": "mark_printed", "reason": "printed on the spare machine"},
        )
        assert response.status_code == 200

        await db.refresh(line)
        assert line.override_state == "printed"
        entries = (await db.execute(select(AuditLog))).scalars().all()
        override = next(e for e in entries if e.action == "override_mark_printed")
        assert override.detail["reason"] == "printed on the spare machine"
        assert override.actor == "admin"

    async def test_cancelling_a_line_is_audited_and_recomputed(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        line = (
            (await db.execute(select(OrderLine).where(OrderLine.sku_raw == "GHOST")))
            .scalars()
            .one()
        )
        await seeded.post(
            f"/api/orders/{order.id}/lines/{line.id}/override", json={"action": "cancel"}
        )
        await db.refresh(line)
        assert line.state == "cancelled"

    async def test_force_print_requires_a_printed_product(self, signed_in, db):
        await make_product(signed_in, sku="CARD", fulfillment="stocked")
        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 8100,
                "transactions": [{"transaction_id": 1, "sku": "CARD", "quantity": 1}],
            },
        )
        await db.commit()
        line = (
            (await db.execute(select(OrderLine).where(OrderLine.order_id == order.id)))
            .scalars()
            .one()
        )
        response = await signed_in.post(
            f"/api/orders/{order.id}/lines/{line.id}/force-print",
            json={"force_print": True},
        )
        assert response.status_code == 400
        assert "'printed' product" in response.json()["detail"]

    async def test_force_print_switches_a_printed_line_to_printing(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        line = (
            (await db.execute(select(OrderLine).where(OrderLine.sku_raw == "PART-A")))
            .scalars()
            .one()
        )
        response = await seeded.post(
            f"/api/orders/{order.id}/lines/{line.id}/force-print",
            json={"force_print": True, "reason": "customer wants a fresh one"},
        )
        assert response.status_code == 200
        await db.refresh(line)
        assert line.force_print is True
        assert line.qty_from_stock == 0
        assert line.qty_to_print == line.quantity

    async def test_assembly_check_off_rejects_non_bundle_lines(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        line = (
            (await db.execute(select(OrderLine).where(OrderLine.sku_raw == "PART-A")))
            .scalars()
            .one()
        )
        response = await seeded.post(
            f"/api/orders/{order.id}/lines/{line.id}/assemble", json={"assembled": True}
        )
        assert response.status_code == 400

    async def test_unknown_override_action_is_rejected(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        line = (await db.execute(select(OrderLine))).scalars().first()
        response = await seeded.post(
            f"/api/orders/{order.id}/lines/{line.id}/override",
            json={"action": "teleport"},
        )
        assert response.status_code == 422

    async def test_label_requires_a_shipstation_match(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        response = await seeded.post(
            f"/api/orders/{order.id}/label",
            json={
                "carrier_code": "stamps_com",
                "service_code": "usps_first_class_mail",
                "weight_value": 4,
                "allow_not_ready": True,
            },
        )
        assert response.status_code == 400
        assert "not been matched" in response.json()["detail"]

    async def test_label_blocked_unless_ready_to_ship(self, seeded, db):
        order = (await db.execute(select(Order))).scalars().one()
        order.shipstation_order_id = 999
        await db.commit()
        response = await seeded.post(
            f"/api/orders/{order.id}/label",
            json={
                "carrier_code": "stamps_com",
                "service_code": "usps_first_class_mail",
                "weight_value": 4,
            },
        )
        assert response.status_code == 400
        assert "not ready to ship" in response.json()["detail"]


class TestLabelCreation:
    async def test_buying_a_label_ships_the_order(self, signed_in, db, monkeypatch):
        class FakeShipStation:
            def __init__(self):
                self.calls = []

            async def create_label_for_order(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "trackingNumber": "9400111899223",
                    "labelData": "JVBERi0xLjQK",  # "%PDF-1.4\n" in base64
                    "shipmentCost": 4.21,
                }

        fake = FakeShipStation()

        async def client_for(_session):
            return fake

        monkeypatch.setattr("app.services.shipping.ss_api.client_for", client_for)

        await make_product(signed_in, sku="STOCKED-1", fulfillment="stocked")
        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 6001,
                "transactions": [{"transaction_id": 1, "sku": "STOCKED-1", "quantity": 1}],
            },
        )
        order.shipstation_order_id = 12345
        await db.commit()
        assert order.status == "ready_to_ship"

        response = await signed_in.post(
            f"/api/orders/{order.id}/label",
            json={
                "carrier_code": "stamps_com",
                "service_code": "usps_ground_advantage",
                "package_code": "package",
                "weight_value": 6.5,
                "weight_units": "ounces",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["tracking_number"] == "9400111899223"
        assert fake.calls[0]["weight"] == {"value": 6.5, "units": "ounces"}

        await db.refresh(order)
        assert order.status == "shipped"
        assert order.label_pdf.startswith(b"%PDF")

        pdf = await signed_in.get(f"/api/orders/{order.id}/label.pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"

        # A second purchase must not be possible.
        again = await signed_in.post(
            f"/api/orders/{order.id}/label",
            json={
                "carrier_code": "stamps_com",
                "service_code": "usps_ground_advantage",
                "weight_value": 6.5,
                "allow_not_ready": True,
            },
        )
        assert again.status_code == 400
        assert "already been created" in again.json()["detail"]

        entries = (await db.execute(select(AuditLog))).scalars().all()
        assert any(entry.action == "label_created" for entry in entries)


class TestSyncAndAudit:
    async def test_sync_log_and_audit_endpoints(self, signed_in):
        assert (await signed_in.get("/api/sync-log")).json() == {"entries": []}
        assert (await signed_in.get("/api/audit-log")).json() == {"entries": []}

    async def test_health_is_public(self, client):
        assert (await client.get("/api/health")).json() == {"ok": True}

    async def test_unknown_api_route_is_404_not_the_spa(self, signed_in):
        response = await signed_in.get("/api/does-not-exist")
        assert response.status_code == 404
