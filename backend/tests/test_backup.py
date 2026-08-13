"""One file that is the whole shop, and the way back from it.

A self-hosted shop has no operations team. It has one machine in a cupboard,
and the day that machine dies is the day it finds out whether anybody ever took
a backup. So the things worth testing hardest are the ones that would make a
restore fail on exactly that day:

* both halves in one file — the database *and* the key its credentials are
  encrypted with, because either alone restores to nothing useful;
* money that survives the round trip as the number it was, not as a float;
* a failed restore leaving the shop that was there intact;
* a backup from a newer PrintFlow refused rather than silently truncated.
"""

from __future__ import annotations

import json
import tarfile
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import PROVIDER_ETSY, Order, Product, User
from app.services import backup, credentials
from app.services.backup import BackupError

pytestmark = pytest.mark.asyncio


async def _shop(db, *, number: str = "9001") -> Order:
    """A shop with something in it worth losing."""
    product = Product(sku=f"SKU-{number}", name="Dragon egg", fulfillment="printed")
    db.add(product)
    order = Order(
        etsy_receipt_id=int(number),
        order_number=number,
        buyer_name="Dana Buyer",
        status="shipped",
        # The figure that must come back as itself.
        label_cost=Decimal("7.41"),
        label_currency="USD",
        revenue=Decimal("42.1900"),
        tracking_number="9400111899223",
        carrier_code="usps",
        ship_to={"name": "Dana Buyer", "city": "Kilnford"},
        label_pdf=b"%PDF-1.4 not really a pdf\x00\xff",
    )
    db.add(order)
    await credentials.save(
        db, PROVIDER_ETSY, {"keystring": "abc123", "shop_id": 7, "shop_name": "Kilnworks"}
    )
    await db.commit()
    return order


@pytest.fixture
def data_dir(tmp_path) -> Path:
    """A stand-in for /data, holding the things a restore has to carry."""
    root = tmp_path / "data"
    (root / "tls").mkdir(parents=True)
    (root / "secret_key").write_text("the-key-everything-is-encrypted-with")
    (root / "tls" / "cert.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\n")
    (root / ".write-probe").write_text("")
    (root / "cloudflared.pid").write_text("1234")
    return root


# --------------------------------------------------------------------------
# What goes in the file
# --------------------------------------------------------------------------


class TestWhatIsCarried:
    async def test_the_database_and_the_key_travel_together(self, db, data_dir):
        # Either alone is useless: a database with no key restores to a shop
        # that cannot talk to Etsy, and a key with no database restores to
        # nothing at all. That is the mistake a pg_dump cron job makes.
        await _shop(db)
        path, manifest = await backup.create(db, data_dir=data_dir)
        try:
            with tarfile.open(path) as archive:
                names = set(archive.getnames())
        finally:
            path.unlink(missing_ok=True)

        assert "database.json" in names
        assert "data/secret_key" in names
        assert "data/tls/cert.pem" in names
        assert manifest["tables"]["orders"] == 1

    async def test_this_containers_own_scratch_is_not_the_shops(self, db, data_dir):
        path, manifest = await backup.create(db, data_dir=data_dir)
        path.unlink(missing_ok=True)
        assert ".write-probe" not in manifest["data_files"]
        assert "cloudflared.pid" not in manifest["data_files"]

    async def test_the_manifest_says_what_it_is(self, db, data_dir):
        await _shop(db)
        path, manifest = await backup.create(db, data_dir=data_dir)
        path.unlink(missing_ok=True)
        assert manifest["format"] == backup.FORMAT_VERSION
        assert manifest["contains_secrets"] is True
        assert manifest["created_at"]


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


