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

import json
from contextlib import asynccontextmanager

import pytest

from app.integrations import bambuddy as bambuddy_api
from app.integrations.bambuddy import (
    BambuddyClient,
    camera_paths,
    discover_paths,
    _trays,
    filament_rows,
    parse_printer,
    read_farm,
)
from app.integrations.base import IntegrationError
from app.models import PROVIDER_BAMBUDDY, AuditLog, PrintFile, PrintJob
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
        return {
            "path": "/openapi.json",
            "discovered": discover_paths(self.spec_paths),
            "cameras": camera_paths(self.spec_paths),
        }

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
# Cameras
# --------------------------------------------------------------------------


# A real 1x1 JPEG. Small, but it starts and ends where a JPEG does.
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 8 + b"\xff\xd9"


def _serve(monkeypatch, content_type: str, chunks: list[bytes], status: int = 200):
    """Stand a camera up behind the proxy, sending exactly these bytes."""

    class Reply:
        headers = {"content-type": content_type}

        async def aiter_bytes(self):
            for chunk in chunks:
                yield chunk

    class Camera:
        base_url = "http://b.local"
        paths = {"camera_token": "/api/v1/printers/camera/stream-token"}
        explicit_paths: dict = {}
        discovered_paths: dict = {}

        @asynccontextmanager
        async def camera(self, printer_id, *, override=None, token=None, style=None):
            if status >= 400:
                raise IntegrationError(
                    "bambuddy", f"HTTP {status}", status_code=status
                )
            yield Reply()

    async def client_for(_session):
        return Camera()

    monkeypatch.setattr(
        "app.routers.printers_router.bambuddy_api.client_for", client_for
    )


class TestDiscoverPrinterCamera:
    def test_the_live_one_beats_the_still(self):
        # A frame can be taken out of a stream; a stream cannot be made from a
        # frame, so the camera itself is the better address of the two.
        found = discover_paths(
            {
                "/api/v1/printers/{id}/camera": {"get": {}},
                "/api/v1/printers/{id}/snapshot": {"get": {}},
            }
        )["printer_camera"]
        assert found["path"] == "/api/v1/printers/{printer_id}/camera"

    def test_the_other_names_a_build_might_use(self):
        for leaf in ("stream", "video", "webcam", "mjpeg", "snapshot"):
            found = discover_paths({f"/api/printers/{{id}}/{leaf}": {"get": {}}})
            assert found["printer_camera"]["path"] == (
                f"/api/printers/{{printer_id}}/{leaf}"
            )

    def test_a_build_with_no_camera_has_none_found(self):
        found = discover_paths(
            {"/api/printers": {"get": {}}, "/api/printers/{id}/files": {"get": {}}}
        )
        assert found["printer_camera"]["path"] is None

    def test_the_machine_can_hang_off_the_camera_instead(self):
        # `/printers/{id}/camera` and `/camera/{id}` name the same thing; which
        # noun owns the other is a matter of taste, and builds differ.
        for path in ("/api/camera/{id}", "/api/v1/cameras/{id}/stream"):
            found = discover_paths({path: {"get": {}}})["printer_camera"]
            assert found["path"] is not None, path

    def test_a_separator_is_not_a_different_word(self):
        for leaf in ("camera_stream", "camera-feed", "camera.snapshot"):
            found = discover_paths({f"/api/printers/{{id}}/{leaf}": {"get": {}}})
            assert found["printer_camera"]["path"] == (
                f"/api/printers/{{printer_id}}/{leaf}"
            ), leaf

    def test_a_word_that_only_looks_camera_ish_is_not_one(self):
        found = discover_paths(
            {
                "/api/printers/{id}/camera-settings": {"get": {}},
                "/api/printers/{id}/stream-history": {"get": {}},
            }
        )["printer_camera"]
        assert found["path"] is None


class TestCameraTarget:
    def _client(self, **payload) -> BambuddyClient:
        return BambuddyClient({"base_url": "http://bambu.local:8000", **payload})

    def test_the_configured_path_by_default(self):
        client = self._client(paths={"printer_camera": "/api/v1/printers/{printer_id}/camera"})
        assert client.camera_target(4) == "http://bambu.local:8000/api/v1/printers/4/camera"

    def test_a_url_the_instance_named_for_itself_is_followed(self):
        client = self._client()
        assert client.camera_target(4, "http://bambu.local:8000/cam/4") == (
            "http://bambu.local:8000/cam/4"
        )
        assert client.camera_is_ours("http://bambu.local:8000/cam/4") is True

    def test_a_url_somewhere_else_on_the_network_is_not(self):
        """PrintFlow would be fetching it on behalf of whoever opened the page."""
        client = self._client()
        elsewhere = "http://192.168.10.30:8080/stream"
        assert client.camera_is_ours(elsewhere) is False
        # And it falls back to the configured path rather than going there.
        assert client.camera_target(4, elsewhere).startswith("http://bambu.local:8000/")

    def test_a_path_with_no_host_is_this_host(self):
        client = self._client()
        assert client.camera_target(4, "/cam/4.jpg") == "http://bambu.local:8000/cam/4.jpg"


class TestStillFrame:
    async def test_a_still_camera_is_passed_straight_through(self, signed_in, monkeypatch):
        _serve(monkeypatch, "image/jpeg", [JPEG])
        response = await signed_in.get("/api/printers/4/camera")
        assert response.status_code == 200
        assert response.content == JPEG
        assert response.headers["content-type"] == "image/jpeg"
        # A card asks again for every frame; a cached one is a frozen printer.
        assert response.headers["cache-control"] == "no-store"

    async def test_one_frame_is_taken_out_of_a_stream(self, signed_in, monkeypatch):
        # Split across chunks, with multipart preamble and trailer around it —
        # a frame is what lies between the markers, whatever separates the parts.
        chunks = [
            b"--pfframe\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG[:10],
            JPEG[10:] + b"\r\n--pfframe\r\n" + JPEG,
        ]
        _serve(monkeypatch, "multipart/x-mixed-replace; boundary=pfframe", chunks)
        response = await signed_in.get("/api/printers/4/camera")
        assert response.status_code == 200
        assert response.content == JPEG
        assert response.headers["content-type"] == "image/jpeg"

    async def test_a_stream_with_no_picture_in_it_says_so(self, signed_in, monkeypatch):
        _serve(monkeypatch, "multipart/x-mixed-replace; boundary=x", [b"not a picture"])
        response = await signed_in.get("/api/printers/4/camera")
        assert response.status_code == 502
        assert "could not read a frame" in response.json()["detail"]

    async def test_a_machine_with_no_camera_is_a_plain_failure(
        self, signed_in, monkeypatch
    ):
        # The card takes its own space back on this; it is not a page error.
        _serve(monkeypatch, "application/json", [b'{"detail":"Not found"}'], status=404)
        assert (await signed_in.get("/api/printers/4/camera")).status_code == 502


