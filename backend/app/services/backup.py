"""Everything PrintFlow knows, in one file — and the way back from it.

A self-hosted shop has no operations team. What it has is one machine in a
cupboard, and the day that machine dies is the day it finds out whether anyone
ever took a backup. So this is deliberately one button and one file: press it,
keep the file somewhere else, and that file is the whole shop.

**Two halves, and both are needed.** The database holds the orders, the
products, the money and the encrypted credentials. The data directory holds the
key those credentials are encrypted *with*. Either alone is useless — a
database without the key restores to a shop that cannot talk to Etsy, and a key
without the database restores to nothing at all — which is exactly the mistake
a hand-rolled `pg_dump` cron job makes. So the archive carries both, and that
is also why it is dangerous enough to be worth a passphrase.

**Written by hand rather than by pg_dump.** The image has no Postgres client
binaries, and adding them would tie the backup to a matching server version —
a restore that fails because the client is 15 and the server is 16 is the worst
possible time to discover a version constraint. Reading the tables through the
ORM's own metadata costs a little speed and buys a file that any build of
PrintFlow can read, on any Postgres, for as long as the columns still exist.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
import uuid as uuid_module
from datetime import date, datetime
from decimal import Decimal
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import Date, DateTime, LargeBinary, Numeric, Uuid, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import SECRET_KEY_FILENAME, get_config, reset_config_cache
from ..models import Base

log = logging.getLogger("printflow.backup")

# Bumped when the archive's own layout changes in a way an older PrintFlow
# could not read. The manifest carries it so a restore can say "this file is
# from a newer PrintFlow" rather than failing halfway through.
FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.json"
DATA_PREFIX = "data/"

# The archive is encrypted in one piece, so it needs a few plaintext bytes at
# the front to say so — otherwise restore cannot tell a passphrase-protected
# file from a corrupt one, and would report the wrong problem.
MAGIC = b"PFBACKUP1\n"
SALT_BYTES = 16

# Things in the data directory that are this container's rather than this
# shop's. Restoring a socket or a log onto another machine is at best noise.
SKIP_NAMES = {".write-probe", "cloudflared.log", "cloudflared.pid", "tunnel.log"}
SKIP_SUFFIXES = (".sock", ".pid")

# Rows are inserted in batches: one round trip per table would hold an entire
# order book in a single statement, and one per row would take minutes.
BATCH = 500


class BackupError(RuntimeError):
    """Anything that stops a backup being made or read."""


# --------------------------------------------------------------------------
# Values, in and out of JSON
# --------------------------------------------------------------------------


def _encode(value: Any) -> Any:
    """One column value, as something JSON can hold without losing it.

    Money is the one that matters: a Decimal through a JSON float comes back as
    7.409999999999999, and this is a file people restore their accounts from.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, uuid_module.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _decode(value: Any, column: Any) -> Any:
    """The same value on the way back, as the column's own type.

    Driven by the column rather than by a tag in the file, so a backup taken
    before a column changed type still loads as whatever it is now.
    """
    if value is None:
        return None
    if isinstance(value, dict) and "__b64__" in value:
        return base64.b64decode(value["__b64__"])
    kind = column.type
    if isinstance(kind, Uuid) and isinstance(value, str):
        try:
            return uuid_module.UUID(value)
        except ValueError:
            return value
    if isinstance(kind, LargeBinary) and isinstance(value, str):
        return base64.b64decode(value)
    if isinstance(kind, Numeric) and isinstance(value, (str, int, float)):
        return Decimal(str(value))
    if isinstance(kind, DateTime) and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(kind, Date) and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return value


# --------------------------------------------------------------------------
# The database, out and back
# --------------------------------------------------------------------------


async def dump_database(session: AsyncSession) -> dict[str, Any]:
    """Every row of every table, in an order that could be inserted back."""
    out: dict[str, Any] = {}
    for table in Base.metadata.sorted_tables:
        rows = (await session.execute(select(table))).mappings().all()
        out[table.name] = [
            {key: _encode(value) for key, value in row.items()} for row in rows
        ]
    return out


async def current_revision(session: AsyncSession) -> str | None:
    """Which migration this database is at, as Alembic records it.

    The table is asked about before it is read. Selecting from a table that is
    not there aborts the transaction, and this runs at the start of a backup —
    so the probe would take the whole thing down on a database built by
    create_all rather than by migrations, which is what a developer's and the
    test suite's are.
    """
    exists = (
        await session.execute(text("select to_regclass('alembic_version')"))
    ).scalar()
    if not exists:
        return None
    found = (await session.execute(text("select version_num from alembic_version"))).first()
    return found[0] if found else None


