"""necro.toml: the acceptance contracts a person writes (or reviews) before freezing them.

    sources = ["src/**/*.py"]          # optional: files a receipt pins (default: the whole project)
    outputs = ["reports/*.xml"]        # optional: files the contracts may write while they run

    [[contract]]
    id = "C1"                          # short, unique
    says = "Shares add up to the total"  # what must be true, in plain words
    run = "python3 -m pytest tests/acceptance/test_split.py -q"   # exit code 0 means pass
    files = ["tests/acceptance/test_split.py"]                    # the files this contract freezes
    timeout = 120                      # optional, seconds
    env = ["DATABASE_URL"]             # optional: variables passed through to the command
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .canon import BudgetExceeded, check_deadline  # noqa: F401  (re-exported)

SPEC_NAME = "necro.toml"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GLOB_CHARS = set("*?[")
# Folders never walked: version control, Contract Necromancer's own folder, caches and installed
# dependencies. Everything else in the project counts, whatever .gitignore says.
SKIP_DIRS = {
    ".git", ".necro", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".idea", ".vscode", ".gradle", ".next", ".turbo", ".nuxt",
    ".svelte-kit", ".parcel-cache", ".cache", ".terraform", "bower_components", "target", "coverage",
}
SKIP_SUFFIXES = (".pyc", ".pyo")
SKIP_NAMES = {".DS_Store"}
MAX_FILES = 50000
CONTRACT_KEYS = {"id", "says", "run", "files", "timeout", "env"}
TOP_KEYS = {"contract", "sources", "outputs"}


def skipped(rel: str) -> bool:
    parts = rel.split("/")
    return bool(SKIP_DIRS & set(parts[:-1])) or parts[-1] in SKIP_NAMES or rel.endswith(SKIP_SUFFIXES)


def walk_files(root, deadline=None) -> list:
    """Project-relative POSIX paths of every regular file under root, minus SKIP_DIRS and caches.
    Symbolic links to files count (their target is what gets hashed); FIFOs, sockets, devices and
    broken or looping links do not."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        check_deadline(deadline)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                if not stat.S_ISREG(os.stat(full).st_mode):
                    continue
            except OSError:
                continue
            rel = Path(full).relative_to(root).as_posix()
            if not skipped(rel):
                out.append(rel)
            if len(out) > MAX_FILES:
                return out
    return out