class TestEnsureCamera:
    async def test_a_build_with_a_camera_is_found_and_kept(self, db):
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/v1/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
        )
        assert await bambuddy_api.ensure_camera(db, client) is True
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["printer_camera"] == (
            "/api/v1/printers/{printer_id}/camera"
        )

    async def test_a_build_without_one_is_asked_exactly_once(self, db):
        """Ten broken pictures per page load, or one question. This is the question."""
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(spec_paths={"/api/printers": {"get": {}}}, listing=LIVE_LISTING)

        assert await bambuddy_api.ensure_camera(db, client) is False
        assert client.specs_read == 1
        await db.commit()

        # A fresh client, as the next page load would build.
        again = FarmClient(spec_paths={"/api/printers": {"get": {}}}, listing=LIVE_LISTING)
        assert await bambuddy_api.ensure_camera(db, again) is False
        assert again.specs_read == 0

    async def test_the_cards_are_told_whether_there_is_a_picture(self, db):
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
        )
        assert [row["camera"] for row in (await read_farm(db, client))["printers"]] == [
            True
        ]

    async def test_a_camera_on_another_machine_is_not_ours_to_fetch(self, db):
        """It is on the shop network; a browser there may reach it, PrintFlow won't."""
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
        )
        await db.commit()
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=[{**LIVE_LISTING[0], "camera_url": "http://192.168.1.9/stream"}],
        )
        row = (await read_farm(db, client))["printers"][0]
        assert row["camera"] is False
        # But the page is still told where it said the camera was.
        assert row["camera_url"] == "http://192.168.1.9/stream"

    async def test_a_wider_rule_asks_a_build_that_was_told_no(self, db):
        """The shop upgrading to get cameras working must not be the one it misses."""
        await credentials.save(
            db,
            PROVIDER_BAMBUDDY,
            {
                "base_url": "http://b.local", "api_key": "k",
                # Asked under an older, narrower set of rules.
                "camera_checked": bambuddy_api.CAMERA_RULES - 1,
            },
        )
        await db.commit()
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}}, "/api/camera/{id}": {"get": {}}},
            listing=LIVE_LISTING,
        )
        assert await bambuddy_api.ensure_camera(db, client) is True
        assert client.paths["printer_camera"] == "/api/camera/{printer_id}"

    async def test_a_camera_named_by_hand_is_not_second_guessed(self, db):
        client = FarmClient(
            spec_paths={"/api/printers": {"get": {}}},
            listing=LIVE_LISTING,
        )
        client.explicit_paths["printer_camera"] = "/my/cam/{printer_id}"
        assert await bambuddy_api.ensure_camera(db, client) is True
        assert client.specs_read == 0


