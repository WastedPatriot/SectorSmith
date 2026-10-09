"""Secrets for integrations, encrypted for the current Windows user with DPAPI (CryptProtectData).

Nothing in 1.4 stores a secret yet. This is here so a future API key never lands in settings.json as plain text.
On other systems there is no DPAPI, so saving a secret is refused rather than written in the clear."""
from __future__ import annotations

import base64
import json
import sys

from ..util import app_dir

ENTROPY = b"SectorSmith integrations"
STORE = "secrets.json"


class SecretsUnavailable(RuntimeError):
    pass


def available() -> bool:
    return sys.platform == "win32"


def _blob_type():
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    return DATA_BLOB


def _crypt(data: bytes, protect: bool) -> bytes:
    if not available():
        raise SecretsUnavailable("Secrets can only be stored on Windows (DPAPI). Nothing was saved.")
    import ctypes
    DATA_BLOB = _blob_type()

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf
    src, _keep = blob(data)
    ent, _keep2 = blob(ENTROPY)
    out = DATA_BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 0x1 CRYPTPROTECT_UI_FORBIDDEN: never show a prompt
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
        raise SecretsUnavailable(f"DPAPI failed ({ctypes.GetLastError()}).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    return _crypt(data, True)


def unprotect(data: bytes) -> bytes:
    return _crypt(data, False)


def _path():
    return app_dir() / STORE


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_secret(name: str, value: str):
    """Encrypt and keep a secret. Raises SecretsUnavailable (and writes nothing) where DPAPI isn't available."""
    enc = protect(value.encode("utf-8"))  # raises before anything touches the disk
    d = _load()
    d[name] = base64.b64encode(enc).decode("ascii")
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def load_secret(name: str) -> str | None:
    raw = _load().get(name)
    if not raw or not available():
        return None
    try:
        return unprotect(base64.b64decode(raw)).decode("utf-8")
    except (ValueError, SecretsUnavailable):
        return None


def forget_secret(name: str):
    d = _load()
    if d.pop(name, None) is not None:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
