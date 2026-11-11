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

from . import detect
from .canon import utc_now
from .gate import APPROVAL_TAMPERED, BUDGET_PROBLEM, evaluate
from .lock import LOCK_REL, LockError, drift, find_root, freeze, lock_hash, lock_path, read_lock
from .receipts import list_receipts
from .runner import contract_env
from .spec import SPEC_NAME, SpecError, load_spec
from .verify import verify_receipt

PERSON_CHECK_BUDGET = 900.0

USAGE = """Contract Necromancer: Hermes cannot call coding work done while a frozen acceptance contract fails.
Freeze, approve and unfreeze with /necro inside Hermes; from a shell they refuse unless a person is clearly typing.

  status            what is frozen here and the latest receipt; before a freeze, the tests it would lock
  freeze [command]  lock your tests by hash (type /necro freeze in the Hermes chat). With no necro.toml it
                    finds them and their command (pytest, unittest, npm test, go test, cargo test), writes
                    necro.toml, locks the tests and the settings that shape the run, and runs them once;
                    put your own test command after freeze to use that instead
  approve           accept a lock someone else froze and committed (type /necro approve in the chat)
  unfreeze          remove the lock so contracts can be edited (type /necro unfreeze in the chat)
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
    KEY_FILE = "approvals.hmac"  # not a credential-looking name: the plugin never reads one (catalog rule 11)
    OLD_KEY_FILE = "approvals.key"  # its name before Nov 11, moved on first use without being opened

    def __init__(self, store=None):
        self.store = store
        self.known: set = set()  # folders seen approved in this process, kept even if the store loses them
        try:
            self.key_path = Path(store.path).parent / self.KEY_FILE if store is not None else None
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
        old = self.key_path.with_name(self.OLD_KEY_FILE)
        if not os.path.lexists(self.key_path) and os.path.lexists(old):
            try:
                os.rename(old, self.key_path)
            except OSError:
                pass
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
    """Each contract's id, words and command, and what it freezes: once, when every contract freezes the same files."""
    out = []
    lists = [", ".join(f["path"] for f in c["files"]) for c in lk["contracts"]]
    shared = len(lists) > 1 and len(set(lists)) == 1
    for c, files in zip(lk["contracts"], lists):
        out.append(f"  {c['id']:<6} {c.get('says') or '(no description)'}")
        out.append(f"         run: {c['run']}")
        if not shared:
            out.append(f"         frozen: {files}")
    if shared:
        out.append(f"  frozen, shared by all {len(lists)}: {lists[0]}")
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
        return nothing_set_up(cwd), 0
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


TOML_EXAMPLE = """    [[contract]]
    id = "C1"
    says = "Your tests pass"
    run = {run}
    files = ["path/to/your/tests/**"]"""


def nothing_set_up(cwd) -> str:
    """One line for /necro status before anything is frozen: what /necro freeze would lock here."""
    here = Path(cwd).expanduser().resolve()
    found = detect.detect(here, budget_s=5.0)
    if found.problem:
        return f"Nothing is locked here: {found.problem}. Open Hermes in your project's folder, then type /necro freeze."
    what = detect.describe(found.patterns, len(found.files)) if found.patterns else None
    if found.run and what:
        return f"Nothing is locked in {here} yet. Found your tests: {what}, run with `{found.run}`. Type /necro freeze to lock them."
    if what:
        return (f"Nothing is locked in {here} yet. Found test files ({what}) but not the command that runs them. "
                "Type /necro freeze <your test command> to lock them.")
    if found.run:
        return (f"Nothing is locked in {here} yet. It looks like a {detect.KINDS[found.kind]} project (`{found.run}`), "
                "but it has no test files to lock. Type /necro help to see how to lock your tests.")
    return (f"Nothing is locked in {here} yet, and no tests were found here. Type /necro freeze <your test command>, "
            "or /necro help.")


