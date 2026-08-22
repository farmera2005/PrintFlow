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


# --------------------------------------------------------------------------
# Every table, not just the ones a test happened to touch
# --------------------------------------------------------------------------


async def _one_of_everything(db) -> dict[str, int]:
    """A row in every table PrintFlow has.

    Written because the first cut of these tests exercised the tables a test
    shop happens to fill — orders, products, credentials — and a real install
    has a TLS certificate, a sync log, made sheets, option rules and variations
    as well. A backup that round-trips six tables and falls over on the
    seventh is not a backup, and the seventh is only ever discovered by
    somebody restoring for real.
    """
    from datetime import datetime, timezone

    from app.models import (
        AppSetting,
        AuditLog,
        BomLine,
        BomOptionRule,
        EtsyProductLink,
        Machine,
        MadeSheet,
        MadeSheetLine,
        MaintenanceLog,
        OAuthState,
        OrderLine,
        PrintJob,
        PrintFile,
        ProductOptionItem,
    ProductVariation,
        SyncLog,
        TlsCertificate,
    )

    now = datetime.now(timezone.utc)
    order = await _shop(db, number="9500")
    product = (await db.execute(select(Product))).scalars().first()
    component = Product(sku="COMP-1", name="Base", fulfillment="stocked")
    db.add(component)
    await db.flush()

    line = OrderLine(
        order_id=order.id,
        product_id=product.id,
        sku_raw="SKU-9500",
        title="Dragon egg",
        quantity=2,
        qty_from_stock=1,
        qty_to_print=1,
        state="printed",
        variations=[{"name": "Colour", "value": "Red"}],
        option_effects=["swapped a component"],
    )
    db.add(line)
    await db.flush()

    sheet = MadeSheet(reference="MS-1", made_on=now, status="draft")
    db.add(sheet)
    await db.flush()

    db.add_all(
        [
            User(username="restored-admin", password_hash="not-a-real-hash"),
            AppSetting(key="a_setting", value={"v": {"nested": [1, 2, 3]}}),
            AuditLog(
                entity_type="order",
                entity_id=order.id,
                action="set_status",
                detail={"from": "new", "to": "shipped"},
                actor="admin",
            ),
            SyncLog(job="etsy_receipt_poll", ok=True, detail="found 3"),
            TlsCertificate(
                cert_pem="-----BEGIN CERTIFICATE-----\nfake\n",
                encrypted_key=b"\x00\x01\x02encrypted\xff",
                fingerprint_sha256="ab" * 32,
                common_name="printflow.local",
                sans=["printflow.local", "127.0.0.1"],
                not_before=now,
                not_after=now,
            ),
            OAuthState(state="abc123", provider="etsy", code_verifier="v",
                       redirect_uri="https://printflow.local/cb"),
            BomLine(bundle_id=product.id, component_id=component.id, quantity=2),
            BomOptionRule(
                bundle_id=product.id,
                option_name="Colour",
                option_value="Red",
                component_id=component.id,
                quantity=1,
            ),
            EtsyProductLink(etsy_listing_id=12345, product_id=product.id),
            ProductVariation(product_id=product.id, label="Large", options=[]),
            ProductOptionItem(
                product_id=product.id,
                options=[{"name": "Scale", "value": "1:64"}],
                qbo_item_id="601",
            ),
            PrintFile(
                product_id=product.id, bambuddy_archive_id=77, units_per_plate=4
            ),
            MadeSheetLine(
                sheet_id=sheet.id,
                product_id=product.id,
                quantity=1,
                unit_cost=Decimal("1.2300"),
            ),
            PrintJob(
                order_line_id=line.id,
                status="done",
                bambuddy_archive_id=77,
                units_expected=4,
                printer_models=["H2D"],
            ),
        ]
    )
    await db.flush()

    # The maintenance book, which is PrintFlow's own and has to come back with
    # everything else — it is the one record about a machine that exists
    # nowhere but here.
    machine = Machine(name="H2D-01", model="H2D", serial="03003A2C003")
    db.add(machine)
    await db.flush()
    db.add(
        MaintenanceLog(
            machine_id=machine.id,
            logged_on=now.date(),
            hours=Decimal("1240.50"),
            status="serviced",
            notes="Nozzle changed",
            actor="adam",
        )
    )
    await db.commit()

    counts: dict[str, int] = {}
    for table in backup.Base.metadata.sorted_tables:
        found = (await db.execute(select(table))).mappings().all()
        counts[table.name] = len(found)
    return counts


