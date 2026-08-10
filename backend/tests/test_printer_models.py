"""Choosing a machine by model, and reading the whole archive list.

A plate is described by what can print it — "any X1C or P1S" — not by one named
machine, because a shop with four printers has more than one tool for the job
and pinning a file to a single machine means the queue stalls whenever that one
is busy. The two things worth testing here are the placement (does a plate land
on a machine that can actually make it, and what happens when none can) and the
archive listing (does the picker really see every file, or only the first page).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.integrations.bambuddy import BambuddyClient
from app.models import PrintJob, PrintMapping, ProductVariation
from app.services import intake, printing, variations

from test_intake_pipeline import FakeBambuddy, FakeQbo, seed_catalog

pytestmark = pytest.mark.asyncio


def printer(printer_id: int, model: str, *, online: bool = True, status: str = "idle"):
    return {"id": printer_id, "name": f"P{printer_id}", "model": model,
            "status": status, "online": online}


# --------------------------------------------------------------------------
# Picking a machine
# --------------------------------------------------------------------------


class TestChoosePrinter:
    def test_no_models_means_no_opinion(self):
        # Empty is "any printer", which is Bambuddy's own dispatch — not a
        # failure, and not a machine PrintFlow should be picking.
        assert printing.choose_printer([], [printer(1, "X1C")]) is None
        assert printing.choose_printer(["  "], [printer(1, "X1C")]) is None

    def test_the_farm_has_nothing_of_that_model(self):
        assert printing.choose_printer(["X1C"], [printer(1, "A1 mini")]) is None
        assert printing.choose_printer(["X1C"], []) is None

    def test_idle_beats_busy_beats_offline(self):
        farm = [
            printer(1, "X1C", online=False),
            printer(2, "X1C", status="printing"),
            printer(3, "X1C", status="idle"),
        ]
        assert printing.choose_printer(["X1C"], farm) == 3
        # Take the busy one over the offline one: a busy machine works through
        # its queue, an unreachable one never starts.
        assert printing.choose_printer(["X1C"], farm[:2]) == 2
        assert printing.choose_printer(["X1C"], farm[:1]) == 1

    def test_any_of_the_named_models_will_do(self):
        farm = [printer(1, "A1 mini"), printer(2, "P1S"), printer(3, "X1C")]
        assert printing.choose_printer(["X1C", "P1S"], farm) == 2
        # Model names are matched the way people type them.
        assert printing.choose_printer(["x1c"], farm) == 3
        assert printing.choose_printer([" P1S "], farm) == 2

    def test_a_run_of_plates_spreads_across_the_machines(self):
        farm = [printer(1, "X1C"), printer(2, "X1C"), printer(3, "X1C")]
        placed: dict[int, int] = {}
        chosen = []
        for _ in range(7):
            picked = printing.choose_printer(["X1C"], farm, placed)
            placed[picked] = placed.get(picked, 0) + 1
            chosen.append(picked)
        # Without the counter every plate would go to printer 1.
        assert chosen == [1, 2, 3, 1, 2, 3, 1]

    def test_a_printer_with_no_usable_id_is_skipped(self):
        farm = [{"id": None, "model": "X1C", "online": True}, printer(9, "X1C")]
        assert printing.choose_printer(["X1C"], farm) == 9


# --------------------------------------------------------------------------
# Which models a plate asks for
# --------------------------------------------------------------------------


class TestPlanResolvesModels:
    def test_the_product_says_which_machines_can_make_it(self):
        mapping = PrintMapping(bambuddy_archive_id=1, plate_number=1, units_per_plate=1,
                               printer_models=["X1C", "P1S"])
        plan = variations.print_plan(mapping, None)
        assert plan.printer_models == ("X1C", "P1S")

    def test_a_variation_narrows_it(self):
        mapping = PrintMapping(bambuddy_archive_id=1, plate_number=1, units_per_plate=1,
                               printer_models=["X1C", "P1S"])
        # A taller version of the same part fits only the bigger machine.
        variation = ProductVariation(label="Tall", options=[], active=True,
                                     printer_models=["X1C"])
        assert variations.print_plan(mapping, variation).printer_models == ("X1C",)

    def test_a_variation_that_says_nothing_keeps_the_product_s_answer(self):
        mapping = PrintMapping(bambuddy_archive_id=1, plate_number=1, units_per_plate=1,
                               printer_models=["X1C"])
        variation = ProductVariation(label="Red", options=[], active=True, printer_models=[])
        assert variations.print_plan(mapping, variation).printer_models == ("X1C",)

    def test_a_variation_with_its_own_file_brings_its_own_machines(self):
        mapping = PrintMapping(bambuddy_archive_id=1, plate_number=1, units_per_plate=1,
                               printer_models=["X1C"])
        variation = ProductVariation(label="Tall", options=[], active=True,
                                     bambuddy_archive_id=77, printer_models=["A1 mini"])
        plan = variations.print_plan(mapping, variation)
        assert (plan.bambuddy_archive_id, plan.printer_models) == (77, ("A1 mini",))


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


class FarmBambuddy(FakeBambuddy):
    """A fake with machines on it, and a count of how often it was asked."""

    def __init__(self, printers: list[dict]):
        super().__init__()
        self.printers = printers
        self.printer_reads = 0

    async def list_printers(self):
        self.printer_reads += 1
        return list(self.printers)


@pytest.fixture
def fake_qbo(monkeypatch):
    """Nothing here is stock-tracked; this only keeps intake off the network."""
    fake = FakeQbo({})

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)
    return fake


@pytest.fixture
def farm(monkeypatch):
    fake = FarmBambuddy([printer(1, "A1 mini"), printer(2, "X1C"), printer(3, "X1C")])

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.printing.bambuddy_api.client_for", client_for)
    return fake


async def _order_for(db, sku: str, qty: int = 1):
    order, _ = await intake.ingest_receipt(
        db,
        {
            "receipt_id": 4200 + qty,
            "transactions": [{"transaction_id": 1, "sku": sku, "quantity": qty}],
        },
    )
    await db.commit()
    return order


class TestDispatchByModel:
    async def test_the_plate_goes_to_a_machine_that_can_make_it(self, db, fake_qbo, farm):
        catalog = await seed_catalog(db)
        mapping = (
            await db.execute(
                select(PrintMapping).where(PrintMapping.product_id == catalog["part_y"].id)
            )
        ).scalar_one()
        mapping.printer_models = ["X1C"]
        await db.commit()

        await _order_for(db, "PART-Y")
        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        await db.commit()

        # Not printer 1: it is an A1 mini, and this file does not fit on it.
        assert farm.enqueued[0]["printer_id"] == 2
        job = (await db.execute(select(PrintJob))).scalars().one()
        assert job.printer_id == 2

    async def test_no_machine_of_that_model_leaves_the_plate_queued_and_says_why(
        self, db, fake_qbo, farm
    ):
        catalog = await seed_catalog(db)
        mapping = (
            await db.execute(
                select(PrintMapping).where(PrintMapping.product_id == catalog["part_y"].id)
            )
        ).scalar_one()
        mapping.printer_models = ["H2D"]
        await db.commit()

        await _order_for(db, "PART-Y")
        assert await printing.dispatch_pending(db) == {"dispatched": 0, "failed": 1}
        await db.commit()

        # Sending it anyway would put the plate on a machine that cannot make it.
        assert farm.enqueued == []
        job = (await db.execute(select(PrintJob))).scalars().one()
        assert job.status == "pending"
        assert "H2D" in job.error

        # The farm gains one, and the same job goes out on the next pass — the
        # error was a note about right now, not a dead end.
        farm.printers.append(printer(4, "H2D"))
        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        await db.commit()
        await db.refresh(job)
        assert (job.status, job.printer_id, job.error) == ("queued", 4, None)

    async def test_saying_nothing_leaves_dispatch_to_bambuddy(self, db, fake_qbo, farm):
        # PART-Y's mapping names no models, so PrintFlow neither reads the farm
        # nor names a printer — Bambuddy places it.
        await seed_catalog(db)
        await _order_for(db, "PART-Y")
        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        await db.commit()

        assert farm.printer_reads == 0
        assert farm.enqueued[0]["printer_id"] is None

    async def test_a_run_of_plates_is_spread_over_the_machines(self, db, fake_qbo, farm):
        catalog = await seed_catalog(db)
        mapping = (
            await db.execute(
                select(PrintMapping).where(PrintMapping.product_id == catalog["part_y"].id)
            )
        ).scalar_one()
        mapping.printer_models = ["X1C"]
        await db.commit()

        # PART-Y is one unit per plate, so four of them is four plates.
        await _order_for(db, "PART-Y", qty=4)
        assert await printing.dispatch_pending(db) == {"dispatched": 4, "failed": 0}
        await db.commit()

        assert sorted(job["printer_id"] for job in farm.enqueued) == [2, 2, 3, 3]
        # One read of the farm for the whole pass, not one per plate.
        assert farm.printer_reads == 1


# --------------------------------------------------------------------------
# Reading the archive list
# --------------------------------------------------------------------------


class PagedClient(BambuddyClient):
    """A client whose archive endpoint behaves like a real paged one."""

    def __init__(self, total: int, *, honours_offset: bool = True):
        super().__init__({"base_url": "http://bambuddy.local"})
        self.total = total
        self.honours_offset = honours_offset
        self.pages = 0

    async def list_archives(self, *, search: str = "", limit: int = 50, offset: int = 0):
        self.pages += 1
        start = offset if self.honours_offset else 0
        return [
            {"id": i, "name": f"file-{i}.3mf"}
            for i in range(start, min(start + limit, self.total))
        ]


class TestIterArchives:
    async def test_it_walks_past_the_first_page(self):
        client = PagedClient(total=430)
        archives, truncated = await client.iter_archives(page_size=100)
        assert [a["id"] for a in archives] == list(range(430))
        assert truncated is False
        # Four full pages and a short one, which is where it stops.
        assert client.pages == 5

    async def test_an_exact_multiple_of_the_page_size_still_terminates(self):
        client = PagedClient(total=200)
        archives, truncated = await client.iter_archives(page_size=100)
        assert len(archives) == 200
        # The third page comes back empty and ends it, rather than looping.
        assert (truncated, client.pages) == (False, 3)

    async def test_hitting_the_cap_says_so(self):
        client = PagedClient(total=10_000)
        archives, truncated = await client.iter_archives(page_size=100, max_pages=3)
        assert len(archives) == 300
        # A partial list is never handed back as if it were everything.
        assert truncated is True

    async def test_an_instance_that_ignores_offset_does_not_loop_forever(self):
        client = PagedClient(total=50, honours_offset=False)
        archives, truncated = await client.iter_archives(page_size=25, max_pages=25)
        # The same twenty-five come back every time; stop once nothing is new.
        assert [a["id"] for a in archives] == list(range(25))
        assert (truncated, client.pages) == (False, 2)


class ModelClient(BambuddyClient):
    def __init__(self, printers: list[dict]):
        super().__init__({"base_url": "http://bambuddy.local"})
        self._printers = printers

    async def list_printers(self, *, retries: int = 2):
        return list(self._printers)


class TestListPrinterModels:
    async def test_it_counts_the_machines_of_each_kind(self):
        client = ModelClient(
            [
                printer(1, "X1C"),
                printer(2, "X1C", online=False),
                printer(3, "A1 mini"),
                {"id": 4, "name": "unknown", "model": None, "online": True},
            ]
        )
        models = await client.list_printer_models()
        assert [m["model"] for m in models] == ["A1 mini", "X1C"]
        assert (models[1]["printers"], models[1]["online"]) == (2, 1)
        assert models[1]["names"] == ["P1", "P2"]
        # A machine with no model reported is not a model to choose from.
        assert all(m["model"] for m in models)

    async def test_no_printers_is_an_empty_list_not_an_error(self):
        assert await ModelClient([]).list_printer_models() == []