def _no_tests(found, command: str | None) -> str:
    example = TOML_EXAMPLE.format(run=json.dumps(command or found.run or "python3 -m pytest -q"))
    if command:
        return (f"Nothing locked: `{command}` names no file in {found.root}, and no test files were found here "
                f"(looked for {detect.LOOKED_FOR}). Write {SPEC_NAME} like this, then type /necro freeze:\n{example}")
    seen = (f" It looks like a {detect.KINDS[found.kind]} project (`{found.run}`), but there is no test file to lock."
            if found.run else "")
    return (f"Nothing locked: no test files in {found.root} (looked for {detect.LOOKED_FOR}).{seen}\n"
            "Two ways forward:\n"
            "  - type /necro freeze <your test command>; it locks the test files it finds and any file here the "
            "command names, such as a test script\n"
            f"  - or write {SPEC_NAME} like this, then type /necro freeze:\n{example}")


FREEZE_RUN_BUDGET = 25.0  # the run at freeze gets the gate's budget


def right_now(root: Path, approvals: Approvals, cache=None, budget_s: float = FREEZE_RUN_BUDGET) -> str:
    """One line on where the newly locked tests stand, from one run within the gate's budget."""
    try:
        v = evaluate(root, budget_s=budget_s, approved=approvals.mapping(), cache=cache, trigger="freeze")
    except Exception:
        return "Run /necro check to see where it stands."
    if v.problems and v.problems[0] == BUDGET_PROBLEM or any(r.status in ("timeout", "unfinished", "skipped")
                                                             for r in v.results):
        return "Run /necro check to see where it stands."
    if any(r.status == "error" for r in v.results):
        return "Right now: the test command could not run. Run /necro check to see why."
    if v.problems:
        return f"Right now: the check does not pass: {v.problems[0]}."
    counts = [detect.test_counts(r.output_tail) for r in v.results]
    failing = total = None
    if counts and all(counts):
        failing, total = sum(c[0] for c in counts), sum(c[1] for c in counts)
    if v.ok:
        if total is None or total == 0:
            return "Right now: they pass."
        return "Right now: the test passes." if total == 1 else f"Right now: all {total} tests pass."
    if failing and total:
        if total == 1:
            return "Right now: the test fails. Hermes can't say done until it passes."
        return (f"Right now: {failing} of {total} tests {'fails' if failing == 1 else 'fail'}. Hermes can't say done "
                "until they pass.")
    return "Right now: the tests fail. Hermes can't say done until they pass."


def _freeze_found(root: Path, approvals: Approvals, command: str | None, cache=None,
                  run_budget_s: float = FREEZE_RUN_BUDGET) -> tuple:
    """/necro freeze where there is no necro.toml: find the tests and their command (or use the person's),
    write necro.toml, freeze it, run it once. All or nothing: when it cannot lock, it changes nothing."""
    if os.path.lexists(root / SPEC_NAME):
        return (f"Could not freeze: {root / SPEC_NAME} is a link, and Contract Necromancer never follows links. Put "
                f"a regular {SPEC_NAME} there, or remove the link and type /necro freeze again."), 1
    found = detect.detect(root)
    if found.problem:
        return f"Nothing locked: {found.problem}. Open Hermes in your project's folder and type /necro freeze there.", 1
    if command:
        try:
            run = detect.as_run(command)
        except ValueError as exc:
            return f"Nothing locked: `{command}` cannot be used as a test command ({exc}).", 1
    else:
        run = found.run
        if run is None and found.patterns:
            what = detect.describe(found.patterns, len(found.files))
            return (f"Nothing locked: found test files in {root}, {what}, but not the command that runs them. "
                    "Type /necro freeze <your test command>, for example /necro freeze make test, to lock them "
                    "with it."), 1
    if run is None:
        return _no_tests(found, command), 1
    shaped = detect.shape(root, run, found.listing)
    if shaped.refusal:
        return f"Nothing locked: {shaped.refusal}.", 1
    patterns = list(found.patterns) + [f for f in shaped.files if f not in found.patterns and f not in found.files]
    # Test-only config alone (pytest.ini, jest.config.*) is not a test to lock; a script or a Makefile the person's
    # own command names is.
    if not found.patterns and (not command or not [f for f in shaped.files if f not in detect.CONFIG_ONLY]):
        return _no_tests(found, command), 1
    problem = detect.runner_problem(run, root)
    if problem:
        if "import pytest" in problem:
            fix = ("Start Hermes where pytest is installed (for example with your virtualenv active), or type "
                   "/necro freeze <your test command>.")
        else:
            fix = "Install it, or type /necro freeze <your test command>."
        return f"Nothing locked: found your tests, {detect.describe(patterns, len(found.files))}, but {problem}. {fix}", 1
    text = detect.toml_text(run, patterns, found.outputs, shaped.pins)
    path = root / SPEC_NAME
    try:
        if not detect.write_new(path, text):
            return f"Nothing locked: {SPEC_NAME} appeared in {root} meanwhile. Read it, then type /necro freeze.", 1
    except OSError as exc:
        return f"Nothing locked: could not write {path} ({exc.strerror or exc}).", 1
    try:
        lk = freeze(root)
    except (SpecError, OSError) as exc:
        detect.remove_if_same(path, text)
        return f"Could not freeze: {exc}", 1
    approvals.approve(root, lock_hash(lk))
    count = len({f["path"] for c in lk["contracts"] for f in c["files"]})
    also = detect.pins_phrase(shaped.pins)
    reply = (f"Locked your tests: `{run}` must pass. Frozen: {detect.describe(patterns, count)}"
             f"{', and ' + also if also else ''}. Hermes can't say \"done\" until this passes, and it can't edit these "
             "files. Undo: /necro unfreeze. Change what counts as done: edit necro.toml, then /necro freeze.")
    if shaped.unlockable:
        reply += " Not locked: " + "; ".join(shaped.unlockable) + "."
    if approvals.store is None:
        reply += " Now type /necro approve in the Hermes chat, so Hermes accepts this lock."
    return reply + "\n" + right_now(root, approvals, cache, run_budget_s), 0


