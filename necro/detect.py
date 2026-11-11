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
    shape: "Shape | None" = None   # what the found command depends on besides the test files


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
    found.shape = shape(root, found.run, files) if found.run else None
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


# What shapes a run besides its test files. Test-only config files are frozen whole when they exist and pinned
# as absent when they do not, so adding one later counts; settings inside a shared file are pinned by field.
PYTEST_FILES = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "**/conftest.py")
PYTEST_PINS = ("pyproject.toml#tool.pytest", "setup.cfg#[tool:pytest]", "tox.ini#[pytest]")
# vitest reads vite.config.* when there is no vitest.config.*
NODE_FILES = ("jest.config.*", "vitest.config.*", "vite.config.*", ".mocharc*")
NODE_PINS = ("package.json#jest", "package.json#mocha", "package.json#ava")
CARGO_FILES = (".cargo/config.toml", ".cargo/config", "build.rs")
CARGO_PINS = ("Cargo.toml#test", "Cargo.toml#package.autotests", "Cargo.toml#package.build", "Cargo.toml#lib.test",
              "Cargo.toml#lib.harness", "Cargo.toml#lib.doctest", "Cargo.toml#bin.*.test", "Cargo.toml#bin.*.harness")
CONFIG_ONLY = set(PYTEST_FILES) | set(NODE_FILES) | set(CARGO_FILES)
RUNNER_FILES = {
    "make": ("GNUmakefile", "makefile", "Makefile"), "gmake": ("GNUmakefile", "makefile", "Makefile"),
    "tox": ("tox.ini",), "nox": ("noxfile.py",), "just": ("justfile", "Justfile", ".justfile"),
    "task": ("Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"),
}
SHELLS = {"sh", "bash", "zsh", "dash"}
WRAPPERS = {"env", "exec", "command", "time", "nice", "nohup"}
NODE_TOOLS = {"npx", "jest", "vitest", "mocha", "ava"}
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
YARN_BUILTINS = {"add", "install", "remove", "upgrade", "init", "info", "dlx", "exec", "workspace", "workspaces",
                 "config", "cache", "global", "why", "link", "unlink", "pack", "publish", "set", "up", "node"}


@dataclass
class Shape:
    """What a run depends on besides its test files."""
    files: list = field(default_factory=list)       # frozen whole: test-only config, runner files, named files
    pins: list = field(default_factory=list)        # settings pinned by field, or files that must stay absent
    unlockable: list = field(default_factory=list)  # files the command names that cannot be locked, in plain words
    refusal: str | None = None                      # why this command cannot be locked at all

    def add(self, kind: str, item: str) -> None:
        bucket = getattr(self, kind)
        if item not in bucket:
            bucket.append(item)