class TestRoundTrip:
    async def test_the_shop_comes_back(self, db, data_dir, tmp_path):
        order = await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)

        # Now lose everything.
        await db.execute(Order.__table__.delete())
        await db.execute(Product.__table__.delete())
        await db.commit()
        assert (await db.execute(select(Order))).scalars().all() == []

        result = await backup.restore(db, raw, data_dir=tmp_path / "restored")
        await db.commit()

        found = (await db.execute(select(Order))).scalars().one()
        assert found.order_number == order.order_number
        assert found.buyer_name == "Dana Buyer"
        assert result["rows"] >= 2

    async def test_money_comes_back_as_the_number_it_was(self, db, data_dir, tmp_path):
        # Through a JSON float, 7.41 returns as 7.409999999999999. People
        # reconcile these against a shipping bill.
        await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        await db.execute(Order.__table__.delete())
        await db.commit()

        await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        found = (await db.execute(select(Order))).scalars().one()
        assert found.label_cost == Decimal("7.41")
        assert found.revenue == Decimal("42.1900")

    async def test_the_label_pdf_survives_being_bytes(self, db, data_dir, tmp_path):
        await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        await db.execute(Order.__table__.delete())
        await db.commit()

        await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        found = (await db.execute(select(Order))).scalars().one()
        assert found.label_pdf == b"%PDF-1.4 not really a pdf\x00\xff"

    async def test_the_json_columns_come_back_as_objects(self, db, data_dir, tmp_path):
        await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        await db.execute(Order.__table__.delete())
        await db.commit()

        await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        found = (await db.execute(select(Order))).scalars().one()
        assert found.ship_to == {"name": "Dana Buyer", "city": "Kilnford"}

    async def test_the_credentials_are_readable_afterwards(self, db, data_dir, tmp_path):
        # The whole point of carrying the key: encrypted rows that cannot be
        # decrypted are a restore that looks fine and works for nothing.
        await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        await db.execute(User.__table__.delete())
        await db.commit()

        await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        stored = await credentials.load(db, PROVIDER_ETSY)
        assert stored["shop_name"] == "Kilnworks"

    async def test_the_key_lands_where_the_shop_looks_for_it(self, db, data_dir, tmp_path):
        await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)

        target = tmp_path / "fresh"
        await backup.restore(db, raw, data_dir=target)
        await db.commit()
        assert (target / "secret_key").read_text() == (
            "the-key-everything-is-encrypted-with"
        )
        assert (target / "tls" / "cert.pem").exists()

    async def test_restoring_replaces_rather_than_adds(self, db, data_dir, tmp_path):
        await _shop(db, number="9001")
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        # Something that happened after the backup was taken.
        await _shop(db, number="9002")

        await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        numbers = [
            row.order_number for row in (await db.execute(select(Order))).scalars().all()
        ]
        assert numbers == ["9001"]


# --------------------------------------------------------------------------
# The passphrase
# --------------------------------------------------------------------------