class CameraFarm(FarmClient):
    """A farm whose camera answers with whatever it was handed."""

    def __init__(self, *, reply: tuple[str, bytes] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.reply = reply

    @asynccontextmanager
    async def camera(self, printer_id, *, override=None, token=None, style=None):
        if self.reply is None:
            raise IntegrationError("bambuddy", "HTTP 404", status_code=404)
        content_type, body = self.reply

        class Reply:
            status_code = 200
            headers = {"content-type": content_type}

            async def aiter_bytes(self):
                yield body

        yield Reply()


class TokenGated(FarmClient):
    """A camera behind a short-lived token of its own — the reported shape.

    Refuses without one and says where to get one, which is how a build that
    does this generally behaves: the refusal is an instruction, not a fault.
    """

    def __init__(self, *, style: str = "query:token", names_the_minter: bool = True,
                 **kwargs):
        super().__init__(**kwargs)
        self.style = style
        self.names_the_minter = names_the_minter
        self.minted = 0
        self.presented: list[tuple[str | None, str | None]] = []

    async def mint_camera_token(self, printer_id=None):
        self.minted += 1
        self.asked.append("POST " + self.paths["camera_token"])
        return "tok"

    @asynccontextmanager
    async def camera(self, printer_id, *, override=None, token=None, style=None):
        self.presented.append((token, style))
        if token != "tok" or style != self.style:
            detail = "Valid camera stream token required."
            if self.names_the_minter:
                detail += " Obtain one from POST /api/v1/printers/camera/stream-token"
            raise IntegrationError(
                "bambuddy", "HTTP 401", status_code=401, body=json.dumps({"detail": detail})
            )

        class Reply:
            status_code = 200
            headers = {"content-type": "image/jpeg"}

            async def aiter_bytes(self):
                yield JPEG

        yield Reply()


class TestTokenGatedCamera:
    async def _saved(self, db, **extra):
        await credentials.save(
            db, PROVIDER_BAMBUDDY,
            {"base_url": "http://b.local", "api_key": "k", **extra},
        )
        await db.commit()

    def _client(self, **kwargs) -> TokenGated:
        return TokenGated(
            spec_paths={
                "/api/printers": {"get": {}},
                "/api/v1/printers/{printer_id}/camera/snapshot": {"get": {}},
                "/api/v1/printers/camera/stream-token": {"post": {}},
            },
            listing=LIVE_LISTING,
            **kwargs,
        )

    async def test_the_refusal_is_followed_rather_than_reported(self, db):
        await self._saved(db)
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client()

        async with bambuddy_api.open_camera(db, client, 1) as response:
            assert response.status_code == 200
        assert client.minted == 1
        # Asked plainly first, then with the token — the second one worked.
        assert client.presented[0] == (None, None)
        assert ("tok", "query:token") in client.presented

    async def test_what_worked_is_remembered(self, db):
        await self._saved(db)
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client()
        async with bambuddy_api.open_camera(db, client, 1):
            pass
        await db.commit()

        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["camera_auth"] == {
            "token_path": "/api/v1/printers/camera/stream-token",
            "style": "query:token",
        }

    async def test_a_known_build_does_not_wait_to_be_refused(self, db):
        """One round trip spent being told what is already known is one too many."""
        await self._saved(
            db,
            camera_auth={
                "token_path": "/api/v1/printers/camera/stream-token",
                "style": "query:token",
            },
        )
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client()

        async with bambuddy_api.open_camera(db, client, 1):
            pass
        # Straight out with a token; never asked without one.
        assert client.presented == [("tok", "query:token")]

    async def test_the_token_is_not_minted_once_per_picture(self, db):
        # A farm of ten cards would otherwise mint ten tokens per page load.
        await self._saved(db)
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client()
        for printer_id in (1, 2, 3):
            async with bambuddy_api.open_camera(db, client, printer_id):
                pass
        assert client.minted == 1

    async def test_a_build_wanting_a_header_instead_is_found_too(self, db):
        # Nothing in a 401 says *how* the token should be presented, so the
        # ways are tried in turn — once, and then remembered.
        await self._saved(db)
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client(style="X-Stream-Token")

        async with bambuddy_api.open_camera(db, client, 1) as response:
            assert response.status_code == 200
        await db.commit()
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["camera_auth"]["style"] == "X-Stream-Token"

    async def test_a_minter_the_refusal_names_is_used_over_a_guess(self, db):
        await self._saved(db)
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client()
        client.paths["camera_token"] = "/api/printers/camera/stream-token"

        async with bambuddy_api.open_camera(db, client, 1):
            pass
        assert client.paths["camera_token"] == "/api/v1/printers/camera/stream-token"

    async def test_a_refusal_that_names_nothing_falls_back_to_the_document(self, db):
        await self._saved(db)
        bambuddy_api.forget_camera_token("http://b.local")
        client = self._client(names_the_minter=False)
        client.paths["camera_token"] = "/api/printers/camera/stream-token"

        async with bambuddy_api.open_camera(db, client, 1):
            pass
        assert client.specs_read == 1
        assert client.paths["camera_token"] == "/api/v1/printers/camera/stream-token"

    async def test_a_failure_that_is_not_about_a_token_is_left_alone(self, db):
        await self._saved(db)

        class Broken(TokenGated):
            @asynccontextmanager
            async def camera(self, printer_id, *, override=None, token=None, style=None):
                raise IntegrationError("bambuddy", "HTTP 500", status_code=500)
                yield  # pragma: no cover

        client = Broken(
            spec_paths={"/api/printers": {"get": {}}}, listing=LIVE_LISTING
        )
        bambuddy_api.forget_camera_token("http://b.local")
        with pytest.raises(IntegrationError) as caught:
            async with bambuddy_api.open_camera(db, client, 1):
                pass
        assert caught.value.status_code == 500
        assert client.minted == 0


class TestDiscoverCameraToken:
    def test_the_minter_is_read_off_the_document(self):
        found = discover_paths(
            {
                "/api/v1/printers/camera/stream-token": {"post": {}},
                "/api/v1/auth/token": {"post": {}},
            }
        )["camera_token"]
        assert found["path"] == "/api/v1/printers/camera/stream-token"

    def test_a_token_endpoint_about_something_else_is_not_it(self):
        found = discover_paths({"/api/v1/auth/token": {"post": {}}})["camera_token"]
        assert found["path"] is None

    def test_the_reported_instance_picks_the_snapshot_among_its_decoys(self):
        """Its camera has ten endpoints and only one of them hands over a picture."""
        spec = {
            path: {"get": {}}
            for path in (
                "/api/v1/printers/{printer_id}/camera/snapshot",
                "/api/v1/printers/{printer_id}/camera/stream",
                "/api/v1/printers/{printer_id}/camera/status",
                "/api/v1/printers/{printer_id}/camera/stop",
                "/api/v1/printers/{printer_id}/camera/test",
                "/api/v1/printers/{printer_id}/camera/check-plate",
                "/api/v1/printers/{printer_id}/camera/plate-detection/status",
                "/api/v1/printers/usb-cameras",
                "/api/v1/camwall/printers",
            )
        }
        spec["/api/v1/printers/camera/stream-token"] = {"post": {}}
        found = discover_paths(spec)

        assert found["printer_camera"]["path"] == (
            "/api/v1/printers/{printer_id}/camera/snapshot"
        )
        # The stream is the runner-up; everything else is not a picture at all.
        assert found["printer_camera"]["alternatives"] == [
            "/api/v1/printers/{printer_id}/camera/stream"
        ]
        assert found["camera_token"]["path"] == "/api/v1/printers/camera/stream-token"


class TestCameraReport:
    async def _saved(self, db, **extra):
        await credentials.save(
            db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k", **extra}
        )
        await db.commit()

    async def test_a_build_with_no_camera_says_the_document_has_none(self, db):
        # Which is not a fault to fix — it is the answer.
        await self._saved(db)
        client = CameraFarm(
            spec_paths={"/api/printers": {"get": {}}}, listing=LIVE_LISTING
        )
        report = await bambuddy_api.camera_report(db, client)

        assert report["available"] is False
        assert report["candidates"] == []
        assert "guess" in report["source"]

    async def test_an_endpoint_that_was_not_recognised_is_still_shown(self, db):
        """The one case a person can fix, and the one PrintFlow cannot see."""
        await self._saved(db)
        client = CameraFarm(
            spec_paths={
                "/api/printers": {"get": {}},
                "/api/v1/monitor/{id}/live-view": {"get": {}},
            },
            listing=LIVE_LISTING,
        )
        report = await bambuddy_api.camera_report(db, client)

        assert report["available"] is False
        # PrintFlow does not call this a camera, but it is on the list to try.
        assert [row["path"] for row in report["candidates"]] == [
            "/api/v1/monitor/{id}/live-view"
        ]

    async def test_it_says_what_the_camera_actually_answered(self, db):
        await self._saved(db)
        client = CameraFarm(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
            reply=("image/jpeg", JPEG),
        )
        report = await bambuddy_api.camera_report(db, client)

        assert report["available"] is True
        assert report["probe"]["looks_like_a_picture"] is True
        assert report["probe"]["starts_with"].startswith("ffd8")

    async def test_a_camera_that_answers_with_a_web_page_is_not_a_camera(self, db):
        # An HTML login page comes back 200 and would leave a blank card with
        # nothing to say for itself. The first bytes give it away.
        await self._saved(db)
        client = CameraFarm(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
            reply=("text/html", b"<!doctype html><title>Sign in</title>"),
        )
        report = await bambuddy_api.camera_report(db, client)

        assert report["probe"]["looks_like_a_picture"] is False
        assert report["probe"]["content_type"] == "text/html"

    async def test_looking_again_forgets_that_it_was_told_no(self, db):
        """Press this after naming the path by hand, or after upgrading Bambuddy."""
        await self._saved(db, camera_checked=bambuddy_api.CAMERA_RULES)
        client = CameraFarm(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
            reply=("image/jpeg", JPEG),
        )

        assert (await bambuddy_api.camera_report(db, client))["available"] is False

        fresh = CameraFarm(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
            reply=("image/jpeg", JPEG),
        )
        again = await bambuddy_api.camera_report(db, fresh, again=True)
        assert again["available"] is True
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["printer_camera"] == (
            "/api/printers/{printer_id}/camera"
        )

    async def test_a_camera_that_will_not_answer_reports_the_failure(self, db):
        await self._saved(db)
        client = CameraFarm(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/camera": {"get": {}}},
            listing=LIVE_LISTING,
            reply=None,
        )
        report = await bambuddy_api.camera_report(db, client)
        assert "404" in report["probe"]["error"]


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


# --------------------------------------------------------------------------
# Everything the machine reports
# --------------------------------------------------------------------------


class TestFlatten:
    """"All available metrics" has to mean all of them, including the ones
    PrintFlow has no name for. That is only useful if the flattening keeps the
    build's own spelling, which is the whole point of showing it."""

    def test_nested_readings_keep_their_parent(self):
        row = parse_printer(
            {"id": 1, "name": "P1S", "ams": {"humidity": 3, "temp": 24.5}}
        )
        assert row["reported"]["ams.humidity"] == 3
        assert row["reported"]["ams.temp"] == 24.5

    def test_a_list_of_scalars_becomes_one_line(self):
        row = parse_printer({"id": 1, "supports": ["ams", "camera", "chamber"]})
        assert row["reported"]["supports"] == "ams, camera, chamber"

    def test_per_layer_arrays_are_not_a_metric(self):
        # A thousand rows of nothing a person reads off a screen.
        row = parse_printer({"id": 1, "layer_times": [{"n": 1}, {"n": 2}]})
        assert "layer_times" not in row["reported"]

    def test_nothing_reported_is_an_empty_table_not_a_missing_one(self):
        assert parse_printer({})["reported"] == {}

    def test_a_field_with_no_name_here_is_still_shown(self):
        row = parse_printer({"id": 1, "xcam_status": "buildplate_marker_detector"})
        assert row["reported"]["xcam_status"] == "buildplate_marker_detector"

    def test_a_build_that_reports_thousands_of_fields_does_not_take_the_page(self):
        row = parse_printer({f"f{n}": n for n in range(500)})
        assert len(row["reported"]) <= 200


class TestFilament:
    def test_an_ams_becomes_one_entry_per_tray(self):
        row = parse_printer(
            {
                "id": 1,
                "ams": [
                    {"id": 1, "type": "PLA", "color": "black", "remain": 82},
                    {"id": 2, "type": "PETG", "color": "white", "remain": 40},
                ],
            }
        )
        assert [(t["slot"], t["type"], t["remaining"]) for t in row["filament"]] == [
            (1, "PLA", 82),
            (2, "PETG", 40),
        ]

    def test_trays_kept_inside_an_object(self):
        row = parse_printer({"id": 1, "ams": {"trays": [{"tray_type": "ABS"}]}})
        assert [t["type"] for t in row["filament"]] == ["ABS"]

    def test_a_tray_with_no_slot_number_is_numbered_by_position(self):
        row = parse_printer({"id": 1, "trays": [{"type": "PLA"}, {"type": "TPU"}]})
        assert [t["slot"] for t in row["filament"]] == [1, 2]

    def test_a_machine_with_one_spool_and_no_ams(self):
        row = parse_printer(
            {"id": 1, "filament_type": "PLA", "filament_color": "orange"}
        )
        assert row["filament"] == [
            {
                "slot": 1, "type": "PLA", "brand": None, "colour": "orange",
                "colour_hex": "#f57c00", "remaining": None,
            }
        ]

    def test_a_machine_that_says_nothing_about_filament(self):
        assert parse_printer({"id": 1, "name": "P1S"})["filament"] == []


class TestMachineFacts:
    def test_what_the_machine_is_rather_than_what_it_is_doing(self):
        row = parse_printer(
            {
                "id": 1, "name": "H2D-01", "dev_id": "0309A2C", "fw_ver": "01.08.02",
                "ip_address": "192.168.10.31", "nozzle_diameter": "0.4",
                "nozzle_type": "hardened_steel", "wifi_signal": "-52dBm",
                "spd_lvl": 2, "cooling_fan_speed": "85", "print_count": 412,
            }
        )
        assert (row["serial"], row["firmware"]) == ("0309A2C", "01.08.02")
        assert (row["ip"], row["nozzle_diameter"]) == ("192.168.10.31", 0.4)
        assert (row["nozzle_type"], row["wifi_signal"]) == ("hardened_steel", "-52dBm")
        assert (row["speed_level"], row["fan_speed"]) == (2, 85)
        assert row["prints_completed"] == 412

    def test_a_build_that_reports_none_of_it(self):
        row = parse_printer({"id": 1, "name": "P1S"})
        assert row["serial"] is None and row["firmware"] is None
        assert row["nozzle_diameter"] is None and row["prints_completed"] is None


# --------------------------------------------------------------------------
# The farm in one line
# --------------------------------------------------------------------------


def _plate(status="pending", printer_id=None, units=1) -> dict:
    return {
        "status": status,
        "printer_id": printer_id,
        "units_expected": units,
        "created_at": "2026-01-01T00:00:00Z",
    }


class TestFarmSummary:
    def test_machines_counted_by_what_they_are_doing(self):
        found = farm._summary(
            [
                {"id": 1, "state": "RUNNING", "remaining_minutes": 20},
                {"id": 2, "state": "IDLE"},
                {"id": 3, "state": "IDLE"},
                {"id": 4, "online": False, "state": "RUNNING"},
            ],
            [],
        )
        assert (found["machines"], found["printing"]) == (4, 1)
        assert (found["idle"], found["offline"]) == (2, 1)

    def test_offline_beats_whatever_it_last_said(self):
        # A machine that was printing when the power went is not printing.
        found = farm._summary([{"id": 1, "online": False, "state": "RUNNING"}], [])
        assert found["printing"] == 0 and found["offline"] == 1

    def test_a_state_nothing_here_has_a_word_for_is_still_counted(self):
        found = farm._summary([{"id": 1, "state": "CALIBRATING"}], [])
        assert found["by_state"] == {"unknown": 1}

    def test_the_farm_is_clear_when_the_last_machine_finishes(self):
        found = farm._summary(
            [
                {"id": 1, "state": "RUNNING", "remaining_minutes": 12},
                {"id": 2, "state": "RUNNING", "remaining_minutes": 240},
            ],
            [],
        )
        assert found["busy_until_minutes"] == 240

    def test_an_idle_farm_has_no_time_to_report(self):
        assert farm._summary([{"id": 1, "state": "IDLE"}], [])["busy_until_minutes"] is None

    def test_finished_plates_are_not_outstanding_work(self):
        found = farm._summary(
            [],
            [
                _plate("pending", None, 3),
                _plate("printing", 1, 2),
                _plate("done", 1, 9),
                _plate("cancelled", 1, 9),
            ],
        )
        assert (found["plates_open"], found["units_open"]) == (2, 5)

    def test_plates_with_no_machine_are_called_out(self):
        found = farm._summary([], [_plate("pending", None), _plate("queued", 1)])
        assert found["plates_waiting"] == 1

    def test_an_empty_farm_still_answers(self):
        found = farm._summary([], [])
        assert found["machines"] == 0 and found["plates_open"] == 0

    async def test_the_page_gets_the_summary_with_the_cards(self, signed_in, db, bambu):
        body = (await signed_in.get("/api/printers")).json()
        assert body["summary"]["machines"] == 2


# --------------------------------------------------------------------------
# Printing something that nobody ordered
# --------------------------------------------------------------------------


class TestPrintNow:
    """The reprint, the test piece, the one the shop needs and nobody bought.

    It bypasses dispatch on purpose — the operator has already decided which
    machine by clicking on it — so the things that matter are that it goes to
    *that* machine, that it is written down, and that a half-failure says how
    far it got rather than leaving somebody to guess."""

    async def test_a_file_goes_to_the_machine_that_was_clicked(
        self, signed_in, db, bambu
    ):
        response = await signed_in.post(
            "/api/printers/2/print", json={"bambuddy_archive_id": 77, "name": "jig.3mf"}
        )
        assert response.status_code == 200, response.text
        assert bambu.enqueued == [
            {
                "archive_id": 77,
                "file_path": None,
                "plate_number": 1,
                "printer_id": 2,
                "print_options": {},
            }
        ]

    async def test_a_file_named_by_path_rather_than_by_archive(
        self, signed_in, db, bambu
    ):
        response = await signed_in.post(
            "/api/printers/1/print", json={"bambuddy_file_path": "/models/jig.3mf"}
        )
        assert response.status_code == 200, response.text
        assert bambu.enqueued[0]["file_path"] == "/models/jig.3mf"

    async def test_copies_are_queued_one_after_another(self, signed_in, db, bambu):
        response = await signed_in.post(
            "/api/printers/1/print",
            json={"bambuddy_archive_id": 5, "copies": 3, "plate_number": 2},
        )
        assert response.json()["copies"] == 3
        assert len(bambu.enqueued) == 3
        assert {row["plate_number"] for row in bambu.enqueued} == {2}

    async def test_a_request_that_names_no_file_is_refused(self, signed_in, db, bambu):
        response = await signed_in.post("/api/printers/1/print", json={"copies": 2})
        assert response.status_code == 422
        assert bambu.enqueued == []

    async def test_nought_copies_is_not_a_print(self, signed_in, db, bambu):
        response = await signed_in.post(
            "/api/printers/1/print", json={"bambuddy_archive_id": 5, "copies": 0}
        )
        assert response.status_code == 422

    async def test_a_failure_partway_says_how_many_already_went(
        self, signed_in, db, bambu, monkeypatch
    ):
        sent = 0
        original = bambu.enqueue

        async def flaky(**kwargs):
            nonlocal sent
            sent += 1
            if sent > 2:
                raise IntegrationError("bambuddy", "Printer rejected the file")
            return await original(**kwargs)

        monkeypatch.setattr(bambu, "enqueue", flaky)
        response = await signed_in.post(
            "/api/printers/1/print", json={"bambuddy_archive_id": 5, "copies": 5}
        )
        assert response.status_code == 502
        assert "2 of 5 had already been queued" in response.json()["detail"]

    async def test_putting_filament_through_a_printer_is_written_down(
        self, signed_in, db, bambu
    ):
        await signed_in.post(
            "/api/printers/2/print",
            json={"bambuddy_archive_id": 9, "name": "spare-clip.3mf", "copies": 2},
        )
        rows = (
            await db.execute(
                AuditLog.__table__.select().where(AuditLog.action == "print_now")
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].detail["printer_id"] == 2
        assert rows[0].detail["name"] == "spare-clip.3mf"
        assert rows[0].detail["copies"] == 2
        assert rows[0].actor == "admin"

    async def test_a_bambuddy_that_is_not_set_up_says_so(self, signed_in, db, monkeypatch):
        async def missing(_session):
            raise credentials.IntegrationNotConfigured("Bambuddy is not connected")

        monkeypatch.setattr("app.services.farm.bambuddy_api.client_for", missing)
        response = await signed_in.post(
            "/api/printers/1/print", json={"bambuddy_archive_id": 5}
        )
        assert response.status_code == 409


# --------------------------------------------------------------------------
# What is loaded
# --------------------------------------------------------------------------


class TestFilamentRows:
    """A build with its own filament endpoint answers in whatever shape it
    likes, and every one of them is the same short list on the card."""

    def test_a_bare_list_of_spools(self):
        assert [t["type"] for t in filament_rows([{"type": "PLA"}, {"type": "ABS"}])] == [
            "PLA", "ABS",
        ]

    def test_spools_wrapped_one_layer_deep(self):
        for key in ("trays", "ams", "filaments", "slots", "spools", "items", "data"):
            found = filament_rows({key: [{"filament_type": "PETG", "remain": 55}]})
            assert [(t["type"], t["remaining"]) for t in found] == [("PETG", 55)], key

    def test_an_ams_reported_as_units_is_flattened(self):
        # The operator loads a slot, not a unit.
        found = filament_rows(
            {
                "units": [
                    {"id": 1, "trays": [{"slot": 1, "type": "PLA"}]},
                    {"id": 2, "trays": [{"slot": 5, "type": "TPU"}]},
                ]
            }
        )
        assert [(t["slot"], t["type"]) for t in found] == [(1, "PLA"), (5, "TPU")]

    def test_a_machine_with_one_spool_answers_with_the_spool_itself(self):
        found = filament_rows({"filament_type": "PLA", "color": "black", "remain": 90})
        assert [(t["type"], t["colour"], t["remaining"]) for t in found] == [
            ("PLA", "black", 90),
        ]

    def test_an_empty_or_unreadable_reply_is_no_spools_not_a_blank_one(self):
        # A single row with nothing in it would draw a tray labelled "unknown",
        # which reads as a loaded spool nobody can identify.
        for reply in ([], {}, {"trays": []}, {"status": "ok"}, None, "nope"):
            assert filament_rows(reply) == [], reply


class TestDiscoverFilament:
    def test_the_leaves_a_build_might_use(self):
        for leaf in ("filament", "filaments", "ams", "spools", "trays", "materials"):
            found = discover_paths({f"/api/printers/{{id}}/{leaf}": {"get": {}}})
            assert found["printer_filament"]["path"] == (
                f"/api/printers/{{printer_id}}/{leaf}"
            ), leaf

    def test_the_template_has_to_be_the_printer(self):
        # /filaments/{id} reads one spool, which is a different endpoint.
        found = discover_paths({"/api/filaments/{id}": {"get": {}}})
        assert found["printer_filament"]["path"] is None

    def test_a_build_with_no_such_endpoint_offers_nothing(self):
        found = discover_paths({"/api/printers": {"get": {}},
                                "/api/printers/{id}": {"get": {}}})
        assert found["printer_filament"]["path"] is None

    def test_the_whole_ams_beats_one_view_of_it(self):
        found = discover_paths(
            {
                "/api/printers/{id}/trays": {"get": {}},
                "/api/printers/{id}/filament": {"get": {}},
            }
        )
        assert found["printer_filament"]["path"] == "/api/printers/{printer_id}/filament"
        assert found["printer_filament"]["alternatives"] == [
            "/api/printers/{printer_id}/trays"
        ]


class FilamentClient(FarmClient):
    """A build that keeps what is loaded on an endpoint of its own."""

    def __init__(self, *, spools=None, **kwargs):
        super().__init__(**kwargs)
        self.spools = spools

    async def _call(self, method: str, path: str, *, retries: int = 2, **kwargs):
        if "filament" in path or path.endswith("/ams"):
            self.asked.append(path)
            if self.spools is None:
                raise IntegrationError("bambuddy", "HTTP 503", status_code=503)
            return self.spools
        return await super()._call(method, path, retries=retries, **kwargs)


async def _connected(db) -> None:
    await credentials.save(
        db, PROVIDER_BAMBUDDY, {"base_url": "http://b.local", "api_key": "k"}
    )
    await db.commit()


FILAMENT_SPEC = {
    "/api/printers": {"get": {}},
    "/api/printers/{printer_id}": {"get": {}},
    "/api/printers/{printer_id}/filament": {"get": {}},
}


class TestFillFilament:
    async def test_a_build_that_keeps_the_ams_apart_is_asked_for_it(self, db):
        await _connected(db)
        client = FilamentClient(
            spec_paths=FILAMENT_SPEC,
            listing=LIVE_LISTING,
            spools=[{"slot": 1, "type": "PLA", "remain": 70}],
        )
        found = await read_farm(db, client)

        assert [t["type"] for t in found["printers"][0]["filament"]] == ["PLA"]
        assert "/api/printers/1/filament" in client.asked

    async def test_a_machine_that_already_said_is_not_asked_again(self, db):
        """The common build puts the AMS on the printer row. That must cost nothing."""
        await _connected(db)
        client = FilamentClient(
            spec_paths=FILAMENT_SPEC,
            listing=[{**LIVE_LISTING[0], "ams": [{"id": 1, "type": "PETG"}]}],
            spools=[{"slot": 1, "type": "PLA"}],
        )
        found = await read_farm(db, client)

        assert [t["type"] for t in found["printers"][0]["filament"]] == ["PETG"]
        assert not any("filament" in path for path in client.asked)

    async def test_a_build_with_no_such_endpoint_is_never_asked(self, db):
        await _connected(db)
        client = FilamentClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}": {"get": {}}},
            listing=LIVE_LISTING,
            spools=[{"type": "PLA"}],
        )
        found = await read_farm(db, client)

        assert found["printers"][0]["filament"] == []
        assert not any("filament" in path for path in client.asked)
        # And it is remembered, so the next farm read costs nothing at all.
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["filament_checked"] == bambuddy_api.FILAMENT_RULES

    async def test_the_endpoint_is_remembered_for_next_time(self, db):
        await _connected(db)
        client = FilamentClient(
            spec_paths=FILAMENT_SPEC, listing=LIVE_LISTING, spools=[{"type": "PLA"}]
        )
        await read_farm(db, client)

        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["printer_filament"] == (
            "/api/printers/{printer_id}/filament"
        )

    async def test_the_document_is_read_once_for_the_whole_farm(self, db):
        await _connected(db)
        client = FilamentClient(
            spec_paths=FILAMENT_SPEC, listing=THIN_LISTING, detail=DETAIL,
            spools=[{"type": "PLA"}],
        )
        await read_farm(db, client)

        assert client.specs_read == 1

    async def test_a_machine_that_will_not_say_keeps_its_card(self, db):
        # What is loaded is the least important thing on a card, and the card
        # is worth more than the trays.
        await _connected(db)
        client = FilamentClient(
            spec_paths=FILAMENT_SPEC, listing=LIVE_LISTING, spools=None
        )
        found = await read_farm(db, client)

        assert [row["name"] for row in found["printers"]] == ["A"]
        assert found["printers"][0]["filament"] == []
        assert "503" in found["detail_error"]

    async def test_an_operator_named_path_is_used_without_asking_the_document(self, db):
        await credentials.save(
            db,
            PROVIDER_BAMBUDDY,
            {
                "base_url": "http://b.local",
                "api_key": "k",
                "paths": {"printer_filament": "/api/printers/{printer_id}/ams"},
            },
        )
        await db.commit()
        client = FilamentClient(
            spec_paths={"/api/printers": {"get": {}},
                        "/api/printers/{printer_id}/ams": {"get": {}}},
            listing=LIVE_LISTING,
            spools={"trays": [{"type": "ASA"}]},
        )
        client.explicit_paths = {"printer_filament": "/api/printers/{printer_id}/ams"}
        client.paths["printer_filament"] = "/api/printers/{printer_id}/ams"
        found = await read_farm(db, client)

        assert [t["type"] for t in found["printers"][0]["filament"]] == ["ASA"]

    async def test_a_farm_read_costs_nothing_once_the_answer_is_stored(self, db):
        """The check is per connection, not per page load."""
        await _connected(db)
        for _ in range(3):
            client = FilamentClient(
                spec_paths={"/api/printers": {"get": {}},
                            "/api/printers/{printer_id}": {"get": {}}},
                listing=LIVE_LISTING,
                spools=[{"type": "PLA"}],
            )
            await read_farm(db, client)
        # Only the first read had a document to look at; after that the stored
        # "this build has none" answers it.
        assert client.specs_read == 0


