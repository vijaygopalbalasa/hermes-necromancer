"""Freezing contracts by hash into .necro/lock.json, finding a frozen project, and spotting drift."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .canon import canonical_json, hash_file, sha256_text, utc_now, write_json_atomic
from .spec import SPEC_NAME, SpecError, expand, load_spec, walk_files

LOCK_REL = ".necro/lock.json"
LOCK_KIND = "hermes-necromancer/lock"


class LockError(ValueError):
    """.necro/lock.json exists but cannot be used."""


def lock_path(root) -> Path:
    return Path(root) / LOCK_REL


def build_lock(root, now: str | None = None, files=None) -> dict:
    """The lock that freezing the project now would write, without writing it."""
    root = Path(root).resolve()
    sp = load_spec(root)
    if files is None:
        files = walk_files(root)
    contracts = []
    for c in sp.contracts:
        frozen = expand(root, c.files, strict=True, where=f"contract {c.id}", files=files)
        contracts.append({
            "id": c.id,
            "says": c.says,
            "run": c.run,
            "timeout": c.timeout,
            "env": list(c.env),
            "patterns": list(c.files),
            "files": [{"path": f, "sha256": hash_file(root / f)} for f in frozen],
        })
    return {
        "kind": LOCK_KIND,
        "version": 1,
        "spec": {"path": SPEC_NAME, "sha256": hash_file(root / SPEC_NAME)},
        "contracts": contracts,
        "sources": sp.sources,
        "frozenAt": now or utc_now(),
    }


def freeze(root, now: str | None = None) -> dict:
    """Read necro.toml, hash every contract file and the spec itself, and write the lock.

    This is a person's act: they have read the contracts and agree they define done."""
    root = Path(root).resolve()
    lk = build_lock(root, now)
    write_json_atomic(lock_path(root), lk)
    return json.loads(json.dumps(lk))


def read_lock(root) -> dict | None:
    """The lock, None when the project has none, LockError when it is unusable."""
    path = lock_path(root)
    try:
        lk = json.loads(path.read_text(encoding="utf-8"))
        canonical_json(lk)  # numbers too large to print, and the like, fail here rather than later
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        raise LockError(f"{LOCK_REL} cannot be read: {str(exc)[:200]}") from None
    if not isinstance(lk, dict) or lk.get("kind") != LOCK_KIND:
        raise LockError(f"{LOCK_REL} is not a Contract Necromancer lock")
    contracts = lk.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise LockError(f"{LOCK_REL} lists no contracts")
    for c in contracts:
        if not (isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("run"), str)
                and isinstance(c.get("files"), list) and c["files"]):
            raise LockError(f"{LOCK_REL} has a malformed contract entry")
        for f in c["files"]:
            if not (isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str)):
                raise LockError(f"{LOCK_REL} has a malformed file entry in contract {c['id']}")
        timeout = c.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600):
            raise LockError(f"{LOCK_REL} has a bad timeout in contract {c['id']}")
        if not isinstance(c.get("env", []), list) or not isinstance(c.get("patterns", []), list):
            raise LockError(f"{LOCK_REL} has a malformed contract {c['id']}")
    if not isinstance(lk.get("spec"), dict):
        raise LockError(f"{LOCK_REL} does not pin {SPEC_NAME}")
    return lk


def lock_hash(lk: dict) -> str:
    """Identity of a freeze: everything in the lock except when it happened."""
    return sha256_text(canonical_json({k: v for k, v in lk.items() if k != "frozenAt"}))


def find_root(start) -> Path | None:
    """The nearest folder at or above ``start`` that holds .necro/lock.json."""
    try:
        p = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not p.is_dir():
        p = p.parent
    for d in (p, *p.parents):
        if (d / LOCK_REL).is_file():
            return d
    return None


def frozen_paths(lk: dict) -> set:
    """Project-relative paths a freeze pins: the spec and every contract file."""
    paths = {lk.get("spec", {}).get("path") or SPEC_NAME}
    for c in lk.get("contracts", []):
        for f in c.get("files", []):
            paths.add(f["path"])
    return paths


def frozen_patterns(lk: dict) -> list:
    out = []
    for c in lk.get("contracts", []):
        out.extend(c.get("patterns") or [f["path"] for f in c.get("files", [])])
    return out


@dataclass
class Drift:
    spec: list = field(default_factory=list)
    contracts: dict = field(default_factory=dict)

    def any(self) -> bool:
        return bool(self.spec or self.contracts)

    def paths(self) -> list:
        out = []
        for reasons in [self.spec, *self.contracts.values()]:
            for r in reasons:
                if ": " in r:
                    out.append(r.split(": ", 1)[1])
                elif r.endswith(" changed since freeze"):
                    out.append(r[: -len(" changed since freeze")])
        return sorted(set(out))


def drift(root, lk: dict, files=None, deadline=None) -> Drift:
    """What changed on disk since the freeze: the spec, and per contract its pinned files and any
    new file that matches one of its patterns."""
    root = Path(root).resolve()
    d = Drift()
    spec_entry = lk.get("spec") or {}
    spec_rel = spec_entry.get("path") or SPEC_NAME
    current = hash_file(root / spec_rel, deadline)
    if current is None:
        d.spec.append(f"missing: {spec_rel}")
    elif current != spec_entry.get("sha256"):
        d.spec.append(f"{spec_rel} changed since freeze")
    for c in lk.get("contracts", []):
        reasons = []
        pinned = {f["path"]: f["sha256"] for f in c.get("files", [])}
        for rel, digest in pinned.items():
            now = hash_file(root / rel, deadline)
            if now is None:
                reasons.append(f"missing: {rel}")
            elif now != digest:
                reasons.append(f"changed since freeze: {rel}")
        patterns = c.get("patterns") or list(pinned)
        if any(set("*?[") & set(pat) for pat in patterns) and files is None:
            files = walk_files(root, deadline)
        try:
            matched = expand(root, patterns, strict=False, where=f"contract {c['id']}", files=files)
        except SpecError as exc:
            reasons.append(f"cannot expand its files: {exc}")
            matched = []
        for rel in matched:
            if rel not in pinned:
                reasons.append(f"added after freeze: {rel}")
        if reasons:
            d.contracts[c["id"]] = reasons
    return d
