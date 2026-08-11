"""Bambuddy client — the local print-farm manager (read/write).

Bambuddy is self-hosted and publishes an OpenAPI document, which is fetched
during setup validation so the instance version can be logged. Because the
instance is local and its schema can move between releases, endpoint paths and
the queue payload field names are stored alongside the credentials and are
editable from Settings → Bambuddy → Advanced. The defaults below match a stock
instance; responses are parsed defensively so a renamed field degrades to
"unknown" rather than crashing a poll.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_PRINTING,
    JOB_QUEUED,
    PROVIDER_BAMBUDDY,
)
from ..services import credentials
from .base import (
    LAN_TIMEOUT,
    IntegrationError,
    TransportFailed,
    deadline,
    new_client,
    request,
    transport_reason,
)

# A camera connects as quickly as any other endpoint but then goes quiet
# between frames, and a machine that is idle may send nothing for a while. The
# read budget is generous for that reason; the connect budget is not.
CAMERA_TIMEOUT = httpx.Timeout(connect=4.0, read=30.0, write=10.0, pool=4.0)

DEFAULT_PATHS: dict[str, str] = {
    "openapi": "/openapi.json",
    "printers": "/api/printers",
    "archives": "/api/archives",
    "queue": "/api/queue",
    # The library, which is what Bambuddy calls its file manager. Folders and
    # files are two separate collections and the structure is carried by parent
    # ids on the rows, so the whole thing is two calls rather than one per
    # folder — and every folder comes back, including the empty ones a walk
    # would never have reached.
    "library_folders": "/api/v1/library/folders",
    "library_files": "/api/v1/library/files",
    # A file manager that instead lists one folder at a time, for builds whose
    # library is not shaped like the above.
    "files": "/api/files",
    # The same thing scoped to one machine. `{printer_id}` is substituted; an
    # instance that keeps one shared library can point this at the same path as
    # `files` and the printer simply becomes where the plate is sent.
    "printer_files": "/api/printers/{printer_id}/files",
    # One machine, in as much detail as the build keeps — read only where the
    # farm listing turns out to be a bare inventory with no live readings on it.
    "printer_detail": "/api/printers/{printer_id}",
    # The machine's camera. Proxied rather than linked: Bambuddy is on the shop
    # LAN and needs an API key, and neither is true of the browser looking at
    # PrintFlow through a tunnel.
    "printer_camera": "/api/printers/{printer_id}/camera",
}

DEFAULT_FIELDS: dict[str, str] = {
    "archive_id": "archive_id",
    "plate_number": "plate",
    "printer_id": "printer_id",
    "file_path": "file_path",
}

# What can go on a printer. Anything else in the file manager — timelapses,
# thumbnails, logs — is real, and is counted, but is not something to map a
# product onto.
PRINTABLE_SUFFIXES = (".3mf", ".gcode", ".gcode.3mf", ".gco", ".bgcode")

DEFAULT_AUTH_HEADER = "X-API-Key"

# Candidate locations for the OpenAPI document, tried in order during setup.
OPENAPI_CANDIDATES = ("/openapi.json", "/api/openapi.json", "/api/v1/openapi.json", "/docs/json")

_STATUS_MAP = {
    "pending": JOB_QUEUED,
    "waiting": JOB_QUEUED,
    "queued": JOB_QUEUED,
    "scheduled": JOB_QUEUED,
    "idle": JOB_QUEUED,
    "sent": JOB_QUEUED,
    "printing": JOB_PRINTING,
    "running": JOB_PRINTING,
    "active": JOB_PRINTING,
    "started": JOB_PRINTING,
    "in_progress": JOB_PRINTING,
    "prepare": JOB_PRINTING,
    "done": JOB_DONE,
    "finished": JOB_DONE,
    "completed": JOB_DONE,
    "complete": JOB_DONE,
    "success": JOB_DONE,
    "succeeded": JOB_DONE,
    "failed": JOB_FAILED,
    "failure": JOB_FAILED,
    "error": JOB_FAILED,
    "cancelled": JOB_CANCELLED,
    "canceled": JOB_CANCELLED,
    "aborted": JOB_CANCELLED,
    "stopped": JOB_CANCELLED,
}


# How to recognise each endpoint we need in an OpenAPI paths object. The first
# keyword is the one the endpoint is actually named after; the rest are what
# other builds have called the same thing. Scored rather than matched outright,
# because "queue" also appears in /api/printers/{id}/queue, which is a different
# endpoint from the farm-wide queue we want.
PATH_ROLES: dict[str, dict[str, Any]] = {
    "printers": {"keywords": ("printer", "device"), "methods": ("get",)},
    "archives": {"keywords": ("archive", "model", "project", "file"), "methods": ("get",)},
    # The queue is the only one we write to, so it has to accept a POST.
    "queue": {"keywords": ("queue", "job", "task"), "methods": ("get", "post")},
    # The file manager. "file" first here, where it is last under archives, so
    # the two roles do not both claim /api/files on an instance that has one.
    # Bambuddy itself calls this domain "library"; the rest are what other
    # builds have called the same screen.
    "files": {
        "keywords": ("file", "library", "filemanager", "storage", "folder", "browse"),
        "methods": ("get",),
    },
    # The library's two collections. Named apart from `files` because an
    # instance that has these does not need the folder-at-a-time endpoint at
    # all, and one that has neither must not have them guessed at.
    "library_folders": {"keywords": ("folder", "directory"), "methods": ("get",)},
    "library_files": {"keywords": ("file", "model"), "methods": ("get",)},
}

# The file manager scoped to one machine. Kept out of PATH_ROLES because every
# rule there rejects a templated path, and this role is nothing but a template.
PRINTER_FILES_ROLE: dict[str, Any] = {
    "keywords": ("file", "library", "filemanager", "storage", "folder", "model"),
    "parents": ("printer", "device"),
}


def _score_path(path: str, methods: set[str], role: dict[str, Any]) -> int | None:
    """Rank a spec path as a candidate for one of our endpoints.

    None means "not a candidate". Higher is better.
    """
    if "{" in path:
        # A templated path is an item endpoint (/printers/{id}), not the
        # collection we list and post to.
        return None
    if not set(role["methods"]).issubset(methods):
        return None

    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    last = segments[-1].lower().rstrip("s")

    try:
        rank = [k for k in role["keywords"]].index(last)
    except ValueError:
        return None

    score = 100 - rank * 10
    # Prefer the shallow, farm-wide endpoint over one nested under another
    # resource: /api/queue beats /api/printers/queue.
    score -= len(segments)
    # A conventional /api prefix is a mild positive signal over a bare /queue.
    if segments[0].lower() == "api":
        score += 2
    return score


def collection_paths(spec_paths: dict[str, Any], limit: int = 400) -> list[dict[str, Any]]:
    """Every listable endpoint in the spec, for the operator to pick from.

    When name matching finds nothing, showing what the instance actually serves
    beats asking someone to guess a path into a text box.
    """
    rows = [
        {"path": str(path), "methods": sorted(m.lower() for m in ops if isinstance(m, str))}
        for path, ops in spec_paths.items()
        if isinstance(ops, dict) and "{" not in str(path) and any(
            isinstance(m, str) and m.lower() == "get" for m in ops
        )
    ]
    rows.sort(key=lambda row: row["path"])
    return rows[:limit]


def camera_paths(spec_paths: dict[str, Any], limit: int = 60) -> list[dict[str, Any]]:
    """Every endpoint whose name mentions a camera, templated ones included.

    Not the same question as "which one is the camera": this is what the
    instance serves that looks remotely like one, so that a farm with no
    pictures on it can be told apart from a farm whose camera PrintFlow failed
    to recognise. One is a build without cameras; the other is a rule to fix.
    """
    # Looser than the matcher on purpose: this list is read by a person who
    # knows their own instance, and a near miss they can recognise is worth
    # more than a short list that is certainly all cameras.
    words = (
        "camera", "cam", "stream", "video", "webcam", "mjpeg", "jpeg", "feed",
        "snapshot", "still", "image", "photo", "monitor", "live", "view",
        "preview", "capture", "thumb",
    )
    rows = [
        {
            "path": str(path),
            "methods": sorted(m.lower() for m in ops if isinstance(m, str)),
        }
        for path, ops in spec_paths.items()
        if isinstance(ops, dict)
        and any(word in str(path).lower() for word in words)
    ]
    rows.sort(key=lambda row: row["path"])
    return rows[:limit]


def _score_printer_files(path: str, methods: set[str]) -> int | None:
    """Rank a spec path as "one printer's file manager".

    Shaped nothing like the others: it must be templated, the template must be
    the printer, and the leaf must be the files. `/api/printers/{id}/files`
    scores; `/api/files/{id}` does not, because there the template is the file.
    """
    if "get" not in methods:
        return None
    segments = [s for s in path.split("/") if s]
    if len(segments) < 3:
        return None
    templated = [i for i, s in enumerate(segments) if s.startswith("{")]
    if len(templated) != 1:
        return None
    slot = templated[0]
    if slot == len(segments) - 1:
        # The template is the last thing, so this reads a single item.
        return None

    parent = segments[slot - 1].lower().rstrip("s")
    if parent not in PRINTER_FILES_ROLE["parents"]:
        return None
    leaf = segments[-1].lower().rstrip("s")
    try:
        rank = list(PRINTER_FILES_ROLE["keywords"]).index(leaf)
    except ValueError:
        return None
    return 100 - rank * 10 - len(segments)


def _score_printer_detail(path: str, methods: set[str]) -> int | None:
    """Rank a spec path as "everything about one machine".

    Two shapes, and the live one is preferred: `/printers/{id}/status` says
    what the machine is doing this second, where `/printers/{id}` may be no
    more than the inventory row again. Anything deeper is a sub-resource — a
    printer's files, its queue — and belongs to another role.
    """
    if "get" not in methods:
        return None
    segments = [s for s in path.split("/") if s]
    templated = [i for i, s in enumerate(segments) if s.startswith("{")]
    if len(templated) != 1 or not segments:
        return None
    slot = templated[0]
    if slot == 0 or segments[slot - 1].lower().rstrip("s") not in ("printer", "device"):
        return None
    tail = segments[slot + 1 :]
    if not tail:
        return 90 - len(segments)
    if len(tail) == 1 and tail[0].lower() in ("status", "state", "info", "detail"):
        return 100 - len(segments)
    return None


def discover_printer_detail(spec_paths: dict[str, Any]) -> dict[str, Any]:
    """The per-printer endpoint, with its parameter renamed to ours."""
    return _discover_under_printer(spec_paths, _score_printer_detail)


# What a camera endpoint is called, best first. The live one is wanted even
# where a build also offers a still: a single frame can be taken out of a
# stream, but a stream cannot be made out of a still.
CAMERA_LEAVES = (
    "camera", "cam", "stream", "video", "webcam", "mjpeg", "feed", "snapshot",
    "image", "still",
)


def _camera_rank(segment: str) -> int | None:
    """How camera-ish one path segment is, or None if it is about something else.

    Split on the separators a path uses, so `camera_stream` and `camera-feed`
    read the same as `camera/stream` — builds disagree about which of the three
    they use and none of them mean anything different by it.
    """
    words = [w.rstrip("s") if w.rstrip("s") in CAMERA_LEAVES else w
             for w in re.split(r"[-_.]", segment.lower()) if w]
    ranks = [CAMERA_LEAVES.index(w) for w in words if w in CAMERA_LEAVES]
    if not ranks or len(ranks) < len(words):
        return None
    return min(ranks)


def _score_printer_camera(path: str, methods: set[str]) -> int | None:
    """Rank a spec path as "this machine's camera".

    Two shapes, because builds disagree about which noun owns the other:
    `/printers/{id}/camera` hangs the camera off the machine, and
    `/camera/{id}` hangs the machine off the camera. Both name one machine's
    camera, which is the only thing that matters here.
    """
    if "get" not in methods:
        return None
    segments = [s for s in path.split("/") if s]
    templated = [i for i, s in enumerate(segments) if s.startswith("{")]
    if len(templated) != 1:
        return None
    slot = templated[0]
    if slot == 0:
        return None
    parent = segments[slot - 1].lower().rstrip("s")
    tail = segments[slot + 1 :]

    if parent in ("printer", "device"):
        ranks = [_camera_rank(s) for s in tail]
        if not ranks or any(rank is None for rank in ranks):
            return None
        # `/printers/{id}/camera` beats `/printers/{id}/camera/snapshot`, and
        # camera beats snapshot, so the shorter and more live path wins.
        return 100 - ranks[0] * 10 - len(tail)

    # `/camera/{id}`, or `/camera/{id}/stream`. The machine is the template, so
    # anything after it must still be about the camera.
    owner = _camera_rank(segments[slot - 1])
    if owner is None:
        return None
    ranks = [_camera_rank(s) for s in tail]
    if any(rank is None for rank in ranks):
        return None
    return 90 - owner * 10 - len(tail)


def _discover_under_printer(spec_paths: dict[str, Any], score) -> dict[str, Any]:
    """Best templated per-printer path for a role, parameter renamed to ours."""
    scored: list[tuple[int, str]] = []
    for path, operations in spec_paths.items():
        if not isinstance(operations, dict):
            continue
        methods = {m.lower() for m in operations if isinstance(m, str)}
        rank = score(str(path), methods)
        if rank is not None:
            scored.append((rank, str(path)))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    normalized = [re.sub(r"\{[^}]+\}", "{printer_id}", path) for _, path in scored]
    return {"path": normalized[0] if normalized else None, "alternatives": normalized[1:6]}


def discover_printer_camera(spec_paths: dict[str, Any]) -> dict[str, Any]:
    return _discover_under_printer(spec_paths, _score_printer_camera)


def discover_printer_files(spec_paths: dict[str, Any]) -> dict[str, Any]:
    """The per-printer file endpoint, rewritten to name its parameter ours.

    Instances call the path parameter every one of id / printer_id / printerId,
    and PrintFlow substitutes `{printer_id}`, so the winner is normalised on the
    way out rather than at every call site.
    """
    scored: list[tuple[int, str]] = []
    for path, operations in spec_paths.items():
        if not isinstance(operations, dict):
            continue
        methods = {m.lower() for m in operations if isinstance(m, str)}
        score = _score_printer_files(str(path), methods)
        if score is not None:
            scored.append((score, str(path)))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    normalized = [re.sub(r"\{[^}]+\}", "{printer_id}", path) for _, path in scored]
    return {
        "path": normalized[0] if normalized else None,
        "alternatives": normalized[1:6],
    }


def discover_paths(spec_paths: dict[str, Any]) -> dict[str, Any]:
    """Read our endpoints off an OpenAPI document.

    Returns the best candidate per role plus every runner-up, because a guess
    the operator cannot see is a guess they cannot correct.
    """
    found: dict[str, Any] = {
        "printer_files": discover_printer_files(spec_paths),
        "printer_detail": discover_printer_detail(spec_paths),
        "printer_camera": discover_printer_camera(spec_paths),
    }
    for name, role in PATH_ROLES.items():
        scored: list[tuple[int, str]] = []
        for path, operations in spec_paths.items():
            if not isinstance(operations, dict):
                continue
            methods = {m.lower() for m in operations if isinstance(m, str)}
            score = _score_path(str(path), methods, role)
            if score is not None:
                scored.append((score, str(path)))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        found[name] = {
            "path": scored[0][1] if scored else None,
            "alternatives": [path for _, path in scored[1:6]],
        }
    return found


def normalize_status(raw: Any) -> str | None:
    """Map a Bambuddy status string onto our print_jobs vocabulary."""
    if raw is None:
        return None
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return _STATUS_MAP.get(key)


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _as_item(data: Any) -> dict[str, Any]:
    """One object, however this build wrapped it."""
    if isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
        return rows[0] if rows else {}
    if not isinstance(data, dict):
        return {}
    for key in ("printer", "device", "data", "result", "item"):
        inner = data.get(key)
        if isinstance(inner, dict):
            return inner
    return data


def _as_list(data: Any) -> list[dict[str, Any]]:
    """Accept a bare list or any of the common envelope shapes."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in (
            "results", "items", "data", "queue", "archives", "printers",
            "files", "entries", "children", "contents", "nodes",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