def do_freeze(cwd, approvals: Approvals, command: str | None = None, cache=None,
              run_budget_s: float = FREEZE_RUN_BUDGET) -> tuple:
    root = spec_root(cwd)
    if root is None:
        return _freeze_found(Path(cwd).expanduser().resolve(), approvals, command, cache, run_budget_s)
    if not (root / SPEC_NAME).is_file():
        return (f"Could not freeze: {root} has a lock ({LOCK_REL}) but no {SPEC_NAME}. Put {SPEC_NAME} back (for "
                "example from git), or type /necro unfreeze to start over."), 1
    if command:
        return (f"Nothing changed: {root / SPEC_NAME} already says what counts as done here. To use `{command}`, "
                f"change run in {SPEC_NAME}, then type /necro freeze."), 1
    try:
        lk = freeze(root)
    except SpecError as exc:
        return f"Could not freeze: {exc}", 1
    digest = lock_hash(lk)
    approvals.approve(root, digest)
    files = len({f["path"] for c in lk["contracts"] for f in c["files"]})
    n = len(lk["contracts"])
    lines = [f"Froze {n} contract{'' if n == 1 else 's'} ({files} file{'' if files == 1 else 's'}) in {root}, "
             f"lock {digest[:12]}:"]
    lines += _contract_lines(lk)
    lines.append("From now on Hermes cannot finish coding work here while any of these fails, and edits to the "
                 f"frozen files are blocked. Commit {LOCK_REL} with the contracts.")
    if approvals.store is None:
        lines.append("Now type /necro approve in the Hermes chat, so Hermes accepts this lock.")
    return "\n".join(lines), 0


def do_unfreeze(cwd, approvals: Approvals) -> tuple:
    root = find_root(cwd)
    if root is None:
        return "Nothing is frozen here.", 0
    if approvals.store is None:
        return ("Unfreezing also clears the approval Hermes keeps for this folder, so do it inside Hermes: type "
                "/necro unfreeze in the Hermes chat. (If this project is never used with Hermes, delete "
                ".necro/lock.json by hand.)"), 1
    lock_path(root).unlink()
    approvals.revoke(root)
    return (f"Unfroze {root}. The contracts can be edited now; receipts stay in .necro/receipts. "
            "Read the new contracts, then run /necro freeze again."), 0


