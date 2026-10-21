"""Cloudflare Tunnel (cloudflared) — a public HTTPS address for OAuth callbacks.

Etsy and Intuit both want an https redirect URI, and Intuit will not accept a
private address for a production app. A Cloudflare Tunnel gives PrintFlow a real
public hostname with a real certificate while opening no inbound ports on the
NAS: cloudflared makes outbound connections only.

The tunnel is configured in the setup wizard like everything else — the token is
encrypted at rest — and the app supervises `cloudflared` as a child process so
there is no second container to wire up.

Two modes:

- **named**  the normal one. Create a tunnel in the Cloudflare Zero Trust
  dashboard, point a public hostname at `http://localhost:8000`, and paste the
  connector token here. The hostname is stable, which matters because it ends up
  registered as an OAuth redirect URI.
- **quick**  a throwaway `*.trycloudflare.com` address with no account needed.
  Useful to try the OAuth round-trip; useless for anything lasting, because the
  hostname changes every restart and the redirect URI would have to be
  re-registered each time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..integrations import base
from ..services import credentials

log = logging.getLogger("printflow.tunnel")

# Kept out of models.PROVIDERS on purpose: this is infrastructure, not one of
# the four order-flow platforms, and it must not appear as a fifth integration
# on the board's health banners.
PROVIDER_TUNNEL = "cloudflare_tunnel"

MODE_OFF = "off"
MODE_NAMED = "named"
MODE_QUICK = "quick"
MODES = (MODE_OFF, MODE_NAMED, MODE_QUICK)

BINARY_CANDIDATES = ("cloudflared", "/usr/local/bin/cloudflared", "/usr/bin/cloudflared")

LOG_BUFFER_LINES = 300
RESTART_BACKOFF_SECONDS = (2, 5, 10, 20, 40, 60)

_QUICK_HOSTNAME = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)", re.IGNORECASE)
_CONNECTION_REGISTERED = re.compile(
    r"Registered tunnel connection|Connection [0-9a-f-]+ registered", re.IGNORECASE
)
_CONNECTION_LOST = re.compile(r"Lost connection|Unregistered tunnel connection", re.IGNORECASE)
_FATAL = re.compile(
    r"failed to (?:connect|run|start)|Couldn't start tunnel|invalid tunnel credentials|"
    r"token is invalid|Unauthorized",
    re.IGNORECASE,
)


class TunnelError(RuntimeError):
    pass


def _close_transport(process: "asyncio.subprocess.Process | None") -> None:
    """Release an exited child's pipes.

    asyncio only tears the subprocess transport down when it is garbage
    collected, which for a supervisor that restarts its child repeatedly means
    a slow drip of open pipes — and a noisy "Event loop is closed" on the way
    out. Closing it explicitly is the standard workaround.
    """
    if process is None:
        return
    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # pragma: no cover - never let cleanup raise
        pass


async def _cancel(task: asyncio.Task | None) -> None:
    """Cancel and await, so nothing is left pending when the loop closes."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: B014 - never propagate
        pass


# --------------------------------------------------------------------------
# Binary discovery
# --------------------------------------------------------------------------


def bundled_path() -> str | None:
    """The cloudflared that came in the image, if the build managed to fetch one."""
    for candidate in BINARY_CANDIDATES:
        found = shutil.which(candidate) if "/" not in candidate else candidate
        if found and os.path.isfile(found) and os.access(found, os.X_OK):
            return found
    return None


def updated_path() -> str | None:
    """The cloudflared PrintFlow downloaded for itself, if there is one.

    In the data volume rather than next to the bundled one, for two reasons
    that both matter on a NAS: the image's filesystem is replaced wholesale by
    the next `docker compose up --build`, and the container does not run as
    root so there is nowhere else it may write.
    """
    path = _download_target()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _download_target() -> Path:
    return get_config().data_dir / "bin" / "cloudflared"


