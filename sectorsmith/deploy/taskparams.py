"""Typed parameters for maintenance tasks: what a task asks for (text, number, yes/no, a choice or a secret), the
values a deployment gives it, and the script text that hands them over.

Values never get pasted into a script as raw text: each one is checked against its type and written as a quoted
literal (PowerShell single quotes, or sh quoting), and parameter names must be plain identifiers. Secrets are kept
encrypted with Windows DPAPI in the library (secrets.json) and only decrypted for the run; outputs and logs show
them masked."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import uuid

TYPES = {"text": "Text", "number": "Number", "bool": "Yes or no", "choice": "Choice", "secret": "Secret"}
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MASK = "********"
TRUE, FALSE = ("true", "yes", "1", "on", "$true"), ("false", "no", "0", "off", "$false", "")
SECRETS = "secrets.json"


def check_name(name: str) -> str:
    if not NAME.match(name or ""):
        raise ValueError(f"'{name}' can't be a parameter name: use letters, digits and _, starting with a letter.")
    if name.lower().startswith("sectorsmith"):
        raise ValueError(f"'{name}' is reserved: names starting with SectorSmith are set by SectorSmith itself.")
    return name


def tidy_parameters(params: list) -> list:
    """Checks a task's parameter list (names, types, choices, defaults). Raises ValueError."""
    out, seen = [], set()
    for p in params or []:
        name = check_name((p.get("name") or "").strip())
        if name.lower() in seen:
            raise ValueError(f"There are two parameters called {name}.")
        seen.add(name.lower())
        kind = p.get("type") or "text"
        if kind not in TYPES:
            raise ValueError(f"{name}: unknown type {kind}.")
        q = {"name": name, "type": kind, "label": (p.get("label") or "").strip() or name,
             "help": (p.get("help") or "").strip(), "required": bool(p.get("required"))}
        if kind == "choice":
            q["choices"] = [c.strip() for c in (p.get("choices") or []) if str(c).strip()]
            if not q["choices"]:
                raise ValueError(f"{name}: a choice needs at least one option.")
        default = p.get("default", "")
        if kind == "secret":
            default = ""  # a secret's value only ever comes from a deployment, encrypted
        elif default not in ("", None):
            default = coerce(q, default)
        q["default"] = default if default is not None else ""
        out.append(q)
    return out


def coerce(p: dict, value):
    """One value checked against its parameter's type. Raises ValueError with a plain message."""
    kind, name = p.get("type", "text"), p.get("label") or p.get("name")
    if kind == "number":
        text = str(value).strip()
        if not re.fullmatch(r"-?\d+(\.\d+)?", text):
            raise ValueError(f"{name} should be a number, not '{text}'.")
        return int(text) if "." not in text else float(text)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE:
            return True
        if text in FALSE:
            return False
        raise ValueError(f"{name} should be yes or no, not '{value}'.")
    if kind == "choice":
        text = str(value)
        if text not in p.get("choices", []):
            raise ValueError(f"{name} should be one of {', '.join(p.get('choices', []))}, not '{text}'.")
        return text
    if kind == "secret":
        if isinstance(value, dict) and value.get("secret"):
            return {"secret": str(value["secret"])}
        raise ValueError(f"{name} is a secret, so it has to be stored encrypted first.")
    return str(value)


def values_for(parameters: list, legacy: dict, given: dict) -> dict:
    """The values a run uses: the task's defaults (and its older untyped params), then the deployment's values.
    Typed values are checked; a required one that's missing raises ValueError."""
    by = {p["name"]: p for p in parameters or []}
    out = {k: v for k, v in (legacy or {}).items() if k not in by}
    for p in parameters or []:
        if p.get("default") not in ("", None):
            out[p["name"]] = p["default"]
    for k, v in (given or {}).items():
        if k in by:
            if v in ("", None) and by[k]["type"] != "bool":
                out.pop(k, None)
                continue
            out[k] = coerce(by[k], v)
        else:
            out[k] = v
    for p in parameters or []:
        if p.get("required") and out.get(p["name"]) in ("", None):
            raise ValueError(f"{p.get('label') or p['name']} needs a value.")
    return out


# ---------------------------------------------------------------------------- script text
def ps_literal(v) -> str:
    if isinstance(v, bool):
        return "$true" if v else "$false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def sh_literal(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return shlex.quote(str(v))


def header(values: dict, language: str, context: dict | None = None) -> str:
    """Variable lines that go above a task script. context: SectorSmith's own (SectorSmithClient, ...)."""
    lines = []
    lit = ps_literal if language == "powershell" else sh_literal
    for k, v in list((context or {}).items()) + list(values.items()):
        if not k.startswith("SectorSmith"):
            check_name(k)
        elif not NAME.match(k):
            raise ValueError(f"bad variable name {k}")
        lines.append(f"${k} = {lit(v)}" if language == "powershell" else f"{k}={lit(v)}")
    return "\n".join(lines) + ("\n" if lines else "")


def render(script: str, values: dict, language: str = "powershell", context: dict | None = None) -> str:
    """The script with its parameters set at the top. Secrets must be decrypted already (see reveal)."""
    if any(isinstance(v, dict) for v in values.values()):
        raise ValueError("a secret wasn't decrypted before the script was built")
    return header(values, language, context) + (script or "")


def secret_values(values: dict) -> list[str]:
    return [str(v) for v in values.values() if isinstance(v, Revealed)]


class Revealed(str):
    """A decrypted secret: a plain string that masks itself in repr and remembers it's secret."""

    def __repr__(self):
        return MASK


def mask(text: str, secrets) -> str:
    for s in sorted({x for x in secrets if x}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


# ---------------------------------------------------------------------------- secrets, encrypted with DPAPI
def _secrets_path(root):
    return os.path.join(root, SECRETS)


def _load(root) -> dict:
    try:
        with open(_secrets_path(root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_secret(root: str, value: str, ref: str | None = None) -> dict:
    """Encrypts a value for this Windows user and keeps it in the library. Returns {"secret": ref}. Raises
    dpapi.SecretsUnavailable (and writes nothing) where DPAPI isn't available."""
    from ..integrations import dpapi
    enc = dpapi.protect(str(value).encode("utf-8"))
    ref = ref or uuid.uuid4().hex[:16]
    d = _load(root)
    d[ref] = base64.b64encode(enc).decode("ascii")
    tmp = _secrets_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, _secrets_path(root))
    return {"secret": ref}


def load_secret(root: str, ref: str) -> str:
    """The decrypted value. Raises ValueError when it's gone or can't be decrypted here."""
    from ..integrations import dpapi
    raw = _load(root).get(ref)
    if not raw:
        raise ValueError("a stored secret is missing: enter it again on the deployment")
    try:
        return dpapi.unprotect(base64.b64decode(raw)).decode("utf-8")
    except (ValueError, dpapi.SecretsUnavailable) as e:
        raise ValueError(f"a stored secret can't be decrypted on this PC or by this user ({e})") from None


def forget_secret(root: str, ref: str):
    d = _load(root)
    if d.pop(ref, None) is not None:
        with open(_secrets_path(root), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)


def reveal(root: str, values: dict) -> dict:
    """Values with secrets decrypted (as Revealed strings, so callers can mask them in output)."""
    return {k: Revealed(load_secret(root, v["secret"])) if isinstance(v, dict) and v.get("secret") else v
            for k, v in values.items()}


def shown(p: dict, value) -> str:
    """A value as the UI shows it: secrets masked."""
    if p.get("type") == "secret" or isinstance(value, dict):
        return MASK if value else "not set"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value) if value not in ("", None) else "-"
