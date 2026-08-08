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
from .base import IntegrationError, new_client, request

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
        self.paths = {**DEFAULT_PATHS, **(payload.get("paths") or {})}
        self.fields = {**DEFAULT_FIELDS, **(payload.get("fields") or {})}
        self.auth_header = str(payload.get("auth_header") or DEFAULT_AUTH_HEADER)

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
        async with new_client(base_url=self.base_url) as client:
            response = await request(
                client,
                method,
                path,
                provider=PROVIDER_BAMBUDDY,
                headers=self._headers(),
                retries=retries,
                **kwargs,
            )
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
            except IntegrationError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
                info = data.get("info") or {}
                return {
                    "path": path,
                    "title": info.get("title"),
                    "version": info.get("version"),
                    "openapi": data.get("openapi") or data.get("swagger"),
                    "operation_count": sum(
                        len(v) for v in (data.get("paths") or {}).values() if isinstance(v, dict)
                    ),
                }
        raise IntegrationError(
            PROVIDER_BAMBUDDY,
            "Could not find an OpenAPI document on this Bambuddy instance. "
            "Set the path under Settings → Bambuddy → Advanced. Tried: "
            + "; ".join(errors),
        )

    async def list_printers(self) -> list[dict[str, Any]]:
        data = await self._call("GET", self.paths["printers"])
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
