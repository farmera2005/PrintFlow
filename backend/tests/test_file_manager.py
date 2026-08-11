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
    split_folder_detail,
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
# The library: folders and files as two collections
# --------------------------------------------------------------------------


# Bambuddy's own library, as its API describes it: a folder collection whose
# rows carry a parent id, and a file collection whose rows carry a folder id.
LIB_FOLDERS = [
    {"id": 1, "name": "Production", "parent_id": None},
    {"id": 2, "name": "Bins", "parent_id": 1},
    {"id": 3, "name": "Dragons", "parent_id": 1},
    {"id": 4, "name": "Large", "parent_id": 3},
    {"id": 5, "name": "Empty shelf", "parent_id": 1},
]
LIB_FILES = [
    {"id": 101, "name": "bin-fan-yes.3mf", "folder_id": 2, "size": 2_400_000},
    {"id": 102, "name": "render.png", "folder_id": 2},
    {"id": 103, "name": "dragon-egg-large.3mf", "folder_id": 4},
    {"id": 104, "name": "loose.3mf", "folder_id": None},
]


def _library(folders=LIB_FOLDERS, files=LIB_FILES, **kwargs):
    from app.integrations.bambuddy import (
        build_library_tree,
        parse_library_file,
        parse_library_folder,
    )

    return build_library_tree(
        [parse_library_folder(row) for row in folders],
        [parse_library_file(row) for row in files],
        **kwargs,
    )


