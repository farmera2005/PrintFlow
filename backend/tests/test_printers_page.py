"""The farm as a screen: what each machine is doing, and what is on it.

A flat queue answers "what is outstanding"; standing in the shop the question is
"which machine should I be looking at". That is a join of two systems that each
know half the answer — Bambuddy knows the machine is at 37% and hot, PrintFlow
knows the plate belongs to order #1042 — and the interesting cases are all the
ones where one half is missing: a build that reports no readings, a machine that
will not answer, a printer that has been unplugged since the plate went to it,
and a Bambuddy that is down altogether while the shop still needs its queue.
"""

from __future__ import annotations

import pytest

from app.integrations.bambuddy import (
    BambuddyClient,
    discover_paths,
    parse_printer,
    read_farm,
)
from app.integrations.base import IntegrationError
from app.models import PROVIDER_BAMBUDDY, PrintFile, PrintJob
from app.services import credentials, farm, intake, printing

from test_intake_pipeline import FakeBambuddy, FakeQbo, seed_catalog
from test_printer_models import printer

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Reading one machine
# --------------------------------------------------------------------------


class TestParsePrinter:
    def test_a_bambu_machine_mid_print(self):
        row = parse_printer(
            {
                "id": 3, "name": "H2D-01", "model": "H2D", "online": True,
                "gcode_state": "RUNNING", "mc_percent": 37, "mc_remaining_time": 84,
                "subtask_name": "bin.3mf", "layer_num": 112, "total_layer_num": 300,
                "nozzle_temper": 221.4, "nozzle_target_temper": 220,
                "bed_temper": 60.2, "bed_target_temper": 60, "chamber_temper": 38.1,
            }
        )
        assert (row["state"], row["progress"], row["remaining_minutes"]) == (
            "RUNNING", 37, 84,
        )
        assert (row["layer"], row["layers"]) == (112, 300)
        assert (row["nozzle_temp"], row["nozzle_target"]) == (221.4, 220.0)
        assert row["current_file"] == "bin.3mf"

    def test_a_build_that_keeps_the_readings_in_an_object(self):
        # "status" is a word on most builds and a whole object on some.
        row = parse_printer(
            {"id": 1, "name": "P1S", "status": {"state": "IDLE", "nozzle_temp": 25}}
        )
        assert (row["status"], row["nozzle_temp"]) == ("IDLE", 25.0)

    def test_a_build_that_says_almost_nothing(self):
        row = parse_printer({"id": 7, "name": "X1C-01", "model": "X1C"})
        assert row["name"] == "X1C-01"
        # Every reading absent rather than zero: "no progress reported" and
        # "0% done" are different things to act on.
        assert row["progress"] is None and row["nozzle_temp"] is None

    def test_a_reading_that_is_not_a_number_is_not_a_reading(self):
        row = parse_printer({"id": 1, "nozzle_temper": "--", "mc_percent": None})
        assert row["nozzle_temp"] is None and row["progress"] is None

    def test_no_fault_is_not_a_fault(self):
        # Bambu reports the error code as 0 when all is well, so "nothing
        # wrong" arrives as a value rather than as an absence.
        for quiet in (0, "0", "", "none", "OK", False):
            assert parse_printer({"id": 1, "print_error": quiet})["error"] is None
        assert parse_printer({"id": 1, "print_error": 131077})["error"] == "131077"


# --------------------------------------------------------------------------
# Finding the per-printer endpoint
# --------------------------------------------------------------------------


class TestDiscoverPrinterDetail:
    def test_the_live_one_beats_the_inventory_row(self):
        found = discover_paths(
            {
                "/api/v1/printers": {"get": {}},
                "/api/v1/printers/{printer_id}": {"get": {}},
                "/api/v1/printers/{printer_id}/status": {"get": {}},
            }
        )["printer_detail"]
        assert found["path"] == "/api/v1/printers/{printer_id}/status"
        assert "/api/v1/printers/{printer_id}" in found["alternatives"]

    def test_the_parameter_is_renamed_to_ours(self):
        found = discover_paths({"/api/printers/{id}": {"get": {}}})["printer_detail"]
        assert found["path"] == "/api/printers/{printer_id}"

    def test_a_printers_sub_resource_is_not_the_printer(self):
        # Its files and its queue are their own roles; neither is "the machine".
        found = discover_paths(
            {
                "/api/printers/{id}/files": {"get": {}},
                "/api/printers/{id}/queue": {"get": {}},
                "/api/files/{id}": {"get": {}},
            }
        )["printer_detail"]
        assert found["path"] is None