class TestSpoolColour:
    """Builds send a colour as a code, a triple, or a word, and the operator
    wants to see the colour rather than read the code for it."""

    def test_bambus_eight_digit_code_is_rgb_then_opacity(self):
        # The alpha is the last pair. Reading the wrong end turns every opaque
        # spool into a shade of nothing.
        [tray] = _trays([{"type": "PLA", "tray_color": "F55C1AFF"}])
        assert tray["colour_hex"] == "#f55c1a"

    def test_six_and_three_digit_codes(self):
        assert _trays([{"color": "#00AE42"}])[0]["colour_hex"] == "#00ae42"
        assert _trays([{"color": "fff"}])[0]["colour_hex"] == "#ffffff"

    def test_a_triple_however_it_is_written(self):
        for raw in ("204,0,0", "rgb(204, 0, 0)", "rgba(204,0,0,1)"):
            assert _trays([{"color": raw}])[0]["colour_hex"] == "#cc0000", raw

    def test_a_colour_filament_is_actually_sold_in(self):
        assert _trays([{"color": "Black"}])[0]["colour_hex"] == "#1a1a1a"

    def test_a_word_nobody_can_draw_keeps_the_word_and_gets_no_swatch(self):
        # Better an honest name than a square in roughly the wrong colour —
        # somebody is about to match a print to this reel.
        [tray] = _trays([{"color": "Galaxy Purple"}])
        assert (tray["colour"], tray["colour_hex"]) == ("Galaxy Purple", None)

    def test_a_code_is_not_repeated_as_a_label(self):
        # The square already said it.
        for raw in ("F55C1AFF", "#00AE42", "204,0,0", "rgb(1,2,3)"):
            assert _trays([{"color": raw}])[0]["colour"] is None, raw

    def test_a_word_is_kept_beside_its_swatch(self):
        # A swatch alone is no use to somebody reading the screen.
        [tray] = _trays([{"color": "black"}])
        assert (tray["colour"], tray["colour_hex"]) == ("black", "#1a1a1a")

    def test_nonsense_is_neither_a_swatch_nor_a_crash(self):
        for raw in ("", "   ", None, True, "1234567890", []):
            [tray] = _trays([{"type": "PLA", "color": raw}])
            assert tray["colour_hex"] is None, raw

    def test_which_pla_it_is(self):
        # "PLA Matte" and "PLA Basic" print differently and the operator is
        # choosing between reels, not between materials.
        [tray] = _trays([{"tray_type": "PLA", "tray_sub_brands": "PLA Matte"}])
        assert (tray["brand"], tray["type"]) == ("PLA Matte", "PLA")

    def test_a_lone_spool_gets_the_same_treatment_as_a_tray(self):
        [tray] = parse_printer(
            {"id": 1, "filament_type": "PLA", "filament_color": "00AE42FF"}
        )["filament"]
        assert (tray["type"], tray["colour_hex"]) == ("PLA", "#00ae42")

    def test_the_colour_survives_the_whole_farm_read(self):
        row = parse_printer(
            {"id": 1, "ams": [{"tray_type": "PLA", "tray_color": "000000FF"}]}
        )
        assert row["filament"][0]["colour_hex"] == "#000000"


