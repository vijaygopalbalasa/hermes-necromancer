"""Canonical JSON and SHA-256 helpers. Standard library only."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path


def canonical_json(value) -> str:
    """JSON with keys sorted at every level and no whitespace, so equal data hashes equally."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


class BudgetExceeded(RuntimeError):
    """Walking or hashing the project took longer than the time allowed."""


def check_deadline(deadline) -> None:
    """``deadline`` is a time.monotonic() value, or None for no limit."""
    if deadline is not None and time.monotonic() > deadline:
        raise BudgetExceeded("the time budget ran out while reading the project's files")


# Credential files are never opened, not even to hash them (catalog rule 11: a plugin never reads .env
# files). A pin records only that one is there, its size, modification time, inode and change time.
CREDENTIAL_NAMES = {".env", ".netrc", ".pgpass", ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
# A name Python or a test runner may import or run is never a credential, whatever else it matches.
CODE_SUFFIXES = (".py", ".pth")
CODE_PREFIXES = ("conftest", "sitecustomize", "usercustomize")


def is_credential(name: str) -> bool:
    """True for a credential file's name (.env, .env.*, keys, .netrc and the like), in any case."""
    name = name.lower()
    if name.endswith(CODE_SUFFIXES) or name.startswith(CODE_PREFIXES):
        return False
    return name in CREDENTIAL_NAMES or name.startswith(".env.") or name.endswith(CREDENTIAL_SUFFIXES)


def is_credential_path(path) -> bool:
    """True for a file with a credential name of its own that is not a link. A link, whatever its name or
    target, is hashed like any other file."""
    path = os.fspath(path)
    return is_credential(os.path.basename(path)) and not os.path.islink(path)


def _sha256(path, deadline=None) -> str | None:
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        digest = hashlib.sha256()
        with open(Path(path), "rb") as handle:
            for i, chunk in enumerate(iter(lambda: handle.read(1 << 20), b"")):
                digest.update(chunk)
                if i % 16 == 15:
                    check_deadline(deadline)
        return digest.hexdigest()
    except OSError:
        return None


def hash_file(path, deadline=None) -> str | None:
    """SHA-256 of a regular file's bytes (a link is followed), or None when it is missing, not a
    regular file (a FIFO, socket or device would block or never end), unreadable, or a credential
    file, which is never opened and so never matches a hash."""
    return None if is_credential_path(path) else _sha256(path, deadline)


def stat_pin(path) -> dict | None:
    """A credential file's pin, read without opening it: its size, modification time, inode and change
    time (touch cannot set a change time back). None when it is missing or not a regular file."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return {"size": st.st_size, "mtimeNs": st.st_mtime_ns, "ino": st.st_ino, "ctimeNs": st.st_ctime_ns}


def file_pin(path, deadline=None):
    """What a receipt pins for a file: the SHA-256 of its bytes or, for a credential file, which is
    never opened, {"size", "mtimeNs", "ino", "ctimeNs"}. None when it is missing."""
    return stat_pin(path) if is_credential_path(path) else _sha256(path, deadline)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, value) -> None:
    """Write pretty JSON next to the target, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
