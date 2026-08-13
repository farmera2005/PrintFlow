"""Take the whole shop away in one file, and put it back.

Two ways in, deliberately. Signed in, it is a Settings screen: press the button,
keep the file. Not signed in, it is the *first* thing the setup wizard offers,
because the person restoring onto a new machine has no account on it yet — and
telling them to create one first would mean creating an account that the
restore then throws away.

The unauthenticated door is only open while there is no admin account, which is
the same gate the wizard's own "create the admin" step sits behind. Once
somebody owns this install, restoring over it takes signing in.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..config import SECRET_KEY_FILENAME, get_config
from ..db import get_session
from ..models import User
from ..services import audit, backup
from ..services.backup import BackupError
from ..services.settings_store import has_admin_user

log = logging.getLogger("printflow.backup")

router = APIRouter(prefix="/api/backup", tags=["backup"])

# A backup is one file and one upload. Bigger than this is not a PrintFlow
# archive, and reading it into memory to find out is the fault rather than the
# check — label PDFs are the bulk and a very busy year is still far under.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


class BackupRequest(BaseModel):
    # Optional, and the screen says loudly what leaving it empty means: the
    # file contains the key every stored credential is encrypted with.
    passphrase: str = ""


@router.get("/status")
async def backup_status(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """What a backup would contain, so the button is not a leap of faith."""
    counts = {
        name: len(rows) for name, rows in (await backup.dump_database(session)).items()
    }
    files = backup.data_files()
    config = get_config()
    return {
        "tables": counts,
        "rows": sum(counts.values()),
        "data_files": [path.name for path in files],
        "alembic_revision": await backup.current_revision(session),
        "format": backup.FORMAT_VERSION,
        # Where the encryption key lives, and therefore whether a backup can
        # carry it. Set in the environment, it is not PrintFlow's to save — and
        # a restore without it comes up unable to read a single credential,
        # which is a thing to learn now rather than on the day of the restore.
        "secret_key_source": config.secret_key_source,
        "carries_secret_key": any(
            path.name == SECRET_KEY_FILENAME for path in files
        ),
    }


@router.post("/download")
async def download_backup(
    body: BackupRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """The whole shop, as one file.

    A POST rather than a GET because it carries a passphrase, and a passphrase
    does not belong in a query string where it would land in the browser's
    history and every proxy log between here and the operator.
    """
    path, manifest = await backup.create(session, passphrase=body.passphrase)
    await audit.record(
        session,
        entity_type="system",
        entity_id=None,
        action="backup_taken",
        detail={
            "rows": sum(manifest["tables"].values()),
            "encrypted": manifest["encrypted"],
            "data_files": len(manifest["data_files"]),
        },
        actor=user.username,
    )
    await session.commit()
    name = backup.suggested_name(bool(body.passphrase))
    return StreamingResponse(
        backup.iter_file(path),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # It is the shop's every credential. Nothing in between should keep
            # a copy of it.
            "Cache-Control": "no-store",
        },
    )


async def _read_upload(archive: UploadFile) -> bytes:
    raw = await archive.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "That file is larger than any PrintFlow backup.",
        )
    return raw


@router.post("/inspect")
async def inspect_backup(
    archive: UploadFile = File(...),
    passphrase: str = Form(""),
    _: User = Depends(require_user),
) -> dict:
    """What is in this file — before anybody replaces a shop with it."""
    return _preview(await _read_upload(archive), passphrase)


def _preview(raw: bytes, passphrase: str) -> dict[str, Any]:
    try:
        return backup.preview(raw, passphrase)
    except BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/restore")
async def restore_backup(
    archive: UploadFile = File(...),
    passphrase: str = Form(""),
    confirm: str = Form(""),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace everything here with what is in the file.

    Typed confirmation rather than a checkbox: this deletes every order,
    product and credential on this install and there is no undo beyond
    whatever backup the operator happens to have of *this* one.
    """
    if confirm.strip().lower() != "restore":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Type "restore" to confirm. This replaces every order, product and '
            "credential on this install.",
        )
    raw = await _read_upload(archive)
    return await _do_restore(session, raw, passphrase, actor=user.username)


async def _do_restore(
    session: AsyncSession, raw: bytes, passphrase: str, *, actor: str
) -> dict[str, Any]:
    try:
        result = await backup.restore(session, raw, passphrase=passphrase)
    except BackupError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Written after the rows are in, and to the restored audit log — this is
    # now part of the history of the shop that was restored, which is where
    # somebody looking for "why does this install have last Tuesday's orders"
    # will actually look.
    await audit.record(
        session,
        entity_type="system",
        entity_id=None,
        action="backup_restored",
        detail={
            "taken_at": result["manifest"].get("created_at"),
            "rows": result["rows"],
            "data_files": result["data_files"],
            "from_revision": result["manifest"].get("alembic_revision"),
        },
        actor=actor,
    )
    await session.commit()
    return result


# --------------------------------------------------------------------------
# The same thing, during setup
# --------------------------------------------------------------------------

setup_router = APIRouter(prefix="/api/setup", tags=["setup"])


async def _no_owner_yet(session: AsyncSession) -> None:
    if await has_admin_user(session):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This install already has an admin account. Sign in, then restore "
            "from Settings.",
        )


@setup_router.post("/restore/inspect")
async def setup_inspect(
    archive: UploadFile = File(...),
    passphrase: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read a backup before the machine has an owner. See the module note."""
    await _no_owner_yet(session)
    return _preview(await _read_upload(archive), passphrase)


@setup_router.post("/restore")
async def setup_restore(
    archive: UploadFile = File(...),
    passphrase: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Restore onto a fresh install, before there is anybody to sign in as.

    No typed confirmation here, unlike the Settings version: there is nothing
    on this machine to destroy — that is what having no admin account means —
    and making somebody confirm the loss of nothing teaches them to click
    through the confirmation that matters.
    """
    await _no_owner_yet(session)
    raw = await _read_upload(archive)
    result = await _do_restore(session, raw, passphrase, actor="setup")
    log.warning("Install restored from a backup during setup")
    # Whoever was in the backup is now the way in. Said plainly, because the
    # password on this machine is now one from another machine.
    result["sign_in_with_restored_account"] = True
    return result