class TestRealAmsShapes:
    """The shape Bambu actually sends, and the ones builds send instead.

    This is the case that was wrong in the first cut: `ams.ams[].tray[]` is a
    list of AMS *units*, each holding its trays, and a reader that looks one
    level down finds a list of units and calls it a list of spools. Nothing
    showed, and the flattened "Everything" table hid it too, because that drops
    lists of objects — so there was no way to see the thing that was missing.
    """

    def test_bambus_own_shape_units_of_trays(self):
        row = parse_printer(
            {
                "id": 1,
                "ams": {
                    "ams": [
                        {
                            "id": "0", "humidity": "4", "temp": "26.4",
                            "tray": [
                                {"id": "0", "tray_type": "PLA", "remain": 82},
                                {"id": "1", "tray_type": "PETG", "remain": 41},
                            ],
                        }
                    ],
                    "tray_now": "0",
                },
            }
        )
        assert [(t["type"], t["remaining"]) for t in row["filament"]] == [
            ("PLA", 82),
            ("PETG", 41),
        ]

    def test_an_ams_unit_is_not_a_spool(self):
        # A unit carries an id, a humidity and a temperature. Read as a spool
        # it draws a tray of unknown filament that is not in the machine.
        row = parse_printer(
            {"id": 1, "ams": {"ams": [{"id": "0", "humidity": "4", "temp": "26.4"}]}}
        )
        assert row["filament"] == []

    def test_two_units_are_one_list_of_slots(self):
        # The operator loads a slot, not a unit.
        row = parse_printer(
            {
                "id": 1,
                "ams": {
                    "ams": [
                        {"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]},
                        {"id": "1", "tray": [{"id": "0", "tray_type": "ABS"}]},
                    ]
                },
            }
        )
        assert [t["type"] for t in row["filament"]] == ["PLA", "ABS"]
        # Bambu numbers trays inside each unit, so both are its slot 0. Two
        # rows labelled the same are worse than a straight count.
        assert [t["slot"] for t in row["filament"]] == [1, 2]

    def test_one_units_own_numbering_is_kept(self):
        row = parse_printer(
            {
                "id": 1,
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "tray": [
                                {"id": "0", "tray_type": "PLA"},
                                {"id": "2", "tray_type": "ABS"},
                            ],
                        }
                    ]
                },
            }
        )
        # It matches the machine's own screen, so it is left alone.
        assert [t["slot"] for t in row["filament"]] == ["0", "2"]

    def test_an_empty_slot_is_not_a_spool_of_unknown_filament(self):
        row = parse_printer(
            {
                "id": 1,
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "tray": [
                                {"id": "0", "tray_type": "PLA", "remain": 50},
                                {"id": "1"},
                                {"id": "2"},
                            ],
                        }
                    ]
                },
            }
        )
        assert [t["type"] for t in row["filament"]] == ["PLA"]

    def test_the_external_spool_counts_too(self):
        # It feeds past the AMS and is the one somebody threaded by hand.
        row = parse_printer(
            {
                "id": 1,
                "ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}]},
                "vt_tray": {"id": "254", "tray_type": "TPU", "tray_color": "F55C1AFF"},
            }
        )
        assert [t["type"] for t in row["filament"]] == ["PLA", "TPU"]

    def test_an_external_spool_on_its_own(self):
        row = parse_printer(
            {"id": 1, "vt_tray": {"tray_type": "PLA", "tray_color": "00AE42FF"}}
        )
        assert [(t["type"], t["colour_hex"]) for t in row["filament"]] == [
            ("PLA", "#00ae42")
        ]

    def test_an_empty_external_spool_is_not_a_spool(self):
        row = parse_printer({"id": 1, "vt_tray": {"id": "254"}})
        assert row["filament"] == []

    def test_the_shapes_that_already_worked_still_do(self):
        # A flat list on the row, and a wrapper with trays in it.
        assert [
            t["type"]
            for t in parse_printer(
                {"id": 1, "ams": [{"type": "PLA"}, {"type": "ABS"}]}
            )["filament"]
        ] == ["PLA", "ABS"]
        assert [
            t["type"]
            for t in parse_printer({"id": 1, "ams": {"trays": [{"tray_type": "PETG"}]}})[
                "filament"
            ]
        ] == ["PETG"]

    def test_a_build_that_buries_it_deeper_is_still_found(self):
        row = parse_printer(
            {"id": 1, "ams": {"units": [{"modules": [{"tray": [{"type": "PLA"}]}]}]}}
        )
        assert [t["type"] for t in row["filament"]] == ["PLA"]

    def test_the_search_does_not_run_away_on_a_deep_reply(self):
        deep: dict = {"type": "PLA"}
        for _ in range(20):
            deep = {"tray": deep}
        assert parse_printer({"id": 1, "ams": deep})["filament"] == []

    def test_a_filament_endpoint_gets_the_same_reader(self):
        # Whatever the shape, it goes through one reader.
        found = filament_rows({"ams": [{"id": "0", "tray": [{"tray_type": "PLA"}]}]})
        assert [t["type"] for t in found] == ["PLA"]

    def test_the_external_spool_is_labelled_not_numbered(self):
        # Bambu calls it tray 254, which is an internal id rather than
        # anything written on the machine.
        row = parse_printer(
            {"id": 1, "vt_tray": {"id": "254", "tray_type": "PLA", "remain": 30}}
        )
        assert [t["slot"] for t in row["filament"]] == ["Ext"]


