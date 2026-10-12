"""Files attached to an order.

The bytes live under the data directory and only a row lives in the database,
which is a trade with two edges: backups carry attachments as files instead of
as base64 inside the database dump, and a row can outlive its file. Both edges
are tested here.

The other half is serving them. PrintFlow's own pages are on this origin and
hold the session cookie, so an uploaded file that a browser agreed to render
would be running as PrintFlow — which makes "how is it served" a correctness
question rather than a detail, and the reason the disposition and the sniffing
header are pinned as tightly as the bytes are.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from app.models import AuditLog, Order, OrderAttachment
from app.services import attachments, backup
from app.services.attachments import AttachmentError
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def _order(db, number="9001") -> Order:
    order = Order(
        source="etsy",
        etsy_receipt_id=int(number),
        order_number=number,
        buyer_name="Dana Buyer",
    )
    db.add(order)
    await db.flush()
    return order


def _upload(content: bytes = b"solid part\n") -> io.BytesIO:
    return io.BytesIO(content)


async def _attach(db, order, *, name="bracket.stl", content=b"solid part\n", **fields):
    saved = await attachments.save(
        db, order, stream=_upload(content), filename=name, **fields
    )
    await db.commit()
    return saved


# --------------------------------------------------------------------------
# Names, and what they are not allowed to decide
# --------------------------------------------------------------------------


class TestNames:
    def test_a_path_is_not_a_name(self):
        # The one that matters: nothing a filename contains may decide where
        # the bytes land.
        assert attachments.clean_name("../../etc/passwd") == "passwd"
        assert attachments.clean_name("C:\\Users\\dana\\model.stl") == "model.stl"
        assert attachments.clean_name("/absolute/thing.3mf") == "thing.3mf"

    def test_the_name_somebody_typed_is_the_name_they_get_back(self):
        """A label, not a path — the bytes are stored under the row's id — so
        it only has to be safe to show, not safe for a filesystem."""
        assert attachments.clean_name("Proof v2 final.png") == "Proof v2 final.png"
        assert attachments.clean_name("order #14 (approved).pdf") == (
            "order #14 (approved).pdf"
        )
        # Including out of the Latin alphabet: a shop selling into France
        # should not be handed "mod_le final.stl" back.
        assert attachments.clean_name("modèle final.stl") == "modèle final.stl"

    def test_a_name_that_lies_about_itself_is_straightened_out(self):
        """A right-to-left override makes `report<RLO>gnp.exe` display as
        `report.png`, which is how somebody is talked into opening it."""
        assert attachments.clean_name("report\u202egnp.exe") == "reportgnp.exe"
        assert attachments.clean_name("two\nlines.stl") == "two_lines.stl"

    def test_a_nameless_file_still_gets_a_name(self):
        assert attachments.clean_name("") == "file"
        assert attachments.clean_name("...") == "file"
        assert attachments.clean_name(None) == "file"

    def test_a_very_long_name_keeps_its_extension(self):
        """The extension is the part that says what the file is."""
        name = attachments.clean_name("a" * 400 + ".stl")
        assert len(name) <= attachments.NAME_LIMIT
        assert name.endswith(".stl")

    def test_sizes_are_written_the_way_people_say_them(self):
        assert attachments.human_size(0) == "0 B"
        assert attachments.human_size(1024) == "1 KB"
        assert attachments.human_size(1536) == "1.5 KB"
        assert attachments.human_size(attachments.MAX_BYTES) == "100 MB"


# --------------------------------------------------------------------------
# Keeping them
# --------------------------------------------------------------------------


class TestStoring:
    async def test_the_file_lands_on_disk_and_the_row_points_at_it(self, db):
        order = await _order(db)
        saved = await _attach(db, order, content=b"solid part\n")

        path = attachments.path_of(saved)
        assert path.read_bytes() == b"solid part\n"
        assert saved.size_bytes == 11
        # Named after the row, not after the upload: the name on disk is the
        # one thing the uploader has no say in.
        assert path.name.startswith(str(saved.id))
        assert path.suffix == ".stl"
        assert saved.filename == "bracket.stl"

    async def test_the_stored_path_is_relative_so_the_volume_can_move(self, db):
        order = await _order(db)
        saved = await _attach(db, order)

        assert not Path(saved.stored_path).is_absolute()
        assert saved.stored_path.startswith(f"attachments/{order.id}/")

    async def test_the_checksum_is_of_what_was_written(self, db):
        import hashlib

        order = await _order(db)
        saved = await _attach(db, order, content=b"solid part\n")
        assert saved.sha256 == hashlib.sha256(b"solid part\n").hexdigest()

    async def test_a_file_too_big_is_refused_and_leaves_nothing_behind(
        self, db, monkeypatch
    ):
        """The half-written file goes with the refusal — otherwise the disk
        fills with the uploads that were rejected."""
        monkeypatch.setattr(attachments, "MAX_BYTES", 16)
        monkeypatch.setattr(attachments, "CHUNK", 8)
        order = await _order(db)

        with pytest.raises(AttachmentError, match="larger than"):
            await attachments.save(
                db, order, stream=_upload(b"x" * 64), filename="big.stl"
            )
        assert list(attachments.root(order.id).glob("*")) == []

    async def test_an_empty_file_is_refused(self, db):
        order = await _order(db)
        with pytest.raises(AttachmentError, match="empty"):
            await attachments.save(db, order, stream=_upload(b""), filename="nothing.stl")

    async def test_two_files_with_one_name_are_two_files(self, db):
        """Named by row id, so the second does not quietly replace the first."""
        order = await _order(db)
        first = await _attach(db, order, content=b"one")
        second = await _attach(db, order, content=b"two")

        assert first.id != second.id
        assert attachments.path_of(first).read_bytes() == b"one"
        assert attachments.path_of(second).read_bytes() == b"two"

    async def test_deleting_takes_the_bytes_with_it(self, db):
        order = await _order(db)
        saved = await _attach(db, order)
        path = attachments.path_of(saved)

        await attachments.remove(db, saved)
        await db.commit()

        assert not path.exists()
        assert await attachments.for_order(db, order.id) == []
        # And the order's folder does not linger empty.
        assert not attachments.root(order.id).exists()

    async def test_a_row_pointing_outside_the_store_is_refused(self, db):
        """Not a defence against a stranger — against a bad restore or a
        hand-edited row turning a download into "read any file on the host"."""
        order = await _order(db)
        saved = await _attach(db, order)
        saved.stored_path = "../../etc/passwd"

        with pytest.raises(AttachmentError, match="not where PrintFlow keeps"):
            attachments.path_of(saved)

    async def test_a_missing_file_says_so_rather_than_pretending(self, db):
        order = await _order(db)
        saved = await _attach(db, order)
        attachments.path_of(saved).unlink()

        assert attachments.serialize(saved)["missing"] is True


class TestHowTheyAreServed:
    async def test_an_image_may_be_drawn_in_place(self, db):
        order = await _order(db)
        saved = await _attach(
            db, order, name="proof.png", content=b"\x89PNG\r\n", content_type="image/png"
        )
        assert attachments.serve_as(saved) == ("image/png", "inline")

    async def test_an_svg_is_a_document_and_is_not(self, db):
        """The reason the inline list is a list and not `image/*`: an SVG can
        carry script, and inline on this origin means running as PrintFlow."""
        order = await _order(db)
        saved = await _attach(
            db, order, name="logo.svg", content=b"<svg/>", content_type="image/svg+xml"
        )
        assert attachments.serve_as(saved) == ("application/octet-stream", "attachment")

    async def test_html_is_never_believed(self, db):
        order = await _order(db)
        saved = await _attach(
            db, order, name="note.html", content=b"<h1>hi</h1>", content_type="text/html"
        )
        assert attachments.serve_as(saved) == ("application/octet-stream", "attachment")

    async def test_a_model_comes_back_as_a_download(self, db):
        order = await _order(db)
        saved = await _attach(db, order, content_type="model/stl")
        assert attachments.serve_as(saved)[1] == "attachment"


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------


def _multipart(name="bracket.stl", content=b"solid part\n", content_type="model/stl"):
    return {"file": (name, content, content_type)}


class TestTheApi:
    async def test_a_file_can_be_attached_and_listed(self, db, signed_in):
        order = await _order(db)
        await db.commit()

        response = await signed_in.post(
            f"/api/orders/{order.id}/files",
            files=_multipart(),
            data={"note": "Customer's own model"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["filename"] == "bracket.stl"
        assert body["size_label"] == "11 B"
        assert body["note"] == "Customer's own model"
        assert body["uploaded_by"] == "admin"

        listed = (await signed_in.get(f"/api/orders/{order.id}/files")).json()
        assert [row["id"] for row in listed["files"]] == [body["id"]]

    async def test_it_arrives_with_the_order_so_the_tab_can_count_it(
        self, db, signed_in
    ):
        order = await _order(db)
        await db.commit()
        await signed_in.post(f"/api/orders/{order.id}/files", files=_multipart())

        detail = (await signed_in.get(f"/api/orders/{order.id}")).json()
        assert [row["filename"] for row in detail["files"]] == ["bracket.stl"]

    async def test_downloading_gives_the_bytes_back_as_an_attachment(
        self, db, signed_in
    ):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(f"/api/orders/{order.id}/files", files=_multipart())
        ).json()

        response = await signed_in.get(f"/api/orders/{order.id}/files/{made['id']}")
        assert response.status_code == 200
        assert response.content == b"solid part\n"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert 'filename="bracket.stl"' in response.headers["content-disposition"]
        # Without this a browser may decide for itself that it looks like HTML.
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_an_uploaded_page_cannot_be_made_to_run_here(self, db, signed_in):
        """The attack this is all for: a file served inline from PrintFlow's
        own origin would have PrintFlow's session cookie."""
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(
                f"/api/orders/{order.id}/files",
                files=_multipart("evil.html", b"<script>steal()</script>", "text/html"),
            )
        ).json()

        response = await signed_in.get(f"/api/orders/{order.id}/files/{made['id']}")
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert made["previewable"] is False

    async def test_a_photograph_is_offered_for_preview(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(
                f"/api/orders/{order.id}/files",
                files=_multipart("proof.png", b"\x89PNG\r\n\x1a\n", "image/png"),
            )
        ).json()
        assert made["previewable"] is True

        response = await signed_in.get(f"/api/orders/{order.id}/files/{made['id']}")
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"].startswith("inline;")
        # Inline, but with nothing it is allowed to load or run.
        assert "sandbox" in response.headers["content-security-policy"]

    async def test_a_name_with_an_accent_survives_the_header(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(
                f"/api/orders/{order.id}/files",
                files=_multipart("modèle final.stl", b"solid", "model/stl"),
            )
        ).json()

        response = await signed_in.get(f"/api/orders/{order.id}/files/{made['id']}")
        disposition = response.headers["content-disposition"]
        # Both spellings: the quoted ASCII one every browser reads, and the
        # encoded one that keeps the accent.
        assert 'filename="modle final.stl"' in disposition
        assert "filename*=UTF-8''mod%C3%A8le%20final.stl" in disposition

    async def test_a_note_can_be_added_afterwards(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(f"/api/orders/{order.id}/files", files=_multipart())
        ).json()

        updated = await signed_in.patch(
            f"/api/orders/{order.id}/files/{made['id']}",
            json={"note": "Approved by the customer on the 3rd"},
        )
        assert updated.status_code == 200
        assert updated.json()["note"] == "Approved by the customer on the 3rd"

    async def test_deleting_removes_the_row_and_the_file(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(f"/api/orders/{order.id}/files", files=_multipart())
        ).json()
        path = attachments.root(order.id) / f"{made['id']}.stl"
        assert path.exists()

        response = await signed_in.delete(f"/api/orders/{order.id}/files/{made['id']}")
        assert response.status_code == 200
        assert not path.exists()
        assert (await signed_in.get(f"/api/orders/{order.id}/files")).json()["files"] == []

    async def test_a_file_belongs_to_its_own_order_only(self, db, signed_in):
        """Otherwise a tidy URL is a way of walking somebody else's files."""
        mine = await _order(db, "9001")
        other = await _order(db, "9002")
        await db.commit()
        made = (
            await signed_in.post(f"/api/orders/{mine.id}/files", files=_multipart())
        ).json()

        wrong = await signed_in.get(f"/api/orders/{other.id}/files/{made['id']}")
        assert wrong.status_code == 404
        assert (
            await signed_in.delete(f"/api/orders/{other.id}/files/{made['id']}")
        ).status_code == 404

    async def test_a_file_that_is_no_longer_on_disk_says_which_one(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(f"/api/orders/{order.id}/files", files=_multipart())
        ).json()
        (attachments.root(order.id) / f"{made['id']}.stl").unlink()

        response = await signed_in.get(f"/api/orders/{order.id}/files/{made['id']}")
        assert response.status_code == 404
        assert "bracket.stl" in response.json()["detail"]

    async def test_an_empty_upload_is_a_bad_request_not_a_crash(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        response = await signed_in.post(
            f"/api/orders/{order.id}/files", files=_multipart(content=b"")
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"]

    async def test_attaching_and_removing_are_both_in_the_audit_trail(
        self, db, signed_in
    ):
        order = await _order(db)
        await db.commit()
        made = (
            await signed_in.post(f"/api/orders/{order.id}/files", files=_multipart())
        ).json()
        await signed_in.delete(f"/api/orders/{order.id}/files/{made['id']}")

        actions = [
            (row.action, row.actor)
            for row in (await db.execute(select(AuditLog))).scalars().all()
        ]
        assert ("file_attached", "admin") in actions
        assert ("file_removed", "admin") in actions

    async def test_an_order_that_does_not_exist_has_no_files(self, db, signed_in):
        response = await signed_in.get(f"/api/orders/{uuid.uuid4()}/files")
        assert response.status_code == 404

    async def test_it_takes_a_signed_in_user(self, db, client):
        order = await _order(db)
        await db.commit()
        assert (await client.get(f"/api/orders/{order.id}/files")).status_code == 401
        assert (
            await client.post(f"/api/orders/{order.id}/files", files=_multipart())
        ).status_code == 401


# --------------------------------------------------------------------------
# The backup, which is the reason they are on disk at all
# --------------------------------------------------------------------------


class TestInTheBackup:
    async def test_an_attachment_is_carried_and_comes_back(self, db):
        order = await _order(db)
        saved = await _attach(db, order, content=b"solid part\n")
        relative = saved.stored_path

        path, manifest = await backup.create(db)
        assert relative in manifest["data_files"]

        # Wiped the way a dead machine is, then restored onto it.
        attachments.path_of(saved).unlink()
        await backup.restore(db, path.read_bytes())
        await db.commit()
        path.unlink(missing_ok=True)

        back = (
            await db.execute(select(OrderAttachment).where(OrderAttachment.id == saved.id))
        ).scalar_one()
        assert attachments.path_of(back).read_bytes() == b"solid part\n"
        assert back.sha256 == saved.sha256

    async def test_a_restore_does_not_leave_the_previous_shop_s_files_behind(self, db):
        """The rows for them are gone — the bytes have to go too, or another
        shop's customers' files sit in the data directory belonging to
        nobody."""
        order = await _order(db)
        kept = await _attach(db, order, content=b"theirs")
        earlier, _ = await backup.create(db)

        # Then a file this install has and that backup has never heard of.
        later = await _order(db, "9100")
        stranger = await _attach(db, later, content=b"ours")
        leftover = attachments.path_of(stranger)
        assert leftover.exists()

        await backup.restore(db, earlier.read_bytes())
        await db.commit()
        earlier.unlink(missing_ok=True)

        # Gone with the row that referenced it, rather than sitting in the
        # data directory belonging to nobody.
        assert not leftover.exists()
        # And what the backup did carry is back where it belongs.
        back = (
            await db.execute(select(OrderAttachment).where(OrderAttachment.id == kept.id))
        ).scalar_one()
        assert attachments.path_of(back).read_bytes() == b"theirs"