def binary_path() -> str | None:
    """Which cloudflared to run: whichever of the two is newer.

    Not simply "the downloaded one wins". A shop that updates in the app and
    then rebuilds the image would otherwise keep running the copy it
    downloaded months ago while a newer one sat in the image unused — the
    update button would have quietly become a way of pinning an old version.
    """
    bundled, updated = bundled_path(), updated_path()
    if updated is None:
        return bundled
    if bundled is None:
        return updated
    if version_tuple(installed_version(updated)) >= version_tuple(
        installed_version(bundled)
    ):
        return updated
    return bundled


def available() -> bool:
    return binary_path() is not None


# What `cloudflared --version` prints:
#   cloudflared version 2026.10.3 (built 2026-10-21-13:11 UTC)
_VERSION = re.compile(r"\bversion\s+v?(\d[\w.\-]*)", re.IGNORECASE)

# Asking a binary its version costs a fork. The answer only changes when
# PrintFlow replaces the file, so it is remembered against the path and its
# modification time — which is also what makes a swap visible immediately.
_version_cache: dict[tuple[str, float], str | None] = {}


def parse_version(output: str) -> str | None:
    match = _VERSION.search(output or "")
    return match.group(1) if match else None


def version_tuple(version: str | None) -> tuple[int, ...]:
    """A version as something comparable.

    Cloudflared numbers releases by calendar — 2026.10.3 — so plain integer
    parts sort correctly. Anything unparseable sorts lowest rather than
    raising: an unreadable version is a reason to prefer the other binary, not
    a reason to fail.
    """
    if not version:
        return (0,)
    parts: list[int] = []
    for piece in version.split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts) or (0,)


