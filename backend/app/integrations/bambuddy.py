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

from typing import Any

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
)

DEFAULT_PATHS: dict[str, str] = {
    "openapi": "/openapi.json",
    "printers": "/api/printers",
    "archives": "/api/archives",
    "queue": "/api/queue",
}

DEFAULT_FIELDS: dict[str, str] = {
    "archive_id": "archive_id",
    "plate_number": "plate",
    "printer_id": "printer_id",
}

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


def discover_paths(spec_paths: dict[str, Any]) -> dict[str, Any]:
    """Read our endpoints off an OpenAPI document.

    Returns the best candidate per role plus every runner-up, because a guess
    the operator cannot see is a guess they cannot correct.
    """
    found: dict[str, Any] = {}
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


def _as_list(data: Any) -> list[dict[str, Any]]:
    """Accept a bare list or any of the common envelope shapes."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "data", "queue", "archives", "printers"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def parse_archive(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _first(row, "id", "archive_id", "archiveId"),
        "name": _first(row, "name", "title", "filename", "file_name", "model_name"),
        "created_at": _first(row, "created_at", "createdAt", "created", "date"),
        "plates": _first(row, "plates", "plate_count", "plateCount"),
        "thumbnail": _first(row, "thumbnail", "thumbnail_url", "image", "cover"),
    }


def parse_printer(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _first(row, "id", "printer_id", "printerId"),
        "name": _first(row, "name", "printer_name", "device_name", "dev_name"),
        "model": _first(row, "model", "printer_model", "dev_model"),
        "status": _first(row, "status", "state", "print_status"),
        "online": _first(row, "online", "is_online", "connected"),
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

    async def list_queue(self) -> list[dict[str, Any]]:
        data = await self._call("GET", self.paths["queue"])
        return [parse_queue_item(row) for row in _as_list(data)]

    def build_queue_body(
        self,
        *,
        archive_id: int,
        plate_number: int | None,
        printer_id: int | None,
        print_options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {self.fields["archive_id"]: archive_id}
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
        archive_id: int,
        plate_number: int | None = None,
        printer_id: int | None = None,
        print_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = self.build_queue_body(
            archive_id=archive_id,
            plate_number=plate_number,
            printer_id=printer_id,
            print_options=print_options,
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
