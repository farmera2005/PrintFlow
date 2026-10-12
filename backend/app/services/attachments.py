"""Files attached to an order.

A 3D-printing shop's orders arrive with things that are not in the order: the
model a customer sent, a photo of the proof they approved, a trade buyer's
purchase order, the email where somebody agreed to the change. All of it lives
in whoever's inbox received it, which is the one place the shop floor cannot
reach while packing.

**Where the bytes go.** Under the data directory, as files, with a row in the
database pointing at each one. The alternative — a `bytea` column, the way the
shipping label is stored — rides along in backups for free, but the backup
builds its JSON in memory and one 80MB model would make the archive 110MB of
base64 inside a single string. Files under the data directory are tarred and
restored like the secret key and the certificate already are, which is the
same guarantee for a fraction of the memory.

**What a filename is allowed to decide: nothing.** The name on disk is the
attachment's own id. The name the person uses is kept beside it as a label and
handed back on download, so a file called `../../etc/passwd` is a file called
`../../etc/passwd` and nothing more.

**How it is served matters as much as where it is kept.** PrintFlow's own UI
is on this origin and holds this session's cookie, so an uploaded page served
inline would run as PrintFlow. Everything downloads as an attachment, with
sniffing turned off, except a short list of image types that cannot carry
script — which is what lets a proof photo show as a thumbnail.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid as uuid_module
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Order, OrderAttachment

log = logging.getLogger("printflow.attachments")


class AttachmentError(RuntimeError):
    """The file cannot be taken. Shown verbatim."""


# Where they live inside the data directory.
FOLDER = "attachments"

# The biggest single file. A sliced 3MF or a detailed STL runs to tens of
# megabytes, so a limit that only suits photographs would refuse exactly the
# files this is for. Written in bytes rather than as a round number of
# megabytes so the message can say both.
MAX_BYTES = 100 * 1024 * 1024

# Read and written in chunks rather than whole: a hundred megabytes through
# `await upload.read()` is a hundred megabytes of this process, per upload, at
# the same time.
CHUNK = 1024 * 1024

# The only types sent back for the browser to render in place. Every one of
# them is a bitmap a browser decodes as pixels and nothing else.
#
# SVG is deliberately absent, and it is the whole reason this is a list rather
# than a `startswith("image/")`: an SVG is a document, it can carry script, and
# serving one inline from this origin would hand a session cookie to whoever
# uploaded it.
INLINE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
)

# What everything else is served as. Not the uploader's content type: a browser
# that is told `text/html` will treat it as HTML however the file was named.
DOWNLOAD_TYPE = "application/octet-stream"

# A filename here is a label and never a path — the bytes are stored under the
# row's id — so the job is not to make it safe for a filesystem but to make it
# safe to *show* and to put in a header. What has to go: anything a path is
# made of, the punctuation a Content-Disposition header is made of, and the
# invisible characters that make a name read as something it is not.
UNSAFE = re.compile(r'[/\\<>:"|?*\x00-\x1f\x7f]+')
NAME_LIMIT = 120


def clean_name(raw: str | None) -> str:
    """The name to keep: harmless, and still the name the person typed.

    Accents and non-Latin scripts survive — "modèle final.stl" is what that
    file is called, and a shop selling into France should not be handed
    "mod_le final.stl" back. What does not survive is anything that could make
    the name lie: path separators, control characters, and the bidirectional
    overrides that let `report‮gnp.exe` display as `report.png`.

    An empty result becomes "file", because something has to be shown and a
    blank row looks broken.
    """
    name = unicodedata.normalize("NFC", (raw or "").strip())
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Cf is the category the bidi overrides and the zero-width joiners live in.
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    name = UNSAFE.sub("_", name).strip(". ")
    if len(name) > NAME_LIMIT:
        # Keep the extension: it is the part that says what the file is.
        stem, _, suffix = name.rpartition(".")
        if stem and len(suffix) <= 10:
            name = f"{stem[: NAME_LIMIT - len(suffix) - 1]}.{suffix}"
        else:
            name = name[:NAME_LIMIT]
    return name or "file"


def _suffix(name: str) -> str:
    """The extension, when it looks like one. Kept on the stored file so a
    human in the data directory can tell a model from a photograph."""
    suffix = Path(name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


def root(order_id: uuid_module.UUID | str | None = None) -> Path:
    base = get_config().data_dir / FOLDER
    return base / str(order_id) if order_id else base


def path_of(attachment: OrderAttachment) -> Path:
    """Where this attachment's bytes are, checked to be where they should be.

    The stored path comes out of the database, so this is not defending
    against a passing stranger — it is defending against a bad restore, a
    hand-edited row, or a bug here. Cheap, and the failure it prevents is
    reading an arbitrary file off the host.
    """
    base = root().resolve()
    full = (get_config().data_dir / attachment.stored_path).resolve()
    if not full.is_relative_to(base):
        raise AttachmentError("That file is not where PrintFlow keeps attachments.")
    return full


def human_size(size: int) -> str:
    """A size somebody reads rather than counts the digits of."""
    if size < 1024:
        return f"{size} B"
    scaled = float(size)
    for unit in ("KB", "MB", "GB"):
        scaled /= 1024.0
        if scaled < 1024 or unit == "GB":
            return f"{scaled:.1f} {unit}".replace(".0 ", " ")
    return f"{scaled:.1f} GB"


def serve_as(attachment: OrderAttachment) -> tuple[str, str]:
    """The content type and disposition to send this back with.

    The uploader's own content type is only honoured for the image types that
    cannot carry script. Everything else — including anything claiming to be
    HTML — is an octet-stream attachment, so a file uploaded to this origin
    can never run on it.
    """
    declared = (attachment.content_type or "").split(";")[0].strip().lower()
    if declared in INLINE_TYPES:
        return declared, "inline"
    return DOWNLOAD_TYPE, "attachment"


async def save(
    session: AsyncSession,
    order: Order,
    *,
    stream: BinaryIO,
    filename: str | None,
    content_type: str | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> OrderAttachment:
    """Write one upload to disk and record it against the order.

    The bytes land first and the row second, so the failure that leaves
    something behind leaves a file nobody references rather than a row
    pointing at nothing — and the file is cleaned up on the way out anyway.
    """
    name = clean_name(filename)
    attachment_id = uuid_module.uuid4()
    folder = root(order.id)
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{attachment_id}{_suffix(name)}"

    digest = hashlib.sha256()
    written = 0
    try:
        with open(stored, "wb") as out:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BYTES:
                    raise AttachmentError(
                        f"“{name}” is larger than {human_size(MAX_BYTES)}, which is "
                        "the most PrintFlow will keep on an order."
                    )
                digest.update(chunk)
                out.write(chunk)
        if not written:
            raise AttachmentError(f"“{name}” is empty.")
    except Exception:
        # Whatever went wrong, the half-written file is not worth keeping.
        stored.unlink(missing_ok=True)
        raise

    attachment = OrderAttachment(
        id=attachment_id,
        order_id=order.id,
        filename=name,
        stored_path=str(stored.relative_to(get_config().data_dir)),
        content_type=(content_type or "").strip()[:200] or None,
        size_bytes=written,
        sha256=digest.hexdigest(),
        note=(note or "").strip() or None,
        uploaded_by=actor,
    )
    session.add(attachment)
    await session.flush()
    return attachment


async def remove(session: AsyncSession, attachment: OrderAttachment) -> None:
    """Forget the row, then the file.

    In that order on purpose: a file left behind by a failure here is invisible
    and harmless, where a row pointing at a deleted file is a download that
    fails for somebody who can see the attachment listed.
    """
    try:
        target = path_of(attachment)
    except AttachmentError:
        target = None
    await session.delete(attachment)
    await session.flush()
    if target is not None:
        target.unlink(missing_ok=True)
        # An order whose last file has gone leaves no empty folder behind.
        try:
            target.parent.rmdir()
        except OSError:
            pass


async def for_order(session: AsyncSession, order_id: uuid_module.UUID) -> list[OrderAttachment]:
    return list(
        (
            await session.execute(
                select(OrderAttachment)
                .where(OrderAttachment.order_id == order_id)
                .order_by(OrderAttachment.created_at)
            )
        )
        .scalars()
        .all()
    )


def serialize(attachment: OrderAttachment) -> dict[str, Any]:
    """One attachment as the drawer reads it.

    `missing` is the honest answer to a row whose file is not there — after a
    half-restore, or a data directory somebody tidied. The alternative is a
    download that 404s from a list that showed it as present.
    """
    try:
        there = path_of(attachment).exists()
    except AttachmentError:
        there = False
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size_bytes": attachment.size_bytes,
        "size_label": human_size(attachment.size_bytes),
        # Whether the browser may draw it in place, decided here rather than
        # from the content type the browser will be given anyway.
        "previewable": serve_as(attachment)[1] == "inline",
        "note": attachment.note,
        "uploaded_by": attachment.uploaded_by,
        "created_at": attachment.created_at,
        "missing": not there,
    }
