"""Bambuddy's file manager: reading its structure, and printing off one machine.

A shop that has sorted its print files into folders has already said what is
what, and a picker that flattens that back into a list of names throws the work
away. So these tests care about the *shape* that comes back — parents, depth,
folders that are folders — and about the two ways a walk can go wrong: never
terminating, and quietly returning half a file manager as if it were all of it.

The other half is where a plate goes. A file on one printer's storage exists
nowhere else, so picking it settles the machine as well as the file, and nothing
downstream is allowed to second-guess that.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.integrations.bambuddy import (
    BambuddyClient,
    discover_paths,
    file_rows,
    parse_file_entry,
)
from app.models import PrintJob, PrintMapping, ProductVariation
from app.services import printing, variations

from test_intake_pipeline import FakeBambuddy, FakeQbo, seed_catalog
from test_printer_models import printer

pytestmark = pytest.mark.asyncio


# A folder tree with everything awkward in it: a nested folder, a file that is
# not printable, and a folder whose name has a space.
TREE: dict[str, list[dict]] = {
    "/": [
        {"name": "Production", "type": "folder"},
        {"name": "Archive 2025", "type": "folder"},
        {"name": "quick-test.3mf", "type": "file", "size": 12, "id": 900},
        {"name": "notes.txt", "type": "file", "size": 4},
    ],
    "/Production": [
        {"name": "Bins", "type": "folder"},
        {"name": "shop-sign.3mf", "type": "file", "id": 901},
    ],
    "/Production/Bins": [
        {"name": "bin-fan-yes.3mf", "type": "file", "id": 902},
        {"name": "render.png", "type": "file"},
    ],
    "/Archive 2025": [{"name": "old-vase.3mf", "type": "file", "id": 903}],
}


class TreeClient(BambuddyClient):
    """A client backed by a dict of folders, counting the calls it makes.

    Stubbed at the transport, not at list_files, so the real parsing and the
    real counting run — the shape of a reply is exactly what these tests are
    about, and a stub that skipped it would test nothing.
    """

    def __init__(self, tree: dict[str, list[dict]], *, unreadable: set[str] | None = None):
        super().__init__({"base_url": "http://bambuddy.local"})
        self.tree = tree
        self.unreadable = unreadable or set()
        self.reads: list[str] = []

    async def _call(self, method: str, path: str, *, retries: int = 2, **kwargs):
        folder = (kwargs.get("params") or {}).get("path") or "/"
        self.reads.append(folder)
        if folder in self.unreadable:
            from app.integrations.base import IntegrationError

            raise IntegrationError("bambuddy", f"Cannot read {folder}")
        return {"files": self.tree.get(folder, [])}


# --------------------------------------------------------------------------
# Reading one row
# --------------------------------------------------------------------------


class TestParseFileEntry:
    def test_a_folder_is_recognised_however_it_is_said(self):
        for row in (
            {"name": "Bins", "type": "folder"},
            {"name": "Bins", "kind": "directory"},
            {"name": "Bins", "is_dir": True},
            {"name": "Bins", "isDirectory": True},
            {"name": "Bins", "children": []},
            {"name": "Bins", "node_type": "dir"},
            {"name": "Bins", "item_type": "directory"},
        ):
            assert parse_file_entry(row)["kind"] == "folder", row

    def test_a_file_is_not_mistaken_for_one(self):
        # `children: null` is a file with the key present, not a folder.
        assert parse_file_entry({"name": "a.3mf", "children": None})["kind"] == "file"
        assert parse_file_entry({"name": "a.3mf", "is_dir": False})["kind"] == "file"
        assert parse_file_entry({"name": "a.3mf", "type": "model/3mf"})["kind"] == "file"

    def test_the_path_is_built_when_the_row_only_has_a_name(self):
        entry = parse_file_entry({"name": "bin.3mf"}, parent="/Production/Bins")
        assert entry["path"] == "/Production/Bins/bin.3mf"

    def test_a_path_the_row_states_wins_and_is_made_absolute(self):
        entry = parse_file_entry({"name": "bin.3mf", "path": "models/bin.3mf"}, parent="/x")
        assert entry["path"] == "/models/bin.3mf"

    def test_only_what_a_printer_takes_is_printable(self):
        assert parse_file_entry({"name": "a.3mf"})["printable"] is True
        assert parse_file_entry({"name": "a.GCODE"})["printable"] is True
        assert parse_file_entry({"name": "a.png"})["printable"] is False
        assert parse_file_entry({"name": "Bins", "type": "folder"})["printable"] is False


class TestFileRows:
    """An empty file manager and one PrintFlow cannot read look identical.

    Every instance is self-hosted, so the reply shape is not a fixed thing to
    code against; a reader that only knew one of them would report a shop's
    whole library as missing and give no way to tell which had happened.
    """

    def test_a_bare_list(self):
        assert file_rows([{"name": "a.3mf"}, "junk"]) == [{"name": "a.3mf"}]

    def test_the_usual_envelopes(self):
        for key in ("files", "items", "results", "data", "entries", "models", "list"):
            assert file_rows({key: [{"name": "a.3mf"}]}) == [{"name": "a.3mf"}], key

    def test_folders_and_files_in_separate_arrays_are_one_folder(self):
        rows = file_rows(
            {"folders": [{"name": "Bins"}], "models": [{"name": "a.3mf"}]}
        )
        assert [row["name"] for row in rows] == ["Bins", "a.3mf"]
        # The array a row came in settles what it is when the row does not say.
        assert parse_file_entry(rows[0])["kind"] == "folder"
        assert parse_file_entry(rows[1])["kind"] == "file"

    def test_a_row_that_states_its_own_type_is_believed(self):
        rows = file_rows({"folders": [{"name": "odd.3mf", "type": "file"}]})
        assert parse_file_entry(rows[0])["kind"] == "file"

    def test_alternative_names_for_one_list_are_not_read_twice(self):
        # Some builds echo the same rows under a second key.
        same = [{"name": "a.3mf"}]
        assert file_rows({"files": same, "items": same}) == same

    def test_one_wrapper_around_the_payload_is_unwrapped(self):
        rows = file_rows({"tree": {"root": [{"label": "Production", "type": "dir"}]}})
        assert parse_file_entry(rows[0])["name"] == "Production"

    def test_two_branches_are_not_guessed_between(self):
        # Picking the wrong one would be worse than saying nothing.
        assert file_rows({"left": {"a": [{"name": "x"}]}, "right": {"b": []}}) == []

    def test_unwrapping_does_not_run_away(self):
        deep: Any = [{"name": "a.3mf"}]
        for _ in range(6):
            deep = {"wrap": deep}
        assert file_rows(deep) == []

    def test_nothing_recognisable_is_no_rows_rather_than_a_crash(self):
        assert file_rows({"total": 0, "page": 1}) == []
        assert file_rows("nope") == []
        assert file_rows(None) == []


class TestAnEmptyAnswerExplainsItself:
    async def test_rows_that_could_not_be_read_are_counted(self):
        # A shape with rows in it, none of which name anything.
        client = TreeClient({"/": [{"id": 1}, {"id": 2}]})
        tree = await client.file_tree()
        assert tree["files"] == []
        # Two rows arrived; none survived. That is not an empty file manager,
        # and the picker needs to be able to say so.
        assert (tree["root_rows"], tree["root_named"]) == (2, 0)

    async def test_a_genuinely_empty_folder_says_zero_rows(self):
        tree = await TreeClient({"/": []}).file_tree()
        assert (tree["root_rows"], tree["root_named"]) == (0, 0)

    async def test_the_endpoint_it_read_is_reported(self):
        tree = await TreeClient(TREE).file_tree()
        assert tree["endpoint"] == "/api/files"
        assert (await TreeClient(TREE).file_tree(printer_id=3))["endpoint"] == (
            "/api/printers/3/files"
        )


# --------------------------------------------------------------------------
# Walking the whole thing
# --------------------------------------------------------------------------


class TestFileTree:
    async def test_it_brings_the_structure_not_just_the_names(self):
        tree = await TreeClient(TREE).file_tree()
        by_path = {node["path"]: node for node in tree["files"]}

        assert by_path["/Production"]["kind"] == "folder"
        assert by_path["/Production"]["parent"] == "/"
        assert by_path["/Production/Bins"]["parent"] == "/Production"
        assert by_path["/Production/Bins/bin-fan-yes.3mf"]["parent"] == "/Production/Bins"
        assert by_path["/Production/Bins/bin-fan-yes.3mf"]["depth"] == 2
        # A folder with a space in its name is a folder like any other.
        assert by_path["/Archive 2025/old-vase.3mf"]["archive_id"] == 903

        assert (tree["folders"], tree["printable"]) == (3, 4)
        assert tree["truncated"] is False

    async def test_every_folder_is_read_once(self):
        client = TreeClient(TREE)
        await client.file_tree()
        assert sorted(client.reads) == [
            "/", "/Archive 2025", "/Production", "/Production/Bins"
        ]

    async def test_children_inline_need_no_extra_calls(self):
        client = TreeClient(
            {
                "/": [
                    {
                        "name": "Production",
                        "type": "folder",
                        "children": [{"name": "a.3mf", "type": "file"}],
                    }
                ]
            }
        )
        tree = await client.file_tree()
        assert [node["path"] for node in tree["files"]] == ["/Production", "/Production/a.3mf"]
        assert client.reads == ["/"]

    async def test_an_instance_that_ignores_the_folder_parameter_is_not_a_deep_tree(self):
        """Every request answered with the root, which is not eight folders."""

        class Ignores(TreeClient):
            async def _call(self, method, path, *, retries=2, **kwargs):
                self.reads.append((kwargs.get("params") or {}).get("path") or "/")
                return {"files": self.tree["/"]}

        client = Ignores({"/": [
            {"name": "Production", "type": "folder"},
            {"name": "bin.3mf", "type": "file"},
        ]})
        tree = await client.file_tree()

        by_path = {node["path"]: node for node in tree["files"]}
        assert sorted(by_path) == ["/Production", "/bin.3mf"]
        # Said out loud, rather than shown as a tower of identical folders.
        assert tree["flat"] is True
        assert by_path["/Production"]["unreadable"] is True
        # Two calls: the root, and the one that proved the parameter is ignored.
        assert client.reads == ["/", "/Production"]

    async def test_a_folder_that_contains_itself_does_not_spin(self):
        # An instance that resolves ".." into a real entry, or a symlink loop.
        client = TreeClient(
            {
                "/": [{"name": "loop", "type": "folder"}],
                "/loop": [{"name": "loop", "type": "folder", "path": "/loop"}],
            }
        )
        tree = await client.file_tree()
        assert [node["path"] for node in tree["files"]] == ["/loop"]
        assert client.reads == ["/", "/loop"]

    async def test_depth_is_capped_and_says_so(self):
        tree = await TreeClient(TREE).file_tree(max_depth=2)
        paths = [node["path"] for node in tree["files"]]
        assert "/Production/Bins" in paths
        # Its contents are past the cap.
        assert "/Production/Bins/bin-fan-yes.3mf" not in paths
        assert tree["truncated"] is True

    async def test_a_node_cap_is_a_partial_answer_that_admits_it(self):
        tree = await TreeClient(TREE).file_tree(max_nodes=3)
        assert len(tree["files"]) == 3
        assert tree["truncated"] is True

    async def test_an_unreadable_folder_does_not_take_the_tree_with_it(self):
        client = TreeClient(TREE, unreadable={"/Production"})
        tree = await client.file_tree()
        by_path = {node["path"]: node for node in tree["files"]}
        # The folder is still there, flagged, and the rest of the farm's files
        # are still pickable — one bad folder is not a reason to have none.
        assert by_path["/Production"]["unreadable"] is True
        assert "/Archive 2025/old-vase.3mf" in by_path

    async def test_an_empty_file_manager_is_empty_not_broken(self):
        tree = await TreeClient({"/": []}).file_tree()
        assert (tree["files"], tree["truncated"]) == ([], False)
        assert (tree["printable"], tree["folders"]) == (0, 0)


class TestFilesPath:
    def test_the_farm_and_one_machine_are_different_endpoints(self):
        client = BambuddyClient({"base_url": "http://b.local"})
        assert client.files_path(None) == "/api/files"
        assert client.files_path(3) == "/api/printers/3/files"

    def test_an_instance_with_one_shared_library_can_point_both_at_it(self):
        client = BambuddyClient(
            {"base_url": "http://b.local", "paths": {"printer_files": "/api/files"}}
        )
        # No {printer_id} to substitute: the printer only says where it prints.
        assert client.files_path(3) == "/api/files"


class TestHealingAWrongEndpoint:
    """A 404 on a path nobody chose is PrintFlow's problem, not the operator's.

    The roles here were added after the Bambuddy connection was made, so they
    were never discovered for it and fell back to a default that is a guess.
    Nobody would think to fix that by re-saving Settings, so a 404 re-reads the
    instance's document instead.
    """

    async def test_a_stale_default_is_rediscovered_and_kept(self, db):
        from app.integrations import bambuddy as api
        from app.models import PROVIDER_BAMBUDDY
        from app.services import credentials

        await credentials.save(
            db, PROVIDER_BAMBUDDY,
            {"base_url": "http://b.local", "api_key": "k",
             # What a connection made before this release looks like: the three
             # older roles discovered, and nothing for the file manager.
             "discovered_paths": {"printers": "/api/printers"}},
        )
        await db.commit()

        client = HealingClient(spec_paths={"/api/library": {"get": {}}})
        tree = await api.read_file_manager(db, client, printer_id=None)
        await db.commit()

        assert [node["name"] for node in tree["files"]] == ["found.3mf"]
        assert client.paths["files"] == "/api/library"
        # And it is remembered, so the next call does not go looking again.
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["files"] == "/api/library"
        # The older discovered path is not lost on the way.
        assert payload["discovered_paths"]["printers"] == "/api/printers"

    async def test_no_per_printer_endpoint_falls_back_to_the_shared_library(self, db):
        from app.integrations import bambuddy as api
        from app.models import PROVIDER_BAMBUDDY
        from app.services import credentials

        await credentials.save(db, PROVIDER_BAMBUDDY,
                               {"base_url": "http://b.local", "api_key": "k"})
        await db.commit()

        # This build has one library for the farm and nothing per machine.
        client = HealingClient(spec_paths={"/api/library": {"get": {}}})
        tree = await api.read_file_manager(db, client, printer_id=3)

        # The files are the shared ones, and it says so rather than pretending
        # they came off that machine — the printer is still where it prints.
        assert tree["shared"] is True
        assert [node["name"] for node in tree["files"]] == ["found.3mf"]

    async def test_an_instance_with_no_file_manager_says_what_it_does_serve(self, db):
        from app.integrations import bambuddy as api
        from app.integrations.base import IntegrationError
        from app.models import PROVIDER_BAMBUDDY
        from app.services import credentials

        await credentials.save(db, PROVIDER_BAMBUDDY,
                               {"base_url": "http://b.local", "api_key": "k"})
        await db.commit()

        client = HealingClient(
            spec_paths={"/api/printers": {"get": {}}, "/api/projects": {"get": {}}},
            never_found=True,
        )
        with pytest.raises(IntegrationError) as caught:
            await api.read_file_manager(db, client, printer_id=None)

        message = str(caught.value)
        assert "no file manager" in message
        # A number is not actionable; the endpoints it really has are.
        assert "listable endpoints" in message
        assert "Advanced" in message

    async def test_a_failure_that_is_not_a_404_is_left_alone(self, db):
        from app.integrations import bambuddy as api
        from app.integrations.base import IntegrationError
        from app.models import PROVIDER_BAMBUDDY
        from app.services import credentials

        await credentials.save(db, PROVIDER_BAMBUDDY,
                               {"base_url": "http://b.local", "api_key": "k"})
        await db.commit()

        client = HealingClient(spec_paths={}, status=500)
        with pytest.raises(IntegrationError) as caught:
            await api.read_file_manager(db, client, printer_id=None)
        # Rediscovery answers "wrong path". This is not that.
        assert caught.value.status_code == 500
        assert client.specs_read == 0


class HealingClient(BambuddyClient):
    """404s until its `files` path is the one its spec actually describes."""

    def __init__(self, *, spec_paths: dict, never_found: bool = False, status: int = 404):
        super().__init__({"base_url": "http://b.local"})
        self.spec_paths = spec_paths
        self.never_found = never_found
        self.status = status
        self.specs_read = 0

    async def fetch_openapi(self):
        self.specs_read += 1
        return {
            "path": "/openapi.json",
            "discovered": discover_paths(self.spec_paths),
            "collections": [
                {"path": path, "methods": ["get"]} for path in sorted(self.spec_paths)
            ],
        }

    async def list_files(self, *, path: str = "", printer_id: int | None = None):
        from app.integrations.base import IntegrationError

        endpoint = self.files_path(printer_id)
        if self.never_found or endpoint not in self.spec_paths:
            raise IntegrationError("bambuddy", f"HTTP {self.status}", status_code=self.status)
        if (path or "/") != "/":
            return []
        return [parse_file_entry({"name": "found.3mf", "type": "file"}, parent="/")]


class TestDiscovery:
    def test_the_file_manager_is_read_off_the_spec(self):
        found = discover_paths(
            {
                "/api/printers": {"get": {}},
                "/api/storage": {"get": {}},
                "/api/queue": {"get": {}, "post": {}},
            }
        )
        assert found["files"]["path"] == "/api/storage"

    def test_a_per_printer_endpoint_is_found_and_renamed_to_our_parameter(self):
        found = discover_paths({"/api/printers/{printerId}/files": {"get": {}}})
        assert found["printer_files"]["path"] == "/api/printers/{printer_id}/files"

    def test_reading_one_file_is_not_a_file_manager(self):
        # The template is the file here, not the printer.
        found = discover_paths({"/api/files/{file_id}": {"get": {}}})
        assert found["printer_files"]["path"] is None


# --------------------------------------------------------------------------
# What gets queued
# --------------------------------------------------------------------------


class TestQueueBody:
    def test_a_path_rides_along_with_the_id(self):
        client = BambuddyClient({"base_url": "http://b.local"})
        body = client.build_queue_body(
            archive_id=7, file_path="/Production/a.3mf", plate_number=2,
            printer_id=3, print_options={"filament": "PLA"},
        )
        assert body == {
            "archive_id": 7, "file_path": "/Production/a.3mf",
            "plate": 2, "printer_id": 3, "filament": "PLA",
        }

    def test_a_file_with_no_id_still_sends_something_printable(self):
        client = BambuddyClient({"base_url": "http://b.local"})
        body = client.build_queue_body(
            archive_id=None, file_path="/p.3mf", plate_number=None,
            printer_id=None, print_options=None,
        )
        assert body == {"file_path": "/p.3mf"}


class TestPlanCarriesTheFile:
    def test_a_file_manager_mapping_needs_no_archive_id(self):
        mapping = PrintMapping(
            bambuddy_archive_id=None, bambuddy_file_path="/Production/a.3mf",
            bambuddy_printer_id=3, plate_number=1, units_per_plate=1, printer_models=[],
        )
        plan = variations.print_plan(mapping, None)
        assert plan.bambuddy_file_path == "/Production/a.3mf"
        assert plan.printer_id == 3

    def test_a_variation_off_another_machine_takes_that_machine_with_it(self):
        mapping = PrintMapping(
            bambuddy_archive_id=1, bambuddy_file_path=None, bambuddy_printer_id=None,
            plate_number=1, units_per_plate=1, printer_models=["X1 Carbon"],
        )
        variation = ProductVariation(
            label="Tall", options=[], active=True,
            bambuddy_archive_id=None, bambuddy_file_path="/p1s/tall.3mf",
            bambuddy_printer_id=3, printer_models=[],
        )
        plan = variations.print_plan(mapping, variation)
        assert (plan.bambuddy_archive_id, plan.bambuddy_file_path) == (None, "/p1s/tall.3mf")
        assert plan.printer_id == 3
        # The product's models describe the product's file, not this one.
        assert plan.printer_models == ()

    def test_nothing_named_at_all_is_still_nothing(self):
        mapping = PrintMapping(
            bambuddy_archive_id=1, bambuddy_file_path=None, bambuddy_printer_id=None,
            plate_number=1, units_per_plate=1, printer_models=[],
        )
        empty = ProductVariation(
            label="Red", options=[], active=True,
            bambuddy_archive_id=None, bambuddy_file_path=None, printer_models=[],
        )
        # An override that names no file falls through to the product's.
        assert variations.print_plan(mapping, empty).bambuddy_archive_id == 1
        assert variations.print_plan(None, empty) is None


class FarmBambuddy(FakeBambuddy):
    def __init__(self, printers: list[dict]):
        super().__init__()
        self.printers = printers
        self.printer_reads = 0

    async def list_printers(self):
        self.printer_reads += 1
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
    fake = FarmBambuddy([printer(1, "A1 mini"), printer(2, "X1C"), printer(3, "X1C")])

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.printing.bambuddy_api.client_for", client_for)
    return fake


class TestDispatchAFileFromOnePrinter:
    async def test_the_plate_goes_where_the_file_is(self, db, fake_qbo, farm):
        catalog = await seed_catalog(db)
        mapping = (
            await db.execute(
                select(PrintMapping).where(PrintMapping.product_id == catalog["part_y"].id)
            )
        ).scalar_one()
        mapping.bambuddy_archive_id = None
        mapping.bambuddy_file_path = "/cache/p1s-only-jig.3mf"
        mapping.bambuddy_printer_id = 3
        mapping.printer_models = []
        await db.commit()

        from app.services import intake

        await intake.ingest_receipt(
            db,
            {"receipt_id": 5150, "transactions": [
                {"transaction_id": 1, "sku": "PART-Y", "quantity": 1}]},
        )
        await db.commit()

        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        await db.commit()

        sent = farm.enqueued[0]
        assert sent["printer_id"] == 3
        assert sent["file_path"] == "/cache/p1s-only-jig.3mf"
        assert sent["archive_id"] is None
        # Nothing to choose, so the farm is never read.
        assert farm.printer_reads == 0

    async def test_a_printer_bound_plate_is_never_moved_by_the_model_rules(
        self, db, fake_qbo, farm
    ):
        catalog = await seed_catalog(db)
        mapping = (
            await db.execute(
                select(PrintMapping).where(PrintMapping.product_id == catalog["part_y"].id)
            )
        ).scalar_one()
        mapping.bambuddy_file_path = "/only-here.3mf"
        mapping.bambuddy_printer_id = 1
        # Contradictory on purpose: printer 1 is an A1 mini, and the models say
        # X1C. The file is on printer 1, so printer 1 is the answer.
        mapping.printer_models = ["X1C"]
        await db.commit()

        from app.services import intake

        await intake.ingest_receipt(
            db,
            {"receipt_id": 5151, "transactions": [
                {"transaction_id": 1, "sku": "PART-Y", "quantity": 1}]},
        )
        await db.commit()
        assert await printing.dispatch_pending(db) == {"dispatched": 1, "failed": 0}
        await db.commit()

        assert farm.enqueued[0]["printer_id"] == 1
        job = (await db.execute(select(PrintJob))).scalars().one()
        assert job.printer_id == 1
