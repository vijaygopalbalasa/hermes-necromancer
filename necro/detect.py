"""Zero-config freezing: find a project's tests and the command that runs them.

/necro freeze in a folder with no necro.toml uses this to write one, then freezes it as usual; that is
the only place it leads to a write, and it is a person's command. Detection itself only reads: the
folder listing, package.json, pyproject.toml, setup.cfg, requirements*.txt, tox.ini and the first
lines of Python test files, by name, never through a link, and never a credential file. Nothing here
runs the project's code: runner_problem looks the command's program up on PATH and, for pytest, asks
that python3 to import pytest in an empty folder outside the project."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .canon import BudgetExceeded, is_credential, is_credential_path
from .receipts import MAX_SOURCES
from .runner import contract_env
from .spec import GLOB_CHARS, glob_regex, walk_files

# Test files and folders, in this order. A pattern is used when it adds a file the ones before it did not.
FREEZE_GLOBS = (
    "tests/**", "test/**", "**/__tests__/**",
    "**/test_*.py", "**/*_test.py", "**/tests.py", "**/conftest.py",
    "**/*.test.*", "**/*.spec.*",
    "**/*_test.go", "**/testdata/**",
)
LOOKED_FOR = "tests/, test/, __tests__/, test_*.py, *_test.py, conftest.py, *.test.*, *.spec.* and *_test.go"
KINDS = {"pytest": "Python", "unittest": "Python", "npm": "Node", "go": "Go", "cargo": "Rust"}
# Files a test run may write that are not in a folder necro never walks; a person can change the list.
OUTPUTS = {
    "pytest": [".coverage", ".coverage.*", "coverage.xml", "htmlcov/**", ".hypothesis/**"],
    "unittest": [".coverage", ".coverage.*", "coverage.xml", "htmlcov/**", ".hypothesis/**"],
    "npm": [".nyc_output/**"],
}
PY_TEST = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
REQUIREMENTS = re.compile(r"^requirements[^/]*\.txt$", re.IGNORECASE)
PYTEST_WORD = re.compile(r"\bpytest\b")
# A test file that imports pytest, or has a test function outside a class (only pytest runs those).
PYTEST_STYLE = re.compile(r"^[ \t]*(?:import|from)[ \t]+pytest\b|^(?:async[ \t]+)?def[ \t]+test|^@pytest\.", re.MULTILINE)
NPM_DEFAULT = "no test specified"
MANIFEST_BYTES = 1 << 20
HEAD_BYTES = 16384
MAX_HEADS = 200
SHELL_PUNCTUATION = set("();<>|&")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class Found:
    root: Path
    kind: str | None = None        # pytest, unittest, npm, go or cargo
    run: str | None = None         # the command that runs the tests, when it is clear
    patterns: list = field(default_factory=list)   # what to freeze: globs, and plain paths
    files: list = field(default_factory=list)      # the files those patterns freeze now
    outputs: list = field(default_factory=list)
    problem: str | None = None     # why this folder was not looked at
    listing: list = field(default_factory=list)    # every file walked


def _read(root: Path, rel: str, limit: int) -> str | None:
    """The first ``limit`` bytes of a regular file in the project as text, or None. Never a credential
    file and never through a link (checked before opening, and O_NOFOLLOW in case it changes)."""
    path = root / rel
    if is_credential(os.path.basename(rel)) or os.path.islink(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _regular(root: Path, rel: str) -> bool:
    """A regular file, not a link (never followed)."""
    try:
        return stat.S_ISREG(os.lstat(root / rel).st_mode)
    except OSError:
        return False


def _freezable(root: Path, rel: str) -> bool:
    return not os.path.islink(root / rel) and not is_credential_path(root / rel)


def freeze_patterns(root: Path, files: list) -> tuple:
    """(patterns, frozen files): each of FREEZE_GLOBS that adds a file, in order. When a glob's matches
    include a link or a credential file, which cannot be frozen, its other new matches are named one
    by one instead."""
    covered, patterns = set(), []
    for glob in FREEZE_GLOBS:
        rx = glob_regex(glob)
        hits = [rel for rel in files if rx.match(rel)]
        good = [rel for rel in hits if _freezable(root, rel)]
        new = [rel for rel in good if rel not in covered]
        if not new:
            continue
        if len(good) == len(hits):
            patterns.append(glob)
            covered.update(good)
        else:
            named = [rel for rel in new if not GLOB_CHARS & set(rel)]
            patterns.extend(named)
            covered.update(named)
    return patterns, sorted(covered)


def _pytest_signal(root: Path, files: list, py_tests: list) -> bool:
    names = set(files)
    if "pytest.ini" in names or any(rel == "conftest.py" or rel.endswith("/conftest.py") for rel in files):
        return True
    configs = ["pyproject.toml", "setup.cfg", "tox.ini"] + sorted(rel for rel in files if REQUIREMENTS.match(rel))
    for name in configs:
        if name in names:
            text = _read(root, name, MANIFEST_BYTES)
            if text and PYTEST_WORD.search(text):
                return True
    for rel in py_tests[:MAX_HEADS]:
        text = _read(root, rel, HEAD_BYTES)
        if text and PYTEST_STYLE.search(text):
            return True
    return False


def _unittest_run(root: Path, py_tests: list) -> str | None:
    """A `python3 -m unittest discover` command that reaches every test file, or None when one
    discover cannot (it would quietly skip some, and on Python 3.11 a run of zero tests passes)."""
    names = [rel.rsplit("/", 1)[-1] for rel in py_tests]
    if all(n.startswith("test") for n in names):
        pattern = None
    elif all(n.endswith("_test.py") for n in names):
        pattern = "*_test.py"
    else:
        return None
    start = ""
    tops = {rel.split("/", 1)[0] for rel in py_tests if "/" in rel}
    if len(tops) == 1 and all("/" in rel for rel in py_tests):
        top = next(iter(tops))
        if not os.path.lexists(root / top / "__init__.py"):
            start = top
    for rel in py_tests:  # every folder between the start folder and a test file must be a package
        inner = rel[len(start) + 1:] if start else rel
        folders = inner.split("/")[:-1]
        for i in range(len(folders)):
            if not os.path.lexists(root / start / "/".join(folders[: i + 1]) / "__init__.py"):
                return None
    argv = ["python3", "-m", "unittest", "discover", "-q"]
    if start:
        argv += ["-s", start]
    if pattern:
        argv += ["-p", pattern]
    return shlex.join(argv)


def _npm_test_script(root: Path) -> str | None:
    text = _read(root, "package.json", MANIFEST_BYTES)
    try:
        pkg = json.loads(text) if text else None
    except ValueError:
        return None
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if isinstance(test, str) and test.strip() and NPM_DEFAULT not in test:
        return test
    return None


def _runner(root: Path, files: list) -> tuple:
    names = set(files)
    py_tests = [rel for rel in files if PY_TEST.search(rel) and _regular(root, rel)]
    if py_tests:
        if _pytest_signal(root, files, py_tests):
            return "pytest", "python3 -m pytest -q"
        run = _unittest_run(root, py_tests)
        return ("unittest", run) if run else (None, None)
    if "package.json" in names and _npm_test_script(root):
        return "npm", "npm test --silent"
    if "go.mod" in names and _regular(root, "go.mod"):
        return "go", "go test ./..."
    if "Cargo.toml" in names and _regular(root, "Cargo.toml"):
        return "cargo", "cargo test --quiet"
    return None, None


def detect(root, budget_s: float = 60.0) -> Found:
    """What /necro freeze would lock in ``root``. Reads only; see the module note."""
    root = Path(root).expanduser().resolve()
    found = Found(root)
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    if root == home:
        found.problem = f"{root} is your home folder, not a project folder"
        return found
    if root == Path(root.anchor):
        found.problem = f"{root} is the top of the disk, not a project folder"
        return found
    try:
        files = walk_files(root, time.monotonic() + budget_s)
    except BudgetExceeded:
        found.problem = f"{root} took too long to read, so it may hold more than one project"
        return found
    if len(files) > MAX_SOURCES:
        found.problem = f"{root} has more than {MAX_SOURCES:,} files, too many to lock as one project"
        return found
    found.listing = files
    found.patterns, found.files = freeze_patterns(root, files)
    found.kind, found.run = _runner(root, files)
    found.outputs = list(OUTPUTS.get(found.kind, []))
    if found.kind == "cargo" and not os.path.lexists(root / "Cargo.lock"):
        found.outputs = ["Cargo.lock"]  # cargo test writes it when it is missing
    return found


def describe(patterns: list, count: int) -> str:
    """'tests/**, **/conftest.py (13 files)': the globs, the plain paths when there are few, and the count."""
    globs = [p for p in patterns if GLOB_CHARS & set(p)]
    plain = [p for p in patterns if p not in globs]
    shown = globs + (plain if len(plain) <= 3 else [f"{len(plain)} {'more ' if globs else ''}test files"])
    return f"{', '.join(shown)} ({count} {'file' if count == 1 else 'files'})"


def as_run(command: str) -> str:
    """The person's command as a contract's run. Shell syntax (&&, |, ;, >, VAR=x cmd) runs through
    sh -c, because a run is otherwise started without a shell. ValueError when it cannot be used."""
    command = (command or "").strip()
    if any(ch in command for ch in "\r\n\0"):
        raise ValueError("it must be one line")
    words = shlex.split(command)
    if not words:
        raise ValueError("it is empty")
    if len(words) == 1 and words[0] != command and any(ch.isspace() for ch in words[0]):
        return as_run(words[0])  # the whole command was quoted
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    if any(token and set(token) <= SHELL_PUNCTUATION for token in lexer) or ASSIGNMENT.match(words[0]):
        return shlex.join(["sh", "-c", command])
    return command


def named_files(root: Path, command: str, listing: list) -> list:
    """Files in the project that the command names, such as a test script: they define done too."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return []
    have, out = set(listing), []
    for word in words:
        if word.startswith("-"):
            if "=" not in word:
                continue
            word = word.split("=", 1)[1]
        if not word:
            continue
        p = Path(word)
        if p.is_absolute():
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
        else:
            rel = os.path.normpath(word).replace(os.sep, "/")
        if rel in have and rel not in out and _freezable(root, rel) and not GLOB_CHARS & set(rel):
            out.append(rel)
    return out


