"""Running frozen contracts: each one's locked command, in the project folder, with a time limit."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .lock import drift

DEFAULT_TIMEOUT = 120
TAIL_CHARS = 2000
# Variables a test command usually needs. Everything else, API keys included, stays out unless a
# contract names it under env = [...].
PASS_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TZ", "TMPDIR", "TEMP", "TMP", "TERM", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR",
    "VIRTUAL_ENV", "CONDA_PREFIX", "JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR",
)
STATUSES = ("pass", "fail", "tampered", "timeout", "unfinished", "skipped", "error")


@dataclass
class Result:
    id: str
    says: str
    status: str
    exit_code: int | None = None
    duration_ms: int = 0
    output_tail: str = ""
    reasons: list = field(default_factory=list)
    timeout_s: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "says": self.says, "status": self.status, "exit_code": self.exit_code,
            "duration_ms": self.duration_ms, "output_tail": self.output_tail, "reasons": list(self.reasons),
        }


def all_pass(results) -> bool:
    results = list(results)
    return bool(results) and all(r.status == "pass" for r in results)


def contract_env(names=()) -> dict:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    for name in names:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _tail(handle) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - TAIL_CHARS * 4))
    return handle.read().decode("utf-8", "replace")[-TAIL_CHARS:]


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_command(root: Path, run: str, env: dict, timeout: float):
    """Run one command. Returns (status, exit_code, duration_ms, output_tail)."""
    try:
        argv = shlex.split(run)
    except ValueError as exc:
        return "error", None, 0, f"could not parse the command: {exc}"
    started = time.monotonic()
    with tempfile.TemporaryFile() as out:
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            return "error", None, 0, f"could not start {argv[0]!r}: {exc}"
        try:
            code = proc.wait(timeout=max(0.05, timeout))
        except subprocess.TimeoutExpired:
            _kill(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return "timeout", None, int((time.monotonic() - started) * 1000), _tail(out)
        finally:
            if proc.poll() is None:
                _kill(proc)
        elapsed = int((time.monotonic() - started) * 1000)
        return ("pass" if code == 0 else "fail"), code, elapsed, _tail(out)


def run_contracts(root, lk: dict, *, budget_s: float | None = None, default_timeout: float = DEFAULT_TIMEOUT,
                  only=None) -> list:
    """Run every frozen contract. A contract whose files drifted is reported as tampered and not run.

    ``budget_s`` bounds the whole run: a contract cut short by it is ``unfinished`` and any contract
    that never started is ``skipped``. Neither counts as a pass."""
    root = Path(root).resolve()
    d = drift(root, lk)
    deadline = time.monotonic() + budget_s if budget_s else None
    results = []
    for c in lk["contracts"]:
        if only and c["id"] not in only:
            continue
        says = c.get("says", "")
        if c["id"] in d.contracts:
            results.append(Result(c["id"], says, "tampered", reasons=list(d.contracts[c["id"]])))
            continue
        own_timeout = float(c.get("timeout") or default_timeout)
        timeout = own_timeout
        capped = False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                results.append(Result(c["id"], says, "skipped", reasons=["not run: the time budget ran out"]))
                continue
            if remaining < own_timeout:
                timeout, capped = remaining, True
        status, code, ms, tail = run_command(root, c["run"], contract_env(c.get("env") or []), timeout)
        reasons = []
        if status == "timeout" and capped:
            status = "unfinished"
            reasons.append("stopped when the time budget ran out")
        elif status == "timeout":
            reasons.append(f"took longer than its {own_timeout:g} s limit")
        results.append(Result(c["id"], says, status, code, ms, tail, reasons, timeout))
    return results
