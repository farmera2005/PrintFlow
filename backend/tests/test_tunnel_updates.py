"""Keeping cloudflared current.

The binary is fetched when the image is built, and Docker caches that layer —
so a shop that rebuilds PrintFlow every week can still be running the
cloudflared it downloaded the day it installed. Cloudflare keeps shipping
releases and eventually stops supporting old connectors, and the way that
turns up is a tunnel that will not connect one morning for no reason anybody
can see.

So PrintFlow can fetch one for itself, into the data volume where it survives
the next rebuild. Three things about that are worth pinning, and all three are
here: that versions compare as numbers rather than as text, that nothing
replaces a working binary until the replacement has been asked what it is, and
that the downloaded copy does not quietly outrank a newer one in a rebuilt
image.
"""

from __future__ import annotations

import stat

import httpx
import pytest

from app.services import tunnel

pytestmark = pytest.mark.asyncio


def _binary(path, version: str = "2026.10.3"):
    """A stand-in cloudflared that answers --version the way the real one does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'echo "cloudflared version {version} (built 2026-10-21-13:11 UTC)"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _mute(path):
    """A binary that runs but will not say what it is — a truncated download."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the config's data directory at a temporary one."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(tunnel, "_download_target", lambda: root / "bin" / "cloudflared")
    # The version cache is keyed by path and mtime, so it cannot leak between
    # tests — but it is cleared anyway, because a stale entry here would be
    # invisible and maddening.
    tunnel._version_cache.clear()
    return root


class TestReadingAVersion:
    def test_the_real_format_is_what_is_parsed(self):
        # Exactly what `cloudflared --version` prints today.
        assert (
            tunnel.parse_version(
                "cloudflared version 2026.10.3 (built 2026-10-21-13:11 UTC)"
            )
            == "2026.10.3"
        )

    def test_a_v_prefix_and_odd_spacing_still_read(self):
        assert tunnel.parse_version("cloudflared  version   v2024.8.3") == "2024.8.3"

    def test_something_that_is_not_a_version_is_none(self):
        assert tunnel.parse_version("") is None
        assert tunnel.parse_version("command not found") is None

    def test_versions_compare_as_numbers_not_as_text(self):
        """The one that matters: 2026.10.3 is newer than 2026.9.1, and a string
        comparison says the opposite."""
        assert tunnel.version_tuple("2026.10.3") > tunnel.version_tuple("2026.9.1")
        assert tunnel.version_tuple("2027.1.0") > tunnel.version_tuple("2026.12.9")

    def test_an_unreadable_version_sorts_lowest_rather_than_raising(self):
        # A binary that will not say what it is is a reason to prefer the
        # other one, not a reason to fail.
        assert tunnel.version_tuple(None) == (0,)
        assert tunnel.version_tuple("not-a-version") == (0,)
        assert tunnel.version_tuple("2026.10.3") > tunnel.version_tuple(None)

    def test_a_binary_is_asked_and_the_answer_remembered(self, tmp_path):
        tunnel._version_cache.clear()
        binary = _binary(tmp_path / "cloudflared", "2026.8.1")
        assert tunnel.installed_version(str(binary)) == "2026.8.1"
        # Asked once: the answer is keyed on the path and its mtime.
        assert tunnel._version_cache[(str(binary), binary.stat().st_mtime)] == "2026.8.1"

    def test_a_binary_that_will_not_answer_reads_as_unknown(self, tmp_path):
        tunnel._version_cache.clear()
        assert tunnel.installed_version(str(_mute(tmp_path / "mute"))) is None

    def test_a_path_that_is_not_there_reads_as_unknown(self, tmp_path):
        assert tunnel.installed_version(str(tmp_path / "nothing")) is None


class TestWhichOneRuns:
    def test_only_the_bundled_one(self, data_dir, monkeypatch, tmp_path):
        bundled = _binary(tmp_path / "image" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: str(bundled))
        assert tunnel.binary_path() == str(bundled)
        assert tunnel.available() is True

    def test_a_newer_download_wins(self, data_dir, monkeypatch, tmp_path):
        bundled = _binary(tmp_path / "image" / "cloudflared", "2026.8.1")
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: str(bundled))

        assert tunnel.binary_path() == str(data_dir / "bin" / "cloudflared")
        summary = tunnel.binary_summary()
        assert summary["version"] == "2026.10.3"
        assert summary["bundled_version"] == "2026.8.1"
        assert summary["using_downloaded"] is True

    def test_but_a_newer_image_takes_it_back(self, data_dir, monkeypatch, tmp_path):
        """After a rebuild the image can be ahead of what was downloaded months
        ago, and the update button must not have become a way of pinning an old
        version."""
        bundled = _binary(tmp_path / "image" / "cloudflared", "2027.1.1")
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: str(bundled))

        assert tunnel.binary_path() == str(bundled)
        summary = tunnel.binary_summary()
        assert summary["using_downloaded"] is False
        assert summary["downloaded_version"] == "2026.10.3"

    def test_a_download_is_used_when_the_image_carries_nothing(
        self, data_dir, monkeypatch
    ):
        """The offline-build case: the image shipped without cloudflared, and
        this is how a shop gets one at all."""
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)

        assert tunnel.binary_path() == str(data_dir / "bin" / "cloudflared")
        assert tunnel.available() is True

    def test_neither_is_not_an_error(self, data_dir, monkeypatch):
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        assert tunnel.binary_path() is None
        assert tunnel.binary_summary()["available"] is False