def _imports(python: str, module: str, env: dict) -> bool:
    with tempfile.TemporaryDirectory(prefix="necro-detect-") as empty:
        try:
            out = subprocess.run([python, "-c", f"import {module}"], cwd=empty, env=env, capture_output=True,
                                 timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False
    return out.returncode == 0


def runner_problem(run: str, root) -> str | None:
    """Why ``run`` cannot work here, or None: its program is not on the PATH contracts run with, or that
    python3 cannot import pytest. Runs nothing from the project unless the person named a program in it."""
    try:
        argv = shlex.split(run)
    except ValueError as exc:
        return f"the command cannot be read ({exc})"
    if not argv:
        return "the command is empty"
    env = contract_env()
    exe = argv[0]
    if "/" in exe:
        path = Path(exe) if os.path.isabs(exe) else Path(root) / exe
        found = str(path) if path.is_file() and os.access(path, os.X_OK) else None
    else:
        found = shutil.which(exe, path=env.get("PATH", ""))
    if not found:
        return f"`{exe}` is not installed or not on the PATH Hermes uses"
    if argv[1:3] == ["-m", "pytest"] and not _imports(found, "pytest", env):
        return f"`{exe}` here cannot import pytest"
    return None


def toml_text(run: str, patterns: list, outputs=()) -> str:
    """necro.toml for one contract, C1, with a plain header saying it was generated and can be edited.
    Strings are written as JSON strings, which TOML reads the same way."""
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731
    lines = [
        "# Written by /necro freeze from the tests it found here. Edit it as you like: run is the command",
        "# that must pass, files are what Hermes cannot edit. Then type /necro freeze again to lock the new",
        "# version, or /necro unfreeze to remove the lock.",
        "# Settings that choose which tests run (package.json, pyproject.toml, Cargo.toml) are not locked;",
        "# add one to files to lock it too.",
        "",
    ]
    if outputs:
        lines += ["# Files a test run may write, such as coverage data; changes to them do not count.",
                  f"outputs = [{', '.join(q(o) for o in outputs)}]", ""]
    lines += ["[[contract]]", 'id = "C1"', 'says = "Your tests pass"', f"run = {q(run)}",
              f"files = [{', '.join(q(p) for p in patterns)}]", ""]
    return "\n".join(lines)


def write_new(path: Path, text: str) -> bool:
    """Create the file; False when anything (a file, a link, even a dangling one) is already there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def remove_if_same(path: Path, text: str) -> None:
    """Take back a file this module wrote, unless it changed since."""
    try:
        if _regular(path.parent, path.name) and path.read_text(encoding="utf-8") == text:
            path.unlink()
    except OSError:
        pass