class TestBuildLibraryTree:
    """The shape is in the data — parent ids — so this is arithmetic, not a walk."""

    def test_the_structure_comes_out_of_the_parent_ids(self):
        by_path = {node["path"]: node for node in _library()["files"]}
        assert by_path["/Production"]["parent"] == "/"
        assert by_path["/Production/Bins"]["parent"] == "/Production"
        assert by_path["/Production/Dragons/Large"]["depth"] == 2
        assert by_path["/Production/Dragons/Large/dragon-egg-large.3mf"]["depth"] == 3

    def test_a_file_with_no_folder_sits_at_the_top(self):
        by_path = {node["path"]: node for node in _library()["files"]}
        assert by_path["/loose.3mf"]["parent"] == "/"

    def test_an_empty_folder_is_still_a_folder(self):
        # The thing a walk cannot find, because nothing points into it.
        paths = [node["path"] for node in _library()["files"]]
        assert "/Production/Empty shelf" in paths

    def test_the_library_id_is_kept_as_the_file_id(self):
        by_path = {node["path"]: node for node in _library()["files"]}
        assert by_path["/Production/Bins/bin-fan-yes.3mf"]["archive_id"] == 101
        assert by_path["/Production/Bins/bin-fan-yes.3mf"]["printable"] is True
        assert by_path["/Production/Bins/render.png"]["printable"] is False

    def test_counts_are_of_what_is_there(self):
        tree = _library()
        assert (tree["folders"], tree["printable"]) == (5, 3)

    def test_a_folder_whose_parent_is_missing_keeps_its_files(self):
        # Deleted mid-read, or outside whatever page came back. Losing the
        # subtree would silently hide files that really are printable.
        tree = _library(
            folders=[{"id": 9, "name": "Orphan", "parent_id": 404}],
            files=[{"id": 1, "name": "a.3mf", "folder_id": 9}],
        )
        assert [node["path"] for node in tree["files"]] == ["/Orphan", "/Orphan/a.3mf"]

    def test_a_folder_that_is_its_own_ancestor_does_not_recurse(self):
        tree = _library(
            folders=[{"id": 1, "name": "A", "parent_id": 2},
                     {"id": 2, "name": "B", "parent_id": 1}],
            files=[{"id": 7, "name": "a.3mf", "folder_id": 1}],
        )
        # Where a cycle hangs is arbitrary — it is a cycle. What matters is that
        # it ends, that each folder appears once, and that the file inside one
        # is still reachable rather than lost with the loop.
        assert len(tree["files"]) == 3
        assert {node["archive_id"] for node in tree["files"]} == {1, 2, 7}
        assert tree["printable"] == 1

    def test_two_files_of_the_same_name_in_one_folder_both_survive(self):
        # Names are not unique in a library; ids are. Overwriting one with the
        # other would quietly lose a file somebody can see in Bambuddy.
        tree = _library(
            folders=[],
            files=[{"id": 1, "name": "a.3mf", "folder_id": None},
                   {"id": 2, "name": "a.3mf", "folder_id": None}],
        )
        assert sorted(node["path"] for node in tree["files"]) == ["/a.3mf", "/a.3mf (2)"]
        assert {node["archive_id"] for node in tree["files"]} == {1, 2}

    def test_a_node_cap_admits_itself(self):
        tree = _library(max_nodes=3)
        assert len(tree["files"]) == 3
        assert tree["truncated"] is True

    def test_rows_that_name_nothing_are_dropped_not_drawn_blank(self):
        tree = _library(folders=[], files=[{"id": 1, "folder_id": None}])
        assert tree["files"] == []

    def test_a_row_that_states_its_own_path_needs_no_parent_chain(self):
        # Some builds carry the ancestry as a string instead of an id, and then
        # every row looks top-level to a reader that only knows about ids.
        tree = _library(
            folders=[{"id": 1, "name": "Hopper", "path": "/Grain Bin (Large)/Hopper"}],
            files=[{"id": 5, "name": "hopper.3mf", "folder_id": 1}],
        )
        paths = [node["path"] for node in tree["files"]]
        assert "/Grain Bin (Large)/Hopper" in paths
        assert "/Grain Bin (Large)/Hopper/hopper.3mf" in paths

    def test_a_file_that_states_its_own_path_is_placed_by_it(self):
        tree = _library(
            folders=[],
            files=[{"id": 5, "name": "a.3mf", "path": "/Deep/Down/a.3mf"}],
        )
        by_path = {node["path"]: node for node in tree["files"]}
        assert by_path["/Deep/Down/a.3mf"]["parent"] == "/Deep/Down"

    def test_the_folders_a_path_implies_are_made_real(self):
        """A node under a folder nobody listed would be drawn nowhere.

        The picker lists a folder's children, and nothing is a child of a folder
        that does not exist — so a file two levels under an absent folder is
        invisible rather than merely misplaced, which is the worst way for it to
        go missing.
        """
        tree = _library(
            folders=[{"id": 1, "name": "Hopper", "path": "/Grain Bin (Large)/Hopper"}],
            files=[{"id": 5, "name": "hopper.3mf", "folder_id": 1}],
        )
        by_path = {node["path"]: node for node in tree["files"]}
        assert by_path["/Grain Bin (Large)"]["kind"] == "folder"
        assert by_path["/Grain Bin (Large)"]["implied"] is True
        assert by_path["/Grain Bin (Large)"]["parent"] == "/"
        # And the one that was really listed is not marked as invented.
        assert "implied" not in by_path["/Grain Bin (Large)/Hopper"]

    def test_a_real_folder_is_not_replaced_by_an_implied_one(self):
        tree = _library(
            folders=[{"id": 1, "name": "Top", "path": "/Top"},
                     {"id": 2, "name": "Under", "path": "/Top/Under"}],
            files=[],
        )
        by_path = {node["path"]: node for node in tree["files"]}
        assert by_path["/Top"]["archive_id"] == 1
        assert "implied" not in by_path["/Top"]

    def test_a_related_object_stands_in_for_its_id(self):
        # Some serialisers nest the parent rather than sending a bare id.
        tree = _library(
            folders=[{"id": 1, "name": "Top", "parent": None},
                     {"id": 2, "name": "Under", "parent": {"id": 1}}],
            files=[{"id": 5, "name": "a.3mf", "folder": {"id": 2}}],
        )
        assert "/Top/Under/a.3mf" in [node["path"] for node in tree["files"]]


class LibraryClient(BambuddyClient):
    """Serves the two collections the way Bambuddy's API describes them."""

    def __init__(self, *, folders=LIB_FOLDERS, files=LIB_FILES, page_size=None):
        super().__init__({"base_url": "http://bambuddy.local"})
        self.folders = folders
        self.files = files
        self.page_size = page_size
        self.calls: list[tuple[str, int]] = []
        self.params: list[dict | None] = []

    async def _call(self, method: str, path: str, *, retries: int = 2, **kwargs):
        params = kwargs.get("params")
        self.params.append(params)
        offset = int((params or {}).get("offset") or 0)
        self.calls.append((path, offset))
        rows = self.folders if path.endswith("folders") else self.files
        if self.page_size:
            rows = rows[offset:offset + self.page_size]
        key = "folders" if path.endswith("folders") else "files"
        return {key: rows, "total": len(self.folders if key == "folders" else self.files)}


