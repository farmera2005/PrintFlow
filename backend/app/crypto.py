"""Encryption of integration credentials at rest.

A Fernet key is derived from SECRET_KEY with HKDF-SHA256 so the operator only
ever has to manage one secret. Rotating SECRET_KEY makes stored credentials
undecryptable — callers get a clear error and the Settings screen prompts a
re-connect rather than crashing the app.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_config

_INFO = b"printflow-credential-encryption-v1"


class CredentialDecryptionError(RuntimeError):
    """Stored payload could not be decrypted with the current SECRET_KEY."""


def _fernet() -> Fernet:
    kdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO)
    key = kdf.derive(get_config().secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_json(payload: dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def decrypt_json(blob: bytes | memoryview | None) -> dict[str, Any]:
    if not blob:
        return {}
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    try:
        return json.loads(_fernet().decrypt(bytes(blob)).decode("utf-8"))
    except InvalidToken as exc:  # pragma: no cover - depends on operator action
        raise CredentialDecryptionError(
            "Stored credentials cannot be decrypted. This happens when SECRET_KEY "
            "changes. Reconnect the integration from Settings."
        ) from exc