def do_approve(cwd, approvals: Approvals) -> tuple:
    root = find_root(cwd)
    if root is None:
        return f"Nothing to approve: no {LOCK_REL} here or above.", 1
    if approvals.store is None:
        return "Approvals are kept inside Hermes, so this cannot record one. Type /necro approve in the Hermes chat.", 1
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


def hermes_ancestors(limit: int = 40, run=None):
    """The command lines of this process's ancestors (parent first), or None when any of them cannot
    be read. Walking all the way up to pid 1 is a complete answer; a detached process that got there
    is left to the terminal check."""
    run = run or subprocess.run
    pid, chain = os.getppid(), []
    for _ in range(limit):
        if pid <= 1:
            return chain
        try:
            out = run(["ps", "-o", "ppid=", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                      timeout=5, stdin=subprocess.DEVNULL, env=contract_env())
        except (OSError, subprocess.SubprocessError):
            return None
        parts = (out.stdout or "").strip().split(None, 1)
        if out.returncode != 0 or not parts:
            return None
        try:
            pid = int(parts[0])
        except ValueError:
            return None
        chain.append(parts[1] if len(parts) > 1 else "")
    return None


def person_problem(verb: str, *, session_env_ok: bool = True, environ=None, stdin_isatty=None, ancestors=_AUTO,
                   cwd=None):
    """Why `necro <verb>` from a shell might not be a person typing it, followed by the exact way forward,
    or None. Fails closed: any doubt refuses. The primary route is /necro <verb> in the Hermes chat, which
    an agent cannot type."""
    env = os.environ if environ is None else environ
    reason = None
    if not session_env_ok:
        reason = ("this Hermes cannot mark its agent's shell (ctx.register_session_env arrived in Hermes 0.23.1), so "
                  "the command cannot tell you from the agent.")
    elif env.get(AGENT_MARKER):
        reason = f"this shell belongs to a Hermes agent ({AGENT_MARKER} is set)."
    else:
        marker = next((name for name in ("AI_AGENT", "HERMES_AGENT") if env.get(name)), None)
        if marker:
            reason = f"{marker} is set, so this may be an AI agent's shell (Hermes sets it in every hermes command)."
        elif not (sys.stdin.isatty() if stdin_isatty is None else stdin_isatty):
            reason = "stdin is not a terminal, so no person is typing this."
        else:
            chain = hermes_ancestors() if ancestors is _AUTO else ancestors
            if chain is None:
                reason = "the programs that started this shell cannot be checked."
            elif any(_HERMES_PROCESS.search(cmd or "") for cmd in chain):
                reason = "a Hermes process started this shell, so it may be the agent's."
    if reason is None:
        return None
    return f"{reason} {instead(verb, cwd if cwd is not None else os.getcwd())}"


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def instead(verb: str, cwd) -> str:
    """The copy-paste way forward after a refusal."""
    line = f"Do this instead: type /necro {verb} in the Hermes chat"
    if verb == "freeze":
        standalone = f"python3 {shlex.quote(str(PLUGIN_DIR / 'necro'))} freeze --dir {shlex.quote(str(Path(cwd).resolve()))}"
        line += f", or run: {standalone} in your own terminal, then type /necro approve in the Hermes chat"
    return line + "."


def in_agent_shell() -> bool:
    return bool(os.environ.get(AGENT_MARKER))


def dispatch(raw: str, cwd, approvals: Approvals, cache=None, from_shell: bool = False,
             session_env_ok: bool = True, run_budget_s: float = FREEZE_RUN_BUDGET) -> tuple:
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
        problem = person_problem(sub, session_env_ok=session_env_ok, cwd=cwd)
        if problem:
            return f"Refused: `necro {sub}` is a person's command, and {problem}", 3
    if sub in ("status", "show"):
        return status(cwd, approvals)
    if sub == "freeze":
        after = re.match(r"\s*freeze\b(.*)$", raw or "", re.IGNORECASE | re.DOTALL)
        command = (after.group(1) if after else shlex.join(rest)).strip()
        return do_freeze(cwd, approvals, command or None, cache, run_budget_s)
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
