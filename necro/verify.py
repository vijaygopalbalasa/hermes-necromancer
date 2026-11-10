"""Re-verify a receipt later, anywhere: CONFIRMED, MISMATCH or SUPERSEDED."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .canon import hash_file
from .lock import LockError, build_lock, drift, find_root, lock_hash, read_lock
from .receipts import RECEIPT_KIND, SourcesError, receipt_hash, source_files
from .runner import run_contracts
from .spec import SpecError


@dataclass
class VerifyResult:
    verdict: str
    message: str
    changed: list = field(default_factory=list)
    contracts: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == "CONFIRMED"


def _mismatch(message: str, changed=(), contracts=()) -> VerifyResult:
    return VerifyResult("MISMATCH", f"MISMATCH. {message}", list(changed), list(contracts))


def _well_formed(rc) -> bool:
    if not isinstance(rc, dict) or rc.get("kind") != RECEIPT_KIND:
        return False
    if not isinstance(rc.get("lock"), dict) or not isinstance(rc["lock"].get("sha256"), str):
        return False
    contracts, sources = rc.get("contracts"), rc.get("sources", [])
    if not isinstance(contracts, list) or not contracts or not isinstance(sources, list):
        return False
    for c in contracts:
        if not (isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("files", []), list)):
            return False
        if not all(isinstance(f, dict) and isinstance(f.get("path"), str) for f in c.get("files", [])):
            return False
    if not all(isinstance(x, dict) and isinstance(x.get("path"), str) for x in sources):
        return False
    return isinstance(rc.get("outputs", []), list)


def verify_receipt(receipt_file, cwd=None, budget_s: float | None = None, approved=None,
                   run: bool = False) -> VerifyResult:
    """Check the receipt's own hash, re-hash every file it pins and look for files added since.
    Then, only when a person approved this lock for this folder (``approved`` maps folder to lock
    hash) or asked for it with ``run``, re-run the frozen contracts and compare each result with the
    one recorded. Without either, the commands of a project nobody here approved are never run: the
    answer is HASHES MATCH, which is not a confirmation."""
    path = Path(receipt_file).expanduser()
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    try:
        rc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _mismatch(f"The receipt cannot be read: {exc}")
    if not _well_formed(rc):
        return _mismatch("This file is not a well-formed Contract Necromancer receipt.")
    try:
        if receipt_hash(rc) != rc.get("receiptHash"):
            return _mismatch("The receipt was altered: its contents no longer match its receiptHash.")
    except (ValueError, RecursionError):
        return _mismatch("The receipt cannot be hashed.")

    root = find_root(path.parent) or (find_root(cwd) if cwd else None)
    if root is None:
        return _mismatch("No .necro/lock.json in any folder above the receipt, so there are no frozen contracts to re-run.")
    try:
        lk = read_lock(root)
    except LockError as exc:
        return _mismatch(str(exc))

    d = drift(root, lk)
    if not d.any():
        try:
            expected = build_lock(root)
        except SpecError as exc:
            return _mismatch(f"necro.toml cannot be read, so the lock cannot be checked against it: {exc}")
        if lock_hash(expected) != lock_hash(lk):
            return _mismatch("The lock .necro/lock.json does not match necro.toml and the frozen files, so it was "
                             "not written by freezing them. Check `git diff .necro/` and freeze again.")
    if lock_hash(lk) != rc["lock"]["sha256"]:
        if not d.any():
            return VerifyResult(
                "SUPERSEDED",
                "SUPERSEDED. The contracts were frozen again after this receipt was written, so it describes an "
                "earlier freeze, not today's contracts. Run `hermes necro check` for a current receipt.",
            )
        return _mismatch(f"The frozen contracts changed since the receipt. Files changed: {', '.join(d.paths())}.", d.paths())

    changed = set(d.paths())
    for c in rc["contracts"]:
        for f in c.get("files", []):
            if hash_file(root / f["path"]) != f.get("sha256"):
                changed.add(f["path"])
    pinned = {s["path"]: s.get("sha256") for s in rc.get("sources", [])}
    for rel, digest in pinned.items():
        if hash_file(root / rel) != digest:
            changed.add(rel)
    try:
        added = sorted(set(source_files(root, lk)) - set(pinned))
    except SourcesError as exc:
        return _mismatch(str(exc))
    if changed or added:
        parts = []
        if changed:
            parts.append(f"Files changed: {', '.join(sorted(changed))}.")
        if added:
            parts.append(f"Files added: {', '.join(added)}.")
        return _mismatch(" ".join(parts), sorted(changed) + added)

    trusted = run or (isinstance(approved, dict) and approved.get(str(root)) == lock_hash(lk))
    if not trusted:
        return VerifyResult(
            "HASHES MATCH",
            f"HASHES MATCH {rc['receiptHash']}. Every file is as the receipt says, but the contracts were not re-run, "
            "because this project's lock is not approved here. Read necro.toml, then run `necro verify --run` to "
            "re-run them.",
        )

    recorded = {c["id"]: c.get("result") for c in rc["contracts"]}
    fresh = {r.id: r for r in run_contracts(root, lk, budget_s=budget_s)}
    touched = sorted(rel for rel, digest in pinned.items() if hash_file(root / rel) != digest)
    touched += sorted(f["path"] for c in rc["contracts"] for f in c.get("files", [])
                      if hash_file(root / f["path"]) != f.get("sha256"))
    try:
        created = sorted(set(source_files(root, lk)) - set(pinned))
    except SourcesError as exc:
        return _mismatch(str(exc))
    if touched or created:
        return _mismatch(f"The contract run changed {', '.join(sorted(set(touched + created)))}; code under test must "
                         "not change project files (a person can declare outputs in necro.toml).", touched + created)
    differ = []
    for cid in sorted(set(recorded) | set(fresh)):
        now = fresh[cid].status if cid in fresh else "missing"
        if recorded.get(cid) != now:
            differ.append(f"{cid} ({recorded.get(cid, 'absent')} then, {now} now)")
    if differ:
        return _mismatch(f"Contracts changed: {', '.join(differ)}.", contracts=differ)
    return VerifyResult("CONFIRMED", f"CONFIRMED {rc['receiptHash']}.")
