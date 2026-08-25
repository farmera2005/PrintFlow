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
            "wix_minutes": 5,
            "bambuddy_minutes": 2,
            "shipstation_minutes": 10,
            # Parcels do not move on a ten-minute timer.
            "tracking_minutes": 30,
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
            # Untouched by this request, so they keep their defaults.
            "wix_minutes": 5,
            "tracking_minutes": 30,
        }

    async def test_finishing_setup_flips_the_flag(self, signed_in):
        assert (await signed_in.post("/api/setup/complete")).json() == {
            "setup_complete": True
        }
        assert (await signed_in.get("/api/setup/status")).json()["setup_complete"] is True

    async def test_integration_status_lists_every_platform(self, signed_in):
        body = (await signed_in.get("/api/settings")).json()
        providers = {row["provider"] for row in body["integrations"]}
        assert providers == {"etsy", "wix", "qbo", "bambuddy", "shipstation"}
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

    async def test_a_product_needs_no_code(self, signed_in):
        """Etsy never required a SKU, so PrintFlow does not require a code."""
        response = await signed_in.post(
            "/api/products", json={"name": "Storage bin", "fulfillment": "printed"}
        )
        assert response.status_code == 201, response.text
        assert response.json()["sku"] == "STORAGE-BIN"

    async def test_generated_codes_do_not_collide(self, signed_in):
        for _ in range(2):
            await signed_in.post(
                "/api/products", json={"name": "Storage bin", "fulfillment": "printed"}
            )
        listing = (await signed_in.get("/api/products")).json()["products"]
        assert sorted(p["sku"] for p in listing) == ["STORAGE-BIN", "STORAGE-BIN-2"]

    async def test_clearing_a_code_keeps_the_old_one(self, signed_in):
        """A product with nothing to print next to it is worse than a stale code."""
        product = await make_product(signed_in, sku="WIDGET")
        updated = await signed_in.put(
            f"/api/products/{product['id']}",
            json={"sku": "", "name": "Widget", "fulfillment": "printed"},
        )
        assert updated.json()["sku"] == "WIDGET"

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

    async def test_print_files_require_printed_fulfillment(self, signed_in):
        stocked = await make_product(signed_in, sku="STOCKED", fulfillment="stocked")
        response = await signed_in.post(
            f"/api/products/{stocked['id']}/print-files",
            json={"bambuddy_archive_id": 12, "units_per_plate": 2},
        )
        assert response.status_code == 400

    async def test_a_print_file_round_trips(self, signed_in):
        product = await make_product(signed_in, sku="PRINTED")
        saved = (
            await signed_in.post(
                f"/api/products/{product['id']}/print-files",
                json={
                    "bambuddy_archive_id": 77,
                    "bambuddy_archive_name": "dragon.3mf",
                    "plate_number": 3,
                    "units_per_plate": 6,
                    "print_options": {"filament": "PETG"},
                    "printer_models": ["H2D"],
                },
            )
        ).json()
        [file] = saved["print_files"]
        assert file["bambuddy_archive_id"] == 77
        assert file["units_per_plate"] == 6
        assert file["print_options"] == {"filament": "PETG"}
        assert file["printer_models"] == ["H2D"]

        cleared = (
            await signed_in.delete(
                f"/api/products/{product['id']}/print-files/{file['id']}"
            )
        ).json()
        assert cleared["print_files"] == []

    async def test_a_product_can_have_a_file_per_machine(self, signed_in):
        """The reason this is a list: one part, sliced once per printer."""
        product = await make_product(signed_in, sku="GRAIN-BIN")
        for archive, model in ((1, "H2C"), (2, "H2D"), (3, "P1S")):
            saved = (
                await signed_in.post(
                    f"/api/products/{product['id']}/print-files",
                    json={
                        "bambuddy_archive_id": archive,
                        "bambuddy_archive_name": f"bin-{model}.3mf",
                        "printer_models": [model],
                    },
                )
            ).json()
        assert [f["printer_models"] for f in saved["print_files"]] == [
            ["H2C"], ["H2D"], ["P1S"]
        ]

        # Editing one leaves the others alone.
        second = saved["print_files"][1]
        edited = (
            await signed_in.put(
                f"/api/products/{product['id']}/print-files/{second['id']}",
                json={
                    "bambuddy_archive_id": 2,
                    "bambuddy_archive_name": "bin-H2D.3mf",
                    "units_per_plate": 4,
                    "printer_models": ["H2D", "X2D"],
                },
            )
        ).json()
        assert [f["printer_models"] for f in edited["print_files"]] == [
            ["H2C"], ["H2D", "X2D"], ["P1S"]
        ]
        assert [f["units_per_plate"] for f in edited["print_files"]] == [1, 4, 1]

    async def test_a_file_pinned_to_a_printer_drops_its_models(self, signed_in):
        # The machine is settled; models would only be a second, contradictory
        # answer to the same question.
        product = await make_product(signed_in, sku="PINNED")
        saved = (
            await signed_in.post(
                f"/api/products/{product['id']}/print-files",
                json={
                    "bambuddy_file_path": "/cache/jig.3mf",
                    "bambuddy_printer_id": 3,
                    "printer_models": ["H2D"],
                },
            )
        ).json()
        [file] = saved["print_files"]
        assert file["bambuddy_printer_id"] == 3
        assert file["printer_models"] == []

    async def test_a_file_that_names_nothing_is_refused(self, signed_in):
        product = await make_product(signed_in, sku="NOFILE")
        response = await signed_in.post(
            f"/api/products/{product['id']}/print-files",
            json={"units_per_plate": 2},
        )
        assert response.status_code == 422

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
        detail = response.json()["detail"]
        assert "1 order line references it" in detail
        assert "inactive" in detail


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
        # Cancelled is a column too: a card has to be draggable to any status.
        assert keys == [
            "new",
            "in_production",
            "assembly",
            "ready_to_ship",
            "shipped",
            "complete",
            "cancelled",
        ]
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
    async def test_buying_a_label_does_not_move_the_order(self, signed_in, db, monkeypatch):
        """Nothing moves a card but a person — not even buying its label.

        The label is bought, the tracking number lands, the lines go to
        `labeled`; the card waits in Ready to Ship until somebody drags it,
        because only they know the parcel has actually gone.
        """
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
        # Labels are only sold for an order somebody has moved to Ready to Ship.
        order.status = "ready_to_ship"
        await db.commit()

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
        assert order.status == "ready_to_ship"
        assert order.tracking_number == "9400111899223"
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

    async def test_what_the_label_cost_reaches_the_board(self, signed_in, db, monkeypatch):
        """Labels are the one thing PrintFlow spends money on."""
        class FakeShipStation:
            async def create_label_for_order(self, **kwargs):
                return {
                    "trackingNumber": "9400111899224",
                    # Postage and insurance are both charged and both land on
                    # the shipping bill, so the card has to show the sum.
                    "shipmentCost": 7.16,
                    "insuranceCost": 0.25,
                    "currency": "USD",
                }

        async def client_for(_session):
            return FakeShipStation()

        monkeypatch.setattr("app.services.shipping.ss_api.client_for", client_for)

        await make_product(signed_in, sku="STOCKED-C", fulfillment="stocked")
        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 6002,
                "transactions": [{"transaction_id": 1, "sku": "STOCKED-C", "quantity": 1}],
            },
        )
        order.shipstation_order_id = 22222
        order.status = "ready_to_ship"
        await db.commit()

        bought = await signed_in.post(
            f"/api/orders/{order.id}/label",
            json={
                "carrier_code": "stamps_com",
                "service_code": "usps_ground_advantage",
                "weight_value": 6.5,
            },
        )
        assert bought.status_code == 200, bought.text
        # A string, not a float: JSON's only number cannot hold 7.41 exactly,
        # and this is a figure somebody reconciles against a bill.
        assert bought.json()["label_cost"] == "7.41"
        assert bought.json()["label_currency"] == "USD"

        board = (await signed_in.get("/api/board")).json()
        cards = [
            card
            for column in board["columns"]
            for card in column["orders"]
            if card["id"] == str(order.id)
        ]
        assert cards[0]["label_cost"] == "7.41"
        assert cards[0]["label_currency"] == "USD"

    async def test_a_label_shipstation_did_not_price_shows_no_price(
        self, signed_in, db, monkeypatch
    ):
        """"Nothing was said" and "it was free" must not look the same."""
        class Silent:
            async def create_label_for_order(self, **kwargs):
                return {"trackingNumber": "9400111899225"}

        async def client_for(_session):
            return Silent()

        monkeypatch.setattr("app.services.shipping.ss_api.client_for", client_for)

        await make_product(signed_in, sku="STOCKED-D", fulfillment="stocked")
        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 6003,
                "transactions": [{"transaction_id": 1, "sku": "STOCKED-D", "quantity": 1}],
            },
        )
        order.shipstation_order_id = 33333
        order.status = "ready_to_ship"
        await db.commit()

        bought = await signed_in.post(
            f"/api/orders/{order.id}/label",
            json={
                "carrier_code": "stamps_com",
                "service_code": "usps_ground_advantage",
                "weight_value": 6.5,
            },
        )
        assert bought.status_code == 200, bought.text
        assert bought.json()["label_cost"] is None
        await db.refresh(order)
        assert order.label_cost is None and order.label_currency is None


