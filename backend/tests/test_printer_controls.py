"""Telling a machine what to do, rather than only watching it.

Two separate questions run through all of this, and keeping them apart is the
whole design. *Can this Bambuddy pause a print* is the instance's answer, read
off its own API document — these are self-hosted builds that differ, and a Pause
button that posts nothing anywhere is worse than no button because the operator
walks away believing the machine stopped. *Should Pause be pressable right now*
is a question about what the machine is doing, and it is asked twice: once to
draw the card, and once on the way through, because the card is up to fifteen
seconds old and the print it offers to pause may have finished.

The cases worth pinning are the awkward ones: a build that spells cancel where
another says stop, a control PrintFlow has never heard of, a machine whose state
the build does not report at all, and a page that has gone stale.
"""

from __future__ import annotations

import pytest

from app.integrations import bambuddy as bambuddy_api
from app.integrations.bambuddy import (
    control_name,
    discover_paths,
    discover_printer_controls,
    ensure_controls,
)
from app.models import PROVIDER_BAMBUDDY, AuditLog
from app.services import controls, credentials, farm

from test_printer_models import printer
from test_printers_page import FarmBambuddy, FarmClient

pytestmark = pytest.mark.asyncio


def spec(*paths: str) -> dict:
    """An OpenAPI paths block where every one of these is a POST."""
    return {path: {"post": {}} for path in paths}


# --------------------------------------------------------------------------
# What the build says it can do
# --------------------------------------------------------------------------


class TestDiscovery:
    def test_the_plain_shape(self):
        found = discover_printer_controls(
            spec(
                "/api/printers/{printer_id}/pause",
                "/api/printers/{printer_id}/resume",
                "/api/printers/{printer_id}/stop",
            )
        )
        assert found == {
            "pause": "/api/printers/{printer_id}/pause",
            "resume": "/api/printers/{printer_id}/resume",
            "stop": "/api/printers/{printer_id}/stop",
        }

    def test_controls_behind_a_wrapper(self):
        """Some builds file all of them under /control or /command."""
        found = discover_printer_controls(
            spec("/api/v1/printers/{id}/control/pause",
                 "/api/v1/printers/{id}/commands/resume")
        )
        assert set(found) == {"pause", "resume"}
        # The parameter is renamed to ours, whatever this build called it.
        assert found["pause"] == "/api/v1/printers/{printer_id}/control/pause"

    def test_a_build_that_spells_it_differently(self):
        """`cancel`, `abort` and `stop` are one button, not three."""
        assert control_name("cancel") == "stop"
        assert control_name("abort") == "stop"
        assert control_name("unpause") == "resume"
        assert control_name("chamber_light") == "light"

        found = discover_printer_controls(spec("/api/printers/{printer_id}/cancel"))
        assert found == {"stop": "/api/printers/{printer_id}/cancel"}

    def test_two_paths_for_the_same_control_is_one_button(self):
        found = discover_printer_controls(
            spec("/api/printers/{printer_id}/pause",
                 "/api/printers/{printer_id}/control/pause")
        )
        assert found == {"pause": "/api/printers/{printer_id}/pause"}

    def test_a_control_printflow_has_never_heard_of_still_appears(self):
        """The point of reading the spec is the ones nobody hardcoded.

        A build that can level the bed says so in its own document, and dropping
        it because PrintFlow's list is short is how a shop misses a control it
        already owns.
        """
        found = discover_printer_controls(spec("/api/printers/{printer_id}/bed-level"))
        assert found == {"bed-level": "/api/printers/{printer_id}/bed-level"}
        assert controls.label_for("bed-level") == "Bed level"

    def test_printing_a_file_is_not_a_control(self):
        """Dispatch has a whole service behind it; it is not a button on a card."""
        assert discover_printer_controls(spec("/api/printers/{printer_id}/print")) == {}

    def test_plumbing_is_not_a_control(self):
        assert discover_printer_controls(
            spec(
                "/api/printers/{printer_id}/upload",
                "/api/printers/{printer_id}/camera",
                "/api/printers/{printer_id}/login",
                "/api/printers/{printer_id}/files",
            )
        ) == {}

    def test_something_that_is_only_readable_is_not_a_control(self):
        assert discover_printer_controls({"/api/printers/{id}/pause": {"get": {}}}) == {}

    def test_it_has_to_be_one_machine(self):
        """A farm-wide /pause-everything is not a button on one card."""
        assert discover_printer_controls(spec("/api/pause")) == {}
        assert discover_printer_controls(spec("/api/queue/{queue_id}/pause")) == {}
        assert discover_printer_controls(
            spec("/api/printers/{printer_id}/jobs/{job_id}/pause")
        ) == {}

    def test_the_controls_ride_along_with_every_other_path(self):
        """Discovery is one read of the document, not a second pass."""
        found = discover_paths(
            {"/api/printers": {"get": {}}, **spec("/api/printers/{printer_id}/pause")}
        )
        assert found["control_pause"]["path"] == "/api/printers/{printer_id}/pause"