class TestEveryTable:
    async def test_a_full_install_round_trips(self, db, data_dir, tmp_path):
        before = await _one_of_everything(db)
        # Nothing should be empty — otherwise this test proves nothing.
        empty = [name for name, count in before.items() if not count]
        assert not empty, f"these tables have no row to round-trip: {empty}" 

        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)

        result = await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        assert result["tables"] == before

    async def test_and_comes_back_as_the_same_values(self, db, data_dir, tmp_path):
        from app.models import AppSetting, TlsCertificate

        await _one_of_everything(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)
        await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()

        # The awkward ones: encrypted bytes, a JSON list, a nested JSON object.
        cert = (await db.execute(select(TlsCertificate))).scalars().one()
        assert cert.encrypted_key == b"\x00\x01\x02encrypted\xff"
        assert cert.sans == ["printflow.local", "127.0.0.1"]
        setting = (
            await db.execute(select(AppSetting).where(AppSetting.key == "a_setting"))
        ).scalars().one()
        assert setting.value == {"v": {"nested": [1, 2, 3]}}


class TestFailuresExplainThemselves:
    """"Internal Server Error" is the same words whether the file was corrupt,
    the database refused a row, or the data volume is read-only — and those
    have three completely different fixes. Whoever is restoring is already
    having a bad day; the least this can do is say which third it was, and
    whether their data was touched."""

    async def test_an_unexpected_failure_names_the_stage_and_the_cause(
        self, signed_in, db, monkeypatch
    ):
        raw = (await signed_in.post("/api/backup/download", json={})).content

        def explode(*_args, **_kwargs):
            raise PermissionError(13, "Read-only file system")

        monkeypatch.setattr("app.services.backup.write_data_files", explode)
        response = await signed_in.post(
            "/api/backup/restore",
            files={"archive": ("b.tar.gz", raw)},
            data={"confirm": "restore"},
        )
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "writing the data directory" in detail
        assert "PermissionError" in detail
        assert "Read-only file system" in detail
        # And the promise that matters most.
        assert "nothing was changed" in detail

    async def test_and_really_does_leave_the_database_alone(
        self, signed_in, db, monkeypatch
    ):
        await _shop(db, number="9600")
        raw = (await signed_in.post("/api/backup/download", json={})).content
        await _shop(db, number="9601")

        def explode(*_args, **_kwargs):
            raise OSError("the volume went away")

        monkeypatch.setattr("app.services.backup.write_data_files", explode)
        await signed_in.post(
            "/api/backup/restore",
            files={"archive": ("b.tar.gz", raw)},
            data={"confirm": "restore"},
        )
        await db.rollback()
        numbers = sorted(
            row.order_number for row in (await db.execute(select(Order))).scalars().all()
        )
        # 9601 was created after the backup and is still here: the restore that
        # would have removed it was rolled back in full.
        assert numbers == ["9600", "9601"]

    async def test_a_database_failure_names_that_stage_instead(
        self, signed_in, db, monkeypatch
    ):
        raw = (await signed_in.post("/api/backup/download", json={})).content

        async def explode(*_args, **_kwargs):
            raise RuntimeError("column does not exist")

        monkeypatch.setattr("app.services.backup.load_database", explode)
        response = await signed_in.post(
            "/api/backup/restore",
            files={"archive": ("b.tar.gz", raw)},
            data={"confirm": "restore"},
        )
        assert "replacing the database" in response.json()["detail"]

    async def test_a_file_permission_quirk_does_not_lose_the_restore(
        self, db, data_dir, tmp_path, monkeypatch
    ):
        # A bind-mounted data directory on a Windows or macOS host refuses
        # chmod outright. Failing a whole restore over one mode bit would be
        # losing the shop to protect its tidiness.
        await _shop(db)
        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)

        def no_chmod(*_args, **_kwargs):
            raise PermissionError("chmod not supported here")

        monkeypatch.setattr("app.services.backup.os.chmod", no_chmod)
        target = tmp_path / "restored"
        result = await backup.restore(db, raw, data_dir=target)
        await db.commit()
        assert "secret_key" in result["data_files"]
        assert (target / "secret_key").exists()

    def test_a_file_with_several_dots_keeps_its_name(self, tmp_path):
        # Named by appending rather than by with_suffix, which replaces the
        # last extension — turning `chain.pem` into `chain.restoring` and
        # leaving the real file untouched.
        written = backup.write_data_files(
            {"tls/chain.pem": b"cert", "secret_key": b"key"}, tmp_path
        )
        assert sorted(written) == ["secret_key", "tls/chain.pem"]
        assert (tmp_path / "tls" / "chain.pem").read_bytes() == b"cert"
        assert not list(tmp_path.rglob("*.restoring"))