class TestAskingWhatIsLatest:
    async def test_the_tag_comes_from_where_the_redirect_points(self, monkeypatch):
        """Read from the redirect rather than the GitHub API, which is rate
        limited per address and would start refusing a shop behind a busy
        office NAT."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "HEAD"
            return httpx.Response(
                302,
                headers={
                    "location": "https://github.com/cloudflare/cloudflared/releases/tag/2026.10.3"
                },
            )

        _patch_http(monkeypatch, handler)
        assert await tunnel.latest_version() == "2026.10.3"

    async def test_a_v_prefixed_tag_is_read_the_same(self, monkeypatch):
        _patch_http(
            monkeypatch,
            lambda request: httpx.Response(
                302, headers={"location": "https://github.com/x/y/releases/tag/v2026.9.1"}
            ),
        )
        assert await tunnel.latest_version() == "2026.9.1"

    async def test_github_being_unreachable_is_an_answer_not_a_crash(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        _patch_http(monkeypatch, handler)
        assert await tunnel.latest_version() is None

    async def test_the_check_says_when_it_could_not_ask(self, data_dir, monkeypatch):
        """"No update" and "could not ask" must not look the same on screen."""
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(
            monkeypatch,
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )

        found = await tunnel.check_for_update()
        assert found["installed"] == "2026.10.3"
        assert found["latest"] is None
        assert found["update_available"] is None

    async def test_an_older_install_is_told_there_is_something_newer(
        self, data_dir, monkeypatch
    ):
        _binary(data_dir / "bin" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(
            monkeypatch,
            lambda request: httpx.Response(
                302, headers={"location": ".../releases/tag/2026.10.3"}
            ),
        )

        found = await tunnel.check_for_update()
        assert (found["installed"], found["latest"]) == ("2026.8.1", "2026.10.3")
        assert found["update_available"] is True

    async def test_a_current_install_is_told_so(self, data_dir, monkeypatch):
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(
            monkeypatch,
            lambda request: httpx.Response(
                302, headers={"location": ".../releases/tag/2026.10.3"}
            ),
        )
        assert (await tunnel.check_for_update())["update_available"] is False


def _patch_http(monkeypatch, handler):
    """Answer PrintFlow's outbound HTTP from a function instead of a network."""
    from app.integrations import base as base_module

    real = base_module.new_client

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(tunnel.base, "new_client", fake)