class TestSyncAndAudit:
    async def test_sync_log_and_audit_endpoints(self, signed_in):
        assert (await signed_in.get("/api/sync-log")).json() == {"entries": []}
        assert (await signed_in.get("/api/audit-log")).json() == {"entries": []}

    async def test_health_is_public_and_identifies_the_build(self, client):
        body = (await client.get("/api/health")).json()
        assert body["ok"] is True
        # Enough to answer "is my update live?" without signing in.
        assert "version" in body
        assert body["features"]["cloudflare_tunnel"] is True

    async def test_unknown_api_route_is_404_not_the_spa(self, signed_in):
        response = await signed_in.get("/api/does-not-exist")
        assert response.status_code == 404


@pytest.mark.asyncio
class TestCacheHeaders:
    """A stale index.html points at the previous content-hashed bundle, so the
    app silently keeps running old code after a correct rebuild."""

    async def test_index_html_must_be_revalidated(self, client):
        response = await client.get("/")
        assert response.status_code in (200, 503)
        if response.status_code == 200:
            assert "no-cache" in response.headers.get("cache-control", "")

    async def test_client_side_routes_are_also_no_cache(self, client):
        response = await client.get("/settings")
        if response.status_code == 200:
            assert "no-cache" in response.headers.get("cache-control", "")

    async def test_api_responses_are_never_stored(self, client):
        response = await client.get("/api/health")
        assert response.headers.get("cache-control") == "no-store"

    async def test_api_no_store_survives_auth_failures(self, client):
        response = await client.get("/api/board")
        assert response.status_code == 401
        assert response.headers.get("cache-control") == "no-store"


