"""The person-facing commands, shared by /necro, `hermes necro` and `python3 necro`."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from .canon import utc_now
from .gate import evaluate
from .lock import LOCK_REL, LockError, drift, find_root, freeze, lock_hash, lock_path, read_lock
from .receipts import list_receipts
from .spec import SPEC_NAME, SpecError, load_spec
from .verify import verify_receipt

PERSON_CHECK_BUDGET = 900.0

USAGE = """Contract Necromancer: Hermes cannot call coding work done while a frozen acceptance contract fails.

  status            what is frozen here, and the latest receipt
  freeze            a person freezes the contracts in necro.toml (by hash)
  approve           accept a lock someone else froze and committed, after reading it
  unfreeze          remove the lock so contracts can be edited (receipts stay)
  check             run every frozen contract now; write a receipt if all pass
  verify [receipt]  re-check a receipt (default: the newest): CONFIRMED, MISMATCH or SUPERSEDED
  receipts          list receipts, newest first
  demo              run the built-in story: planted bug, refusal, fix, receipt"""


class Approvals:
    """For each project folder, the one lock hash a person accepted there (by freezing it or with
    /necro approve). Kept in Hermes's per-plugin state, outside the project. Freezing again replaces
    the folder's approval, so rolling back to an older lock is refused; unfreezing removes it."""

    KEY = "approved_roots"
    LIMIT = 500

    def __init__(self, store=None):
        self.store = store

    def _data(self) -> dict:
        data = self.store.get(self.KEY, {}) if self.store is not None else {}
        return data if isinstance(data, dict) else {}

    def mapping(self):
        """{folder: lock hash}, or None when there is no state store (standalone use: no check)."""
        if self.store is None:
            return None
        return {root: entry.get("lock") for root, entry in self._data().items() if isinstance(entry, dict)}

    def approve(self, root: Path, digest: str) -> None:
        if self.store is None:
            return
        data = self._data()
        data[str(Path(root).resolve())] = {"lock": digest, "at": utc_now()}
        if len(data) > self.LIMIT:
            data = dict(sorted(data.items(), key=lambda kv: kv[1].get("at", ""))[-self.LIMIT:])
        self.store.set(self.KEY, data)

    def revoke(self, root: Path) -> None:
        if self.store is None:
            return
        data = self._data()
        data.pop(str(Path(root).resolve()), None)
        self.store.set(self.KEY, data)

    def approved_here(self, root: Path, digest: str) -> bool:
        m = self.mapping()
        return m is None or m.get(str(Path(root).resolve())) == digest


def spec_root(cwd) -> Path | None:
    p = Path(cwd).expanduser().resolve()
    for d in (p, *p.parents):
        if (d / SPEC_NAME).is_file() or (d / LOCK_REL).is_file():
            return d
    return None


def _contract_lines(lk: dict) -> list:
    out = []
    for c in lk["contracts"]:
        files = ", ".join(f["path"] for f in c["files"])
        out.append(f"  {c['id']:<6} {c.get('says') or '(no description)'}")
        out.append(f"         run: {c['run']}")
        out.append(f"         frozen: {files}")
    return out


def status(cwd, approvals: Approvals) -> tuple:
    root = find_root(cwd)
    if root is None:
        draft = spec_root(cwd)
        if draft and (draft / SPEC_NAME).is_file():
            try:
                sp = load_spec(draft)
                listed = "\n".join(f"  {c.id:<6} {c.says or '(no description)'}" for c in sp.contracts)
                return (f"Contracts in {draft / SPEC_NAME} are not frozen yet:\n{listed}\n"
                        "Read them, then run /necro freeze (or `hermes necro freeze`)."), 0
            except SpecError as exc:
                return f"{draft / SPEC_NAME} is not ready to freeze: {exc}", 1
        return (f"Nothing is frozen at {Path(cwd).resolve()} (no {LOCK_REL} here or above). Write {SPEC_NAME} with "
                "your acceptance contracts, then run /necro freeze."), 0
    try:
        lk = read_lock(root)
    except LockError as exc:
        return f"{root}: {exc}", 1
    digest = lock_hash(lk)
    approved = "yes" if approvals.approved_here(root, digest) else "no (run /necro approve after reading it)"
    lines = [f"Frozen contracts in {root} (lock {digest[:12]}, frozen {lk.get('frozenAt', '?')}, approved here: {approved}):"]
    lines += _contract_lines(lk)
    d = drift(root, lk)
    for reason in d.spec:
        lines.append(f"  ! {reason}")
    for cid, reasons in d.contracts.items():
        lines.append(f"  ! {cid}: {'; '.join(reasons)}")
    latest = list_receipts(root)
    lines.append(f"Latest receipt: {os.path.relpath(latest[0], root)}" if latest else "No receipt yet.")
    return "\n".join(lines), 0


def do_freeze(cwd, approvals: Approvals) -> tuple:
    root = spec_root(cwd)
    if root is None or not (root / SPEC_NAME).is_file():
        return f"Could not freeze: no {SPEC_NAME} in {Path(cwd).resolve()} or above.", 1
    try:
        lk = freeze(root)
    except SpecError as exc:
        return f"Could not freeze: {exc}", 1
    digest = lock_hash(lk)
    approvals.approve(root, digest)
    files = sum(len(c["files"]) for c in lk["contracts"])
    lines = [f"Froze {len(lk['contracts'])} contracts ({files} files) in {root}, lock {digest[:12]}:"]
    lines += _contract_lines(lk)
    lines.append("From now on Hermes cannot finish coding work here while any of these fails, and edits to the "
                 f"frozen files are blocked. Commit {LOCK_REL} with the contracts.")
    return "\n".join(lines), 0