def installed_version(path: str | None = None) -> str | None:
    """What this cloudflared says it is, or None if it will not say.

    Run with a short timeout and the result cached: `--version` returns
    immediately on a working binary, so anything slower is a binary that is
    not going to work anyway and must not hold up a settings page.
    """
    target = path or binary_path()
    if not target:
        return None
    try:
        stamp = os.path.getmtime(target)
    except OSError:
        return None
    key = (target, stamp)
    if key in _version_cache:
        return _version_cache[key]
    try:
        result = subprocess.run(  # noqa: S603 - a path this module chose
            [target, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        found = parse_version(f"{result.stdout}\n{result.stderr}")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not ask %s for its version: %s", target, exc)
        found = None
    _version_cache[key] = found
    return found


# --------------------------------------------------------------------------
# Keeping cloudflared current
#
# The binary is fetched when the image is built, and that build layer is cached
# on the shop's own machine — so a `docker compose up --build` a year later
# rebuilds PrintFlow and leaves cloudflared exactly as it was. Cloudflare keeps
# shipping releases and eventually stops supporting old connectors, and the
# first sign of that is a tunnel that will not connect on a Tuesday morning.
#
# So PrintFlow can fetch one for itself, into the data volume, where it
# survives the next rebuild and needs no Docker knowledge to apply.
# --------------------------------------------------------------------------

# Where releases come from. The same place the Dockerfile uses, so an install
# that could be built can also be updated.
RELEASES = "https://github.com/cloudflare/cloudflared/releases"
LATEST_ASSET = RELEASES + "/latest/download/cloudflared-linux-{arch}"
LATEST_PAGE = RELEASES + "/latest"

# What the kernel calls this machine, against what Cloudflare calls the build.
ARCH_NAMES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
}

# A download is tens of megabytes over whatever a shop has, so it gets room —
# but a bounded amount of it, because somebody is watching a button.
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
CHECK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
# Anything larger is not cloudflared, and is not going to be written to the
# data volume to find out.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


def arch_name() -> str:
    return ARCH_NAMES.get(platform.machine().lower(), "amd64")


def download_url(arch: str | None = None) -> str:
    return LATEST_ASSET.format(arch=arch or arch_name())


async def latest_version() -> str | None:
    """The newest release Cloudflare has published, without downloading it.

    Read from where `/releases/latest` redirects to — `/releases/tag/2026.10.3`
    — rather than from the GitHub API, which is rate-limited per address and
    would start refusing a shop behind a busy office NAT. None when GitHub
    cannot be reached, which is a thing to report rather than an error: the
    update itself does not depend on this answer.
    """
    try:
        async with base.new_client(timeout=CHECK_TIMEOUT, follow_redirects=False) as client:
            response = await client.head(LATEST_PAGE)
        location = response.headers.get("location", "")
    except httpx.HTTPError as exc:
        log.info("Could not reach GitHub for the cloudflared version: %s", exc)
        return None
    tag = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
    return tag.lstrip("v") or None


async def check_for_update() -> dict[str, Any]:
    """What is running, what is available, and whether they differ."""
    running = installed_version()
    latest = await latest_version()
    return {
        "installed": running,
        "latest": latest,
        "arch": arch_name(),
        # Unknown rather than False when GitHub could not be reached: "no
        # update" and "could not ask" must not look the same on a screen.
        "update_available": (
            None
            if latest is None or running is None
            else version_tuple(latest) > version_tuple(running)
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def install_latest() -> dict[str, Any]:
    """Download the current cloudflared and put it where PrintFlow will run it.

    Nothing replaces the running binary until the downloaded one has been
    asked what it is and answered — a truncated download or a wrong-
    architecture build that got written into place would turn "update the
    tunnel" into "lose the tunnel", on the machine whose owner is least able
    to go and fix it.
    """
    before = installed_version()
    target = _download_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f"{target.name}.downloading")

    url = download_url()
    written = 0
    try:
        async with base.new_client(timeout=DOWNLOAD_TIMEOUT) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise TunnelError(
                        f"Cloudflare's download returned HTTP {response.status_code}. "
                        "Try again later, or update the image instead."
                    )
                with open(staged, "wb") as out:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise TunnelError(
                                "That download is far too large to be cloudflared."
                            )
                        out.write(chunk)
    except httpx.HTTPError as exc:
        staged.unlink(missing_ok=True)
        raise TunnelError(
            f"Could not download cloudflared: {exc}. This machine needs to reach "
            "github.com to fetch it."
        ) from exc
    except TunnelError:
        staged.unlink(missing_ok=True)
        raise

    staged.chmod(0o755)
    fetched = installed_version(str(staged))
    if not fetched:
        staged.unlink(missing_ok=True)
        still = f"cloudflared {before}" if before else "the bundled cloudflared"
        raise TunnelError(
            "What was downloaded does not run on this machine — it did not even "
            f"report a version. Nothing was replaced; {still} is still in use."
        )

    # Replaced in one step. A half-written file at this path is a tunnel that
    # cannot start, and os.replace is the only way to avoid one.
    os.replace(staged, target)
    log.warning("cloudflared updated: %s -> %s", before or "unknown", fetched)
    return {
        "installed": fetched,
        "previous": before,
        "changed": fetched != before,
        "path": str(target),
    }


def binary_summary() -> dict[str, Any]:
    """Which cloudflared is in use, and what else is on the machine.

    Both are reported rather than only the winner, because "the one I
    downloaded is not the one running" is otherwise a silent surprise — and
    after an image rebuild it is the correct outcome.
    """
    chosen = binary_path()
    downloaded = updated_path()
    return {
        "available": chosen is not None,
        "path": chosen,
        "version": installed_version(chosen) if chosen else None,
        "bundled_version": installed_version(bundled_path()) if bundled_path() else None,
        "downloaded_version": (
            installed_version(downloaded) if downloaded else None
        ),
        "using_downloaded": chosen is not None and chosen == downloaded,
        "arch": arch_name(),
    }


def remove_downloaded() -> bool:
    """Go back to the cloudflared that came with the image.

    The way out of a downloaded binary that turns out to be wrong for this
    machine in some way `--version` could not detect. Nothing to reinstall:
    the bundled one was never touched.
    """
    target = _download_target()
    if not target.exists():
        return False
    target.unlink()
    return True


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def build_command(
    mode: str, *, binary: str, local_port: int, metrics_port: int = 0
) -> list[str]:
    """Build the cloudflared argv.

    The connector token is deliberately *not* an argument — it goes in the
    environment instead, so it never shows up in `ps` output.
    """
    base = [binary, "tunnel", "--no-autoupdate", "--metrics", f"127.0.0.1:{metrics_port}"]
    if mode == MODE_NAMED:
        return [*base, "run"]
    if mode == MODE_QUICK:
        return [*base, "--url", f"http://127.0.0.1:{local_port}"]
    raise TunnelError(f"Cannot build a command for mode {mode!r}")


def build_env(mode: str, token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TUNNEL_TOKEN", None)
    if mode == MODE_NAMED:
        if not token:
            raise TunnelError("A Cloudflare tunnel token is required for a named tunnel.")
        env["TUNNEL_TOKEN"] = token
    return env


def redact(line: str, token: str | None) -> str:
    if token and len(token) > 8:
        line = line.replace(token, "…redacted…")
    return line


def parse_log_line(line: str) -> dict[str, Any]:
    """Extract what the UI needs from a cloudflared log line."""
    result: dict[str, Any] = {}
    match = _QUICK_HOSTNAME.search(line)
    if match:
        result["hostname"] = match.group(1)
    if _CONNECTION_REGISTERED.search(line):
        result["connection_registered"] = True
    if _CONNECTION_LOST.search(line):
        result["connection_lost"] = True
    if _FATAL.search(line):
        result["error"] = line.strip()[:500]
    return result


def public_url(hostname: str | None) -> str | None:
    if not hostname:
        return None
    # removeprefix, not lstrip: lstrip takes a character set and would eat the
    # leading letters of a hostname like "printflow.example.com".
    host = hostname.strip().removeprefix("https://").removeprefix("http://").strip("/")
    return f"https://{host}" if host else None


# --------------------------------------------------------------------------
# Stored configuration
# --------------------------------------------------------------------------


async def load_config(session: AsyncSession) -> dict[str, Any]:
    payload = await credentials.load(session, PROVIDER_TUNNEL)
    return {
        "mode": payload.get("mode", MODE_OFF),
        "token": payload.get("token"),
        "hostname": payload.get("hostname"),
        "enabled": bool(payload.get("enabled", False)),
    }


async def save_config(
    session: AsyncSession,
    *,
    mode: str,
    token: str | None,
    hostname: str | None,
    enabled: bool,
) -> dict[str, Any]:
    if mode not in MODES:
        raise TunnelError(f"mode must be one of {', '.join(MODES)}")
    existing = await load_config(session)
    # A blank token on an edit means "keep the stored one".
    resolved_token = (token or "").strip() or existing.get("token")
    if mode == MODE_NAMED and enabled and not resolved_token:
        raise TunnelError(
            "Paste the connector token from your Cloudflare Zero Trust tunnel."
        )
    payload = {
        "mode": mode,
        "token": resolved_token,
        "hostname": (hostname or "").strip() or (
            existing.get("hostname") if mode == existing.get("mode") else None
        ),
        "enabled": enabled and mode != MODE_OFF,
    }
    await credentials.save(session, PROVIDER_TUNNEL, payload, mark_connected=False)
    return payload


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    """Config as the UI may see it — never the token itself."""
    token = config.get("token")
    return {
        "mode": config.get("mode", MODE_OFF),
        "enabled": bool(config.get("enabled")),
        "hostname": config.get("hostname"),
        "has_token": bool(token),
        "token_hint": f"…{token[-6:]}" if isinstance(token, str) and len(token) > 6 else None,
    }


# --------------------------------------------------------------------------
# Process supervision
# --------------------------------------------------------------------------


class TunnelSupervisor:
    """Runs cloudflared as a child process and keeps it running."""

    def __init__(self, local_port: int) -> None:
        self.local_port = local_port
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._logs: deque[str] = deque(maxlen=LOG_BUFFER_LINES)
        self._token: str | None = None

        self.mode: str = MODE_OFF
        self.enabled: bool = False
        self.hostname: str | None = None
        self.connections: int = 0
        self.last_error: str | None = None
        self.restarts: int = 0

    # -- state ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def status(self) -> dict[str, Any]:
        return {
            "binary_available": available(),
            "running": self.running,
            "mode": self.mode,
            "enabled": self.enabled,
            "hostname": self.hostname,
            "public_url": public_url(self.hostname),
            "connections": self.connections,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "pid": self._process.pid if self.running and self._process else None,
        }

    def logs(self, limit: int = 100) -> list[str]:
        return list(self._logs)[-limit:]

    # -- lifecycle ------------------------------------------------------

    async def apply(self, config: dict[str, Any]) -> None:
        """Bring the process in line with a (possibly new) configuration."""
        async with self._lock:
            await self._stop_locked()
            self.mode = config.get("mode", MODE_OFF)
            self.enabled = bool(config.get("enabled"))
            self._token = config.get("token")
            # A named tunnel's hostname is operator-supplied; a quick tunnel
            # tells us its own once it connects.
            self.hostname = config.get("hostname") if self.mode == MODE_NAMED else None
            self.connections = 0
            self.last_error = None
            self.restarts = 0
            if self.enabled and self.mode != MODE_OFF:
                await self._start_locked()

    async def start_from_db(self, session: AsyncSession) -> None:
        await self.apply(await load_config(session))

    async def stop(self) -> None:
        async with self._lock:
            self.enabled = False
            await self._stop_locked()

    async def restart(self) -> None:
        async with self._lock:
            await self._stop_locked()
            if self.enabled and self.mode != MODE_OFF:
                await self._start_locked()

    async def _start_locked(self) -> None:
        # A restart arrives here from the watchdog, which cannot call
        # _stop_locked (that would cancel the task doing the calling). Retire
        # the previous reader explicitly, or it keeps the dead process's pipes
        # — and its transport — alive.
        await _cancel(self._reader)
        self._reader = None
        self._process = None

        binary = binary_path()
        if binary is None:
            self.last_error = (
                "cloudflared is not installed in this container. Rebuild the image "
                "with network access, or run cloudflared alongside PrintFlow yourself."
            )
            log.error(self.last_error)
            return
        try:
            command = build_command(
                self.mode, binary=binary, local_port=self.local_port
            )
            env = build_env(self.mode, self._token)
        except TunnelError as exc:
            self.last_error = str(exc)
            log.error("Tunnel not started: %s", exc)
            return

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except OSError as exc:
            self.last_error = f"Could not start cloudflared: {exc}"
            log.error(self.last_error)
            self._process = None
            return

        self._logs.append(f"$ {' '.join(command)}")
        self._reader = asyncio.create_task(self._pump_logs(), name="printflow-tunnel-logs")
        self._watchdog = asyncio.create_task(self._watch(), name="printflow-tunnel-watch")
        log.info("Cloudflare tunnel started (%s mode, pid %s)", self.mode, self._process.pid)

    async def _stop_locked(self) -> None:
        process, reader, watchdog = self._process, self._reader, self._watchdog
        self._process, self._reader, self._watchdog = None, None, None

        await _cancel(watchdog)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        await _cancel(reader)
        _close_transport(process)
        self.connections = 0

    # -- internals ------------------------------------------------------

    async def _pump_logs(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    return
                line = redact(raw.decode("utf-8", "replace").rstrip(), self._token)
                self._logs.append(line)
                parsed = parse_log_line(line)
                if "hostname" in parsed:
                    self.hostname = parsed["hostname"]
                    log.info("Cloudflare quick tunnel hostname: %s", self.hostname)
                if parsed.get("connection_registered"):
                    self.connections += 1
                    self.last_error = None
                if parsed.get("connection_lost"):
                    self.connections = max(0, self.connections - 1)
                if "error" in parsed:
                    self.last_error = parsed["error"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Tunnel log reader stopped: %s", exc)

    async def _watch(self) -> None:
        """Restart cloudflared if it dies while it is supposed to be up."""
        attempt = 0
        while True:
            process = self._process
            if process is None:
                return
            code = await process.wait()
            _close_transport(process)
            if not self.enabled:
                return
            delay = RESTART_BACKOFF_SECONDS[min(attempt, len(RESTART_BACKOFF_SECONDS) - 1)]
            self.last_error = f"cloudflared exited with code {code}; retrying in {delay}s"
            log.warning(self.last_error)
            self._logs.append(f"-- {self.last_error} --")
            await asyncio.sleep(delay)
            attempt += 1
            self.restarts += 1
            async with self._lock:
                if not self.enabled:
                    return
                await self._start_locked()
            return  # the freshly started process gets its own watchdog