class TestInstalling:
    async def test_a_download_lands_in_the_data_volume_and_is_used(
        self, data_dir, monkeypatch
    ):
        """In the data volume rather than the image: the next rebuild replaces
        the image wholesale, and the container cannot write anywhere else."""
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(monkeypatch, _serves(b"#!/bin/sh\necho 'cloudflared version 2026.10.3'\n"))

        result = await tunnel.install_latest()

        assert result["installed"] == "2026.10.3"
        assert result["changed"] is True
        assert tunnel.binary_path() == str(data_dir / "bin" / "cloudflared")
        assert (data_dir / "bin" / "cloudflared").stat().st_mode & stat.S_IEXEC

    async def test_what_was_running_before_is_reported(self, data_dir, monkeypatch, tmp_path):
        bundled = _binary(tmp_path / "image" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: str(bundled))
        _patch_http(monkeypatch, _serves(b"#!/bin/sh\necho 'cloudflared version 2026.10.3'\n"))

        result = await tunnel.install_latest()
        assert (result["previous"], result["installed"]) == ("2026.8.1", "2026.10.3")

    async def test_downloading_the_same_version_changes_nothing(
        self, data_dir, monkeypatch
    ):
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        _patch_http(monkeypatch, _serves(b"#!/bin/sh\necho 'cloudflared version 2026.10.3'\n"))

        result = await tunnel.install_latest()
        assert result["changed"] is False

    async def test_a_binary_that_will_not_say_what_it_is_never_replaces_a_working_one(
        self, data_dir, monkeypatch
    ):
        """A truncated download written into place would turn "update the
        tunnel" into "lose the tunnel", on the machine least able to fix it."""
        _binary(data_dir / "bin" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(monkeypatch, _serves(b"#!/bin/sh\nexit 1\n"))

        with pytest.raises(tunnel.TunnelError, match="does not run on this machine"):
            await tunnel.install_latest()

        # Still the one that was working, and no debris beside it.
        assert tunnel.installed_version() == "2026.8.1"
        assert sorted(p.name for p in (data_dir / "bin").iterdir()) == ["cloudflared"]

    async def test_a_refused_download_says_so_and_leaves_things_alone(
        self, data_dir, monkeypatch
    ):
        _binary(data_dir / "bin" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(monkeypatch, lambda request: httpx.Response(503, content=b"nope"))

        with pytest.raises(tunnel.TunnelError, match="HTTP 503"):
            await tunnel.install_latest()
        assert tunnel.installed_version() == "2026.8.1"

    async def test_no_network_says_what_it_needs(self, data_dir, monkeypatch):
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(
            monkeypatch,
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        )
        with pytest.raises(tunnel.TunnelError, match="github.com"):
            await tunnel.install_latest()

    async def test_something_far_too_large_is_not_written_out(
        self, data_dir, monkeypatch
    ):
        monkeypatch.setattr(tunnel, "MAX_DOWNLOAD_BYTES", 32)
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(monkeypatch, _serves(b"x" * 4096))

        with pytest.raises(tunnel.TunnelError, match="far too large"):
            await tunnel.install_latest()
        assert not (data_dir / "bin" / "cloudflared").exists()

    def test_reverting_goes_back_to_the_image_s_copy(
        self, data_dir, monkeypatch, tmp_path
    ):
        bundled = _binary(tmp_path / "image" / "cloudflared", "2026.8.1")
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: str(bundled))
        assert tunnel.binary_summary()["using_downloaded"] is True

        assert tunnel.remove_downloaded() is True
        assert tunnel.binary_path() == str(bundled)
        # Nothing to reinstall: the bundled one was never touched.
        assert tunnel.installed_version() == "2026.8.1"

    def test_reverting_with_nothing_downloaded_is_not_an_error(self, data_dir):
        assert tunnel.remove_downloaded() is False

    def test_the_url_follows_the_machine_s_architecture(self, monkeypatch):
        monkeypatch.setattr(tunnel.platform, "machine", lambda: "aarch64")
        assert tunnel.arch_name() == "arm64"
        assert tunnel.download_url().endswith("cloudflared-linux-arm64")

        monkeypatch.setattr(tunnel.platform, "machine", lambda: "something-odd")
        # Guessing amd64 beats refusing to offer an update at all.
        assert tunnel.arch_name() == "amd64"


def _serves(payload: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    return handler


class TestOverTheWire:
    async def test_the_version_is_on_the_security_page_without_asking_github(
        self, db, signed_in, data_dir, monkeypatch
    ):
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)

        body = (await signed_in.get("/api/security")).json()
        binary = body["tunnel"]["binary"]
        assert binary["version"] == "2026.10.3"
        assert binary["using_downloaded"] is True

    async def test_the_check_reports_both_versions(
        self, db, signed_in, data_dir, monkeypatch
    ):
        _binary(data_dir / "bin" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(
            monkeypatch,
            lambda request: httpx.Response(
                302, headers={"location": ".../releases/tag/2026.10.3"}
            ),
        )

        found = (await signed_in.get("/api/security/tunnel/version")).json()
        assert (found["installed"], found["latest"]) == ("2026.8.1", "2026.10.3")
        assert found["update_available"] is True

    async def test_updating_installs_and_is_written_down(
        self, db, signed_in, data_dir, monkeypatch
    ):
        from sqlalchemy import select

        from app.models import AuditLog

        _binary(data_dir / "bin" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(monkeypatch, _serves(b"#!/bin/sh\necho 'cloudflared version 2026.10.3'\n"))

        response = await signed_in.post("/api/security/tunnel/update")
        assert response.status_code == 200, response.text
        assert response.json()["installed"] == "2026.10.3"

        rows = [
            (row.action, row.detail, row.actor)
            for row in (await db.execute(select(AuditLog))).scalars().all()
        ]
        assert ("cloudflared_updated", {"from": "2026.8.1", "to": "2026.10.3"}, "admin") in rows

    async def test_a_failed_update_is_a_bad_gateway_with_the_reason(
        self, db, signed_in, data_dir, monkeypatch
    ):
        _binary(data_dir / "bin" / "cloudflared", "2026.8.1")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: None)
        _patch_http(monkeypatch, lambda request: httpx.Response(500, content=b""))

        response = await signed_in.post("/api/security/tunnel/update")
        assert response.status_code == 502
        assert "HTTP 500" in response.json()["detail"]

    async def test_reverting_over_the_wire(self, db, signed_in, data_dir, monkeypatch, tmp_path):
        bundled = _binary(tmp_path / "image" / "cloudflared", "2026.8.1")
        _binary(data_dir / "bin" / "cloudflared", "2026.10.3")
        monkeypatch.setattr(tunnel, "bundled_path", lambda: str(bundled))

        response = await signed_in.post("/api/security/tunnel/binary/revert")
        assert response.status_code == 200
        assert response.json()["removed"] is True
        assert response.json()["binary"]["version"] == "2026.8.1"

    async def test_all_of_it_takes_a_signed_in_user(self, db, client):
        assert (await client.get("/api/security/tunnel/version")).status_code == 401
        assert (await client.post("/api/security/tunnel/update")).status_code == 401
        assert (await client.post("/api/security/tunnel/binary/revert")).status_code == 401
