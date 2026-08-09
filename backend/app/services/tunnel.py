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
import re
import shutil
from collections import deque
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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


def binary_path() -> str | None:
    for candidate in BINARY_CANDIDATES:
        found = shutil.which(candidate) if "/" not in candidate else candidate
        if found and os.path.isfile(found) and os.access(found, os.X_OK):
            return found
    return None


def available() -> bool:
    return binary_path() is not None


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
