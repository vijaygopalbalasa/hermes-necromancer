"""Running frozen contracts: each one's locked command, in the project folder, with a time limit."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
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
    "CONDA_PREFIX", "JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR",
)  # never VIRTUAL_ENV or PYTHONPATH: inside Hermes they describe Hermes's own Python
SYSTEM_DIRS = {"/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin"}


def _within(path: str, folder: str) -> bool:
    return bool(folder) and (path == folder or path.startswith(folder.rstrip(os.sep) + os.sep))


def contract_path(value: str) -> str:
    """PATH as Hermes was started with it. Inside Hermes (hermes_cli is loaded), Hermes puts its own launcher folder
    and its environment's bin first (pm/environments.py), so python3 there would be Hermes's Python: entries under
    sys.prefix, the interpreter's own folder or the Hermes checkout are dropped, system folders never, the rest
    keeps its order. Outside Hermes, PATH is left alone."""
    module = sys.modules.get("hermes_cli")
    if not value or module is None:
        return value
    runtime = [os.path.realpath(sys.prefix), os.path.realpath(os.path.dirname(sys.executable))]
    try:
        runtime.append(os.path.realpath(str(Path(module.__file__).resolve().parents[1])))
    except (AttributeError, TypeError, IndexError, OSError):
        pass
    keep = []
    for entry in value.split(os.pathsep):
        real = os.path.realpath(entry) if entry else entry
        if entry and real not in SYSTEM_DIRS and any(_within(real, folder) for folder in runtime):
            continue
        keep.append(entry)
    return os.pathsep.join(keep)
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
    if "PATH" in env:
        env["PATH"] = contract_path(env["PATH"])
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
    """Run one command. Returns (status, exit_code, duration_ms, output_tail).

    Python gets an empty bytecode cache of its own (PYTHONPYCACHEPREFIX), so a stale or planted
    .pyc in the project is never imported in place of its source, and no __pycache__ lands in the
    project. Anything the command leaves running in its process group is killed afterwards."""
    try:
        argv = shlex.split(run)
    except ValueError as exc:
        return "error", None, 0, f"could not parse the command: {exc}"
    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryDirectory(prefix="necro-pycache-") as pycache:
        env = {**env, "PYTHONPYCACHEPREFIX": pycache}
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
            _kill(proc)  # the leader has exited; this ends anything it left behind in its group
        elapsed = int((time.monotonic() - started) * 1000)
        return ("pass" if code == 0 else "fail"), code, elapsed, _tail(out)


def run_contracts(root, lk: dict, *, budget_s: float | None = None, default_timeout: float = DEFAULT_TIMEOUT,
                  only=None, deadline: float | None = None, drift_result=None) -> list:
    """Run every frozen contract. A contract whose files drifted is reported as tampered and not run.

    ``budget_s`` (or an absolute monotonic ``deadline``) bounds the whole run: a contract cut short
    by it is ``unfinished`` and any contract that never started is ``skipped``. Neither counts as a
    pass."""
    root = Path(root).resolve()
    if deadline is None and budget_s:
        deadline = time.monotonic() + budget_s
    d = drift_result if drift_result is not None else drift(root, lk, deadline=deadline)
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