def do_unfreeze(cwd, approvals: Approvals) -> tuple:
    root = find_root(cwd)
    if root is None:
        return "Nothing is frozen here.", 0
    lock_path(root).unlink()
    approvals.revoke(root)
    return (f"Unfroze {root}. The contracts can be edited now; receipts stay in .necro/receipts. "
            "Read the new contracts, then run /necro freeze again."), 0


def do_approve(cwd, approvals: Approvals) -> tuple:
    root = find_root(cwd)
    if root is None:
        return f"Nothing to approve: no {LOCK_REL} here or above.", 1
    try:
        lk = read_lock(root)
    except LockError as exc:
        return f"Could not approve: {exc}", 1
    d = drift(root, lk)
    if d.any():
        return "Could not approve: files differ from the lock: " + ", ".join(d.paths()) + ". Freeze again instead.", 1
    digest = lock_hash(lk)
    approvals.approve(root, digest)
    lines = [f"Approved lock {digest[:12]} for {root}. Hermes now gates done here on:"]
    lines += _contract_lines(lk)
    return "\n".join(lines), 0


def _result_line(r) -> str:
    says = f"  {r.says}" if r.says else ""
    extra = f" ({'; '.join(r.reasons)})" if r.reasons else ""
    return f"  {r.status.upper():<10} {r.id}{says}{extra}"


def do_check(cwd, approvals: Approvals, cache=None, budget_s: float = PERSON_CHECK_BUDGET) -> tuple:
    root = find_root(cwd)
    if root is None:
        return f"Nothing is frozen here (no {LOCK_REL}).", 1
    v = evaluate(root, budget_s=budget_s, approved=approvals.mapping(), cache=cache, trigger="check")
    passed = sum(1 for r in v.results if r.status == "pass")
    lines = [f"{passed} of {len(v.results)} frozen contracts pass in {root}." if v.results else f"Could not check {root}."]
    lines += [f"  ! {p}" for p in v.problems]
    lines += [_result_line(r) for r in v.results]
    for r in v.failing():
        tail = [ln for ln in (r.output_tail or "").strip().splitlines() if ln.strip()][-6:]
        if tail:
            lines.append(f"  --- end of {r.id} output ---")
            lines += [f"  {ln[:200]}" for ln in tail]
    if v.ok:
        lines.append(f"Receipt {v.receipt_hash[:12]}: {os.path.relpath(v.receipt_path, root)}")
    return "\n".join(lines), 0 if v.ok else 2


def do_verify(cwd, receipt: str | None = None) -> tuple:
    if receipt:
        target = receipt
    else:
        root = find_root(cwd)
        latest = list_receipts(root) if root else []
        if not latest:
            return "No receipt to verify here.", 1
        target = str(latest[0])
    v = verify_receipt(target, cwd=str(cwd))
    return v.message, 0 if v.ok else 1


def do_receipts(cwd) -> tuple:
    root = find_root(cwd)
    if root is None:
        return "Nothing is frozen here.", 0
    rows = []
    for path in list_receipts(root)[:15]:
        try:
            rc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append(f"  {rc.get('receiptHash', '?')[:12]}  {rc.get('createdAt', '?')}  {rc.get('trigger') or ''}  "
                    f"{os.path.relpath(path, root)}")
    return ("Receipts, newest first:\n" + "\n".join(rows)) if rows else "No receipts yet.", 0


PERSON_ONLY = {"freeze", "unfreeze", "approve"}


def in_agent_shell() -> bool:
    """Hermes exports HERMES_AGENT=true and AI_AGENT=hermes-agent into every command its terminal
    tool runs; other agents set AI_AGENT too."""
    return bool(os.environ.get("HERMES_AGENT") or os.environ.get("AI_AGENT"))


def dispatch(raw: str, cwd, approvals: Approvals, cache=None, from_shell: bool = False) -> tuple:
    try:
        words = shlex.split(raw or "")
    except ValueError:
        words = (raw or "").split()
    sub = words[0].lower() if words else "status"
    rest = words[1:]
    if from_shell and sub in PERSON_ONLY and in_agent_shell():
        return (f"Refused: `necro {sub}` is a person's command and this shell belongs to an AI agent "
                "(HERMES_AGENT or AI_AGENT is set). Run it in your own terminal, or type /necro "
                f"{sub} in Hermes."), 3
    if sub in ("status", "show"):
        return status(cwd, approvals)
    if sub == "freeze":
        return do_freeze(cwd, approvals)
    if sub == "unfreeze":
        return do_unfreeze(cwd, approvals)
    if sub == "approve":
        return do_approve(cwd, approvals)
    if sub == "check":
        return do_check(cwd, approvals, cache)
    if sub == "verify":
        return do_verify(cwd, rest[0] if rest else None)
    if sub == "receipts":
        return do_receipts(cwd)
    if sub == "demo":
        from .demo import run_demo
        return run_demo(), 0
    return USAGE, 0 if sub in ("help", "-h", "--help") else 1
