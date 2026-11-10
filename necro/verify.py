"""Re-verify a receipt later, anywhere: CONFIRMED, MISMATCH or SUPERSEDED."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .canon import file_pin, hash_file, is_credential_path
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
    if is_credential_path(path):
        return _mismatch(f"{path.name} is a credential file, not a receipt, and Contract Necromancer never opens "
                         "credential files.")
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
    pins = {s["path"]: s for s in rc.get("sources", [])}
    local_only = []  # pins another checkout may lack: files git ignored, and credential files
    for rel, entry in pins.items():
        # file_pin decides by the file's name, whatever the receipt says, so a credential file is never
        # opened; its pin is its size and modification time.
        now = file_pin(root / rel)
        want = ({"size": entry.get("size"), "mtimeNs": entry.get("mtimeNs")} if entry.get("credential")
                else entry.get("sha256"))
        if now is None and (entry.get("ignored") or is_credential_path(root / rel)):
            local_only.append(rel)
        elif now != want:
            changed.add(rel)
    try:
        present = set(source_files(root, lk))
    except SourcesError as exc:
        return _mismatch(str(exc))
    unpinned = sorted(present - set(pins))
    # A file the receipt does not pin is added, whether git ignores it or not: an ignored unittest.py can
    # change a run. Only a credential file, which is never opened and which a checkout may well have of
    # its own, is noted instead.
    local_credentials = [rel for rel in unpinned if is_credential_path(root / rel)]
    added = [rel for rel in unpinned if rel not in local_credentials]
    if changed or added:
        parts = []
        if changed:
            parts.append(f"Files changed: {', '.join(sorted(changed))}.")
        if added:
            parts.append(f"Files added: {', '.join(added)}.")
        return _mismatch(" ".join(parts), sorted(changed) + added)
    notes = _notes(sorted(local_only), local_credentials)

    trusted = run or (isinstance(approved, dict) and approved.get(str(root)) == lock_hash(lk))
    if not trusted:
        return VerifyResult(
            "HASHES MATCH",
            f"HASHES MATCH {rc['receiptHash']}{notes}. The files are as the receipt says, but the contracts were not "
            "re-run, because this project's lock is not approved here. Read necro.toml, then run "
            "`necro verify --run` to re-run them.",
        )

    recorded = {c["id"]: c.get("result") for c in rc["contracts"]}
    before = {rel: file_pin(root / rel) for rel in present}
    fresh = {r.id: r for r in run_contracts(root, lk, budget_s=budget_s)}
    try:
        after_files = set(source_files(root, lk))
    except SourcesError as exc:
        return _mismatch(str(exc))
    touched = sorted(rel for rel in present | after_files if file_pin(root / rel) != before.get(rel))
    touched += sorted(f["path"] for c in rc["contracts"] for f in c.get("files", [])
                      if hash_file(root / f["path"]) != f.get("sha256"))
    if touched:
        return _mismatch(f"The contract run changed {', '.join(sorted(set(touched)))}; code under test must not change "
                         "project files (a person can declare outputs in necro.toml).", touched)
    differ = []
    for cid in sorted(set(recorded) | set(fresh)):
        now = fresh[cid].status if cid in fresh else "missing"
        if recorded.get(cid) != now:
            differ.append(f"{cid} ({recorded.get(cid, 'absent')} then, {now} now)")
    if differ:
        return _mismatch(f"Contracts changed: {', '.join(differ)}.", contracts=differ)
    return VerifyResult("CONFIRMED", f"CONFIRMED {rc['receiptHash']}{notes}.")


def _notes(local_only: list, local_credentials: list) -> str:
    """What differs in this checkout without being a mismatch by itself, for the verdict line."""
    parts = []
    if local_only:
        n = len(local_only)
        parts.append(f"{n} local-only file{'s' if n != 1 else ''} not present here: {', '.join(local_only)}")
    if local_credentials:
        n = len(local_credentials)
        parts.append(f"{n} local credential file{'s' if n != 1 else ''} not in the receipt: "
                     f"{', '.join(local_credentials)}")
    return f" ({'; '.join(parts)})" if parts else ""
