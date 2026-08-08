"""Runtime entry point: serves HTTP and HTTPS from one process.

Both listeners share the same FastAPI app. HTTP stays available so an operator
who has just changed the certificate — or typed the wrong hostname — can always
get back in; HTTPS is what the OAuth redirect URIs point at.

The HTTPS listener is supervised rather than configured once, so generating a
new certificate in the setup wizard takes effect immediately instead of
requiring a container restart mid-setup.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .config import get_config
from .db import session_scope
from .services import tls

log = logging.getLogger("printflow.server")


class TlsSupervisor:
    """Owns the HTTPS uvicorn server and can swap its certificate at runtime."""

    def __init__(self, app, host: str = "0.0.0.0", port: int | None = None) -> None:
        config = get_config()
        self.app = app
        self.host = host
        self.port = port if port is not None else config.https_port
        self.tls_dir = config.tls_dir
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.fingerprint: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.running:
            return
        try:
            async with session_scope() as session:
                row = await tls.ensure_certificate(session)
                await session.commit()
                cert_pem = row.cert_pem
                key_pem = tls.private_key_of(row)
                self.fingerprint = row.fingerprint_sha256
            cert_path, key_path = tls.materialize(cert_pem, key_pem, self.tls_dir)
        except Exception as exc:
            # A broken certificate must never take the whole app down; HTTP keeps
            # serving so the operator can fix it from the UI.
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("HTTPS listener could not start: %s", self.last_error)
            return

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            # The HTTP listener owns the app lifespan; running it twice in one
            # process would start the scheduler twice.
            lifespan="off",
            proxy_headers=True,
            forwarded_allow_ips="*",
            # Browsers hold keep-alive sockets open, so a graceful shutdown would
            # otherwise stall for the full timeout on every certificate swap.
            timeout_graceful_shutdown=3,
            log_level="info",
        )
        server = uvicorn.Server(config)
        # uvicorn installs signal handlers by default, which would fight with the
        # HTTP server's for control of shutdown.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._server = server
        self._task = asyncio.create_task(server.serve(), name="printflow-https")
        self.last_error = None
        log.info("HTTPS listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        server, task = self._server, self._task
        self._server, self._task = None, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("HTTPS listener stopped with an error: %s", exc)

    async def reload(self) -> None:
        """Restart the listener so a newly saved certificate takes effect."""
        async with self._lock:
            await self._stop_locked()
            # Give the OS a moment to release the port before rebinding.
            await asyncio.sleep(0.25)
            await self._start_locked()
        if self.last_error:
            raise RuntimeError(self.last_error)

    def schedule_reload(self, delay: float = 0.75) -> None:
        """Reload shortly, off the request path.

        The request asking for a new certificate usually arrives *over* the
        listener being replaced, so reloading inline would drop the connection
        before the response was written and the operator would see a network
        error instead of a confirmation.
        """

        async def later() -> None:
            await asyncio.sleep(delay)
            try:
                await self.reload()
            except Exception as exc:  # surfaced through status(), not raised
                log.error("Deferred HTTPS reload failed: %s", exc)

        asyncio.create_task(later(), name="printflow-https-reload")

    def status(self) -> dict[str, object]:
        return {
            "running": self.running,
            "port": self.port,
            "fingerprint_sha256": self.fingerprint,
            "last_error": self.last_error,
        }


async def serve() -> None:
    from .main import app

    config = get_config()
    supervisor = TlsSupervisor(app)
    # main.py's lifespan starts and stops the supervisor, so the HTTPS listener
    # comes up only once the app is actually ready to serve.
    app.state.tls_supervisor = supervisor

    http = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=config.http_port,
            proxy_headers=True,
            forwarded_allow_ips="*",
            log_level="info",
        )
    )
    log.info(
        "PrintFlow starting: HTTP on %s, HTTPS on %s, data dir %s, secret key from %s",
        config.http_port,
        config.https_port,
        config.data_dir,
        config.secret_key_source,
    )
    await http.serve()


def main() -> None:
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