class TestLibraryTreeOverTheApi:
    async def test_two_calls_bring_the_whole_structure(self):
        client = LibraryClient()
        tree = await client.library_tree()
        assert (tree["folders"], tree["printable"]) == (5, 3)
        # One call per collection — not one per folder.
        assert [path for path, _ in client.calls] == [
            "/api/v1/library/folders", "/api/v1/library/files"
        ]
        assert tree["flat"] is False

    async def test_the_first_request_carries_no_paging_at_all(self):
        """A pile of paging parameters is a good way to be answered with nothing.

        An endpoint that validates its query strictly, or that reads `page`
        while being handed `offset` too, can return an empty list for a
        collection that is full — and empty is indistinguishable from an empty
        library. Asking plainly first means the common case never rests on
        guessing the paging dialect.
        """
        client = LibraryClient()
        await client.library_tree()
        assert client.params == [None, None]

    async def test_paging_is_entered_only_on_evidence(self):
        # `total` says there is more than came back, so there is more to ask for.
        client = LibraryClient(page_size=2)
        rows = await client._all_rows("/api/v1/library/files")
        assert len(rows) == len(LIB_FILES)
        # Two requests, then it stops: the count is reached, so a third would
        # only be asking an instance to confirm it has nothing left.
        assert [offset for _, offset in client.calls] == [0, 2]

    async def test_an_instance_that_ignores_paging_does_not_spin(self):
        class Ignores(LibraryClient):
            async def _call(self, method, path, *, retries=2, **kwargs):
                self.calls.append((path, 0))
                # Always everything, and always claiming there is more.
                return {"files": self.files, "total": 99}

        client = Ignores()
        rows = await client._all_rows("/api/v1/library/files")
        # The same rows every time: taken once, then stopped.
        assert len(rows) == len(LIB_FILES)
        assert len(client.calls) == 2


class TestAFolderListThatAnswersForOneLevel:
    """"List folders" returning the top level is a reasonable thing to be.

    It is also indistinguishable from a complete list, right up until a folder
    that holds only subfolders shows as empty and everything filed inside it is
    invisible. Reported exactly that way: the folders whose files sat directly
    in them were fine, and every folder with subfolders was empty.
    """

    class OneLevel(LibraryClient):
        """Answers `parent_id`, defaulting to the top level."""

        async def _call(self, method, path, *, retries=2, **kwargs):
            params = kwargs.get("params") or {}
            if path.endswith("folders"):
                parent = params.get("parent_id")
                self.calls.append((path, parent))
                rows = [f for f in self.folders if f.get("parent_id") == parent]
                return {"folders": rows, "total": len(rows)}
            folder_id = params.get("folder_id")
            self.calls.append((path, folder_id))
            if folder_id is None:
                return {"files": [], "total": 0}
            return {"files": [f for f in self.files if f.get("folder_id") == folder_id]}

    async def test_the_subfolders_and_their_files_are_found(self):
        client = self.OneLevel()
        tree = await client.library_tree()

        by_path = {node["path"]: node for node in tree["files"]}
        # Two levels below a folder the top-level list never mentioned.
        assert "/Production/Bins" in by_path
        assert "/Production/Dragons/Large" in by_path
        assert "/Production/Dragons/Large/dragon-egg-large.3mf" in by_path
        assert tree["folders"] == 5
        assert "subfolders asked for" in tree["endpoint"]

    async def test_a_child_row_that_omits_its_parent_still_nests(self):
        # The parent is known from the question that was asked.
        class Terse(self.OneLevel):
            async def _call(self, method, path, *, retries=2, **kwargs):
                data = await super()._call(method, path, retries=retries, **kwargs)
                if path.endswith("folders"):
                    return {"folders": [{k: v for k, v in row.items() if k != "parent_id"}
                                        for row in data["folders"]]}
                return data

        tree = await Terse().library_tree()
        assert "/Production/Bins" in [node["path"] for node in tree["files"]]

    async def test_a_complete_list_is_not_walked_at_all(self):
        # Rows that already sit inside each other: the list is whole, and asking
        # each folder what is under it would be requests for nothing.
        client = LibraryClient()
        await client.library_tree()
        assert len([1 for path, _ in client.calls if path.endswith("folders")]) == 1

    async def test_an_instance_that_ignores_parent_id_stops_after_one_round(self):
        class Ignores(LibraryClient):
            async def _call(self, method, path, *, retries=2, **kwargs):
                self.calls.append((path, (kwargs.get("params") or {}).get("parent_id")))
                if path.endswith("folders"):
                    # The same top level, whatever it is asked.
                    return {"folders": [f for f in self.folders
                                        if f.get("parent_id") is None]}
                return {"files": self.files}

        client = Ignores()
        tree = await client.library_tree()
        # The root call, then one probe per parameter name worth trying — and
        # then it stops, because every one of them answered with the top level
        # it already had. It never walks the folders one by one on the strength
        # of an endpoint that is not filtering.
        folder_calls = [1 for path, _ in client.calls if path.endswith("folders")]
        assert len(folder_calls) == 4
        assert tree["folders"] == 1


