"""Licences: a client's licence key or licence file for a package, handed to its installer at install time.

Keys and files are encrypted with Windows DPAPI in the library (refused where DPAPI isn't available). At install
time the key replaces {licence_key} in the install command and the file is copied next to the installer and its
path replaces {licence_file}; both also arrive as the environment variables SECTORSMITH_LICENCE_KEY and
SECTORSMITH_LICENCE_FILE. Logs, sessions and the UI only ever show them masked."""
from __future__ import annotations

import base64
import os
import re

from . import taskparams

MAX_FILE = 4 * 1024 * 1024
ENV_KEY, ENV_FILE = "SECTORSMITH_LICENCE_KEY", "SECTORSMITH_LICENCE_FILE"


def _folder(store, lic_id):
    return os.path.join(store.root, "licences", lic_id)


def masked(lic) -> str:
    """'Key ending 7Q2X' or 'File setup.lic' for the tables; never the whole key."""
    if lic.kind == "file":
        return f"File {lic.file_name}" if lic.file_name else "File"
    tail = re.sub(r"[^A-Za-z0-9]", "", lic.hint or "")[-4:]
    return f"Key ending {tail}" if tail else "Key"


def save(store, lic, key: str | None = None, file_path: str | None = None):
    """Store a licence with its key or file, encrypted. Raises ValueError, or dpapi.SecretsUnavailable (and keeps
    nothing) where DPAPI isn't available."""
    from ..integrations import dpapi
    if not store.get("packages", lic.package_id):
        raise ValueError("Pick the package this licence is for.")
    if lic.client_id and not store.get("clients", lic.client_id):
        raise ValueError("That client isn't in the library any more.")
    other = next((x for x in store.licences if x.id != lic.id and x.client_id == lic.client_id
                  and x.package_id == lic.package_id), None)
    if other is not None:
        raise ValueError("This client already has a licence for that package. Edit that one instead.")
    if lic.kind == "key":
        if key:
            ref = lic.secret.get("secret") if isinstance(lic.secret, dict) else None
            lic.secret = taskparams.save_secret(store.root, key.strip(), ref=ref)
            lic.hint = key.strip()[-4:]
        elif not lic.secret:
            raise ValueError("Type the licence key.")
    else:
        if file_path:
            if os.path.getsize(file_path) > MAX_FILE:
                raise ValueError("Licence files over 4 MB aren't supported.")
            with open(file_path, "rb") as f:
                enc = dpapi.protect(f.read())
            folder = _folder(store, lic.id)
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "licence.bin"), "wb") as f:
                f.write(base64.b64encode(enc))
            lic.file_name = re.sub(r"[^\w.+-]+", "_", os.path.basename(file_path))
        elif not lic.file_name:
            raise ValueError("Pick the licence file.")
    return store.upsert("licences", lic)


def key_of(store, lic) -> str:
    if not isinstance(lic.secret, dict) or not lic.secret.get("secret"):
        raise ValueError("this licence has no key stored")
    return taskparams.Revealed(taskparams.load_secret(store.root, lic.secret["secret"]))


def file_of(store, lic) -> bytes:
    from ..integrations import dpapi
    try:
        with open(os.path.join(_folder(store, lic.id), "licence.bin"), "rb") as f:
            return dpapi.unprotect(base64.b64decode(f.read()))
    except (OSError, ValueError, dpapi.SecretsUnavailable) as e:
        raise ValueError(f"the licence file can't be read here ({e})") from None


def forget(store, lic):
    if isinstance(lic.secret, dict) and lic.secret.get("secret"):
        taskparams.forget_secret(store.root, lic.secret["secret"])
    import shutil
    shutil.rmtree(_folder(store, lic.id), ignore_errors=True)


def for_host(store, package_id: str, hostname: str):
    """The licence a PC gets for a package: its client's own, else one for every client."""
    clients = {c.id for c in store.clients_of(hostname)}
    mine = [x for x in store.licences if x.package_id == package_id]
    return next((x for x in mine if x.client_id in clients), None) or \
        next((x for x in mine if not x.client_id), None)