def known_revisions() -> set[str]:
    """Every migration this build of PrintFlow knows how to be at.

    Used to tell an *older* backup — one whose revision is somewhere in our
    history, and which loads fine because migrations here only add columns —
    from a *newer* one, whose revision means nothing to us and whose extra
    columns would be silently dropped on the way in.
    """
    root = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    found: set[str] = set()
    try:
        for path in root.glob("*.py"):
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("revision ="):
                    found.add(stripped.split("=", 1)[1].strip().strip("'\""))
                    break
    except OSError:
        pass
    return found


def parents_first(table: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order a table's own rows so a parent is inserted before its children.

    Sorting the *tables* by dependency is not enough when a table points at
    itself, and two here do: a product variant names its master product, and a
    bundle's component line names the ordered line it came from. Those rows
    come out of a plain SELECT in whatever order the database felt like, so a
    child can perfectly well arrive first — and Postgres checks a foreign key
    the moment the row lands, not at the end of the transaction.

    Which made this the sort of bug that hides: a shop with no bundles and no
    variants restores perfectly, and one with either fails on an INSERT, with
    the outcome depending on row order that nobody chose. It has to be decided
    here rather than hoped for.
    """
    self_columns = [
        fk.parent.name for fk in table.foreign_keys if fk.column.table is table
    ]
    keys = list(table.primary_key.columns)
    if not self_columns or len(keys) != 1:
        return rows

    key = keys[0].name
    known = {str(row.get(key)) for row in rows}
    placed: set[str] = set()
    ordered: list[dict[str, Any]] = []
    waiting = rows
    while waiting:
        later: list[dict[str, Any]] = []
        for row in waiting:
            parents = [
                str(row[column])
                for column in self_columns
                if row.get(column) is not None
            ]
            # A parent that is not in this backup at all cannot be waited for.
            # It is a row the database will reject on its own terms, which is a
            # better error than hanging here deciding what to do about it.
            if all(parent in placed or parent not in known for parent in parents):
                ordered.append(row)
                placed.add(str(row.get(key)))
            else:
                later.append(row)
        if len(later) == len(waiting):
            # A cycle, or a row that is its own parent. Nothing here can fix
            # that; hand the rest to the database and let it say so plainly
            # rather than looping forever.
            ordered.extend(later)
            break
        waiting = later
    return ordered


async def load_database(session: AsyncSession, data: dict[str, Any]) -> dict[str, int]:
    """Replace every row in every table with the ones in the archive.

    Deleted in reverse dependency order and inserted in forward order, so
    foreign keys hold at every point. All inside the caller's transaction: a
    restore that fails halfway must leave the shop it was replacing intact,
    because the alternative is no shop at all.
    """
    tables = list(Base.metadata.sorted_tables)
    for table in reversed(tables):
        await session.execute(table.delete())

    counts: dict[str, int] = {}
    for table in tables:
        rows = data.get(table.name)
        if not rows:
            counts[table.name] = 0
            continue
        columns = {column.name: column for column in table.columns}
        prepared = []
        for row in rows:
            # Columns this build no longer has are dropped, and ones it has
            # gained take their defaults. That is what makes an older backup
            # loadable at all — and it is safe only because a *newer* backup is
            # refused before we get here.
            prepared.append(
                {
                    name: _decode(value, columns[name])
                    for name, value in row.items()
                    if name in columns
                }
            )
        prepared = parents_first(table, prepared)
        for start in range(0, len(prepared), BATCH):
            await session.execute(table.insert(), prepared[start : start + BATCH])
        counts[table.name] = len(prepared)
    return counts


# --------------------------------------------------------------------------
# The data directory
# --------------------------------------------------------------------------


def data_files(root: Path | None = None) -> list[Path]:
    """The files worth carrying: the secret key, the certificate, the tunnel."""
    root = root or get_config().data_dir
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in SKIP_NAMES or path.name.endswith(SKIP_SUFFIXES):
            continue
        found.append(path)
    return found


# --------------------------------------------------------------------------
# The archive
# --------------------------------------------------------------------------


def _key_from(passphrase: str, salt: bytes) -> Fernet:
    """A key from a passphrase, deliberately slow to derive.

    scrypt rather than a plain hash because the thing being protected is every
    credential the shop has, and the file will sit in somebody's cloud drive
    for years. The cost parameters are the usual interactive ones: about a
    tenth of a second here, and a very long time for anybody guessing.
    """
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))


async def create(
    session: AsyncSession, *, passphrase: str = "", data_dir: Path | None = None
) -> tuple[Path, dict[str, Any]]:
    """Write the whole shop to a file, and return it with its manifest.

    Built on disk rather than in memory: the label PDFs alone can run to tens
    of megabytes, and a backup is not worth an out-of-memory kill.
    """
    config = get_config()
    root = data_dir or config.data_dir
    database = await dump_database(session)
    revision = await current_revision(session)
    files = data_files(root)

    manifest = {
        "format": FORMAT_VERSION,
        "created_at": datetime.now().astimezone().isoformat(),
        "alembic_revision": revision,
        "git_sha": config.git_sha,
        "built_at": config.built_at,
        "tables": {name: len(rows) for name, rows in database.items()},
        "data_files": [str(path.relative_to(root)) for path in files],
        # Where the key came from on the machine that made this. When it came
        # from the environment there is no key file to carry, so the archive is
        # only half a backup — and the half it is missing is the one that makes
        # the credentials readable. Recorded so a restore can say so rather
        # than leaving every integration to fail mysteriously afterwards.
        "secret_key_source": config.secret_key_source,
        "carries_secret_key": any(
            path.name == SECRET_KEY_FILENAME for path in files
        ),
        # Said out loud in the file itself, because somebody will open it.
        "contains_secrets": True,
        "encrypted": bool(passphrase),
    }

    handle = tempfile.NamedTemporaryFile(prefix="printflow-backup-", suffix=".tmp", delete=False)
    handle.close()
    plain = Path(handle.name)
    try:
        with tarfile.open(plain, "w:gz") as archive:
            _add_bytes(archive, MANIFEST_NAME, json.dumps(manifest, indent=2).encode("utf-8"))
            _add_bytes(
                archive,
                DATABASE_NAME,
                json.dumps(database, separators=(",", ":")).encode("utf-8"),
            )
            for path in files:
                archive.add(path, arcname=DATA_PREFIX + str(path.relative_to(root)))
        if not passphrase:
            return plain, manifest

        salt = os.urandom(SALT_BYTES)
        token = _key_from(passphrase, salt).encrypt(plain.read_bytes())
        sealed = plain.with_suffix(".sealed")
        with open(sealed, "wb") as out:
            out.write(MAGIC)
            out.write(salt)
            out.write(token)
        plain.unlink(missing_ok=True)
        return sealed, manifest
    except Exception:
        plain.unlink(missing_ok=True)
        raise


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(payload))


def unseal(raw: bytes, passphrase: str) -> bytes:
    """The tar out of the uploaded file, whether or not it was encrypted."""
    if not raw.startswith(MAGIC):
        if passphrase:
            raise BackupError(
                "This backup is not encrypted, so it needs no passphrase. "
                "Clear the passphrase and try again."
            )
        return raw
    if not passphrase:
        raise BackupError("This backup is encrypted. Enter the passphrase it was made with.")
    salt = raw[len(MAGIC) : len(MAGIC) + SALT_BYTES]
    try:
        return _key_from(passphrase, salt).decrypt(raw[len(MAGIC) + SALT_BYTES :])
    except InvalidToken as exc:
        raise BackupError(
            "That passphrase does not open this backup. There is no way to "
            "recover the contents without it."
        ) from exc


def read_archive(raw: bytes, passphrase: str = "") -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    """Open an uploaded file: its manifest, its rows, and its data files."""
    body = unseal(raw, passphrase)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    except tarfile.TarError as exc:
        raise BackupError(
            "This file is not a PrintFlow backup — it could not be opened as one."
        ) from exc

    manifest: dict[str, Any] = {}
    database: dict[str, Any] = {}
    files: dict[str, bytes] = {}
    with archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = member.name.lstrip("./")
            handle = archive.extractfile(member)
            if handle is None:
                continue
            payload = handle.read()
            if name == MANIFEST_NAME:
                manifest = json.loads(payload)
            elif name == DATABASE_NAME:
                database = json.loads(payload)
            elif name.startswith(DATA_PREFIX):
                relative = name[len(DATA_PREFIX) :]
                # A member named ../../etc/passwd would otherwise be written
                # wherever it liked. The archive is one PrintFlow wrote, but it
                # arrives as an upload and is treated as one.
                if relative and not _escapes(relative):
                    files[relative] = payload

    if not manifest:
        raise BackupError("This file has no PrintFlow manifest, so it is not a backup.")
    return manifest, database, files


def _escapes(relative: str) -> bool:
    path = Path(relative)
    return path.is_absolute() or ".." in path.parts


def check(manifest: dict[str, Any]) -> None:
    """Refuse a file this build cannot honestly restore."""
    fmt = manifest.get("format")
    if not isinstance(fmt, int) or fmt > FORMAT_VERSION:
        raise BackupError(
            f"This backup is format {fmt}, and this PrintFlow reads up to "
            f"{FORMAT_VERSION}. Update PrintFlow, then restore it."
        )
    revision = manifest.get("alembic_revision")
    known = known_revisions()
    if revision and known and revision not in known:
        raise BackupError(
            f"This backup was taken by a newer PrintFlow (database revision "
            f"{revision}, which this build does not know). Restoring it would "
            f"silently drop whatever that version added. Update PrintFlow to at "
            f"least that version and restore it there."
        )


def write_data_files(files: dict[str, bytes], root: Path | None = None) -> list[str]:
    """Put the secret key and the certificate back where they live.

    Written last, and only once the database has been replaced: the key is what
    makes the restored credentials readable, so a half-restore that swapped the
    key but not the rows would leave a working shop unable to decrypt its own
    credentials.
    """
    root = (root or get_config().data_dir).resolve()
    written: list[str] = []
    for relative, payload in sorted(files.items()):
        target = (root / relative).resolve()
        if not str(target).startswith(str(root)):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Through a temporary file so a crash mid-write cannot leave a
        # truncated secret key, which would lock the shop out of its own
        # credentials with no way back. Named by appending rather than by
        # with_suffix, which replaces the last extension and would turn
        # `chain.pem` into `chain.restoring`.
        staged = target.parent / (target.name + ".restoring")
        staged.write_bytes(payload)
        try:
            os.chmod(staged, 0o600)
        except OSError:
            # Best effort. A bind-mounted data directory on a Windows or macOS
            # host, and some network filesystems, refuse chmod outright — and
            # failing a whole restore over file permissions would be losing the
            # shop to protect the tidiness of one mode bit.
            log.warning("Could not set permissions on %s", target)
        shutil.move(str(staged), str(target))
        written.append(relative)
    # The secret key has probably just changed underneath a cached Config.
    reset_config_cache()
    return written


@contextmanager
def _stage(what: str):
    """Name the step, so a failure inside it is not just a stack trace.

    BackupError already carries a sentence written for the operator, so it goes
    through untouched. Anything else is a surprise — a driver refusing a value,
    a read-only volume — and gets the step's name attached, which is the
    difference between "restore failed" and "restore failed while writing the
    data directory", one of which can be acted on.
    """
    try:
        yield
    except BackupError:
        raise
    except Exception as exc:
        raise RestoreFailed(what, exc) from exc


class RestoreFailed(RuntimeError):
    """An unexpected failure, with the step it happened in."""

    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")
        self.stage = stage
        self.cause = cause


async def restore(
    session: AsyncSession,
    raw: bytes,
    *,
    passphrase: str = "",
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Replace this shop with the one in the file.

    The caller commits. Everything the database does is one transaction, so a
    restore that fails leaves what was there before — which matters most
    precisely when somebody is restoring because things have already gone
    wrong once today.
    """
    # Each stage named, because "Internal Server Error" is the same words
    # whether the file was corrupt, the database refused a row, or the data
    # volume is read-only — and those have three completely different fixes.
    # Whatever goes wrong, the operator gets told which third of this it was.
    with _stage("reading the backup file"):
        manifest, database, files = read_archive(raw, passphrase)
        check(manifest)
    with _stage("replacing the database"):
        counts = await load_database(session, database)
    with _stage("writing the data directory"):
        written = write_data_files(files, data_dir)
    log.warning(
        "Restored a backup taken %s: %s rows across %s tables, %s data files",
        manifest.get("created_at"),
        sum(counts.values()),
        len(counts),
        len(written),
    )
    return {
        "manifest": manifest,
        "tables": counts,
        "rows": sum(counts.values()),
        "data_files": written,
        # The key on disk is only used when SECRET_KEY is not set in the
        # environment. If it is, the restored credentials were encrypted with a
        # different key and will not open — worth saying now rather than
        # letting every integration fail mysteriously afterwards.
        "secret_key_from_environment": get_config().secret_key_source == "environment",
    }


def preview(raw: bytes, passphrase: str = "") -> dict[str, Any]:
    """What is in this file, without restoring any of it."""
    manifest, database, files = read_archive(raw, passphrase)
    problem: str | None = None
    try:
        check(manifest)
    except BackupError as exc:
        problem = str(exc)
    return {
        "manifest": manifest,
        "rows": sum(len(rows) for rows in database.values()),
        "tables": {name: len(rows) for name, rows in database.items()},
        "data_files": sorted(files),
        "problem": problem,
    }


def suggested_name(encrypted: bool) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    return f"printflow-{stamp}.{'pfbackup' if encrypted else 'tar.gz'}"


def iter_file(path: Path, chunk: int = 64 * 1024) -> Iterable[bytes]:
    """Stream the archive out and delete it once it has gone."""
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    return
                yield block
    finally:
        path.unlink(missing_ok=True)
