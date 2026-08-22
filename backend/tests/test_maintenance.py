"""The maintenance book: what has been done to each machine, and when.

The thing most worth pinning here is not the arithmetic — there is barely any —
but the independence. These records are the one thing PrintFlow knows about a
printer that comes from nowhere else: not from Bambuddy, not from Etsy, not
from QuickBooks. A shop changes farm managers, re-adds a printer under a new
id, or services a machine no farm manager ever saw, and the history has to
still be there afterwards. Several tests exist only to hold that line.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Machine, MaintenanceLog
from app.services import maintenance
from app.services.maintenance import MaintenanceError

pytestmark = pytest.mark.asyncio


async def _machine(client, name="H2D-01", **fields):
    response = await client.post(
        "/api/maintenance/machines", json={"name": name, **fields}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _log(client, machine_id, **fields):
    response = await client.post(
        f"/api/maintenance/machines/{machine_id}/logs",
        json={"notes": "Nozzle changed", **fields},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Reading what was typed
# --------------------------------------------------------------------------


class TestTheFields:
    """Hours, date, notes, status — and what each of them refuses."""

    def test_hours_take_a_decimal_reading(self):
        assert maintenance.hours("1240.5") == Decimal("1240.50")
        # Shops write four figures with a comma in them.
        assert maintenance.hours("1,240") == Decimal("1240.00")

    def test_no_reading_is_a_real_answer(self):
        """Plenty of entries are written from memory rather than the screen.

        An invented zero would put a fake reading between two real ones and
        make the next service look overdue.
        """
        assert maintenance.hours(None) is None
        assert maintenance.hours("") is None

    def test_rubbish_hours_are_refused_rather_than_read_as_zero(self):
        with pytest.raises(MaintenanceError, match="not a number of hours"):
            maintenance.hours("about a thousand")
        with pytest.raises(MaintenanceError, match="negative"):
            maintenance.hours("-5")

    def test_no_date_means_today(self):
        """The usual case: somebody logging what they have just done."""
        assert maintenance.check_date(None) == datetime.now(timezone.utc).date()
        assert maintenance.check_date("2026-08-14") == date(2026, 8, 14)

    def test_a_status_has_to_be_one_of_the_five(self):
        assert maintenance.check_status("DOWN") == "down"
        with pytest.raises(MaintenanceError, match="not a status"):
            maintenance.check_status("knackered")

    async def test_an_entry_with_nothing_in_it_is_refused(self, signed_in, db):
        """A date and a status alone say nothing anybody can act on later."""
        machine = await _machine(signed_in)

        response = await signed_in.post(
            f"/api/maintenance/machines/{machine['id']}/logs",
            json={"status": "ok", "notes": "", "hours": ""},
        )

        assert response.status_code == 400
        assert "note or an hour reading" in response.json()["detail"]

    async def test_an_hour_reading_alone_is_enough(self, signed_in, db):
        """"1240" against a date is a perfectly good service record."""
        machine = await _machine(signed_in)
        body = await _log(signed_in, machine["id"], notes="", hours="1240")
        assert body["logs"][0]["hours"] == "1240.00"


# --------------------------------------------------------------------------
# The book itself
# --------------------------------------------------------------------------


class TestTheBook:
    async def test_an_entry_lands_on_its_machine(self, signed_in, db):
        machine = await _machine(signed_in)

        body = await _log(
            signed_in,
            machine["id"],
            logged_on="2026-08-14",
            hours="1240.5",
            status="serviced",
            notes="Nozzle changed, belts tensioned",
        )

        entry = body["logs"][0]
        assert entry["logged_on"] == "2026-08-14"
        assert entry["hours"] == "1240.50"
        assert entry["status"] == "serviced"
        assert entry["notes"] == "Nozzle changed, belts tensioned"

    async def test_it_is_signed(self, signed_in, db):
        """A maintenance log nobody signed is one nobody can ask about."""
        machine = await _machine(signed_in)
        body = await _log(signed_in, machine["id"])
        assert body["logs"][0]["actor"] == "admin"

    async def test_the_newest_entry_is_the_machine_s_condition(self, signed_in, db):
        """Rather than a status somebody has to remember to change separately.

        Read from the history, so it cannot drift from the entries that explain
        it — and it is a thing they were going to write anyway.
        """
        machine = await _machine(signed_in)
        await _log(signed_in, machine["id"], logged_on="2026-08-01", status="serviced")

        body = await _log(
            signed_in, machine["id"], logged_on="2026-08-14", status="down",
            notes="Extruder jammed",
        )

        assert body["status"] == "down"
        assert body["status_label"] == "Out of service"
        assert body["needs_somebody"] is True

    async def test_the_book_reads_newest_first(self, signed_in, db):
        machine = await _machine(signed_in)
        await _log(signed_in, machine["id"], logged_on="2026-08-01")
        body = await _log(signed_in, machine["id"], logged_on="2026-08-14")
        assert [row["logged_on"] for row in body["logs"]] == ["2026-08-14", "2026-08-01"]

    async def test_the_last_reading_is_the_last_one_anybody_wrote(self, signed_in, db):
        """A note about a rattle need not carry the hour counter.

        The machine's hours are still 1240 — the newest entry simply did not
        say, and showing a blank there would look like the counter was reset.
        """
        machine = await _machine(signed_in)
        await _log(signed_in, machine["id"], logged_on="2026-08-01", hours="1240")

        body = await _log(
            signed_in, machine["id"], logged_on="2026-08-14", notes="Rattling",
            status="attention",
        )

        assert body["logs"][0]["hours"] is None
        assert body["last_hours"] == "1240.00"

    async def test_a_machine_nobody_has_logged_has_no_condition(self, signed_in, db):
        """Which is a real state, not an error."""
        machine = await _machine(signed_in)
        assert machine["status"] is None
        assert machine["needs_somebody"] is False

    async def test_an_entry_can_be_corrected(self, signed_in, db):
        """A shop's own notebook, not an accounting system: a typo in an hour
        reading should be crossed out rather than lived with."""
        machine = await _machine(signed_in)
        body = await _log(signed_in, machine["id"], hours="240")
        entry = body["logs"][0]

        fixed = await signed_in.patch(
            f"/api/maintenance/machines/{machine['id']}/logs/{entry['id']}",
            json={"hours": "1240"},
        )

        assert fixed.json()["logs"][0]["hours"] == "1240.00"

    async def test_correcting_one_records_what_it_was(self, signed_in, db):
        """The crossing-out has to be kept somewhere, and this is where."""
        from app.models import AuditLog

        machine = await _machine(signed_in)
        entry = (await _log(signed_in, machine["id"], hours="240"))["logs"][0]
        await signed_in.patch(
            f"/api/maintenance/machines/{machine['id']}/logs/{entry['id']}",
            json={"hours": "1240"},
        )

        rows = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "maintenance_log_changed")
            )
        ).scalars().all()
        assert rows[0].detail["was"]["hours"] == "240.00"

    async def test_an_entry_can_be_deleted(self, signed_in, db):
        machine = await _machine(signed_in)
        entry = (await _log(signed_in, machine["id"]))["logs"][0]

        body = (
            await signed_in.delete(
                f"/api/maintenance/machines/{machine['id']}/logs/{entry['id']}"
            )
        ).json()

        assert body["logs"] == []

    async def test_an_entry_belonging_to_another_machine_is_not_found(
        self, signed_in, db
    ):
        one = await _machine(signed_in, name="H2D-01")
        two = await _machine(signed_in, name="H2D-02")
        entry = (await _log(signed_in, one["id"]))["logs"][0]

        response = await signed_in.delete(
            f"/api/maintenance/machines/{two['id']}/logs/{entry['id']}"
        )

        assert response.status_code == 404


# --------------------------------------------------------------------------
# Machines
# --------------------------------------------------------------------------


class TestMachines:
    async def test_trouble_sorts_to_the_top(self, signed_in, db):
        """A list in name order buries the machine somebody opened the tab for."""
        fine = await _machine(signed_in, name="A2L-01")
        broken = await _machine(signed_in, name="Z-Printer")
        await _log(signed_in, fine["id"], status="ok")
        await _log(signed_in, broken["id"], status="down", notes="Jammed")

        body = (await signed_in.get("/api/maintenance")).json()

        assert [row["name"] for row in body["machines"]][0] == "Z-Printer"

    async def test_a_machine_with_no_entries_sits_under_the_broken_ones(
        self, signed_in, db
    ):
        """Not a problem, but the other thing worth noticing."""
        await _machine(signed_in, name="A-Logged")
        await _machine(signed_in, name="Z-Never-Logged")
        logged = (await signed_in.get("/api/maintenance")).json()["machines"]
        await _log(signed_in, logged[0]["id"] if logged[0]["name"] == "A-Logged"
                   else logged[1]["id"], status="ok")

        names = [row["name"] for row in (await signed_in.get("/api/maintenance")).json()["machines"]]

        assert names == ["Z-Never-Logged", "A-Logged"]

    async def test_two_machines_cannot_share_a_name(self, signed_in, db):
        await _machine(signed_in, name="H2D-01")
        response = await signed_in.post(
            "/api/maintenance/machines", json={"name": "H2D-01"}
        )
        assert response.status_code == 400
        assert "already a machine" in response.json()["detail"]

    async def test_retiring_keeps_the_history_and_moves_it_aside(self, signed_in, db):
        machine = await _machine(signed_in)
        await _log(signed_in, machine["id"])

        body = (
            await signed_in.patch(
                f"/api/maintenance/machines/{machine['id']}", json={"active": False}
            )
        ).json()

        assert body["active"] is False
        assert len(body["logs"]) == 1

    async def test_deleting_takes_the_book_with_it(self, signed_in, db):
        machine = await _machine(signed_in)
        await _log(signed_in, machine["id"])

        body = (
            await signed_in.delete(f"/api/maintenance/machines/{machine['id']}")
        ).json()

        assert body["logs"] == 1
        assert (await db.execute(select(MaintenanceLog))).scalars().all() == []
        assert (await db.execute(select(Machine))).scalars().all() == []

    async def test_signing_in_is_required(self, client):
        assert (await client.get("/api/maintenance")).status_code == 401
        assert (
            await client.post("/api/maintenance/machines", json={"name": "x"})
        ).status_code == 401


# --------------------------------------------------------------------------
# Independent of everything else
# --------------------------------------------------------------------------


class TestItStandsOnItsOwn:
    """The point of the whole tab.

    Every other thing PrintFlow knows about a printer is read live from
    Bambuddy and is only as durable as that connection. This is the one record
    that has to survive it.
    """

    async def test_a_machine_needs_no_farm_at_all(self, signed_in, db):
        """No Bambuddy configured, and the book still works end to end."""
        machine = await _machine(signed_in, name="Old Ender in the corner")

        body = await _log(signed_in, machine["id"], hours="4000", status="due")

        assert body["bambuddy_printer_id"] is None
        assert body["status"] == "due"

    async def test_the_history_survives_the_printer_leaving_the_farm(
        self, signed_in, db
    ):
        """The link going stale takes nothing with it.

        A shop that re-points Bambuddy, or removes a printer from it, keeps
        every entry — which is exactly what a log keyed on somebody else's id
        would have lost.
        """
        machine = await _machine(signed_in, name="H2D-01", bambuddy_printer_id="3")
        await _log(signed_in, machine["id"], hours="1240")

        cleared = (
            await signed_in.patch(
                f"/api/maintenance/machines/{machine['id']}",
                json={"bambuddy_printer_id": ""},
            )
        ).json()

        assert cleared["bambuddy_printer_id"] is None
        assert cleared["last_hours"] == "1240.00"

    async def test_an_unreachable_farm_offers_nothing_rather_than_failing(
        self, signed_in, db
    ):
        """Adopting is a shortcut to typing a name. It cannot be a dependency."""
        response = await signed_in.get("/api/maintenance/adoptable")

        assert response.status_code == 200
        assert response.json()["printers"] == []
        assert response.json()["why"]

    async def test_it_offers_only_printers_this_book_does_not_have(self, db):
        await maintenance.add_machine(db, name="H2D-01", bambuddy_printer_id="3")
        await maintenance.add_machine(db, name="P1S-01")
        await db.flush()

        offered = await maintenance.adoptable(
            db,
            [
                {"id": 3, "name": "H2D-01", "model": "H2D"},   # linked already
                {"id": 6, "name": "P1S-01", "model": "P1S"},   # same name
                {"id": 9, "name": "X2D-01", "model": "X2D"},   # new
            ],
        )

        assert [row["name"] for row in offered] == ["X2D-01"]

    async def test_adopting_one_links_it_without_depending_on_it(self, signed_in, db):
        body = await _machine(
            signed_in, name="X2D-01", model="X2D", bambuddy_printer_id="9"
        )
        assert body["bambuddy_printer_id"] == "9"
        # And it is an ordinary machine from that moment on.
        assert (await _log(signed_in, body["id"], hours="12"))["last_hours"] == "12.00"