@lru_cache(maxsize=512)
def glob_regex(pattern: str):
    """A regex for a glob anchored at the project root: ``*`` and ``?`` stay inside one folder,
    ``**`` spans folders, ``[...]`` is a character class. The same on every Python version."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2:]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(rel: str, pattern: str, ignore_case: bool = False) -> bool:
    """True when a project-relative path is named by a pattern (a glob, or a plain path)."""
    if GLOB_CHARS & set(pattern):
        return re.match(glob_regex(pattern).pattern, rel, re.IGNORECASE if ignore_case else 0) is not None
    return rel.lower() == pattern.lower() if ignore_case else rel == pattern


class SpecError(ValueError):
    """necro.toml is missing or does not describe valid contracts."""


@dataclass(frozen=True)
class Contract:
    id: str
    says: str
    run: str
    files: list = field(default_factory=list)
    timeout: int | None = None
    env: list = field(default_factory=list)


@dataclass(frozen=True)
class Spec:
    contracts: list
    sources: list | None
    outputs: list | None = None


def _check_pattern(pattern: str, where: str) -> None:
    if not isinstance(pattern, str) or not pattern.strip():
        raise SpecError(f"{where}: a file pattern must be a non-empty string")
    if pattern.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", pattern) or "\\" in pattern:
        raise SpecError(f"{where}: '{pattern}' is outside the project (use a path relative to necro.toml)")
    if ".." in Path(pattern).parts:
        raise SpecError(f"{where}: '{pattern}' is outside the project (no '..')")


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def expand(root, patterns, strict: bool = False, where: str = "files", files=None, deadline=None) -> list:
    """Project-relative POSIX paths of the files the patterns name, sorted and unique.

    A pattern is a path relative to the project root or a glob anchored there (``**`` spans
    folders). Caches such as __pycache__ never match. With ``strict`` (freezing) a pattern that
    matches nothing, or a match that is a link pointing outside the project, raises SpecError;
    otherwise such links are simply included, so their target is hashed like any file."""
    root = Path(root).resolve()
    found = set()
    for pattern in patterns:
        _check_pattern(pattern, where)
        if GLOB_CHARS & set(pattern):
            if files is None:
                files = walk_files(root, deadline)
            hits = [rel for rel in files if glob_regex(pattern).match(rel)]
        else:
            rel = Path(pattern).as_posix()
            full = root / rel
            try:
                regular = stat.S_ISREG(os.stat(full).st_mode)
            except OSError:
                regular = False
            hits = [rel] if regular and not skipped(rel) else []
        for rel in hits:
            if strict and not _inside(root, root / rel):
                raise SpecError(f"{where}: '{rel}' points outside the project")
            found.add(rel)
        if strict and not hits:
            raise SpecError(f"{where}: '{pattern}' matches no file")
    return sorted(found)


def _contract(raw, index: int, root: Path) -> Contract:
    where = f"contract #{index + 1}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: expected a [[contract]] table")
    unknown = sorted(set(raw) - CONTRACT_KEYS)
    if unknown:
        raise SpecError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(CONTRACT_KEYS))})")
    cid = raw.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise SpecError(f"{where}: 'id' must be 1 to 40 letters, digits, '.', '_' or '-'")
    where = f"contract {cid}"
    says = raw.get("says", "")
    if not isinstance(says, str):
        raise SpecError(f"{where}: 'says' must be a string")
    run = raw.get("run")
    if not isinstance(run, str) or not run.strip():
        raise SpecError(f"{where}: 'run' must be the command that checks it (exit code 0 means pass)")
    try:
        if not shlex.split(run):
            raise ValueError("empty")
    except ValueError as exc:
        raise SpecError(f"{where}: 'run' cannot be parsed as a command ({exc})") from None
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise SpecError(f"{where}: 'files' must list the contract's own files, so they can be frozen")
    expand(root, files, strict=True, where=where)
    timeout = raw.get("timeout")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600):
        raise SpecError(f"{where}: 'timeout' must be a whole number of seconds from 1 to 3600")
    env = raw.get("env", [])
    if not isinstance(env, list) or not all(isinstance(e, str) and ENV_RE.match(e) for e in env):
        raise SpecError(f"{where}: 'env' must be a list of variable names")
    return Contract(id=cid, says=says.strip(), run=run.strip(), files=list(files), timeout=timeout, env=list(env))


def load_spec(root) -> Spec:
    root = Path(root).resolve()
    path = root / SPEC_NAME
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"no {SPEC_NAME} in {root}") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SpecError(f"{SPEC_NAME} is not valid TOML: {exc}") from None
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        raise SpecError(f"{SPEC_NAME}: unknown key(s) {', '.join(unknown)} (allowed: contract, sources)")
    raw_contracts = data.get("contract")
    if not isinstance(raw_contracts, list) or not raw_contracts:
        raise SpecError(f"{SPEC_NAME} has no [[contract]] tables")
    contracts = [_contract(raw, i, root) for i, raw in enumerate(raw_contracts)]
    seen = set()
    for c in contracts:
        if c.id in seen:
            raise SpecError(f"{SPEC_NAME}: the id {c.id} is used twice")
        seen.add(c.id)
    lists = {}
    for key in ("sources", "outputs"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, list) or not value:
                raise SpecError(f"{SPEC_NAME}: '{key}' must be a non-empty list of file patterns")
            for pattern in value:
                _check_pattern(pattern, key)
            lists[key] = list(value)
    return Spec(contracts=contracts, sources=lists.get("sources"), outputs=lists.get("outputs"))