# --------------------------------------------------------------------------
# Reading the farm
# --------------------------------------------------------------------------


class FarmClient(BambuddyClient):
    """A Bambuddy whose spec is exactly the paths it was given."""

    def __init__(self, *, spec_paths: dict, listing: list[dict], detail: dict | None = None):
        super().__init__({"base_url": "http://b.local"})
        self.spec_paths = spec_paths
        self.listing = listing
        self.detail = detail
        self.asked: list[str] = []
        self.specs_read = 0

    async def fetch_openapi(self):
        self.specs_read += 1
        return {"path": "/openapi.json", "discovered": discover_paths(self.spec_paths)}

    async def _call(self, method: str, path: str, *, retries: int = 2, **kwargs):
        import re

        self.asked.append(path)
        # A detail call arrives with the id substituted; the spec has the
        # template, so put it back before deciding whether this build serves it.
        template = re.sub(r"/\d+(?=/|$)", "/{printer_id}", path)
        if path not in self.spec_paths and template not in self.spec_paths:
            raise IntegrationError("bambuddy", "HTTP 404", status_code=404)
        if path.rstrip("/").endswith("printers"):
            return self.listing
        if self.detail is None:
            raise IntegrationError("bambuddy", "HTTP 500", status_code=500)
        return self.detail


LIVE_LISTING = [
    {"id": 1, "name": "A", "model": "H2D", "online": True, "gcode_state": "RUNNING",
     "mc_percent": 12},
]
THIN_LISTING = [
    {"id": 1, "name": "A", "model": "H2D", "online": True},
    {"id": 2, "name": "B", "model": "P1S", "online": True},
]
DETAIL = {"gcode_state": "RUNNING", "mc_percent": 55, "nozzle_temper": 220}


class TestReadFarm:
    async def test_a_listing_that_already_says_everything_is_one_call(self, db):
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}": {"get": {}}},
            listing=LIVE_LISTING,
        )
        found = await read_farm(db, client)

        assert found["detailed"] is True
        assert client.asked == ["/api/printers"]

    async def test_a_bare_inventory_asks_each_machine(self, db):
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}": {"get": {}}},
            listing=THIN_LISTING,
            detail=DETAIL,
        )
        found = await read_farm(db, client)

        assert [row["progress"] for row in found["printers"]] == [55, 55]
        # The listing stays the spine — a thin detail reply cannot blank a name.
        assert [row["name"] for row in found["printers"]] == ["A", "B"]
        assert client.asked.count("/api/printers/1") == 1

    async def test_the_moved_endpoint_is_found_once_for_the_whole_farm(self, db):
        """Re-reading the document per machine would be ten fetches for one fact."""
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/v1/printers/{printer_id}/status": {"get": {}}},
            listing=THIN_LISTING,
            detail=DETAIL,
        )
        found = await read_farm(db, client)

        assert found["detailed"] is True
        assert client.specs_read == 1
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["printer_detail"] == (
            "/api/v1/printers/{printer_id}/status"
        )

    async def test_a_machine_that_will_not_answer_keeps_its_row(self, db):
        # A printer PrintFlow cannot reach is still a printer. A farm screen
        # that quietly drops one is worse than useless.
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}": {"get": {}}},
            listing=THIN_LISTING,
            detail=None,
        )
        found = await read_farm(db, client)

        assert [row["name"] for row in found["printers"]] == ["A", "B"]
        assert found["detailed"] is False
        assert "500" in found["detail_error"]


# --------------------------------------------------------------------------
# The screen itself
# --------------------------------------------------------------------------


class FarmBambuddy(FakeBambuddy):
    def __init__(self, printers: list[dict]):
        super().__init__()
        self.printers = printers

    async def list_printers(self):
        return list(self.printers)


@pytest.fixture
def fake_qbo(monkeypatch):
    fake = FakeQbo({})

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)
    return fake


@pytest.fixture
def bambu(monkeypatch):
    fake = FarmBambuddy([printer(1, "H2C"), printer(2, "H2D")])

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.printing.bambuddy_api.client_for", client_for)
    monkeypatch.setattr("app.services.farm.bambuddy_api.client_for", client_for)

    async def read(_session, client, **_kwargs):
        return {"printers": await client.list_printers(), "detailed": True,
                "detail_error": None}

    monkeypatch.setattr("app.services.farm.bambuddy_api.read_farm", read)
    return fake