class TestPassphrase:
    async def test_an_encrypted_backup_is_not_readable_as_it_stands(self, db, data_dir):
        await _shop(db)
        path, manifest = await backup.create(db, passphrase="correct horse", data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)

        assert manifest["encrypted"] is True
        assert raw.startswith(backup.MAGIC)
        # The buyer's name is in there somewhere, but not like this.
        assert b"Dana Buyer" not in raw
        with pytest.raises(BackupError):
            backup.read_archive(raw)

    async def test_and_is_with_the_passphrase(self, db, data_dir, tmp_path):
        await _shop(db)
        path, _ = await backup.create(db, passphrase="correct horse", data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        await db.execute(Order.__table__.delete())
        await db.commit()

        await backup.restore(db, raw, passphrase="correct horse", data_dir=tmp_path / "r")
        await db.commit()
        assert (await db.execute(select(Order))).scalars().one().buyer_name == "Dana Buyer"

    async def test_the_wrong_passphrase_says_so_and_says_it_is_final(self, db, data_dir):
        await _shop(db)
        path, _ = await backup.create(db, passphrase="right", data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        with pytest.raises(BackupError, match="does not open"):
            backup.read_archive(raw, "wrong")

    async def test_a_passphrase_on_a_plain_backup_is_explained_not_ignored(
        self, db, data_dir
    ):
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        with pytest.raises(BackupError, match="not encrypted"):
            backup.read_archive(raw, "unnecessary")


# --------------------------------------------------------------------------
# Files that are not backups
# --------------------------------------------------------------------------


class TestRefusals:
    def test_something_that_is_not_an_archive(self):
        with pytest.raises(BackupError, match="not a PrintFlow backup"):
            backup.read_archive(b"just some bytes")

    def test_an_archive_with_no_manifest(self):
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("something.txt")
            info.size = 2
            archive.addfile(info, BytesIO(b"hi"))
        with pytest.raises(BackupError, match="no PrintFlow manifest"):
            backup.read_archive(buffer.getvalue())

    def test_a_backup_from_a_newer_printflow_is_refused(self):
        # Its extra columns would be dropped on the way in, and the operator
        # would never know which ones.
        with pytest.raises(BackupError, match="newer PrintFlow"):
            backup.check({"format": 1, "alembic_revision": "0099_from_the_future"})

    def test_a_newer_archive_format_is_refused(self):
        with pytest.raises(BackupError, match="format"):
            backup.check({"format": backup.FORMAT_VERSION + 1})

    def test_an_older_backup_is_allowed(self):
        # Migrations here only add columns, so old rows load and the new
        # columns take their defaults.
        revision = sorted(backup.known_revisions())[0]
        backup.check({"format": 1, "alembic_revision": revision})

    def test_a_database_built_without_migrations_is_not_a_reason_to_refuse(self):
        backup.check({"format": 1, "alembic_revision": None})

    def test_a_member_that_tries_to_escape_the_data_directory(self, tmp_path):
        # The archive is one PrintFlow wrote, but it arrives as an upload.
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, payload in (
                ("manifest.json", json.dumps({"format": 1}).encode()),
                ("database.json", b"{}"),
                ("data/../../escaped", b"nope"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, BytesIO(payload))
        _, _, files = backup.read_archive(buffer.getvalue())
        assert files == {}


# --------------------------------------------------------------------------
# A restore that fails
# --------------------------------------------------------------------------


class TestFailedRestore:
    async def test_a_refused_file_leaves_the_shop_alone(self, db, data_dir, tmp_path):
        # This matters most precisely when somebody is restoring because
        # something has already gone wrong today.
        await _shop(db)
        with pytest.raises(BackupError):
            await backup.restore(db, b"not a backup at all", data_dir=tmp_path / "r")
        await db.rollback()
        assert (await db.execute(select(Order))).scalars().one().order_number == "9001"

    async def test_a_backup_from_the_future_does_not_touch_the_database(
        self, db, data_dir, tmp_path
    ):
        await _shop(db)
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, payload in (
                (
                    "manifest.json",
                    json.dumps(
                        {"format": 1, "alembic_revision": "0099_from_the_future"}
                    ).encode(),
                ),
                ("database.json", json.dumps({"orders": []}).encode()),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, BytesIO(payload))

        with pytest.raises(BackupError, match="newer PrintFlow"):
            await backup.restore(db, buffer.getvalue(), data_dir=tmp_path / "r")
        await db.rollback()
        # Checked before a single row was deleted.
        assert (await db.execute(select(Order))).scalars().all() != []


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


class TestEndpoints:
    async def test_taking_a_backup_needs_signing_in(self, client):
        assert (await client.post("/api/backup/download", json={})).status_code == 401

    async def test_the_status_says_what_would_be_saved(self, signed_in, db):
        await _shop(db)
        body = (await signed_in.get("/api/backup/status")).json()
        assert body["tables"]["orders"] == 1
        assert body["rows"] >= 1

    async def test_downloading_gives_a_file_that_can_be_read_back(self, signed_in, db):
        await _shop(db)
        response = await signed_in.post("/api/backup/download", json={})
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"

        manifest, database, _ = backup.read_archive(response.content)
        assert manifest["format"] == backup.FORMAT_VERSION
        assert len(database["orders"]) == 1

    async def test_a_download_is_written_down(self, signed_in, db):
        from app.models import AuditLog

        await signed_in.post("/api/backup/download", json={})
        rows = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "backup_taken")
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_inspecting_says_what_is_in_it_without_restoring(self, signed_in, db):
        await _shop(db)
        raw = (await signed_in.post("/api/backup/download", json={})).content
        await db.execute(Order.__table__.delete())
        await db.commit()

        body = (
            await signed_in.post(
                "/api/backup/inspect", files={"archive": ("b.tar.gz", raw)}
            )
        ).json()
        assert body["tables"]["orders"] == 1
        assert body["problem"] is None
        # Looking is not restoring.
        assert (await db.execute(select(Order))).scalars().all() == []

    async def test_restoring_needs_the_word_typed(self, signed_in, db):
        raw = (await signed_in.post("/api/backup/download", json={})).content
        response = await signed_in.post(
            "/api/backup/restore",
            files={"archive": ("b.tar.gz", raw)},
            data={"confirm": "yes"},
        )
        assert response.status_code == 400
        assert "restore" in response.json()["detail"]

    async def test_and_then_does_it(self, signed_in, db):
        await _shop(db)
        raw = (await signed_in.post("/api/backup/download", json={})).content
        await db.execute(Order.__table__.delete())
        await db.commit()

        response = await signed_in.post(
            "/api/backup/restore",
            files={"archive": ("b.tar.gz", raw)},
            data={"confirm": "restore"},
        )
        assert response.status_code == 200, response.text
        assert (await db.execute(select(Order))).scalars().one().order_number == "9001"

    async def test_an_empty_file_is_not_a_backup(self, signed_in):
        response = await signed_in.post(
            "/api/backup/inspect", files={"archive": ("b.tar.gz", b"")}
        )
        assert response.status_code == 400


# --------------------------------------------------------------------------
# Restoring onto a machine nobody owns yet
# --------------------------------------------------------------------------


class TestDuringSetup:
    async def test_a_fresh_install_can_be_restored_without_signing_in(
        self, client, signed_in, db
    ):
        # The person restoring onto a new machine has no account on it, and
        # making them create one first creates an account the restore throws
        # away.
        await _shop(db)
        raw = (await signed_in.post("/api/backup/download", json={})).content
        await db.execute(Order.__table__.delete())
        await db.execute(User.__table__.delete())
        await db.commit()

        response = await client.post(
            "/api/setup/restore", files={"archive": ("b.tar.gz", raw)}
        )
        assert response.status_code == 200, response.text
        assert response.json()["sign_in_with_restored_account"] is True
        assert (await db.execute(select(Order))).scalars().one().order_number == "9001"

    async def test_the_restored_admin_is_the_way_in(self, client, signed_in, db):
        raw = (await signed_in.post("/api/backup/download", json={})).content
        await db.execute(User.__table__.delete())
        await db.commit()

        await client.post("/api/setup/restore", files={"archive": ("b.tar.gz", raw)})
        users = (await db.execute(select(User))).scalars().all()
        assert [user.username for user in users] == ["admin"]

    async def test_an_owned_install_will_not_be_restored_over_anonymously(
        self, client, signed_in, db
    ):
        # Once somebody owns this install, replacing it takes signing in.
        raw = (await signed_in.post("/api/backup/download", json={})).content
        response = await client.post(
            "/api/setup/restore", files={"archive": ("b.tar.gz", raw)}
        )
        assert response.status_code == 409
        assert "Sign in" in response.json()["detail"]

    async def test_nor_inspected(self, client, signed_in, db):
        raw = (await signed_in.post("/api/backup/download", json={})).content
        response = await client.post(
            "/api/setup/restore/inspect", files={"archive": ("b.tar.gz", raw)}
        )
        assert response.status_code == 409

    async def test_a_fresh_install_can_look_before_it_leaps(self, client, signed_in, db):
        await _shop(db)
        raw = (await signed_in.post("/api/backup/download", json={})).content
        await db.execute(User.__table__.delete())
        await db.commit()

        body = (
            await client.post(
                "/api/setup/restore/inspect", files={"archive": ("b.tar.gz", raw)}
            )
        ).json()
        assert body["tables"]["orders"] == 1


class TestTheKeyThatMightNotTravel:
    """The one way a backup can be quietly incomplete.

    When SECRET_KEY comes from the environment there is no key file to carry,
    so the archive holds credentials that nothing in it can decrypt. The
    restore looks fine right up until every integration fails at once, which is
    the worst possible moment to find out. So it is said at both ends.
    """

    async def test_a_backup_says_whether_it_carries_the_key(self, db, data_dir):
        path, manifest = await backup.create(db, data_dir=data_dir)
        path.unlink(missing_ok=True)
        assert manifest["carries_secret_key"] is True

    async def test_and_says_when_it_does_not(self, db, tmp_path):
        empty = tmp_path / "no-key"
        empty.mkdir()
        path, manifest = await backup.create(db, data_dir=empty)
        path.unlink(missing_ok=True)
        assert manifest["carries_secret_key"] is False
        assert manifest["data_files"] == []

    async def test_the_status_screen_says_it_before_the_button_is_pressed(
        self, signed_in
    ):
        body = (await signed_in.get("/api/backup/status")).json()
        assert "carries_secret_key" in body
        assert body["secret_key_source"] in ("file", "environment", "generated")

    async def test_a_restore_says_when_the_environment_will_override_the_key(
        self, db, data_dir, tmp_path
    ):
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        result = await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        # Whichever it is here, the answer is reported rather than assumed.
        assert result["secret_key_from_environment"] in (True, False)