def _words_of(text: str) -> list:
    """The simple commands of a shell string, split at && || ; | & and parentheses; a redirection's target
    is left out, since it is written, not run."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    out, cur, skip = [], [], False
    for token in tokens:
        if token and set(token) <= SHELL_PUNCTUATION:
            if "<" in token or ">" in token:
                skip = True
                continue
            if cur:
                out.append(cur)
            cur = []
        elif skip:
            skip = False
        else:
            cur.append(token)
    if cur:
        out.append(cur)
    return out


def _simple(words: list) -> list:
    """A simple command without leading VAR=x assignments and env/exec/time wrappers."""
    i = 0
    while i < len(words):
        if ASSIGNMENT.match(words[i]):
            i += 1
        elif os.path.basename(words[i]) in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or ASSIGNMENT.match(words[i])):
                i += 1
        else:
            break
    return words[i:]


def _rel(root: Path, word: str) -> str | None:
    """A word as a path inside the project, or None."""
    if not word or word.startswith(("$", "~")) or "\x00" in word:
        return None
    full = os.path.normpath(word if os.path.isabs(word) else os.path.join(root, word))
    prefix = str(root).rstrip("/") + "/"
    return full[len(prefix):] if full.startswith(prefix) else None


def _hidden_in(rel: str) -> str | None:
    """The folder necro never walks (.venv, node_modules and the like) that holds this path, if any."""
    from .spec import SKIP_DIRS
    return next((part for part in rel.split("/")[:-1] if part in SKIP_DIRS), None)


def _freeze_or_pin(root: Path, item: str, have: set, sh: Shape) -> None:
    """A test-only config file (or glob): frozen whole when it is there, else pinned, so adding it counts."""
    rx = glob_regex(item) if GLOB_CHARS & set(item) else None
    hits = [rel for rel in have if (rx.match(rel) if rx else rel == item)]
    if hits and all(_freezable(root, rel) for rel in hits):
        sh.add("files", item)
    else:
        sh.add("pins", item)


def _script_names(prog: str, args: list) -> list:
    """The package.json scripts a package-manager command runs."""
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return []
    verb = words[0]
    if verb in ("test", "t", "tst") and prog != "bun":
        return ["test"]
    if verb in ("run", "run-script", "rum", "urn"):
        return words[1:2]
    if prog in ("yarn", "pnpm") and verb not in YARN_BUILTINS:
        return [verb]
    return []


def _script_pin(name: str) -> str:
    return "package.json#scripts." + (json.dumps(name) if "." in name or '"' in name else name)


def _one(root: Path, words: list, sh: Shape, have: set, top: bool, depth: int, seen: set) -> None:
    """Lock what one simple command depends on."""
    words = _simple(words)
    if not words or depth > 6:
        return
    exe, args = words[0], words[1:]
    prog = os.path.basename(exe)
    if top and "/" in exe:
        rel = _rel(root, exe)
        hidden = _hidden_in(rel) if rel else None
        if hidden:
            rest = " ".join(args)
            if prog.startswith("python") or prog in ("pytest", "py.test"):
                instead = "python3 " + (rest if prog.startswith("python") else f"-m pytest {rest}".strip())
            elif hidden == "node_modules":
                instead = f"npx {prog} {rest}".strip()
            else:
                instead = "a program on your PATH"
            sh.refusal = (f"that program ({exe}) lives in {hidden}, which is not locked; use {instead.strip()}, or "
                          "lock it yourself in necro.toml")
            return
    if prog in SHELLS and len(args) >= 2 and args[0] == "-c":
        for inner in _words_of(args[1]):
            _one(root, inner, sh, have, top, depth + 1, seen)
        return
    if prog in PACKAGE_MANAGERS:
        names = _script_names(prog, args)
        if names:
            _node(root, sh, have)
            _scripts(root, names, sh, have, depth, seen)
    if prog in NODE_TOOLS or (prog in ("pnpm", "yarn") and args[:1] in (["exec"], ["dlx"])):
        _node(root, sh, have)
    if prog in ("pytest", "py.test") or (prog.startswith("python") and args[:2] == ["-m", "pytest"]):
        for item in PYTEST_FILES:
            _freeze_or_pin(root, item, have, sh)
        for pin in PYTEST_PINS:
            sh.add("pins", pin)
    if prog == "cargo" and args[:1] in (["test"], ["t"], ["nextest"]):
        for item in CARGO_FILES:
            _freeze_or_pin(root, item, have, sh)
        for pin in CARGO_PINS:
            sh.add("pins", pin)
    for name in RUNNER_FILES.get(prog, ()):
        if name in have and _freezable(root, name):
            sh.add("files", name)
    for word in ([exe] if "/" in exe else []) + args:
        if word.startswith("-"):
            if "=" not in word:
                continue
            word = word.split("=", 1)[1]
        rel = _rel(root, word)
        if not rel or rel == "." or not os.path.lexists(root / rel) or os.path.isdir(root / rel) and not os.path.islink(root / rel):
            continue
        if os.path.islink(root / rel):
            sh.add("unlockable", f"{rel} is a link, which Contract Necromancer never follows")
        elif is_credential_path(root / rel):
            sh.add("unlockable", f"{rel} is a credential file, which Contract Necromancer never opens")
        elif rel not in have:
            hidden = _hidden_in(rel)
            if top and hidden:
                sh.add("unlockable", f"{rel} is in {hidden}, which is never locked")
        elif GLOB_CHARS & set(rel):
            sh.add("unlockable", f"{rel} has a * ? or [ in its name, so it cannot be named in necro.toml")
        else:
            sh.add("files", rel)


def _node(root: Path, sh: Shape, have: set) -> None:
    for item in NODE_FILES:
        _freeze_or_pin(root, item, have, sh)
    for pin in NODE_PINS:
        sh.add("pins", pin)


def _scripts(root: Path, names: list, sh: Shape, have: set, depth: int, seen: set) -> None:
    """Pin each package.json script a command runs, with its pre and post scripts, and lock what it runs."""
    text = _read(root, "package.json", MANIFEST_BYTES) if "package.json" in have else None
    try:
        scripts = (json.loads(text) or {}).get("scripts") if text else None
    except (ValueError, AttributeError):
        scripts = None
    for name in names:
        if name in seen or len(seen) > 40:
            continue
        seen.add(name)
        for pin in (name, "pre" + name, "post" + name):
            sh.add("pins", _script_pin(pin))
        body = scripts.get(name) if isinstance(scripts, dict) else None
        if isinstance(body, str):
            for inner in _words_of(body):
                _one(root, inner, sh, have, False, depth + 1, seen)


def shape(root, run: str, listing: list) -> Shape:
    """What ``run`` depends on besides the test files: see Shape. Reads package.json, by name, and nothing else."""
    root = Path(root)
    sh = Shape()
    try:
        argv = shlex.split(run)
    except ValueError:
        return sh
    _one(root, argv, sh, set(listing), True, 0, set())
    return sh


def pins_phrase(pins: list) -> str:
    """The pinned settings in plain words, for the reply."""
    parts = []
    if any(p.startswith("package.json#") for p in pins):
        parts.append("the test settings in package.json")
    if any(p in PYTEST_PINS or p in PYTEST_FILES for p in pins):
        parts.append("pytest's settings")
    if any(p.startswith("Cargo.toml#") for p in pins):
        parts.append("the test settings in Cargo.toml")
    return " and ".join(parts)


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


def toml_text(run: str, patterns: list, outputs=(), pins=()) -> str:
    """necro.toml for one contract, C1, with a plain header saying it was generated and can be edited.
    Strings are written as JSON strings, which TOML reads the same way."""
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731
    lines = [
        "# Written by /necro freeze from the tests it found here. Edit it as you like: run is the command",
        "# that must pass, files are what Hermes cannot edit. Then type /necro freeze again to lock the new",
        "# version, or /necro unfreeze to remove the lock.",
    ]
    if pins:
        lines += ["# pins lock the settings that shape the run (a test script, pytest's settings) by field: a change",
                  "# to one counts as tampering, while other edits in the same file, such as dependencies, are fine."]
    lines.append("")
    if outputs:
        lines += ["# Files a test run may write, such as coverage data; changes to them do not count.",
                  f"outputs = [{', '.join(q(o) for o in outputs)}]", ""]
    lines += ["[[contract]]", 'id = "C1"', 'says = "Your tests pass"', f"run = {q(run)}",
              f"files = [{', '.join(q(p) for p in patterns)}]"]
    if pins:
        lines.append(f"pins = [{', '.join(q(p) for p in pins)}]")
    lines.append("")
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


# Linear, line-by-line patterns only: test output can carry anything the code under test prints.
_COUNT = re.compile(r"(\d+) (failed|passed|errors?|total)\b")
_PYTEST_TIME = re.compile(r" in [\d.]+s\b")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in")
_UNITTEST_KIND = re.compile(r"(failures|errors)=(\d+)")
_NODE = re.compile(r"^[#\u2139] (tests|fail) (\d+)$")  # TAP (#) and spec (\u2139) reporters
_CARGO = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed;")


def test_counts(output: str):
    """(failing, total) from the end of a test run's output (pytest, unittest, jest, vitest, node --test or
    cargo), or None when it cannot tell. Reads line by line with linear patterns."""
    lines = (output or "").splitlines()
    for line in reversed(lines):  # pytest: "1 failed, 3 passed in 0.12s"
        if _PYTEST_TIME.search(line) and not line.lstrip().startswith(("Ran ", "Tests")):
            parts = {}
            for n, kind in _COUNT.findall(line):
                parts["error" if kind.startswith("error") else kind] = int(n)
            if parts:
                failing = parts.get("failed", 0) + parts.get("error", 0)
                return failing, failing + parts.get("passed", 0)
    ran = [m for m in (_UNITTEST_RAN.match(line) for line in lines) if m]
    if ran:
        failed = [line for line in lines if line.startswith("FAILED (")]
        failing = sum(int(n) for _, n in _UNITTEST_KIND.findall(failed[-1])) if failed else 0
        return failing, int(ran[-1].group(1))
    for line in reversed(lines):
        text = line.strip()
        if text.startswith("Tests:") and "total" in text:  # jest: "Tests:  2 failed, 2 passed, 4 total"
            parts = {kind: int(n) for n, kind in _COUNT.findall(text)}
            return parts.get("failed", 0), parts.get("total", 0)
        if text.startswith("Tests ") and text.endswith(")") and "(" in text:  # vitest: "Tests  2 failed | 2 passed (4)"
            parts = {kind: int(n) for n, kind in _COUNT.findall(text)}
            total = text.rsplit("(", 1)[1][:-1]
            if total.isdigit():
                return parts.get("failed", 0), int(total)
    node = dict(m.groups() for m in (_NODE.match(line) for line in lines) if m)
    if "tests" in node:
        return int(node.get("fail", 0)), int(node["tests"])
    cargo = [m.groups() for m in (_CARGO.match(line) for line in lines) if m]
    if cargo:
        passed, failed = sum(int(p) for p, _ in cargo), sum(int(f) for _, f in cargo)
        return failed, passed + failed
    return None