class TestPrintingTwiceByAccident:
    """Pressing Print again after an error must not print again.

    The dangerous case is not a double click, it is an answer that never
    arrives: a proxy in front of PrintFlow times out, or a tunnel drops, and
    the operator sees an error for a plate that is already on the machine.
    Nothing in the browser can tell that apart from a request that never
    landed — so the retry has to be safe rather than the operator having to
    guess, because guessing wrong costs a plate of filament.
    """

    async def test_the_same_request_twice_prints_once(self, signed_in, db, bambu):
        body = {"bambuddy_archive_id": 5, "request_id": "abc-123"}
        first = await signed_in.post("/api/printers/1/print", json=body)
        second = await signed_in.post("/api/printers/1/print", json=body)

        assert first.status_code == 200 and second.status_code == 200
        assert len(bambu.enqueued) == 1
        # And says which of the two it was, because "it worked" and "it had
        # already worked" are different things to read after an error.
        assert first.json().get("already_done") is not True
        assert second.json()["already_done"] is True
        assert second.json()["queued"] == first.json()["queued"]

    async def test_copies_are_not_multiplied_by_a_retry(self, signed_in, db, bambu):
        body = {"bambuddy_archive_id": 5, "copies": 3, "request_id": "def-456"}
        await signed_in.post("/api/printers/1/print", json=body)
        await signed_in.post("/api/printers/1/print", json=body)
        assert len(bambu.enqueued) == 3

    async def test_a_genuinely_new_print_is_not_mistaken_for_a_retry(
        self, signed_in, db, bambu
    ):
        # Two deliberate prints of the same file are two prints. Only the id
        # decides, and the dialog issues a new one each time it opens.
        await signed_in.post(
            "/api/printers/1/print",
            json={"bambuddy_archive_id": 5, "request_id": "one"},
        )
        await signed_in.post(
            "/api/printers/1/print",
            json={"bambuddy_archive_id": 5, "request_id": "two"},
        )
        assert len(bambu.enqueued) == 2

    async def test_without_an_id_nothing_is_deduplicated(self, signed_in, db, bambu):
        # An older browser, or a script. Two requests are two prints, which is
        # the behaviour that was there before and is still the honest reading
        # of a request that declines to identify itself.
        for _ in range(2):
            await signed_in.post(
                "/api/printers/1/print", json={"bambuddy_archive_id": 5}
            )
        assert len(bambu.enqueued) == 2

    async def test_a_retry_after_a_failure_still_prints(self, signed_in, db, bambu):
        # The other half, and the one that would be far worse to get wrong:
        # when the first attempt genuinely did not reach Bambuddy, the retry
        # has to actually print rather than reporting a success that never was.
        working = bambu.enqueue

        async def refuse(**_kwargs):
            raise IntegrationError("bambuddy", "Connection refused")

        bambu.enqueue = refuse
        body = {"bambuddy_archive_id": 5, "request_id": "ghi-789"}
        assert (await signed_in.post("/api/printers/1/print", json=body)).status_code == 502

        bambu.enqueue = working
        again = await signed_in.post("/api/printers/1/print", json=body)
        assert again.status_code == 200
        assert again.json().get("already_done") is not True
        assert len(bambu.enqueued) == 1