async def _made_on(db, product, models: list[str]) -> None:
    """Restrict this product's one file to these machines."""
    await db.execute(
        PrintFile.__table__.update()
        .where(PrintFile.product_id == product.id)
        .values(printer_models=models)
    )
    await db.commit()


async def _order(db, sku: str, qty: int, receipt: int = 7700):
    await intake.ingest_receipt(
        db,
        {"receipt_id": receipt, "transactions": [
            {"transaction_id": 1, "sku": sku, "quantity": qty}]},
    )
    await db.commit()


class TestOverview:
    async def test_each_plate_sits_under_the_machine_it_went_to(self, db, fake_qbo, bambu):
        catalog = await seed_catalog(db)
        await _made_on(db, catalog["part_y"], ["H2C"])
        await _order(db, "PART-Y", 2)
        await printing.dispatch_pending(db)
        await db.commit()

        view = await farm.overview(db)
        by_name = {row["id"]: row for row in view["printers"]}
        assert len(by_name[1]["plates"]) == 2
        assert by_name[2]["plates"] == []
        assert view["unplaced"] == []

    async def test_a_plate_with_no_machine_is_not_lost(self, db, fake_qbo, bambu):
        # Nothing on the farm can make it, so it never reached a card. Without
        # somewhere of its own it would simply be invisible.
        catalog = await seed_catalog(db)
        await _made_on(db, catalog["part_y"], ["X2D"])
        await _order(db, "PART-Y", 1)
        await printing.dispatch_pending(db)
        await db.commit()

        view = await farm.overview(db)
        assert len(view["unplaced"]) == 1
        assert "X2D" in view["unplaced"][0]["error"]

    async def test_a_plate_on_a_printer_the_farm_forgot(self, db, fake_qbo, bambu):
        """The machine was unplugged after the plate went to it."""
        catalog = await seed_catalog(db)
        await _made_on(db, catalog["part_y"], ["H2C"])
        await _order(db, "PART-Y", 1)
        await printing.dispatch_pending(db)
        await db.commit()
        bambu.printers = [printer(2, "H2D")]

        view = await farm.overview(db)
        assert [row["plates"] for row in view["printers"]] == [[]]
        assert len(view["unplaced"]) == 1

    async def test_a_finished_plate_leaves_the_machine_but_not_the_record(
        self, db, fake_qbo, bambu
    ):
        catalog = await seed_catalog(db)
        await _made_on(db, catalog["part_y"], ["H2C"])
        await _order(db, "PART-Y", 1)
        await printing.dispatch_pending(db)
        job = (await db.execute(PrintJob.__table__.select())).first()
        await db.execute(
            PrintJob.__table__.update().where(PrintJob.id == job.id).values(status="done")
        )
        await db.commit()

        view = await farm.overview(db)
        assert all(row["plates"] == [] for row in view["printers"])
        assert view["unplaced"] == []
        # Still in the full list, which is where the finished ones are shown.
        assert [row["status"] for row in view["plates"]] == ["done"]

    async def test_a_farm_that_cannot_be_read_still_shows_the_work(
        self, db, fake_qbo, bambu, monkeypatch
    ):
        """This is exactly when an operator needs the queue and the Re-queue button."""
        catalog = await seed_catalog(db)
        await _made_on(db, catalog["part_y"], ["H2C"])
        await _order(db, "PART-Y", 1)

        async def down(_session, _client, **_kwargs):
            raise IntegrationError("bambuddy", "Connection refused")

        monkeypatch.setattr("app.services.farm.bambuddy_api.read_farm", down)
        view = await farm.overview(db)

        assert view["printers"] == []
        assert "Connection refused" in view["error"]
        assert len(view["unplaced"]) == 1


class TestEndpoint:
    async def test_the_page_reads_the_farm_in_one_call(self, signed_in, db, bambu):
        body = (await signed_in.get("/api/printers")).json()
        assert [row["name"] for row in body["printers"]] == ["P1", "P2"]
        assert body["unplaced"] == [] and body["error"] is None

    async def test_the_flat_queue_endpoint_is_gone(self, signed_in):
        # Plates are read through the farm now; only the verbs remain.
        assert (await signed_in.get("/api/print-jobs")).status_code == 404
