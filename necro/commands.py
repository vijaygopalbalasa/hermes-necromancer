"""The person-facing commands, shared by /necro, `hermes necro` and `python3 necro`."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path

from .canon import utc_now
from .gate import APPROVAL_TAMPERED, evaluate
from .lock import LOCK_REL, LockError, drift, find_root, freeze, lock_hash, lock_path, read_lock
from .receipts import list_receipts
from .spec import SPEC_NAME, SpecError, load_spec
from .verify import verify_receipt

PERSON_CHECK_BUDGET = 900.0

USAGE = """Contract Necromancer: Hermes cannot call coding work done while a frozen acceptance contract fails.
Freeze, approve and unfreeze with /necro inside Hermes; from a shell they refuse unless a person is clearly typing.

  status            what is frozen here, and the latest receipt
  freeze            a person freezes the contracts in necro.toml (by hash); best typed as /necro freeze
  approve           accept a lock someone else froze and committed, after reading it
  unfreeze          remove the lock so contracts can be edited (receipts stay)
  check             run every frozen contract now; write a receipt if all pass
  verify [receipt]  re-check a receipt (default: the newest): CONFIRMED, MISMATCH or SUPERSEDED;
                    a lock nobody approved here is checked by hash only, unless you add --run
  receipts          list receipts, newest first
  demo              run the built-in story: planted bug, refusal, fix, receipt"""


class Approvals:
    """For each project folder, the one lock hash a person accepted there (by /necro freeze or
    /necro approve), in Hermes's per-plugin state outside the project. Freezing again replaces the
    folder's approval, so rolling back to an older lock is refused; unfreezing removes it.

    Each record carries an HMAC made with a key kept in a file beside the store, so a record written
    or edited without necro (by hand, or by an agent's shell) fails its check and the folder is
    refused. A process running as the same user can read that key: this detects ordinary tampering;
    it does not stop a determined process."""

    KEY = "approved_roots"
    LIMIT = 500

    def __init__(self, store=None):
        self.store = store
        self.known: set = set()  # folders seen approved in this process, kept even if the store loses them
        try:
            self.key_path = Path(store.path).parent / "approvals.key" if store is not None else None
        except Exception:
            self.key_path = None
        self._memory_key = None

    def _data(self) -> dict:
        data = self.store.get(self.KEY, {}) if self.store is not None else {}
        return data if isinstance(data, dict) else {}

    def _key(self, create: bool = False):
        if self.key_path is None:
            if self._memory_key is None and create:
                self._memory_key = secrets.token_bytes(32)
            return self._memory_key
        try:
            return bytes.fromhex(self.key_path.read_text().strip())
        except (OSError, ValueError):
            if not create:
                return None
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._key(create=False)
        with os.fdopen(fd, "w") as handle:
            handle.write(key.hex())
        return key

    @staticmethod
    def _mac(key: bytes, root: str, digest: str, at: str) -> str:
        return hmac.new(key, f"{root}\n{digest}\n{at}".encode(), hashlib.sha256).hexdigest()

    def mapping(self):
        """{folder: lock hash, or gate.APPROVAL_TAMPERED when its record fails the check}, or None
        when there is no state store (standalone use: no approvals at all)."""
        if self.store is None:
            return None
        key = self._key()
        out = {}
        for root, entry in self._data().items():
            good = (isinstance(entry, dict) and key is not None and isinstance(entry.get("mac"), str)
                    and hmac.compare_digest(entry["mac"], self._mac(key, root, str(entry.get("lock")),
                                                                     str(entry.get("at")))))
            out[root] = entry.get("lock") if good else APPROVAL_TAMPERED
            self.known.add(root)
        return out

    def approve(self, root: Path, digest: str) -> None:
        if self.store is None:
            return
        root_s = str(Path(root).resolve())
        key = self._key(create=True)
        at = utc_now()
        data = self._data()
        data[root_s] = {"lock": digest, "at": at, "mac": self._mac(key, root_s, digest, at)}
        if len(data) > self.LIMIT:
            data = dict(sorted(data.items(), key=lambda kv: str(kv[1].get("at", "")))[-self.LIMIT:])
        self.store.set(self.KEY, data)
        self.known.add(root_s)

    def revoke(self, root: Path) -> None:
        root_s = str(Path(root).resolve())
        self.known.discard(root_s)
        if self.store is None:
            return
        data = self._data()
        data.pop(root_s, None)
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
                        "Read them, then type /necro freeze in Hermes."), 0
            except SpecError as exc:
                return f"{draft / SPEC_NAME} is not ready to freeze: {exc}", 1
        return (f"Nothing is frozen at {Path(cwd).resolve()} (no {LOCK_REL} here or above). Write {SPEC_NAME} with "
                "your acceptance contracts, then run /necro freeze."), 0
    try:
        lk = read_lock(root)
    except LockError as exc:
        return f"{root}: {exc}", 1
    digest = lock_hash(lk)
    record = (approvals.mapping() or {}).get(str(root)) if approvals.store is not None else digest
    if record == APPROVAL_TAMPERED:
        approved = "no: its approval record was changed outside necro (run /necro freeze again)"
    else:
        approved = "yes" if record == digest else "no (run /necro approve after reading it)"
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


def do_verify(cwd, receipt: str | None = None, approvals: Approvals | None = None, run: bool = False) -> tuple:
    if receipt:
        target = receipt
    else:
        root = find_root(cwd)
        latest = list_receipts(root) if root else []
        if not latest:
            return "No receipt to verify here.", 1
        target = str(latest[0])
    v = verify_receipt(target, cwd=str(cwd), approved=approvals.mapping() if approvals else None, run=run)
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
AGENT_MARKER = "NECRO_AGENT_SESSION"
_AUTO = object()
_HERMES_PROCESS = re.compile(r"(?:^|[\s/])hermes(?:\s|$)|hermes_cli|hermes-agent|hermes_bootstrap", re.IGNORECASE)


def hermes_ancestors(limit: int = 40):
    """The command lines of this process's ancestors (parent first), or None when they cannot be read."""
    pid, chain = os.getppid(), []
    for _ in range(limit):
        if pid <= 1:
            return chain
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=5, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        parts = out.stdout.strip().split(None, 1)
        if out.returncode != 0 or not parts:
            return None
        try:
            pid = int(parts[0])
        except ValueError:
            return None
        chain.append(parts[1] if len(parts) > 1 else "")
    return chain


def person_problem(verb: str, *, session_env_ok: bool = True, environ=None, stdin_isatty=None, ancestors=_AUTO):
    """Why `necro <verb>` from a shell might not be a person typing it, or None. Fails closed: any
    doubt refuses. The primary route is /necro <verb> inside Hermes, which an agent cannot type."""
    env = os.environ if environ is None else environ
    use_slash = f"Type /necro {verb} in Hermes instead."
    if not session_env_ok:
        return ("this Hermes cannot mark its agent's shell (ctx.register_session_env arrived in Hermes 0.23.1), so "
                f"the command cannot tell you from the agent. {use_slash}")
    if env.get(AGENT_MARKER):
        return f"this shell belongs to a Hermes agent ({AGENT_MARKER} is set). {use_slash}"
    marker = next((name for name in ("AI_AGENT", "HERMES_AGENT") if env.get(name)), None)
    if marker:
        return (f"{marker} is set, so this may be an AI agent's shell (Hermes sets it in every hermes command). "
                f"{use_slash} Or run python3 {shlex.quote(str(Path(__file__).resolve().parent))} {verb} in your own terminal.")
    if not (sys.stdin.isatty() if stdin_isatty is None else stdin_isatty):
        return f"stdin is not a terminal, so no person is typing this. {use_slash}"
    chain = hermes_ancestors() if ancestors is _AUTO else ancestors
    if chain is None:
        return f"the programs that started this shell cannot be checked. {use_slash}"
    if any(_HERMES_PROCESS.search(cmd or "") for cmd in chain):
        return f"a Hermes process started this shell, so it may be the agent's. {use_slash}"
    return None


def in_agent_shell() -> bool:
    return bool(os.environ.get(AGENT_MARKER))


def dispatch(raw: str, cwd, approvals: Approvals, cache=None, from_shell: bool = False,
             session_env_ok: bool = True) -> tuple:
    """Run one person-facing command. ``from_shell`` is True for `hermes necro` and `python3 necro`;
    their freeze, unfreeze and approve then refuse unless a person is clearly typing them. The /necro
    slash command (from_shell False) comes from the person's own chat input, which an agent cannot type."""
    try:
        words = shlex.split(raw or "")
    except ValueError:
        words = (raw or "").split()
    sub = words[0].lower() if words else "status"
    rest = words[1:]
    if from_shell and sub in PERSON_ONLY:
        problem = person_problem(sub, session_env_ok=session_env_ok)
        if problem:
            return f"Refused: `necro {sub}` is a person's command, and {problem}", 3
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
        run = "--run" in rest
        rest = [w for w in rest if w != "--run"]
        return do_verify(cwd, rest[0] if rest else None, approvals, run=run)
    if sub == "receipts":
        return do_receipts(cwd)
    if sub == "demo":
        from .demo import run_demo
        return run_demo(), 0
    return USAGE, 0 if sub in ("help", "-h", "--help") else 1
