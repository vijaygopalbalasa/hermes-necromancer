"""Canonical JSON and SHA-256 helpers. Standard library only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def canonical_json(value) -> str:
    """JSON with keys sorted at every level and no whitespace, so equal data hashes equally."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def hash_file(path) -> str | None:
    """SHA-256 of a file's bytes, or None when it is missing or unreadable."""
    try:
        digest = hashlib.sha256()
        with open(Path(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, PermissionError):
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, value) -> None:
    """Write pretty JSON next to the target, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