class TestSplitFolderDetail:
    """"Get Folder" returns the folder together with what it holds."""

    def test_children_and_files_are_both_read(self):
        # The bug this exists for: taking only the first list key found read
        # the files and left every subfolder unseen, which is a library with no
        # structure in it.
        subs, files = split_folder_detail(
            {"id": 3, "name": "Grain Bin", "children": [{"id": 10, "name": "Hopper"}],
             "files": [{"id": 103, "name": "hopper.3mf"}]}
        )
        assert [row["name"] for row in subs] == ["Hopper"]
        assert [row["name"] for row in files] == ["hopper.3mf"]

    def test_a_children_array_is_split_on_what_each_row_is(self):
        subs, files = split_folder_detail(
            {"children": [
                {"name": "Lids"},                       # no extension: a folder
                {"name": "lid.3mf"},                    # a print file
                {"name": "Odd", "type": "file"},        # says so itself
                {"name": "weird.3mf", "type": "folder"},
            ]}
        )
        assert [row["name"] for row in subs] == ["Lids", "weird.3mf"]
        assert [row["name"] for row in files] == ["lid.3mf", "Odd"]

    def test_named_arrays_are_believed_over_the_guess(self):
        subs, files = split_folder_detail(
            {"folders": [{"name": "no-dot-but-a-folder"}], "files": [{"name": "x"}]}
        )
        assert [row["name"] for row in subs] == ["no-dot-but-a-folder"]
        assert [row["name"] for row in files] == ["x"]

    def test_the_same_rows_under_two_names_are_not_counted_twice(self):
        same = [{"id": 1, "name": "a.3mf"}]
        _subs, files = split_folder_detail({"files": same, "children": same})
        assert files == same

    def test_one_wrapper_is_unwrapped(self):
        subs, _files = split_folder_detail({"folder": {"children": [{"name": "Bins"}]}})
        assert [row["name"] for row in subs] == ["Bins"]

    def test_nothing_recognisable_is_nothing(self):
        assert split_folder_detail({"id": 1, "name": "Empty"}) == ([], [])
        assert split_folder_detail("nope") == ([], [])


class TestDrillingInWithGetFolder:
    """The instance filters nothing, and the item endpoint is the only way down.

    There is no "list subfolders" in Bambuddy's library API — there is "Get
    Folder", which is how a file manager drills in and which returns the folder
    together with what it holds.
    """

    class DetailOnly(LibraryClient):
        def _tree(self):
            kids: dict = {}
            for folder in self.folders:
                kids.setdefault(folder.get("parent_id"), []).append(folder)
            return kids

        async def _call(self, method, path, *, retries=2, **kwargs):
            self.calls.append((path, (kwargs.get("params") or {}).get("parent_id")))
            item = path.rsplit("/", 1)[-1]
            if item.isdigit():
                fid = int(item)
                return {
                    "id": fid,
                    "children": self._tree().get(fid, []),
                    "files": [f for f in self.files if f.get("folder_id") == fid],
                }
            if path.endswith("folders"):
                # Filters nothing: always the top level.
                return {"folders": self._tree().get(None, [])}
            return {"files": [], "total": 0}

    async def test_the_whole_structure_comes_out_of_the_item_endpoint(self):
        client = self.DetailOnly()
        tree = await client.library_tree()

        paths = [node["path"] for node in tree["files"]]
        assert "/Production/Bins" in paths
        assert "/Production/Dragons/Large" in paths
        assert "/Production/Dragons/Large/dragon-egg-large.3mf" in paths
        assert tree["folders"] == 5
        assert "subfolders asked for" in tree["endpoint"]
        assert "files came with the folders" in tree["endpoint"]

    async def test_the_query_parameters_are_tried_before_drilling_in(self):
        # One request each to find out, then the method that works for the rest.
        client = self.DetailOnly()
        await client.library_tree()
        probes = [path for path, _ in client.calls if path.endswith("folders")]
        assert len(probes) == 4  # the root list, then parent_id / parent / folder_id


