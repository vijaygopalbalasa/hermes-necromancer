"""Re-verify a receipt later, anywhere: CONFIRMED, MISMATCH or SUPERSEDED."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .canon import hash_file
from .lock import LockError, drift, find_root, lock_hash, read_lock
from .receipts import RECEIPT_KIND, SourcesError, receipt_hash, source_files
from .runner import run_contracts


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


def verify_receipt(receipt_file, cwd=None, budget_s: float | None = None) -> VerifyResult:
    """Check the receipt's own hash, re-hash every file it pins, look for files added since,
    then re-run the frozen contracts and compare each result with the one recorded."""
    path = Path(receipt_file).expanduser()
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    try:
        rc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _mismatch(f"The receipt cannot be read: {exc}")
    if not isinstance(rc, dict) or rc.get("kind") != RECEIPT_KIND or not rc.get("contracts"):
        return _mismatch("This file is not a Contract Necromancer receipt.")
    if receipt_hash(rc) != rc.get("receiptHash"):
        return _mismatch("The receipt was altered: its contents no longer match its receiptHash.")

    root = find_root(path.parent) or (find_root(cwd) if cwd else None)
    if root is None:
        return _mismatch("No .necro/lock.json in any folder above the receipt, so there are no frozen contracts to re-run.")
    try:
        lk = read_lock(root)
    except LockError as exc:
        return _mismatch(str(exc))

    d = drift(root, lk)
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
            if hash_file(root / f["path"]) != f["sha256"]:
                changed.add(f["path"])
    pinned = {s["path"]: s["sha256"] for s in rc.get("sources", [])}
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

    recorded = {c["id"]: c.get("result") for c in rc["contracts"]}
    fresh = {r.id: r for r in run_contracts(root, lk, budget_s=budget_s)}
    differ = []
    for cid in sorted(set(recorded) | set(fresh)):
        now = fresh[cid].status if cid in fresh else "missing"
        if recorded.get(cid) != now:
            differ.append(f"{cid} ({recorded.get(cid, 'absent')} then, {now} now)")
    if differ:
        return _mismatch(f"Contracts changed: {', '.join(differ)}.", contracts=differ)
    return VerifyResult("CONFIRMED", f"CONFIRMED {rc['receiptHash']}.")
