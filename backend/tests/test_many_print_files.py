"""One product, several files, and which machine each plate ends up on.

A farm whose library is sorted by printer model has the same part sliced once
per machine. Those files are alternatives, not a sequence: the right one is
whichever names a printer that is free when the plate is actually sent, and that
is not knowable when the plate is planned. So the choice travels with the job
and is made at dispatch — which is the whole subject here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import PROVIDER_BAMBUDDY, PrintFile, PrintJob, ProductVariation
from app.services import credentials, intake, printing, variations

from test_intake_pipeline import FakeBambuddy, FakeQbo, seed_catalog
from test_printer_models import printer

pytestmark = pytest.mark.asyncio


def _file(archive: int, models: list[str], **fields) -> PrintFile:
    fields.setdefault("plate_number", 1)
    fields.setdefault("units_per_plate", 1)
    fields.setdefault("print_options", {})
    return PrintFile(
        bambuddy_archive_id=archive,
        bambuddy_archive_name=f"part-{archive}.3mf",
        printer_models=models,
        **fields,
    )


def _candidate(models: list[str], printer_id: int | None = None, **fields) -> dict:
    return {
        "archive_id": fields.get("archive_id", 1),
        "file_path": None,
        "name": "part.3mf",
        "plate_number": 1,
        "units_per_plate": 1,
        "printer_models": models,
        "printer_id": printer_id,
        "print_options": fields.get("print_options", {}),
    }


# --------------------------------------------------------------------------
# What a line could be printed from
# --------------------------------------------------------------------------


class TestPrintPlans:
    def test_every_file_is_a_way_of_making_it(self):
        plans = variations.print_plans(
            [_file(1, ["H2C"]), _file(2, ["H2D"]), _file(3, ["P1S"])], None
        )
        assert [plan.bambuddy_archive_id for plan in plans] == [1, 2, 3]

    def test_a_file_that_names_its_machines_comes_first(self):
        # "Anything" is the absence of a decision; naming them is one.
        plans = variations.print_plans([_file(1, []), _file(2, ["H2D"])], None)
        assert [plan.bambuddy_archive_id for plan in plans] == [2, 1]

    def test_a_file_naming_nothing_at_all_is_not_a_way_of_making_it(self):
        blank = PrintFile(plate_number=1, units_per_plate=1, printer_models=[])
        assert variations.print_plans([blank], None) == []

    def test_a_variation_with_its_own_file_replaces_the_lot(self):
        # A different part, not a different slicing of the same one.
        variation = ProductVariation(
            label="Tall", options=[], active=True,
            bambuddy_archive_id=99, printer_models=["X2D"],
        )
        plans = variations.print_plans([_file(1, ["H2C"]), _file(2, ["H2D"])], variation)
        assert [plan.bambuddy_archive_id for plan in plans] == [99]

    def test_a_variation_without_one_adjusts_each_of_them(self):
        variation = ProductVariation(
            label="Double", options=[], active=True, units_per_plate=8
        )
        plans = variations.print_plans([_file(1, ["H2C"]), _file(2, ["H2D"])], variation)
        assert [plan.units_per_plate for plan in plans] == [8, 8]

    def test_print_options_ride_with_the_file_they_belong_to(self):
        plans = variations.print_plans(
            [
                _file(1, ["H2C"], print_options={"filament": "PLA"}),
                _file(2, ["H2D"], print_options={"filament": "PETG"}),
            ],
            None,
        )
        assert [plan.print_options for plan in plans] == [
            {"filament": "PLA"}, {"filament": "PETG"}
        ]


# --------------------------------------------------------------------------
# Choosing between them
# --------------------------------------------------------------------------


class TestChooseCandidate:
    def test_the_file_whose_machine_is_free_wins(self):
        farm = [printer(1, "H2C"), printer(2, "H2D")]
        candidate, printer_id = printing.choose_candidate(
            [_candidate(["X2D"]), _candidate(["H2D"])], farm
        )
        assert (candidate["printer_models"], printer_id) == (["H2D"], 2)

    def test_nothing_available_anywhere_is_nothing(self):
        farm = [printer(1, "H2C")]
        assert printing.choose_candidate([_candidate(["H2D"]), _candidate(["X2D"])], farm) is None

    def test_a_run_of_plates_spreads_over_the_machine_types(self):
        # Two files, one machine each: four plates should not all queue behind
        # the first simply because it is listed first.
        farm = [printer(1, "H2C"), printer(2, "H2D")]
        options = [_candidate(["H2C"]), _candidate(["H2D"])]
        placed: dict[int, int] = {}
        chosen = []
        for _ in range(4):
            _candidate_used, printer_id = printing.choose_candidate(options, farm, placed)
            placed[printer_id] = placed.get(printer_id, 0) + 1
            chosen.append(printer_id)
        assert chosen == [1, 2, 1, 2]

    def test_a_file_pinned_to_a_machine_goes_to_that_machine(self):
        # It lives there; there is nothing to choose.
        chosen = printing.choose_candidate([_candidate([], printer_id=7)], [])
        assert chosen[1] == 7

    def test_a_named_machine_beats_anything(self):
        farm = [printer(1, "H2D")]
        candidate, printer_id = printing.choose_candidate(
            [_candidate([]), _candidate(["H2D"])], farm
        )
        assert (candidate["printer_models"], printer_id) == (["H2D"], 1)

    def test_anything_is_still_better_than_nothing(self):
        # No machine of the named model, but a file that will go anywhere.
        candidate, printer_id = printing.choose_candidate(
            [_candidate(["X2D"]), _candidate([])], [printer(1, "H2C")]
        )
        assert (candidate["printer_models"], printer_id) == ([], None)


# --------------------------------------------------------------------------
# End to end
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
def farm(monkeypatch):
    fake = FarmBambuddy(
        [printer(1, "H2C"), printer(2, "H2D"), printer(3, "H2D"), printer(4, "P1S")]
    )

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.printing.bambuddy_api.client_for", client_for)
    return fake


async def _sliced_for_each_machine(db, catalog):
    """PART-Y, the same part sliced for three machines — the reported shape."""
    await db.execute(
        PrintFile.__table__.delete().where(
            PrintFile.product_id == catalog["part_y"].id
        )
    )
    for archive, model in ((601, "H2C"), (602, "H2D"), (603, "P1S")):
        db.add(
            PrintFile(
                product_id=catalog["part_y"].id,
                bambuddy_archive_id=archive,
                bambuddy_archive_name=f"part-y-{model}.3mf",
                plate_number=1,
                units_per_plate=1,
                printer_models=[model],
                print_options={"slicer": model},
            )
        )
    await db.commit()


async def _order(db, qty: int, receipt: int = 8100):
    await intake.ingest_receipt(
        db,
        {"receipt_id": receipt, "transactions": [
            {"transaction_id": 1, "sku": "PART-Y", "quantity": qty}]},
    )
    await db.commit()


class TestThroughIntakeAndDispatch:
    async def test_a_plate_takes_the_file_for_the_machine_it_lands_on(
        self, db, fake_qbo, farm
    ):
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        await _order(db, 1)

        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        await db.commit()

        sent = farm.enqueued[0]
        # Whichever machine it went to, the file is the one sliced for it.
        by_printer = {1: 601, 2: 602, 3: 602, 4: 603}
        assert sent["archive_id"] == by_printer[sent["printer_id"]]
        # And the options that belong to that file went with it.
        assert sent["print_options"]["slicer"] in ("H2C", "H2D", "P1S")

    async def test_a_run_of_plates_uses_the_whole_farm(self, db, fake_qbo, farm):
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        await _order(db, 4)

        assert await printing.dispatch_pending(db) == {"dispatched": 4, "failed": 0}
        await db.commit()

        # Four machines, four plates, and no machine loaded twice while another
        # sat idle — the point of having a file for each of them.
        assert sorted(job["printer_id"] for job in farm.enqueued) == [1, 2, 3, 4]
        assert {job["archive_id"] for job in farm.enqueued} == {601, 602, 603}

    async def test_the_machines_it_cannot_reach_are_not_used(self, db, fake_qbo, farm):
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        # The H2D file goes; only H2C and P1S remain.
        await db.execute(
            PrintFile.__table__.delete().where(PrintFile.bambuddy_archive_id == 602)
        )
        await db.commit()
        await _order(db, 4)

        await printing.dispatch_pending(db)
        await db.commit()
        assert {job["printer_id"] for job in farm.enqueued} == {1, 4}

    async def test_no_machine_for_any_of_them_leaves_the_plate_queued(
        self, db, fake_qbo, farm
    ):
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        farm.printers = [printer(9, "A1 mini")]
        await _order(db, 1)

        assert await printing.dispatch_pending(db) == {"dispatched": 0, "failed": 1}
        await db.commit()

        job = (await db.execute(select(PrintJob))).scalars().one()
        assert job.status == "pending"
        # Everything it looked for, so the operator knows what to plug in.
        for model in ("H2C", "H2D", "P1S"):
            assert model in job.error

    async def test_a_pending_plate_names_a_file_before_it_is_sent(
        self, db, fake_qbo, farm
    ):
        """The queue screen has to show something for a plate not yet away."""
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        await _order(db, 1)

        job = (await db.execute(select(PrintJob))).scalars().one()
        assert job.status == "pending"
        assert job.bambuddy_archive_id in (601, 602, 603)
        assert len(job.candidates) == 3

    async def test_adding_a_file_corrects_a_plate_that_has_not_gone_out(
        self, db, fake_qbo, farm
    ):
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        await _order(db, 1)
        job = (await db.execute(select(PrintJob))).scalars().one()
        assert len(job.candidates) == 3

        # A fourth machine gets its own slicing after the order arrived.
        db.add(
            PrintFile(
                product_id=catalog["part_y"].id,
                bambuddy_archive_id=604,
                bambuddy_archive_name="part-y-X2D.3mf",
                plate_number=1,
                units_per_plate=1,
                printer_models=["X2D"],
            )
        )
        await db.commit()

        line = job.order_line_id
        from app.models import OrderLine

        await printing.plan_jobs(db, [await db.get(OrderLine, line)])
        await db.commit()

        job = (await db.execute(select(PrintJob))).scalars().one()
        assert len(job.candidates) == 4

    async def test_the_queue_screen_says_which_file_and_which_machine(
        self, db, fake_qbo, farm
    ):
        """Six plates of one product are no longer six of the same thing."""
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        await _order(db, 4)

        waiting = await printing.open_jobs_overview(db)
        # Before dispatch there is no machine yet, so it says what could take it.
        # Which file it opens on is arbitrary — the point is that it names one.
        assert {row["file_label"] for row in waiting} <= {
            "part-y-H2C.3mf", "part-y-H2D.3mf", "part-y-P1S.3mf"
        }
        assert all(row["file_label"] for row in waiting)
        assert all(row["printer_id"] is None for row in waiting)
        assert all(len(row["printer_models"]) == 1 for row in waiting)

        await printing.dispatch_pending(db)
        await db.commit()

        sent = await printing.open_jobs_overview(db)
        assert {row["file_label"] for row in sent} == {
            "part-y-H2C.3mf", "part-y-H2D.3mf", "part-y-P1S.3mf"
        }
        # And each row names the machine it actually went to.
        assert sorted(row["printer_id"] for row in sent) == [1, 2, 3, 4]

    async def test_a_queue_that_moved_is_found_rather_than_reported(
        self, db, fake_qbo, monkeypatch
    ):
        """The picker heals a moved endpoint; sending a plate has to as well.

        An instance that keeps its library at /api/v1 keeps its queue there too,
        and a connection made before that release never discovered either. The
        file manager already re-reads the document on a 404 — a queue that does
        not would leave a shop able to pick files and unable to print them.
        """
        from app.integrations.base import IntegrationError

        class MovedQueue(FarmBambuddy):
            def __init__(self, printers):
                super().__init__(printers)
                self.paths = {"queue": "/api/queue", "printers": "/api/printers"}
                self.resolved: list[tuple[str, ...]] = []

            async def enqueue(self, **kwargs):
                if self.paths["queue"] != "/api/v1/queue":
                    raise IntegrationError("bambuddy", "HTTP 404", status_code=404)
                return await super().enqueue(**kwargs)

            async def resolve_paths(self, roles):
                self.resolved.append(tuple(roles))
                self.paths["queue"] = "/api/v1/queue"
                return {"queue": "/api/v1/queue"}

        fake = MovedQueue([printer(1, "H2C"), printer(2, "H2D"), printer(4, "P1S")])

        async def client_for(_session):
            return fake

        monkeypatch.setattr("app.services.printing.bambuddy_api.client_for", client_for)
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()

        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        await _order(db, 1)

        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        assert fake.resolved == [("queue",)]
        # And the corrected path is kept, so the next plate does not pay for it.
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["queue"] == "/api/v1/queue"

    async def test_plate_maths_uses_the_least_any_file_promises(
        self, db, fake_qbo, farm
    ):
        """Over-printing wastes filament; under-printing ships an order short."""
        catalog = await seed_catalog(db)
        await _sliced_for_each_machine(db, catalog)
        rows = (
            await db.execute(
                select(PrintFile).where(PrintFile.product_id == catalog["part_y"].id)
            )
        ).scalars().all()
        rows[0].units_per_plate = 4
        rows[1].units_per_plate = 2
        rows[2].units_per_plate = 4
        await db.commit()

        await _order(db, 4)
        jobs = (await db.execute(select(PrintJob))).scalars().all()
        # Two per plate is the worst case, so four units needs two plates.
        assert len(jobs) == 2
        assert {job.units_expected for job in jobs} == {2}