class TestLabelQuote:
    """What it will cost, before the money is spent.

    The quote is a different thing from the charge — the carrier prices again
    when the label is actually bought — but "is this the $30 service or the $9
    one" is exactly the question being asked at the moment of choosing, and it
    was previously unanswerable without leaving PrintFlow.
    """

    class FakeShipStation:
        def __init__(self, *, warehouses=None, rates=None, ship_to=None):
            self.warehouses = warehouses if warehouses is not None else [
                {"warehouseId": 9, "isDefault": True,
                 "originAddress": {"postalCode": "61234"}},
            ]
            self.rates = rates if rates is not None else [
                {"serviceCode": "ups_ground", "serviceName": "UPS® Ground",
                 "shipmentCost": 11.42, "otherCost": 0.63},
                {"serviceCode": "ups_2nd_day_air", "serviceName": "UPS 2nd Day Air®",
                 "shipmentCost": 31.08, "otherCost": 0},
            ]
            self.ship_to = ship_to if ship_to is not None else {
                "postalCode": "62341-3104", "state": "IL", "country": "US",
                "residential": True,
            }
            self.asked: list[dict] = []

        async def get_order(self, order_id):
            return {"shipTo": self.ship_to, "advancedOptions": {"warehouseId": 9}}

        async def list_warehouses(self):
            return self.warehouses

        async def get_rates(self, **kwargs):
            self.asked.append(kwargs)
            return self.rates

    async def _order(self, signed_in, db, sku, receipt):
        await make_product(signed_in, sku=sku, fulfillment="stocked")
        order, _ = await intake.ingest_receipt(
            db,
            {"receipt_id": receipt,
             "transactions": [{"transaction_id": 1, "sku": sku, "quantity": 1}]},
        )
        order.shipstation_order_id = 44444
        await db.commit()
        return order

    def _use(self, monkeypatch, fake):
        async def client_for(_session):
            return fake

        monkeypatch.setattr("app.services.shipping.ss_api.client_for", client_for)

    async def test_every_service_is_priced_in_one_ask(self, signed_in, db, monkeypatch):
        # The dropdown has a dozen lines and the operator is choosing between
        # them, so pricing them one at a time would be a dozen round trips.
        fake = self.FakeShipStation()
        self._use(monkeypatch, fake)
        order = await self._order(signed_in, db, "STOCKED-Q1", 6101)

        body = (
            await signed_in.get(
                f"/api/orders/{order.id}/label-rates"
                "?carrier_code=ups_walleted&weight_value=3&weight_units=pounds"
            )
        ).json()

        assert body["available"] is True
        # Surcharges are charged too, so they are part of the number shown.
        assert body["rates"] == [
            {"service_code": "ups_ground", "service_name": "UPS® Ground", "total": "12.05"},
            {"service_code": "ups_2nd_day_air", "service_name": "UPS 2nd Day Air®",
             "total": "31.08"},
        ]
        assert len(fake.asked) == 1
        # No service named: that is what makes one ask cover all of them.
        assert "service_code" not in fake.asked[0] or fake.asked[0]["service_code"] is None
        assert fake.asked[0]["from_postal_code"] == "61234"
        assert fake.asked[0]["to_postal_code"] == "62341-3104"

    async def test_it_ships_from_the_order_s_own_warehouse(self, signed_in, db, monkeypatch):
        fake = self.FakeShipStation(
            warehouses=[
                {"warehouseId": 1, "isDefault": True,
                 "originAddress": {"postalCode": "10001"}},
                {"warehouseId": 9, "originAddress": {"postalCode": "61234"}},
            ]
        )
        self._use(monkeypatch, fake)
        order = await self._order(signed_in, db, "STOCKED-Q2", 6102)

        await signed_in.get(
            f"/api/orders/{order.id}/label-rates?carrier_code=ups&weight_value=3"
        )
        # The order names warehouse 9, so 9 is where it ships from — not the
        # default, which is a different building.
        assert fake.asked[0]["from_postal_code"] == "61234"

    async def test_no_ship_from_address_says_so_rather_than_guessing(
        self, signed_in, db, monkeypatch
    ):
        self._use(monkeypatch, self.FakeShipStation(warehouses=[]))
        order = await self._order(signed_in, db, "STOCKED-Q3", 6103)

        body = (
            await signed_in.get(
                f"/api/orders/{order.id}/label-rates?carrier_code=ups&weight_value=3"
            )
        ).json()
        assert body["available"] is False
        assert "ship-from" in body["reason"]

    async def test_a_carrier_that_will_not_quote_does_not_block_the_purchase(
        self, signed_in, db, monkeypatch
    ):
        """Not knowing the price is a worse screen, not a broken one."""
        from app.integrations.base import IntegrationError

        class Refuses(self.FakeShipStation):
            async def get_rates(self, **kwargs):
                raise IntegrationError("shipstation", "HTTP 400 — carrier not enabled")

        self._use(monkeypatch, Refuses())
        order = await self._order(signed_in, db, "STOCKED-Q4", 6104)

        response = await signed_in.get(
            f"/api/orders/{order.id}/label-rates?carrier_code=ups&weight_value=3"
        )
        assert response.status_code == 200
        assert response.json()["available"] is False
        assert "carrier not enabled" in response.json()["reason"]

    async def test_an_order_shipstation_has_never_seen_is_not_quoted(
        self, signed_in, db, monkeypatch
    ):
        self._use(monkeypatch, self.FakeShipStation())
        await make_product(signed_in, sku="STOCKED-Q5", fulfillment="stocked")
        order, _ = await intake.ingest_receipt(
            db,
            {"receipt_id": 6105,
             "transactions": [{"transaction_id": 1, "sku": "STOCKED-Q5", "quantity": 1}]},
        )
        await db.commit()

        body = (
            await signed_in.get(
                f"/api/orders/{order.id}/label-rates?carrier_code=ups&weight_value=3"
            )
        ).json()
        assert body["available"] is False
        assert "Not matched" in body["reason"]