# --------------------------------------------------------------------------
# When a button applies
# --------------------------------------------------------------------------


OFFERED = {
    "pause": "/api/printers/{printer_id}/pause",
    "resume": "/api/printers/{printer_id}/resume",
    "stop": "/api/printers/{printer_id}/stop",
    "home": "/api/printers/{printer_id}/home",
}


def enabled_at(state: str) -> set[str]:
    return {
        row["action"] for row in controls.for_state(OFFERED, state) if row["enabled"]
    }


class TestWhenItApplies:
    def test_a_printing_machine(self):
        assert enabled_at("printing") == {"pause", "stop"}

    def test_a_paused_machine(self):
        assert enabled_at("paused") == {"resume", "stop"}

    def test_a_free_machine(self):
        """Nothing to pause or stop, and homing is safe only now."""
        assert enabled_at("idle") == {"home"}

    def test_a_failed_machine_can_still_be_cleared(self):
        assert "stop" in enabled_at("failed")
        assert "home" in enabled_at("failed")

    def test_an_offline_machine_takes_no_orders(self):
        assert enabled_at("offline") == set()

    def test_a_build_that_reports_no_state_is_not_locked_out(self):
        """`unknown` means the build did not say, not that nothing is happening.

        Some builds report no live status at all. Greying every button out over
        a reading PrintFlow never got would be an opinion dressed up as a fact,
        and it would take the controls away from exactly the shops that most
        need them.
        """
        assert enabled_at("unknown") == set(OFFERED)

    def test_what_does_not_apply_stays_on_the_card_with_a_reason(self):
        """A row that rearranges itself between the glance and the click.

        The button beside Resume is Stop. If Resume disappears when a print
        starts, Stop moves under the cursor.
        """
        rows = controls.for_state(OFFERED, "printing")
        assert [row["action"] for row in rows] == ["pause", "resume", "stop", "home"]
        resume = next(row for row in rows if row["action"] == "resume")
        assert (resume["enabled"], resume["why"]) == (False, "Nothing is paused")

    def test_offline_says_offline_rather_than_the_usual_reason(self):
        rows = controls.for_state(OFFERED, "offline")
        assert {row["why"] for row in rows} == {"The machine is offline"}

    def test_stopping_a_print_asks_first(self):
        stop = next(
            row for row in controls.for_state(OFFERED, "printing", printer="H2D-01")
            if row["action"] == "stop"
        )
        assert stop["danger"] is True
        assert "H2D-01" in stop["confirm"]

    def test_pausing_does_not(self):
        pause = next(
            row for row in controls.for_state(OFFERED, "printing")
            if row["action"] == "pause"
        )
        assert (pause["danger"], pause["confirm"]) == (False, None)

    def test_a_control_nobody_recognises_asks_first(self):
        """PrintFlow cannot say what it does, which is reason enough to ask."""
        row = controls.for_state({"bed-level": "/x"}, "idle")[0]
        assert row["confirm"] == "Send bed level to this printer?"

    def test_a_build_with_no_controls_gets_no_buttons(self):
        assert controls.for_state({}, "printing") == []