# How a file-manager listing packages its rows. A single list under one of the
# entry keys is the common case; some builds keep folders in an array of their
# own and the things you can print in another, and both halves are the folder.
FOLDER_LIST_KEYS = ("folders", "directories", "dirs", "subfolders", "subdirectories")
ENTRY_LIST_KEYS = (
    "files", "entries", "children", "contents", "nodes", "items", "results",
    "data", "objects", "models", "projects", "prints", "list",
)


def _states_its_kind(row: dict[str, Any]) -> bool:
    """Does this row say whether it is a folder, rather than leaving it to be guessed?"""
    return bool(
        row.get("type")
        or row.get("kind")
        or row.get("node_type")
        or row.get("entry_type")
        or row.get("item_type")
        or isinstance(row.get("children"), list)
    ) or any(
        row.get(flag) is not None
        for flag in ("is_dir", "isDir", "is_directory", "isDirectory", "is_folder")
    )


def split_folder_detail(data: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One folder's own record, split into the folders and files inside it.

    "Get Folder" is how a file manager drills in, and what it returns is the
    folder together with what it holds. A named array says what it holds; a
    plain `children` does not, so each row there is judged on its own — an
    explicit type where the row has one, and otherwise a name with no extension,
    because that is what a folder's name looks like and a print file's never is.
    """
    if not isinstance(data, dict):
        return [], []

    subs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for key in FOLDER_LIST_KEYS:
        if isinstance(data.get(key), list):
            subs += [row for row in data[key] if isinstance(row, dict)]
            break
    # A folder's own record holds two different things, and they are not
    # alternative names for one list: {"children": [...], "files": [...]} means
    # both. Taking only the first key found read the files and left every
    # subfolder unseen.
    for key in ("files", "models", "prints", "projects", "documents"):
        if isinstance(data.get(key), list):
            files += [row for row in data[key] if isinstance(row, dict)]
            break
    for key in ("children", "contents", "nodes", "entries", "items"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            folderish = (
                parse_file_entry(row)["kind"] == "folder"
                if _states_its_kind(row)
                else "." not in parse_file_entry(row)["name"]
            )
            (subs if folderish else files).append(row)
        break

    # A build that echoes the same rows under two names must not have them
    # counted twice.
    files = list({json.dumps(row, sort_keys=True, default=str): row for row in files}.values())
    subs = list({json.dumps(row, sort_keys=True, default=str): row for row in subs}.values())

    if subs or files:
        return subs, files
    # {"folder": {...}} and the like: one wrapper, nothing to choose wrongly.
    inner = [value for value in data.values() if isinstance(value, dict)]
    if len(inner) == 1 and len(data) <= 2:
        return split_folder_detail(inner[0])
    return [], []


def _envelope_total(data: Any) -> int | None:
    """How many rows the collection says it has, when it says."""
    if not isinstance(data, dict):
        return None
    for key in ("total", "count", "total_count", "totalCount", "total_items"):
        value = data.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def file_rows(data: Any, _depth: int = 0) -> list[dict[str, Any]]:
    """Every entry in a file-manager listing, however this build packages one.

    A reader that took the first list it recognised would show one half of a
    folder and call the other half missing, which reads as an empty file
    manager rather than as a shape nobody taught it. So both halves are taken,
    and the key a row arrived under settles what it is when the row itself does
    not say — the container is better evidence than a guess from the filename.
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []

    rows: list[dict[str, Any]] = []
    folders = next((key for key in FOLDER_LIST_KEYS if isinstance(data.get(key), list)), None)
    if folders:
        rows += [
            row if (row.get("type") or row.get("kind")) else {**row, "type": "folder"}
            for row in data[folders]
            if isinstance(row, dict)
        ]
    # Only the first entry key: they are alternative names for one list, not
    # halves of it, so reading several would show the same rows twice.
    entries = next((key for key in ENTRY_LIST_KEYS if isinstance(data.get(key), list)), None)
    if entries:
        rows += [row for row in data[entries] if isinstance(row, dict)]
    if rows or _depth >= 3:
        return rows

    # Nothing recognised at this level. A reply that is one wrapper around the
    # real payload — {"tree": {"root": [...]}} — is a shape, not a dead end, and
    # a single key leaves nothing to choose wrongly between. More than one and
    # this stops guessing, because picking the wrong branch would be worse than
    # saying nothing.
    inner = list(data.values())
    if len(inner) == 1 and isinstance(inner[0], (dict, list)):
        return file_rows(inner[0], _depth + 1)
    return []


def parse_archive(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _first(row, "id", "archive_id", "archiveId"),
        "name": _first(row, "name", "title", "filename", "file_name", "model_name"),
        "created_at": _first(row, "created_at", "createdAt", "created", "date"),
        "plates": _first(row, "plates", "plate_count", "plateCount"),
        "thumbnail": _first(row, "thumbnail", "thumbnail_url", "image", "cover"),
    }


def _first_scalar(data: dict[str, Any], *names: str) -> Any:
    """The first of these that is a plain value — skipping any that is an object."""
    for name in names:
        value = data.get(name)
        if value is not None and not isinstance(value, (dict, list)):
            return value
    return None


def _number(value: Any) -> float | None:
    """A reading, or nothing. A temperature that came back as "--" is nothing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _rounded(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(round(number))


# What a machine says when nothing is wrong, in the spellings seen.
_NO_FAULT = {"", "0", "0.0", "none", "null", "ok", "no_error", "noerror", "normal"}


def _fault(value: Any) -> str | None:
    if value is None or value is False:
        return None
    text = str(value).strip()
    return None if text.lower() in _NO_FAULT else text


def parse_printer(row: dict[str, Any]) -> dict[str, Any]:
    """One machine, as much of it as this build cares to say.

    The first five fields are all dispatch needs and every build has them. The
    rest are what a person standing in the shop wants — is it running, how far
    through, what is on it, is it hot — and no two builds spell them the same,
    so each is read from every spelling seen and left null where the instance
    is silent. A missing reading is shown as missing rather than as a zero: "0%
    done" and "no progress reported" are different things to act on.
    """
    nested = row.get("status") if isinstance(row.get("status"), dict) else {}
    task = row.get("print") if isinstance(row.get("print"), dict) else {}
    # A nested object wins for the readings, since a build that has one keeps
    # the live values there and only a summary at the top level.
    look = {**row, **nested, **task}
    return {
        "id": _first(row, "id", "printer_id", "printerId"),
        "name": _first(row, "name", "printer_name", "device_name", "dev_name"),
        "model": _first(row, "model", "printer_model", "dev_model"),
        # "status" is a word on most builds and a whole object on some; where
        # it is an object the word is inside it.
        "status": _first_scalar(look, "status", "state", "print_status"),
        "online": _first(row, "online", "is_online", "connected"),
        # What it is doing, in Bambu's own vocabulary — RUNNING, PAUSE, FINISH.
        "state": _first_scalar(
            look, "gcode_state", "gcodeState", "print_state", "printState", "job_state"
        ),
        # How far through, 0–100.
        "progress": _rounded(
            _first(look, "progress", "percent", "percentage", "mc_percent", "print_percent")
        ),
        "remaining_minutes": _rounded(
            _first(
                look, "remaining_minutes", "remaining_time", "mc_remaining_time",
                "time_remaining", "eta_minutes",
            )
        ),
        # The plate on it now, which is how a card is matched to a job by eye.
        "current_file": _first_scalar(
            look, "current_file", "subtask_name", "task_name", "job_name",
            "print_name", "filename", "file",
        ),
        "layer": _rounded(_first(look, "layer", "layer_num", "current_layer")),
        "layers": _rounded(_first(look, "layers", "total_layer_num", "total_layers")),
        "nozzle_temp": _number(_first(look, "nozzle_temp", "nozzle_temper", "nozzle")),
        "nozzle_target": _number(
            _first(look, "nozzle_target", "nozzle_target_temper", "nozzle_target_temp")
        ),
        "bed_temp": _number(_first(look, "bed_temp", "bed_temper", "bed")),
        "bed_target": _number(
            _first(look, "bed_target", "bed_target_temper", "bed_target_temp")
        ),
        "chamber_temp": _number(_first(look, "chamber_temp", "chamber_temper", "chamber")),
        # Whatever it is unhappy about. Bambu machines report this as a numeric
        # code that is 0 when all is well, so "no fault" arrives as a value
        # rather than as an absence and has to be recognised — a card reading
        # "error: 0" in red would be worse than one saying nothing.
        "error": _fault(
            _first_scalar(look, "error", "print_error", "error_message", "last_error")
        ),
        # Some builds hand the camera over as a URL on the row instead of
        # serving an endpoint for it. Kept as sent; whether it can be proxied
        # is a question about where it points, answered at the proxy.
        "camera_url": _first_scalar(
            look, "camera_url", "cameraUrl", "stream_url", "webcam_url", "mjpeg_url"
        ),
    }


def join_path(parent: str, name: str) -> str:
    """Folder + entry, as one absolute file-manager path."""
    return "/" + "/".join(part for part in f"{parent}/{name}".split("/") if part)


def is_printable(name: str) -> bool:
    return str(name or "").lower().endswith(PRINTABLE_SUFFIXES)


def parse_file_entry(row: dict[str, Any], *, parent: str = "") -> dict[str, Any]:
    """One row of a file-manager listing, whatever this build calls its columns.

    Folder or file is the one thing that must be right — get it wrong and the
    walk either stops at the top level or recurses into a 3MF — so it is read
    from an explicit type, an explicit flag, or the presence of children, and
    only falls back to "file" when the row says none of those.
    """
    name = str(
        _first(
            row, "name", "filename", "file_name", "basename", "title",
            "label", "display_name", "displayName", "text",
        )
        or ""
    ).strip().strip("/")
    raw_path = _first(row, "path", "full_path", "fullPath", "key", "location")
    path = str(raw_path).strip() if raw_path else ""
    if path:
        path = join_path("", path)
    else:
        path = join_path(parent, name)
    if not name:
        name = path.rsplit("/", 1)[-1]

    kind = str(
        _first(
            row, "type", "kind", "node_type", "nodeType", "entry_type",
            "item_type", "object_type", "mime_type",
        )
        or ""
    ).strip().lower()
    children = row.get("children")
    if kind in {"dir", "directory", "folder"} or kind.startswith("folder"):
        folder = True
    elif kind in {"file", "model", "archive"} or "/" in kind:
        folder = False
    else:
        flag = _first(row, "is_dir", "isDir", "is_directory", "isDirectory", "is_folder")
        folder = bool(flag) if flag is not None else isinstance(children, list)

    return {
        "name": name,
        "path": path,
        "kind": "folder" if folder else "file",
        "size": _first(row, "size", "file_size", "bytes", "length"),
        "modified": _first(row, "modified", "modified_at", "updated_at", "mtime", "date"),
        # A file manager entry that is also a known archive can be queued by id,
        # which is the older and better-supported of the two ways to send it.
        "archive_id": _first(row, "archive_id", "archiveId", "model_id", "id"),
        "printable": False if folder else is_printable(name),
        "children": children if isinstance(children, list) else None,
    }


def _ref_id(value: Any) -> Any:
    """An id, whether the row carried the id or the whole related object."""
    if isinstance(value, dict):
        return _first(value, "id", "folder_id", "uuid")
    return value


def parse_library_folder(row: dict[str, Any]) -> dict[str, Any]:
    """One row of the library's folder collection.

    A row may say where it sits in two ways: an id pointing at its parent, or a
    path that spells the whole ancestry out. The path is taken when there is
    one — it needs no chain to resolve and cannot be broken by a parent that
    did not come back in the same reply.
    """
    raw_path = _first(row, "path", "full_path", "fullPath", "folder_path", "location")
    return {
        "id": _first(row, "id", "folder_id", "folderId", "uuid"),
        "name": str(
            _first(row, "name", "title", "label", "folder_name", "display_name") or ""
        ).strip().strip("/"),
        "parent_id": _ref_id(
            _first(
                row, "parent_id", "parentId", "parent_folder_id", "parentFolderId",
                "parent", "folder_id",
            )
        ),
        "path": join_path("", str(raw_path)) if raw_path else None,
        # How many files the folder says it holds. Worth having: it saves asking
        # an endpoint for the contents of a folder that has already said it has
        # none, and it is the only cross-check on whether a read was complete.
        "file_count": _first(row, "file_count", "fileCount", "files_count", "num_files"),
    }


def flatten_library_folders(
    rows: list[dict[str, Any]], *, max_folders: int = 4000, max_depth: int = 16
) -> list[dict[str, Any]]:
    """The folder list, with any subtree it carries inline brought out flat.

    Bambuddy's folder list answers with the top level and hangs each row's whole
    subtree off it under `children`. The structure is in the reply already — it
    is just not at the top of it, and a reader that only looked at the top saw a
    library with no subfolders in it whatsoever.
    """
    out: list[dict[str, Any]] = []

    def walk(rows: list[Any], parent_id: Any, depth: int) -> None:
        for row in rows:
            if not isinstance(row, dict) or len(out) >= max_folders:
                continue
            folder = parse_library_folder(row)
            if folder["id"] is None:
                continue
            # A nested row usually repeats its parent; where it does not, the
            # nesting itself said so.
            if folder["parent_id"] is None and parent_id is not None:
                folder["parent_id"] = parent_id
            out.append(folder)
            children = row.get("children")
            if isinstance(children, list) and depth < max_depth:
                walk(children, folder["id"], depth + 1)

    walk(rows, None, 0)
    return out


def parse_library_file(row: dict[str, Any]) -> dict[str, Any]:
    """One row of the library's file collection."""
    raw_path = _first(row, "path", "full_path", "fullPath", "file_path", "location")
    return {
        "path": join_path("", str(raw_path)) if raw_path else None,
        "id": _first(row, "id", "file_id", "fileId", "uuid"),
        "name": str(
            _first(
                row, "name", "filename", "file_name", "title", "label", "display_name",
                "original_filename",
            )
            or ""
        ).strip().strip("/"),
        "folder_id": _ref_id(
            _first(row, "folder_id", "folderId", "parent_id", "parentId", "folder")
        ),
        "size": _first(row, "size", "file_size", "bytes", "length"),
        "modified": _first(
            row, "modified", "modified_at", "updated_at", "created_at", "mtime"
        ),
        "plates": _first(row, "plates", "plate_count", "plateCount"),
    }


def build_library_tree(
    folders: list[dict[str, Any]],
    files: list[dict[str, Any]],
    *,
    max_nodes: int = 8000,
) -> dict[str, Any]:
    """Folders and files, two flat collections, assembled into the structure.

    The library carries its shape as a parent id on every row rather than as
    something you discover by walking, so this is arithmetic on two lists — and
    it gets things a walk cannot: empty folders, and folders whose parent is
    missing, which would otherwise be silently unreachable.

    Paths are built from names because a path is what a person reads and what
    the mapping stores. Names are not unique and ids are, so a collision keeps
    both by disambiguating with the id rather than letting one file overwrite
    another.
    """
    by_id = {folder["id"]: folder for folder in folders if folder["id"] is not None}
    paths: dict[Any, str] = {}
    truncated = False

    def path_of(folder_id: Any, seen: frozenset = frozenset()) -> str:
        """A folder's absolute path, resolving its parents on the way up."""
        if folder_id in paths:
            return paths[folder_id]
        folder = by_id.get(folder_id)
        # A parent that is not in the collection, or a folder that is its own
        # ancestor, hangs off the root rather than disappearing with its files.
        if folder is None or folder_id in seen:
            return ""
        if folder.get("path"):
            # The row spelled its ancestry out, so there is no chain to walk and
            # no parent that has to have come back in the same reply.
            paths[folder_id] = folder["path"]
            return paths[folder_id]
        parent = path_of(folder["parent_id"], seen | {folder_id})
        paths[folder_id] = join_path(parent, folder["name"] or str(folder_id))
        return paths[folder_id]

    nodes: dict[str, dict[str, Any]] = {}

    def folderish_parent(path: str) -> str:
        cut = path.rfind("/")
        return path[:cut] if cut > 0 else "/"

    def place(path: str, node: dict[str, Any]) -> None:
        """Add a node, keeping both when two rows want the same path."""
        nonlocal truncated
        if len(nodes) >= max_nodes:
            truncated = True
            return
        unique = path
        if unique in nodes:
            unique = f"{path} ({node.get('library_id')})"
            if unique in nodes:
                return
        node["path"] = unique
        node["parent"] = folderish_parent(unique)
        node["depth"] = max(0, unique.count("/") - 1)
        nodes[unique] = node

    for folder in folders:
        if folder["id"] is None:
            continue
        path = path_of(folder["id"])
        if not path:
            continue
        place(
            path,
            {
                "name": folder["name"] or str(folder["id"]),
                "kind": "folder",
                "size": None,
                "modified": None,
                "archive_id": folder["id"],
                "printable": False,
                "library_id": folder["id"],
            },
        )

    for row in files:
        if not row["name"] and not row.get("path"):
            continue
        if row.get("path"):
            where = row["path"]
        else:
            parent = path_of(row["folder_id"]) if row["folder_id"] is not None else ""
            where = join_path(parent, row["name"])
        place(
            where,
            {
                "name": row["name"] or row["path"].rsplit("/", 1)[-1],
                "kind": "file",
                "size": row["size"],
                "modified": row["modified"],
                # The library's own id for this file, which is what everything
                # else in its API takes.
                "archive_id": row["id"],
                "printable": is_printable(row["name"]),
                "library_id": row["id"],
            },
        )

    # A node whose folder never came back would be drawn nowhere: the picker
    # lists a folder's children, and nothing is a child of a folder that does
    # not exist. Two rows deep under an absent folder is invisible, not empty,
    # which is the worst way for a file to go missing. So the folders a path
    # implies are made real.
    for path in [node["path"] for node in nodes.values()]:
        parts = path.strip("/").split("/")
        for depth in range(1, len(parts)):
            branch = "/" + "/".join(parts[:depth])
            if branch in nodes:
                continue
            nodes[branch] = {
                "name": parts[depth - 1],
                "kind": "folder",
                "size": None,
                "modified": None,
                "archive_id": None,
                "printable": False,
                "library_id": None,
                "path": branch,
                "parent": folderish_parent(branch),
                "depth": max(0, branch.count("/") - 1),
                # Nothing described it; it is known only because something
                # inside it named it.
                "implied": True,
            }

    listing = sorted(
        nodes.values(), key=lambda node: (node["path"].count("/"), node["path"].lower())
    )
    return {
        "files": listing,
        "truncated": truncated,
        "printable": sum(1 for node in listing if node["printable"]),
        "folders": sum(1 for node in listing if node["kind"] == "folder"),
    }


def parse_queue_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _first(row, "id", "queue_id", "queueId", "job_id", "jobId"),
        "status": _first(row, "status", "state", "print_status", "job_status"),
        "archive_id": _first(row, "archive_id", "archiveId", "archive"),
        "printer_id": _first(row, "printer_id", "printerId"),
        "error": _first(row, "error", "error_message", "message", "failure_reason"),
    }


class BambuddyClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        base_url = str(payload.get("base_url") or "").rstrip("/")
        if not base_url:
            raise IntegrationError(PROVIDER_BAMBUDDY, "No Bambuddy base URL configured")
        self.base_url = base_url
        self.api_key = str(payload.get("api_key") or "")
        # Three layers, weakest first: our defaults, what this instance's spec
        # said last time we read it, and what the operator typed into Advanced.
        # Discovered paths are kept apart from typed ones so that re-validating
        # after a Bambuddy upgrade can move them — folding them into `paths`
        # would make them indistinguishable from a deliberate choice and pin the
        # config to endpoints that no longer exist.
        self.explicit_paths = {k: v for k, v in (payload.get("paths") or {}).items() if v}
        self.discovered_paths = {
            k: v for k, v in (payload.get("discovered_paths") or {}).items() if v
        }
        self.paths = {**DEFAULT_PATHS, **self.discovered_paths, **self.explicit_paths}
        self.fields = {**DEFAULT_FIELDS, **(payload.get("fields") or {})}
        self.auth_header = str(payload.get("auth_header") or DEFAULT_AUTH_HEADER)
        # The instance's OpenAPI document, once anything in this request has
        # had cause to read it. Per client, so it lives exactly as long as the
        # request does and never goes stale between them.
        self.last_spec: dict[str, Any] | None = None
        # What the most recent folder listing actually contained, so an empty
        # tree can say whether Bambuddy sent nothing or sent something unread.
        self.last_listing: dict[str, Any] = {}

    def url_for(self, path: str) -> str:
        """The absolute URL a call will actually hit.

        Built the same way httpx merges a relative path onto a base URL, so a
        base URL with a path component shows its real effect: base
        `http://host:8080/api` plus `/api/printers` is `/api/api/printers`.
        """
        return str(httpx.URL(self.base_url + "/").join(path.lstrip("/")))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers[self.auth_header] = self.api_key
            # Some builds expect a bearer token instead of the custom header;
            # sending both is harmless and avoids a config foot-gun.
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        return headers

    async def _call(
        self, method: str, path: str, *, retries: int = 2, **kwargs: Any
    ) -> Any:
        async with new_client(base_url=self.base_url, timeout=LAN_TIMEOUT) as client:
            try:
                response = await request(
                    client,
                    method,
                    path,
                    provider=PROVIDER_BAMBUDDY,
                    headers=self._headers(),
                    retries=retries,
                    **kwargs,
                )
            except IntegrationError as exc:
                # Name the whole URL, not just the status. "404" tells the
                # operator nothing when the base URL and the path are configured
                # separately and either could be wrong — and a base URL that
                # already ends in /api silently doubles up into /api/api/…,
                # which is invisible unless the joined URL is printed.
                exc.args = (f"{exc.args[0] if exc.args else 'Request failed'} at {self.url_for(path)}",)
                raise
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(
                PROVIDER_BAMBUDDY, f"Bambuddy returned non-JSON from {path}"
            ) from exc

    async def fetch_openapi(self) -> dict[str, Any]:
        """Locate and read the instance's OpenAPI document (setup validation)."""
        errors: list[str] = []
        candidates = [self.paths.get("openapi") or "", *OPENAPI_CANDIDATES]
        for path in [p for p in dict.fromkeys(candidates) if p]:
            try:
                data = await self._call("GET", path, retries=0)
            except TransportFailed:
                # The host is not answering at all. The remaining candidates are
                # on the same host, so they can only fail the same way — and each
                # one costs a full timeout the operator is sitting through.
                raise
            except IntegrationError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
                info = data.get("info") or {}
                spec_paths = data.get("paths") or {}
                return {
                    "path": path,
                    "title": info.get("title"),
                    "version": info.get("version"),
                    "openapi": data.get("openapi") or data.get("swagger"),
                    "operation_count": sum(
                        len(v) for v in spec_paths.values() if isinstance(v, dict)
                    ),
                    # Kept so the endpoints can be read off the instance instead
                    # of guessed. Bambuddy ships hundreds of them and they move
                    # between releases; the spec is the only statement of what
                    # this particular instance serves.
                    "discovered": discover_paths(spec_paths),
                    "collections": collection_paths(spec_paths),
                    "cameras": camera_paths(spec_paths),
                }
        raise IntegrationError(
            PROVIDER_BAMBUDDY,
            "Could not find an OpenAPI document on this Bambuddy instance. "
            "Set the path under Settings → Bambuddy → Advanced. Tried: "
            + "; ".join(errors),
        )

    async def validate(self) -> dict[str, Any]:
        """Setup check: read the spec, adopt the paths it states, list printers.

        Runs under one budget covering every call. An operator waiting on a
        form needs an answer, and a reverse proxy in front of PrintFlow will
        replace a slow reply with its own error page long before httpx's
        per-request timeouts have finished stacking up.
        """
        async with deadline(PROVIDER_BAMBUDDY, "Checking the Bambuddy address"):
            spec: dict[str, Any] = {}
            try:
                spec = await self.fetch_openapi()
            except TransportFailed:
                # Nothing is answering — reporting the printers call separately
                # would just repeat the same failure.
                raise
            except IntegrationError as exc:
                # The spec is informational; printers is the real test.
                spec = {"warning": str(exc)}

            adopted = self.adopt_discovered(spec.get("discovered") or {})
            try:
                printers = await self.list_printers(retries=0)
            except IntegrationError as exc:
                raise self._explain_404(exc, "printers", spec) from exc
        return {"openapi": spec, "printers": printers, "adopted_paths": adopted}

    def adopt_discovered(self, discovered: dict[str, Any]) -> dict[str, str]:
        """Take the paths the instance's own spec states, over our defaults.

        Only fills roles the operator has not set by hand: an explicit choice in
        Advanced settings outranks anything found by matching names.
        """
        explicit = set(self.explicit_paths)
        adopted: dict[str, str] = {}
        for name, result in discovered.items():
            path = (result or {}).get("path")
            if not path or name in explicit:
                continue
            self.discovered_paths[name] = path
            if self.paths.get(name) != path:
                self.paths[name] = path
                adopted[name] = path
        return adopted

    async def resolve_paths(self, roles: tuple[str, ...]) -> dict[str, str]:
        """Re-read the instance's spec and adopt what it now says for these roles.

        Bambuddy serves several hundred endpoints and moves them between
        releases, which is why PrintFlow reads them off the instance rather than
        assuming. The gap that leaves: a role added in a *later PrintFlow*
        release was never discovered for a connection made before that role
        existed, so it silently falls back to a default that may be a guess. The
        symptom is a 404 on an endpoint nobody chose, and the fix — re-saving
        Settings — is not something anyone would think of.

        So a 404 re-reads the document instead. Roles the operator set by hand
        are left alone, because adopt_discovered will not overwrite them.
        """
        # A client lives exactly as long as one request, so a document already
        # read during it is the same document — and a request that needs two
        # roles corrected should not fetch it twice to learn both.
        spec = self.last_spec or await self.fetch_openapi()
        # Kept so that explaining a failure afterwards does not re-fetch a
        # document that was read moments ago, on a request already failing.
        self.last_spec = spec
        discovered = spec.get("discovered") or {}
        return self.adopt_discovered({name: discovered.get(name) for name in roles})

    def _explain_404(
        self, exc: IntegrationError, role: str, spec: dict[str, Any]
    ) -> IntegrationError:
        """A 404 means the host is right and the path is wrong — say so."""
        if exc.status_code != 404:
            return exc
        if spec.get("warning"):
            hint = (
                "PrintFlow could not read this instance's OpenAPI document either, "
                "so it cannot look the right path up. Set it under Advanced."
            )
        else:
            hint = (
                f"Its OpenAPI document does not describe a {role} endpoint that "
                "PrintFlow recognises, so the path has to be set under Advanced."
            )
        return IntegrationError(
            PROVIDER_BAMBUDDY,
            f"{exc.args[0] if exc.args else 'Request failed'}. "
            f"The address is reachable — Bambuddy answered, it just has nothing "
            f"at that path. {hint}",
            status_code=exc.status_code,
            body=exc.body,
        )

    async def list_printers(self, *, retries: int = 2) -> list[dict[str, Any]]:
        data = await self._call("GET", self.paths["printers"], retries=retries)
        return [parse_printer(row) for row in _as_list(data)]

    def printer_detail_path(self, printer_id: Any) -> str:
        return self.paths["printer_detail"].replace("{printer_id}", str(printer_id))

    def camera_target(self, printer_id: Any, override: str | None = None) -> str:
        """The absolute URL of a machine's camera, refusing to leave this host.

        A build may hand the camera over as a URL on the printer row rather than
        as an endpoint of its own. That URL arrives from outside, and PrintFlow
        would be fetching it on behalf of whoever opened the page — so it is
        followed only where it points back at the Bambuddy this connection is
        already talking to. Anywhere else and the configured path is used
        instead; the UI offers the original as a link the browser can try
        itself, which is the honest version of "PrintFlow cannot reach that".
        """
        path = self.paths["printer_camera"].replace("{printer_id}", str(printer_id))
        if not override:
            return self.url_for(path)
        candidate = httpx.URL(str(override))
        if not candidate.is_absolute_url:
            return self.url_for(str(override))
        base = httpx.URL(self.base_url)
        if (candidate.scheme, candidate.host, candidate.port) == (
            base.scheme, base.host, base.port
        ):
            return str(candidate)
        return self.url_for(path)

    def camera_is_ours(self, override: str | None) -> bool:
        """Whether that camera URL is one this instance would serve."""
        if not override:
            return True
        candidate = httpx.URL(str(override))
        if not candidate.is_absolute_url:
            return True
        base = httpx.URL(self.base_url)
        return (candidate.scheme, candidate.host, candidate.port) == (
            base.scheme, base.host, base.port
        )

    @asynccontextmanager
    async def camera(
        self, printer_id: Any, *, override: str | None = None
    ) -> AsyncIterator[httpx.Response]:
        """The camera's response, still open — an image, or a stream of them.

        Kept as a context manager rather than read into memory: a live camera is
        multipart and never ends, so the only sane thing to do with it is hand
        the bytes on as they arrive.
        """
        target = self.camera_target(printer_id, override)
        client = new_client(timeout=CAMERA_TIMEOUT)
        try:
            async with client.stream("GET", target, headers=self._headers()) as response:
                if response.status_code >= 400:
                    body = (await response.aread())[:400].decode("utf-8", "replace")
                    raise IntegrationError(
                        PROVIDER_BAMBUDDY,
                        f"Unexpected response from bambuddy at {target} "
                        f"(HTTP {response.status_code})",
                        status_code=response.status_code,
                        body=body,
                    )
                yield response
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransportFailed(
                PROVIDER_BAMBUDDY, transport_reason(exc, client, target)
            ) from exc
        finally:
            await client.aclose()

    async def read_printer(self, printer_id: Any) -> dict[str, Any]:
        """One machine, asked about directly."""
        data = await self._call("GET", self.printer_detail_path(printer_id))
        return parse_printer(_as_item(data))

    async def list_archives(
        self, *, search: str = "", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search:
            # Instances differ on the query parameter name; send the common ones.
            params["search"] = search
            params["q"] = search
        data = await self._call("GET", self.paths["archives"], params=params)
        rows = [parse_archive(row) for row in _as_list(data)]
        if search:
            # Fall back to client-side filtering when the server ignored the query.
            needle = search.lower()
            filtered = [r for r in rows if needle in str(r.get("name") or "").lower()]
            if filtered:
                return filtered
        return rows

    async def iter_archives(
        self, *, search: str = "", page_size: int = 200, max_pages: int = 25
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every archive, not just the first page.

        A shop with three hundred print files could only ever see fifty of them,
        which made the picker a search box you had to already know the answer
        for. Returns (archives, truncated) — truncated says the cap was hit, so
        a partial list is never presented as the whole thing.

        Deduplicated by id: an instance that ignores `offset` would otherwise
        hand back the same page until the cap ran out.
        """
        seen: dict[Any, dict[str, Any]] = {}
        truncated = False
        for page in range(max_pages):
            rows = await self.list_archives(
                search=search, limit=page_size, offset=page * page_size
            )
            fresh = [row for row in rows if row.get("id") not in seen]
            for row in fresh:
                seen[row.get("id")] = row
            if len(rows) < page_size or not fresh:
                break
            if page == max_pages - 1:
                truncated = True
        return list(seen.values()), truncated

    def files_path(self, printer_id: int | None) -> str:
        """Which endpoint holds the files — the farm's, or one machine's."""
        if printer_id is None:
            return self.paths["files"]
        template = self.paths["printer_files"]
        if "{printer_id}" not in template:
            # An operator has pointed the per-printer role at a plain path, which
            # is how an instance with one shared library is configured. The
            # printer then only says where the plate goes.
            return template
        return template.replace("{printer_id}", str(printer_id))

    async def list_files(
        self, *, path: str = "", printer_id: int | None = None
    ) -> list[dict[str, Any]]:
        """One folder of the file manager, folders first then files by name."""
        params: dict[str, Any] = {}
        if path and path != "/":
            # Instances differ on what they call the folder; send the common ones.
            params = {"path": path, "dir": path, "folder": path}
        data = await self._call("GET", self.files_path(printer_id), params=params)
        raw = file_rows(data)
        rows = [parse_file_entry(row, parent=path) for row in raw]
        rows = [row for row in rows if row["name"]]
        # Counted so an empty answer can say which kind of empty it is: nothing
        # there, or rows PrintFlow could not read. The two need different fixes
        # and look identical from the outside.
        self.last_listing = {
            "endpoint": self.files_path(printer_id),
            "rows": len(raw),
            "named": len(rows),
        }
        rows.sort(key=lambda row: (row["kind"] != "folder", row["name"].lower()))
        return rows

    async def _probe(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            data = await self._call("GET", endpoint, params=params or None)
        except IntegrationError as exc:
            return {"endpoint": self.url_for(endpoint), "error": str(exc)}
        body = json.dumps(data, indent=2, default=str)
        return {
            "endpoint": self.url_for(endpoint),
            "keys": sorted(data.keys()) if isinstance(data, dict) else None,
            "kind": type(data).__name__,
            "rows_found": len(file_rows(data)),
            "total": _envelope_total(data),
            "body": body[:6000],
            "body_truncated": len(body) > 6000,
        }

    async def raw_farm(self) -> dict[str, Any]:
        """The untouched printer replies, for a farm screen that is missing things.

        Same reason as the file manager: every instance is self-hosted, no two
        of them spell a temperature the same, and a card with blank readings
        cannot be diagnosed from outside. The listing and one machine's detail
        side by side say which of the two has the numbers in it.
        """
        probes = [await self._probe(self.paths["printers"])]
        try:
            printers = await self.list_printers()
        except IntegrationError:
            printers = []
        first = next((row["id"] for row in printers if row.get("id") is not None), None)
        if first is not None:
            probes.append(await self._probe(self.printer_detail_path(first)))
        return {"probes": probes}

    async def raw_listing(
        self, *, path: str = "", printer_id: int | None = None
    ) -> dict[str, Any]:
        """The untouched responses, for when the parsed answer is missing things.

        There is no way to guess from here why one particular self-hosted
        instance returned something PrintFlow made nothing of. Showing the
        replies turns that into a question somebody can actually answer — every
        endpoint that feeds the picker, because "folders but no files" is a
        different fault from "nothing at all" and both land here.
        """
        if printer_id is not None:
            return {"probes": [await self._probe(self.files_path(printer_id))]}

        # Every call the tree is built from, in the order it makes them. Which
        # one is wrong is not guessable from outside a self-hosted instance, but
        # four replies side by side make it obvious.
        probes = [
            await self._probe(self.paths["library_folders"]),
            await self._probe(self.paths["library_files"]),
        ]
        folders = file_rows(
            await self._call("GET", self.paths["library_folders"], params=None)
        )
        first = next(
            (parse_library_folder(row)["id"] for row in folders
             if parse_library_folder(row)["id"] is not None),
            None,
        )
        if first is not None:
            probes.append(
                await self._probe(self.paths["library_folders"], {"parent_id": first})
            )
            probes.append(
                await self._probe(self.paths["library_files"], {"folder_id": first})
            )
        if path:
            probes.append(await self._probe(self.files_path(None), {"path": path}))
        return {"probes": probes}

    async def _all_rows(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 500,
        max_pages: int = 40,
    ) -> list[dict[str, Any]]:
        """Every row of a collection, following its paging only if it has any.

        The first request carries no paging at all. An endpoint that validates
        its query strictly, or that reads `page` while being handed `offset`
        too, can answer a well-meant pile of paging parameters with nothing —
        and "nothing" from a collection that has rows in it is the worst answer
        available, because it looks exactly like an empty library. Asking
        plainly first means the common case never depends on guessing the
        paging dialect right.

        Paging is entered only on evidence: a `total` in the envelope larger
        than what came back, or a full-looking first page. Rows are deduplicated
        by identity, so an instance that ignores limit/offset cannot spin.
        """
        first = await self._call("GET", path, params=params or None)
        rows = file_rows(first)
        total = _envelope_total(first)
        if not rows:
            return []
        if total is not None and len(rows) >= total:
            return rows
        if total is None and len(rows) < page_size:
            # No count, and a short first page: there is no reason to think a
            # second one exists.
            return rows

        seen = {json.dumps(row, sort_keys=True, default=str) for row in rows}
        for page in range(1, max_pages):
            batch = file_rows(
                await self._call(
                    "GET",
                    path,
                    params={
                        **(params or {}),
                        "limit": len(rows) or page_size,
                        "offset": len(rows),
                    },
                )
            )
            fresh = 0
            for row in batch:
                key = json.dumps(row, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                fresh += 1
            if not fresh or (total is not None and len(rows) >= total):
                break
        return rows

    async def _files_by_folder(
        self, folders: list[dict[str, Any]], *, max_folders: int = 400
    ) -> tuple[list[dict[str, Any]], bool]:
        """One request per folder, for a files endpoint that needs to be asked.

        The flat list is the right way to do this and is tried first. Some
        builds only answer for a named folder, though, and a shop whose library
        is all folders would otherwise be shown twenty empty ones — which is
        worse than a slower read.
        """
        rows: list[dict[str, Any]] = []
        # Folders that have already said they hold nothing are not asked. On a
        # library organised by printer model that is most of them, and a request
        # per folder is the whole cost of this path.
        folders = [folder for folder in folders if folder["file_count"] != 0]
        truncated = len(folders) > max_folders
        for folder in folders[:max_folders]:
            folder_id = folder["id"]
            if folder_id is None:
                continue
            batch = await self._all_rows(
                self.paths["library_files"], params={"folder_id": folder_id}
            )
            for row in batch:
                # The folder is known from the question that was asked, so a row
                # that does not repeat it is still placed correctly.
                row.setdefault("folder_id", folder_id)
            rows += batch
        return rows, truncated

    async def _inside(
        self, folder_id: Any, method: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """What one folder holds, asked the way this instance answers."""
        if method == "detail":
            item = f"{self.paths['library_folders'].rstrip('/')}/{folder_id}"
            return split_folder_detail(await self._call("GET", item))
        rows = await self._all_rows(
            self.paths["library_folders"], params={method: folder_id}
        )
        return rows, []

    async def _child_method(self, folder_id: Any, known: set[Any]) -> str | None:
        """How to ask this instance what is inside a folder.

        There are two shapes and no way to tell them apart but to try. A list
        endpoint may filter on a parent, in which case one query parameter does
        it — but instances disagree on the name, and one that does not filter
        answers with the top level again, which is not an answer. Otherwise the
        item endpoint, "Get Folder", is how a file manager drills in, and what
        it returns is the folder together with what it holds.

        Settled once on the first folder and then used for the rest, so this
        costs a couple of requests rather than tripling every one of them.
        """
        for key in ("parent_id", "parent", "folder_id"):
            try:
                rows, _ = await self._inside(folder_id, key)
            except IntegrationError:
                continue
            # The same top level over again is the parameter being ignored.
            if any(parse_library_folder(row)["id"] not in known for row in rows):
                return key
        try:
            subs, files = await self._inside(folder_id, "detail")
        except IntegrationError:
            return None
        return "detail" if (subs or files) else None

    async def _descend_folders(
        self,
        folders: list[dict[str, Any]],
        *,
        max_folders: int = 400,
        max_depth: int = 8,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """Subfolders — and anything they hold — for a list that gives one level.

        A "list folders" that returns the top level and nothing else is a
        reasonable thing for an API to be: it is the folder list you would draw
        on a first screen. It is indistinguishable from a complete list, though,
        right up until a folder that holds only subfolders shows as empty and
        everything filed inside it is invisible.
        """
        seen = {folder["id"] for folder in folders if folder["id"] is not None}
        queue = [(folder, 0) for folder in folders if folder["id"] is not None]
        if not queue:
            return [], [], False

        method = await self._child_method(queue[0][0]["id"], seen)
        if method is None:
            return [], [], False

        found: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        truncated = False
        while queue:
            folder, depth = queue.pop(0)
            if depth >= max_depth or len(seen) >= max_folders:
                truncated = truncated or bool(queue)
                break
            try:
                rows, inner = await self._inside(folder["id"], method)
            except IntegrationError:
                continue
            for row in inner:
                # The folder is known from the question that was asked.
                row.setdefault("folder_id", folder["id"])
            files += inner
            for row in rows:
                child = parse_library_folder(row)
                if child["id"] is None or child["id"] in seen:
                    continue
                seen.add(child["id"])
                # The parent is known from the question, whatever the row says —
                # and some builds say nothing, having already been asked.
                if child["parent_id"] is None and not child["path"]:
                    child["parent_id"] = folder["id"]
                found.append(child)
                queue.append((child, depth + 1))
        return found, files, truncated

    async def library_tree(self, *, max_nodes: int = 8000) -> dict[str, Any]:
        """The library's whole structure, from its folder and file collections.

        Two calls where two calls will do: the rows carry their own parent, so
        the shape is already in the data. Where they will not — a folder list
        that answers for one level, a file list that wants a folder named — it
        asks per folder rather than showing a library with holes in it.
        """
        folder_rows = await self._all_rows(self.paths["library_folders"])
        folders = flatten_library_folders(folder_rows)
        known = {folder["id"] for folder in folders if folder["id"] is not None}
        truncated = False
        notes: list[str] = []

        # Does anything in this list sit inside anything else in it? If not, the
        # list may be one level rather than all of them, and the only way to
        # find out is to ask.
        inner_files: list[dict[str, Any]] = []
        if folders and not any(
            folder["parent_id"] in known or folder["path"] for folder in folders
        ):
            nested, inner_files, cut = await self._descend_folders(folders)
            if nested:
                folders += nested
                notes.append("subfolders asked for")
            truncated = truncated or cut

        file_rows_raw = await self._all_rows(self.paths["library_files"])
        if not file_rows_raw and inner_files:
            # The drill-in brought the contents along with the structure.
            file_rows_raw = inner_files
            notes.append("files came with the folders")
        if not file_rows_raw and folders:
            # Folders but no files is not a library anybody keeps. Far likelier
            # is a files endpoint that only answers for a named folder.
            file_rows_raw, cut = await self._files_by_folder(folders)
            truncated = truncated or cut
            if file_rows_raw:
                notes.append("files asked per folder")

        tree = build_library_tree(
            folders,
            [parse_library_file(row) for row in file_rows_raw],
            max_nodes=max_nodes,
        )
        return {
            **tree,
            "truncated": tree["truncated"] or truncated,
            "endpoint": (
                f"{self.paths['library_folders']} + {self.paths['library_files']}"
                + (f" ({', '.join(notes)})" if notes else "")
            ),
            "root_rows": len(folders) + len(file_rows_raw),
            "root_named": len(tree["files"]),
            "flat": False,
        }

    async def file_tree(
        self,
        *,
        printer_id: int | None = None,
        max_nodes: int = 4000,
        max_depth: int = 8,
    ) -> dict[str, Any]:
        """The file manager's whole structure, flattened to a list of nodes.

        The point is to bring the *structure*, not a heap of filenames: every
        node carries its own path and its parent's, so the picker can redraw the
        same folders the operator sees in Bambuddy.

        Walked breadth-first so that a wide, shallow library is complete before
        a deep corner of it is explored, and capped on both nodes and depth —
        a file manager rooted at a filesystem could otherwise be unbounded. An
        instance that nests `children` inline is handled without extra calls.

        Paths already seen are never re-walked, so a folder that links to its
        own parent cannot spin. Nor can an instance that ignores the folder
        parameter and answers every request with its root: that hands back the
        parent's listing under a deeper name each time, which is not a cycle by
        path and used to build a tower of identical folders until the depth cap
        stopped it. A folder whose contents are its parent's contents is not a
        folder that was read — it is the parameter being ignored, and saying so
        is more use than eight copies of the same three files.
        """
        root: list[dict[str, Any]] = []
        nodes: dict[str, dict[str, Any]] = {}
        truncated = False
        flat = False
        prints: dict[str, str] = {}

        def fingerprint(rows: list[dict[str, Any]]) -> str:
            return "|".join(sorted(f"{row['kind']}:{row['name']}" for row in rows))

        def take(rows: list[dict[str, Any]], parent: str, depth: int) -> list[dict[str, Any]]:
            """Record a folder's rows; hand back the folders worth descending."""
            nonlocal truncated
            descend: list[dict[str, Any]] = []
            for row in rows:
                if row["path"] in nodes:
                    continue
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
                children = row.pop("children", None)
                node = {**row, "parent": parent or "/", "depth": depth}
                nodes[node["path"]] = node
                if node["kind"] != "folder":
                    continue
                if isinstance(children, list):
                    take(
                        [parse_file_entry(child, parent=node["path"]) for child in children
                         if isinstance(child, dict)],
                        node["path"],
                        depth + 1,
                    )
                elif depth + 1 < max_depth:
                    descend.append(node)
                else:
                    truncated = True
            return descend

        root_rows = await self.list_files(printer_id=printer_id)
        # The root listing is the one worth reporting on: everything below it is
        # only reached because the root named it.
        listing = dict(self.last_listing)
        prints["/"] = fingerprint(root_rows)
        root = take(root_rows, "", 0)
        queue = list(root)
        while queue:
            folder = queue.pop(0)
            if len(nodes) >= max_nodes:
                truncated = True
                break
            try:
                rows = await self.list_files(path=folder["path"], printer_id=printer_id)
            except IntegrationError:
                # One unreadable folder is not a reason to have no file manager.
                # It stays in the tree, empty, rather than vanishing from it.
                folder["unreadable"] = True
                continue
            if rows and fingerprint(rows) == prints.get(folder["parent"]):
                # The parent's listing again. Descending would invent a folder
                # per level for as deep as the cap allows.
                folder["unreadable"] = True
                flat = True
                continue
            prints[folder["path"]] = fingerprint(rows)
            queue.extend(take(rows, folder["path"], folder["depth"] + 1))

        nodes_out = sorted(
            nodes.values(),
            key=lambda node: (node["path"].count("/"), node["path"].lower()),
        )
        return {
            "files": nodes_out,
            "truncated": truncated,
            "printable": sum(1 for node in nodes_out if node["printable"]),
            "folders": sum(1 for node in nodes_out if node["kind"] == "folder"),
            # What the root reply held, so nothing here has to be guessed at
            # from an empty list.
            "endpoint": listing.get("endpoint"),
            "root_rows": listing.get("rows", 0),
            "root_named": listing.get("named", 0),
            # This instance answered a folder request with its root, so what is
            # here is one level and the folders in it could not be opened.
            "flat": flat,
        }

    async def list_printer_models(self) -> list[dict[str, Any]]:
        """The distinct models this farm has, with how many of each.

        A job is described by what can print it, not by which machine is free —
        so the thing to choose from is the model, and the count is what tells
        an operator whether choosing it strands the job on one machine.
        """
        printers = await self.list_printers()
        models: dict[str, dict[str, Any]] = {}
        for printer in printers:
            name = str(printer.get("model") or "").strip()
            if not name:
                continue
            entry = models.setdefault(
                name, {"model": name, "printers": 0, "online": 0, "names": []}
            )
            entry["printers"] += 1
            if printer.get("online"):
                entry["online"] += 1
            if printer.get("name"):
                entry["names"].append(str(printer["name"]))
        return sorted(models.values(), key=lambda entry: entry["model"].lower())

    async def list_queue(self) -> list[dict[str, Any]]:
        data = await self._call("GET", self.paths["queue"])
        return [parse_queue_item(row) for row in _as_list(data)]

    def build_queue_body(
        self,
        *,
        archive_id: int | None,
        plate_number: int | None,
        printer_id: int | None,
        print_options: dict[str, Any] | None,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if archive_id is not None:
            body[self.fields["archive_id"]] = archive_id
        # Sent alongside the id when there is one: an instance that only knows
        # about paths still gets a path, and one that only knows about ids
        # ignores a field it does not read.
        if file_path:
            body[self.fields["file_path"]] = file_path
        if plate_number is not None:
            body[self.fields["plate_number"]] = plate_number
        # Omitting printer_id lets Bambuddy dispatch to whichever printer is free.
        if printer_id is not None:
            body[self.fields["printer_id"]] = printer_id
        if print_options:
            body.update(print_options)
        return body

    async def enqueue(
        self,
        *,
        archive_id: int | None = None,
        plate_number: int | None = None,
        printer_id: int | None = None,
        print_options: dict[str, Any] | None = None,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        body = self.build_queue_body(
            archive_id=archive_id,
            plate_number=plate_number,
            printer_id=printer_id,
            print_options=print_options,
            file_path=file_path,
        )
        data = await self._call("POST", self.paths["queue"], json=body)
        item = parse_queue_item(data if isinstance(data, dict) else {})
        if item.get("id") is None:
            rows = _as_list(data)
            if rows:
                item = parse_queue_item(rows[0])
        return item

    async def cancel(self, queue_id: int) -> None:
        await self._call(
            "DELETE",
            f"{self.paths['queue'].rstrip('/')}/{queue_id}",
            expected=(200, 202, 204, 404),
        )

    def queue_item_url(self, queue_id: int | None) -> str | None:
        if queue_id is None:
            return None
        return f"{self.base_url}/queue/{queue_id}"


async def client_for(session: AsyncSession) -> BambuddyClient:
    payload = await credentials.require(session, PROVIDER_BAMBUDDY)
    return BambuddyClient(payload)


async def remember_paths(session: AsyncSession, adopted: dict[str, str]) -> None:
    """Keep a re-discovered path, so the next call does not have to find it again."""
    if not adopted:
        return
    payload = await credentials.load(session, PROVIDER_BAMBUDDY)
    if not payload:
        return
    payload["discovered_paths"] = {**(payload.get("discovered_paths") or {}), **adopted}
    await credentials.save(session, PROVIDER_BAMBUDDY, payload, mark_connected=False)


async def with_healing(
    session: AsyncSession,
    client: BambuddyClient,
    roles: tuple[str, ...],
    call: Any,
) -> Any:
    """Run a call; on a 404, re-read the instance's spec and run it once more.

    The same reason as the file manager: a path that 404s is a path nobody
    chose, either because it was never discovered for this connection or
    because the instance moved it in an upgrade. An instance whose library is
    at /api/v1/library is unlikely to have left everything else at /api.
    """
    try:
        return await call()
    except IntegrationError as exc:
        if exc.status_code != 404:
            raise
    adopted = await client.resolve_paths(roles)
    await remember_paths(session, adopted)
    return await call()


# Bumped whenever the rules for recognising a camera endpoint change. A stored
# "this build has no camera" was an answer to the rules of the day, and a
# release that widens them has to ask again — otherwise the shop that upgrades
# to get their cameras working is the one shop the fix cannot reach.
CAMERA_RULES = 2


async def ensure_camera(session: AsyncSession, client: BambuddyClient) -> bool:
    """Whether this instance serves printer cameras — asked once, then remembered.

    The alternative is drawing a camera on every card and letting each one fail,
    which on a farm of ten is ten broken pictures and ten pointless requests
    every time the page loads. The alternative to *that* is re-reading the
    instance's document on every load, which is one pointless request instead of
    ten but still one too many for a fact that does not change between releases.

    So the answer is stored beside the connection: the path when there is one,
    and — when there is not — which version of the matching rules said so. It is
    asked again when those rules change, when Settings is re-validated, and when
    somebody presses Look again on the Printers screen. All three are moments
    where the previous answer might have stopped being true.
    """
    if client.explicit_paths.get("printer_camera"):
        return True
    if client.discovered_paths.get("printer_camera"):
        return True
    payload = await credentials.load(session, PROVIDER_BAMBUDDY)
    if payload is None:
        return False
    if payload.get("camera_checked") == CAMERA_RULES:
        return False
    spec = client.last_spec or await client.fetch_openapi()
    client.last_spec = spec
    found = (spec.get("discovered") or {}).get("printer_camera")
    adopted = client.adopt_discovered({"printer_camera": found})
    if adopted or (found or {}).get("path"):
        await remember_paths(session, adopted or {"printer_camera": found["path"]})
        return True
    payload["camera_checked"] = CAMERA_RULES
    await credentials.save(session, PROVIDER_BAMBUDDY, payload, mark_connected=False)
    return False


async def camera_report(
    session: AsyncSession, client: BambuddyClient, *, again: bool = False
) -> dict[str, Any]:
    """Why there are no pictures — with what the instance says, not a guess.

    Every one of these instances is self-hosted and none of them are quite the
    same shape. "No camera" has causes that are indistinguishable from outside:
    the build has none, it has one under a name PrintFlow does not recognise, it
    has one that is not in the OpenAPI document at all, or it has one that
    answers with something that is not a picture. Each has a different fix and
    only the instance can tell them apart, so this asks it and shows the answer.
    """
    if again:
        payload = await credentials.load(session, PROVIDER_BAMBUDDY) or {}
        if payload.pop("camera_checked", None) is not None:
            await credentials.save(
                session, PROVIDER_BAMBUDDY, payload, mark_connected=False
            )
        client.discovered_paths.pop("printer_camera", None)
        client.paths["printer_camera"] = (
            client.explicit_paths.get("printer_camera")
            or DEFAULT_PATHS["printer_camera"]
        )

    available = await ensure_camera(session, client)
    spec = client.last_spec
    if spec is None:
        try:
            spec = client.last_spec = await client.fetch_openapi()
        except IntegrationError:
            spec = None

    try:
        printers = await with_healing(
            session, client, ("printers",), client.list_printers
        )
    except IntegrationError:
        printers = []
    first = next((row for row in printers if row.get("id") is not None), None)

    probe: dict[str, Any] | None = None
    if first is not None:
        target = client.camera_target(first["id"], first.get("camera_url"))
        probe = {"endpoint": target, "printer": first.get("name")}
        try:
            async with client.camera(
                first["id"], override=first.get("camera_url")
            ) as response:
                head = b""
                async for chunk in response.aiter_bytes():
                    head += chunk
                    if len(head) >= 64:
                        break
                probe["status"] = response.status_code
                probe["content_type"] = response.headers.get("content-type")
                # The first bytes say what it really is: ffd8 is a JPEG, "<" is
                # an error page, "{" is JSON explaining itself.
                probe["starts_with"] = head[:24].hex()
                probe["looks_like_a_picture"] = head[:2] == b"\xff\xd8" or str(
                    response.headers.get("content-type") or ""
                ).startswith(("image/", "multipart/"))
        except IntegrationError as exc:
            probe["error"] = str(exc)

    return {
        "available": available,
        "path": client.paths["printer_camera"],
        "source": (
            "typed under Advanced"
            if client.explicit_paths.get("printer_camera")
            else "read off this instance"
            if client.discovered_paths.get("printer_camera")
            else "PrintFlow's default, which is a guess"
        ),
        # Everything the document mentions that sounds like a camera. Empty is
        # the answer "this build has none", which is not a fault to fix.
        "candidates": (spec or {}).get("cameras") or [],
        "spec_path": (spec or {}).get("path"),
        "printer": first,
        "probe": probe,
    }


def _has_readings(printer: dict[str, Any]) -> bool:
    """Whether this row says anything about what the machine is doing now."""
    return any(
        printer.get(field) is not None
        for field in (
            "state", "progress", "remaining_minutes", "current_file",
            "nozzle_temp", "bed_temp", "chamber_temp", "layer",
        )
    )


async def read_farm(
    session: AsyncSession, client: BambuddyClient, *, max_detail: int = 32
) -> dict[str, Any]:
    """Every machine, with whatever this build will say about each.

    The farm listing is asked first and plainly, because on most builds it
    already carries the live readings and one call is the whole answer. Only
    where it comes back as a bare inventory — names and models, nothing about
    what any of them is doing — is each machine asked about individually, which
    is a call per printer and worth avoiding when the first call sufficed.

    A machine that will not answer keeps its inventory row rather than
    disappearing: a printer PrintFlow cannot reach is still a printer, and a
    farm screen that quietly drops one is worse than useless.
    """
    printers = await with_healing(session, client, ("printers",), client.list_printers)
    cameras = bool(printers) and await ensure_camera(session, client)

    def with_cameras(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Whether PrintFlow can put a picture on this card. A camera URL
        # pointing somewhere else on the network is not one it will fetch.
        for row in rows:
            row["camera"] = cameras and client.camera_is_ours(row.get("camera_url"))
        return rows

    live = any(_has_readings(row) for row in printers)
    if not printers or live:
        return {
            "printers": with_cameras(printers), "detailed": live, "detail_error": None
        }

    detailed: list[dict[str, Any]] = []
    failure: str | None = None
    healed = False
    for row in printers[:max_detail]:
        if row.get("id") is None:
            detailed.append(row)
            continue
        try:
            # The default for this role is a guess — it is the one endpoint
            # PrintFlow reaches for only when the listing came back thin, so a
            # connection made before it existed never discovered it. Heal once
            # for the farm rather than once per machine: a document re-read per
            # printer would be ten fetches to learn the same thing.
            first_ask, healed = not healed, True
            extra = await (
                with_healing(
                    session, client, ("printer_detail",),
                    lambda: client.read_printer(row["id"]),
                )
                if first_ask
                else client.read_printer(row["id"])
            )
        except IntegrationError as exc:
            failure = failure or str(exc)
            detailed.append(row)
            continue
        # The listing is the spine; the detail only fills in what it left null,
        # so a thin detail reply cannot blank out a name the list did have.
        detailed.append({**row, **{k: v for k, v in extra.items() if v is not None}})
    detailed.extend(printers[max_detail:])
    return {
        "printers": with_cameras(detailed),
        "detailed": any(_has_readings(row) for row in detailed),
        "detail_error": failure,
    }


async def read_file_manager(
    session: AsyncSession, client: BambuddyClient, *, printer_id: int | None = None
) -> dict[str, Any]:
    """The file manager, healing a wrong endpoint on the way.

    There are two shapes of file manager and PrintFlow does not get to choose
    which one an instance has. The library — folders and files as two
    collections, each row carrying its parent — is preferred where it exists,
    because two calls beat one per folder and it brings back the empty folders
    a walk would never reach. Failing that, a folder-at-a-time endpoint.

    A 404 means the path is wrong rather than the instance being down, and there
    are three reasons it can be wrong, each with its own answer:

    * the role was added after this connection was made, so it was never
      discovered — re-read the document and adopt what it says;
    * the instance has no per-printer file endpoint at all, only one shared
      library — read that instead, and say so, because the printer is still a
      real answer to *where the plate goes* even when it is not where the file
      is kept;
    * the instance genuinely serves no file manager — say that, and name what it
      does serve, so the operator can point it at the right endpoint under
      Advanced instead of being told a number.
    """
    ROLES = ("library_folders", "library_files", "files", "printer_files")

    async def attempt(shared: bool) -> dict[str, Any] | None:
        """The library, then the walk. None means neither answered."""
        if printer_id is None or shared:
            try:
                return {**await client.library_tree(), "shared": shared}
            except IntegrationError as exc:
                if exc.status_code != 404:
                    raise
        try:
            return {
                **await client.file_tree(printer_id=None if shared else printer_id),
                "shared": shared,
            }
        except IntegrationError as exc:
            if exc.status_code != 404:
                raise
        return None

    found = await attempt(shared=False)
    if found is not None:
        return found

    adopted = await client.resolve_paths(ROLES)
    await remember_paths(session, adopted)
    found = await attempt(shared=False)
    if found is not None:
        return found

    # This build keeps one library for the whole farm. Reading it is the right
    # answer — the file exists, and the printer chosen alongside it is still
    # where the plate is going.
    if printer_id is not None:
        found = await attempt(shared=True)
        if found is not None:
            return found

    raise await _no_file_manager(client)


async def _no_file_manager(client: BambuddyClient) -> IntegrationError:
    """Turn "404" into something an operator can act on."""
    tried = ", ".join(
        dict.fromkeys(
            [
                client.paths["library_folders"],
                client.paths["library_files"],
                client.paths["files"],
                client.paths["printer_files"],
            ]
        )
    )
    spec = getattr(client, "last_spec", None)
    if spec is None:
        try:
            spec = await client.fetch_openapi()
        except IntegrationError:
            spec = {}
    listable = [row["path"] for row in (spec.get("collections") or [])]
    likely = [
        path
        for path in listable
        if any(word in path.lower() for word in ("file", "librar", "folder", "storage"))
    ]
    if likely:
        hint = (
            "Its OpenAPI document does list "
            + ", ".join(likely[:5])
            + " — set the File manager endpoint to whichever of those is the "
            "file browser, under Settings → Bambuddy → Advanced."
        )
    elif listable:
        hint = (
            f"Its OpenAPI document describes {len(listable)} listable endpoints and "
            "none of them look like a file browser. This build may not have one — "
            "the Bambuddy library still works as a source of print files — or you "
            "can name the endpoint yourself under Settings → Bambuddy → Advanced."
        )
    else:
        hint = (
            "PrintFlow could not read its OpenAPI document either, so it cannot "
            "look the right path up. Set it under Settings → Bambuddy → Advanced."
        )
    return IntegrationError(
        PROVIDER_BAMBUDDY,
        f"This Bambuddy has no file manager at {tried}. {hint}",
        status_code=404,
    )