class TestAFilesEndpointThatWantsToBeAsked:
    """Folders arrive, the flat file list is empty, and the library is not.

    The reported symptom: twenty folders, every one of them saying "0 items".
    A files endpoint that only answers for a named folder is far likelier than
    a shop keeping twenty empty folders, so it is worth one request each to
    find out.
    """

    class PerFolder(LibraryClient):
        async def _call(self, method, path, *, retries=2, **kwargs):
            params = kwargs.get("params") or {}
            self.calls.append((path, params.get("folder_id")))
            if path.endswith("folders"):
                return {"folders": self.folders}
            folder_id = params.get("folder_id")
            if folder_id is None:
                return {"files": [], "total": 0}
            return {"files": [f for f in self.files if f.get("folder_id") == folder_id]}

    async def test_the_files_are_found_by_asking_per_folder(self):
        client = self.PerFolder()
        tree = await client.library_tree()

        by_path = {node["path"]: node for node in tree["files"]}
        assert "/Production/Bins/bin-fan-yes.3mf" in by_path
        assert "/Production/Dragons/Large/dragon-egg-large.3mf" in by_path
        # A file in no folder cannot be asked for by folder, so it is the one
        # thing this path does not recover. Everything filed is here.
        assert "/loose.3mf" not in by_path
        assert tree["printable"] == 2
        # And it says so, rather than looking like the flat read worked.
        assert "asked per folder" in tree["endpoint"]
        # One request per folder, after the flat one came back empty.
        assert [fid for path, fid in client.calls if path.endswith("files")] == [
            None, 1, 2, 3, 4, 5
        ]

    async def test_a_row_that_omits_its_folder_still_lands_in_it(self):
        # The folder is known from the question that was asked.
        class Terse(self.PerFolder):
            async def _call(self, method, path, *, retries=2, **kwargs):
                data = await super()._call(method, path, retries=retries, **kwargs)
                if path.endswith("files"):
                    return {"files": [{k: v for k, v in row.items() if k != "folder_id"}
                                      for row in data["files"]]}
                return data

        tree = await Terse().library_tree()
        assert "/Production/Bins/bin-fan-yes.3mf" in [n["path"] for n in tree["files"]]

    async def test_a_library_with_no_folders_does_not_start_asking(self):
        class Empty(self.PerFolder):
            def __init__(self):
                super().__init__(folders=[], files=[])

        client = Empty()
        tree = await client.library_tree()
        assert tree["files"] == []
        # Nothing to ask about, so the flat call is the only file call made.
        assert len([1 for path, _ in client.calls if path.endswith("files")]) == 1


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

    async def test_any_endpoint_heals_the_same_way(self, db):
        """A library at /api/v1 means the rest moved too, printers included."""
        from app.integrations import bambuddy as api
        from app.models import PROVIDER_BAMBUDDY
        from app.services import credentials

        await credentials.save(db, PROVIDER_BAMBUDDY,
                               {"base_url": "http://b.local", "api_key": "k"})
        await db.commit()

        class Farm(HealingClient):
            async def _call(self, method, path, *, retries=2, **kwargs):
                from app.integrations.base import IntegrationError

                if path not in self.spec_paths:
                    raise IntegrationError("bambuddy", "HTTP 404", status_code=404)
                return [{"id": 1, "name": "X1C-01", "model": "X1 Carbon", "online": True}]

        client = Farm(spec_paths={"/api/v1/printers": {"get": {}}})
        # The default is /api/printers, which this instance does not have.
        printers = await api.with_healing(db, client, ("printers",), client.list_printers)
        await db.commit()

        assert [p["name"] for p in printers] == ["X1C-01"]
        assert client.paths["printers"] == "/api/v1/printers"
        payload = await credentials.load(db, PROVIDER_BAMBUDDY)
        assert payload["discovered_paths"]["printers"] == "/api/v1/printers"

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
    """404s on every endpoint its spec does not describe.

    Stubbed at the transport so the real path resolution runs — which endpoint
    is called, and in which order, is the whole subject here.
    """

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

    async def _call(self, method: str, path: str, *, retries: int = 2, **kwargs):
        from app.integrations.base import IntegrationError

        if self.never_found or path not in self.spec_paths:
            raise IntegrationError(
                "bambuddy", f"HTTP {self.status}", status_code=self.status
            )
        if (kwargs.get("params") or {}).get("path"):
            return {"files": []}
        return {"files": [{"name": "found.3mf", "type": "file"}]}


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