# --------------------------------------------------------------------------
# Asked once, then remembered
# --------------------------------------------------------------------------


class TestEnsureControls:
    async def test_what_the_build_offers_is_found_and_kept(self, db):
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        **spec("/api/printers/{printer_id}/pause")},
            listing=[],
        )

        assert await ensure_controls(db, client) == {
            "pause": "/api/printers/{printer_id}/pause"
        }
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["control_pause"] == (
            "/api/printers/{printer_id}/pause"
        )

    async def test_a_build_with_none_is_asked_exactly_once(self, db):
        """The farm screen redraws every fifteen seconds; this cannot be a fetch."""
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(spec_paths={"/api/printers": {"get": {}}}, listing=[])

        assert await ensure_controls(db, client) == {}
        assert client.specs_read == 1

        again = FarmClient(
            spec_paths={"/api/printers": {"get": {}}}, listing=[]
        )
        assert await ensure_controls(db, again) == {}
        assert again.specs_read == 0

    async def test_a_known_answer_costs_nothing(self, db):
        await credentials.save(
            db,
            PROVIDER_BAMBUDDY,
            {
                "base_url": "http://b.local",
                "discovered_paths": {"control_stop": "/api/printers/{printer_id}/stop"},
            },
        )
        await db.commit()
        client = await bambuddy_api.client_for(db)

        assert await ensure_controls(db, client) == {
            "stop": "/api/printers/{printer_id}/stop"
        }

    async def test_a_path_typed_by_hand_counts(self, db):
        """A build whose control is named something PrintFlow cannot guess."""
        await credentials.save(
            db,
            PROVIDER_BAMBUDDY,
            {
                "base_url": "http://b.local",
                "paths": {"control_pause": "/weird/{printer_id}/hold"},
            },
        )
        await db.commit()
        client = await bambuddy_api.client_for(db)

        assert await ensure_controls(db, client) == {"pause": "/weird/{printer_id}/hold"}

    async def test_nothing_is_ever_guessed(self, db):
        """No default path for a control, deliberately.

        A guessed listing path that 404s costs one failed read. A guessed
        control path is a POST at a machine mid-print, and a build serving
        something else at that address would be sent it.
        """
        await credentials.save(db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local"})
        await db.commit()
        client = await bambuddy_api.client_for(db)
        assert client.printer_controls() == {}


# --------------------------------------------------------------------------
# The page and the endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def bambu(monkeypatch):
    fake = FarmBambuddy([])
    fake.controls = dict(OFFERED)

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.farm.bambuddy_api.client_for", client_for)
    monkeypatch.setattr("app.routers.printers_router.bambuddy_api.client_for", client_for)

    async def read(_session, client, **_kwargs):
        return {"printers": list(client.printers), "detailed": True, "detail_error": None}

    monkeypatch.setattr("app.services.farm.bambuddy_api.read_farm", read)
    return fake


class TestOnThePage:
    async def test_each_card_carries_its_own_buttons(self, db, bambu):
        bambu.printers = [printer(1, "H2D", status="RUNNING"), printer(2, "H2D")]

        cards = (await farm.overview(db))["printers"]

        first = {row["action"]: row["enabled"] for row in cards[0]["controls"]}
        second = {row["action"]: row["enabled"] for row in cards[1]["controls"]}
        assert first["pause"] is True and first["home"] is False
        assert second["pause"] is False and second["home"] is True

    async def test_the_machine_is_named_in_what_it_asks(self, db, bambu):
        bambu.printers = [printer(1, "H2D", status="RUNNING")]
        card = (await farm.overview(db))["printers"][0]
        stop = next(row for row in card["controls"] if row["action"] == "stop")
        assert "P1" in stop["confirm"]

    async def test_a_build_with_no_controls_puts_none_on_the_cards(self, db, bambu):
        bambu.controls = {}
        bambu.printers = [printer(1, "H2D")]
        assert (await farm.overview(db))["printers"][0]["controls"] == []


class TestTheEndpoint:
    async def test_it_reaches_the_machine(self, signed_in, bambu):
        bambu.printers = [printer(1, "H2D", status="RUNNING")]

        response = await signed_in.post("/api/printers/1/control/pause")

        assert response.status_code == 200
        assert bambu.sent == [("1", "pause")]

    async def test_a_build_that_cannot_do_it_says_so(self, signed_in, bambu):
        bambu.controls = {"pause": "/api/printers/{printer_id}/pause"}
        bambu.printers = [printer(1, "H2D", status="RUNNING")]

        response = await signed_in.post("/api/printers/1/control/stop")

        assert response.status_code == 409
        assert "does not offer" in response.json()["detail"]
        assert bambu.sent == []

    async def test_a_stale_page_does_not_pause_the_next_plate(self, signed_in, bambu):
        """The card is up to fifteen seconds old and the print has finished.

        Sending it anyway would at best do nothing and at worst pause whatever
        started next — a print nobody is watching, quietly stopping overnight.
        """
        bambu.printers = [printer(1, "H2D", status="idle")]

        response = await signed_in.post("/api/printers/1/control/pause")

        assert response.status_code == 409
        assert "it is idle" in response.json()["detail"]
        assert bambu.sent == []

    async def test_a_machine_the_farm_will_not_describe_is_still_commanded(
        self, signed_in, bambu
    ):
        """PrintFlow could not read a status. The operator can see the machine."""
        bambu.printers = []

        assert (await signed_in.post("/api/printers/9/control/pause")).status_code == 200
        assert bambu.sent == [("9", "pause")]

    async def test_a_bare_listing_makes_it_ask_the_machine_itself(
        self, signed_in, bambu, monkeypatch
    ):
        """Some builds list names and models and keep the live state elsewhere.

        Taking the listing's silence for "nothing is happening" would refuse
        every Pause on those builds; taking it for "who knows" would let a stale
        card through unchecked. So the one machine is asked directly, and only
        when the cheap answer was no answer.
        """
        bambu.printers = [{"id": 1, "name": "P1", "model": "H2D", "online": True}]
        asked: list[int] = []

        async def read_printer(printer_id):
            asked.append(printer_id)
            return {"state": "RUNNING"}

        monkeypatch.setattr(bambu, "read_printer", read_printer, raising=False)

        assert (await signed_in.post("/api/printers/1/control/pause")).status_code == 200
        assert asked == [1]
        assert bambu.sent == [("1", "pause")]

    async def test_and_that_answer_is_believed_when_it_refuses(
        self, signed_in, bambu, monkeypatch
    ):
        bambu.printers = [{"id": 1, "name": "P1", "model": "H2D", "online": True}]

        async def read_printer(_printer_id):
            return {"state": "FINISH"}

        monkeypatch.setattr(bambu, "read_printer", read_printer, raising=False)

        response = await signed_in.post("/api/printers/1/control/pause")
        assert response.status_code == 409
        assert bambu.sent == []

    async def test_it_is_written_down(self, signed_in, db, bambu):
        bambu.printers = [printer(1, "H2D", status="RUNNING")]
        await signed_in.post("/api/printers/1/control/stop")

        rows = (
            await db.execute(
                AuditLog.__table__.select().where(AuditLog.action == "printer_control")
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].detail == {"printer_id": 1, "control": "stop", "state": "printing"}

    async def test_signing_in_is_required(self, client):
        assert (await client.post("/api/printers/1/control/stop")).status_code == 401