class TestSelfReferencingRows:
    """Two tables point at themselves: a product variant names its master, and
    a bundle's component line names the ordered line it came from.

    Sorting the *tables* by dependency does nothing for those — the rows come
    out of a plain SELECT in whatever order the database felt like, and
    Postgres checks a foreign key the moment the row lands rather than at the
    end of the transaction. So a child arriving before its parent is a straight
    IntegrityError, and which one arrives first is nobody's decision.

    That is what made this hide: a shop with no bundles and no variants
    restores perfectly. The reported symptom was "Internal Server Error".
    """

    def test_a_child_listed_first_is_still_inserted_second(self):
        from app.models import Product

        master, variant = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        ordered = backup.parents_first(
            Product.__table__,
            [
                {"id": variant, "sku": "V", "parent_id": master},
                {"id": master, "sku": "M", "parent_id": None},
            ],
        )
        assert [row["sku"] for row in ordered] == ["M", "V"]

    def test_a_chain_comes_out_in_order(self):
        from app.models import OrderLine

        a, b, c = ("aaaa", "bbbb", "cccc")
        ordered = backup.parents_first(
            OrderLine.__table__,
            [
                {"id": c, "parent_line_id": b},
                {"id": b, "parent_line_id": a},
                {"id": a, "parent_line_id": None},
            ],
        )
        assert [row["id"] for row in ordered] == [a, b, c]

    def test_a_table_that_does_not_point_at_itself_is_left_alone(self):
        rows = [{"id": "2"}, {"id": "1"}]
        assert backup.parents_first(Order.__table__, rows) == rows

    def test_a_parent_that_is_not_in_the_backup_does_not_hang_it(self):
        from app.models import Product

        # The database will reject this row on its own terms, which is a better
        # error than looping here forever deciding what to do about it.
        rows = [{"id": "1", "parent_id": "missing"}]
        assert backup.parents_first(Product.__table__, rows) == rows

    def test_a_row_that_is_its_own_parent_does_not_hang_it_either(self):
        from app.models import Product

        rows = [{"id": "1", "parent_id": "1"}]
        assert backup.parents_first(Product.__table__, rows) == rows

    async def test_a_shop_with_variants_and_bundles_round_trips(
        self, db, data_dir, tmp_path
    ):
        # The end-to-end version of the bug, through the real archive.
        from app.models import OrderLine

        order = await _shop(db, number="9700")
        master = (await db.execute(select(Product))).scalars().first()
        variant = Product(
            sku="VARIANT-1", name="Dragon egg, large", fulfillment="printed",
            parent_id=master.id,
        )
        db.add(variant)
        bundle = OrderLine(
            order_id=order.id, product_id=master.id, quantity=1,
            qty_from_stock=0, qty_to_print=1, state="exploded", variations=[],
        )
        db.add(bundle)
        await db.flush()
        db.add(
            OrderLine(
                order_id=order.id, parent_line_id=bundle.id, product_id=variant.id,
                quantity=1, qty_from_stock=0, qty_to_print=1, state="printed",
                variations=[],
            )
        )
        await db.commit()

        path, _ = await backup.create(db, data_dir=data_dir)
        raw = path.read_bytes()
        path.unlink(missing_ok=True)

        result = await backup.restore(db, raw, data_dir=tmp_path / "r")
        await db.commit()
        assert result["tables"]["products"] == 2
        assert result["tables"]["order_lines"] == 2

        # And the relationships survived, rather than merely inserting.
        restored = (
            await db.execute(select(Product).where(Product.sku == "VARIANT-1"))
        ).scalars().one()
        assert restored.parent_id == master.id

    async def test_rows_in_the_worst_possible_order_still_load(self, db):
        """The deterministic version, because the natural one is luck.

        A freshly inserted table hands its rows back in insertion order, which
        is parents-first, so a test that just writes rows and reads them back
        passes with or without the fix. Real tables do not look like that: an
        UPDATE in Postgres writes a new row version at the end of the heap, and
        PrintFlow updates an order line every time it recomputes one — so an
        edited parent ends up *after* its own children. That is the shape this
        forces, by reversing every table's rows before loading them.
        """
        from app.models import OrderLine

        order = await _shop(db, number="9800")
        master = (await db.execute(select(Product))).scalars().first()
        db.add(
            Product(
                sku="V-9800", name="Variant", fulfillment="printed",
                parent_id=master.id,
            )
        )
        bundle = OrderLine(
            order_id=order.id, product_id=master.id, quantity=1,
            qty_from_stock=0, qty_to_print=1, state="exploded", variations=[],
        )
        db.add(bundle)
        await db.flush()
        db.add(
            OrderLine(
                order_id=order.id, parent_line_id=bundle.id, quantity=1,
                qty_from_stock=0, qty_to_print=1, state="printed", variations=[],
            )
        )
        await db.commit()

        dumped = await backup.dump_database(db)
        upside_down = {name: list(reversed(rows)) for name, rows in dumped.items()}

        counts = await backup.load_database(db, upside_down)
        await db.commit()
        assert counts["products"] == 2
        assert counts["order_lines"] == 2
