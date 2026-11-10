"""necro.toml: the acceptance contracts a person writes (or reviews) before freezing them.

    sources = ["src/**/*.py"]          # optional: files a receipt pins (default: the whole project)

    [[contract]]
    id = "C1"                          # short, unique
    says = "Shares add up to the total"  # what must be true, in plain words
    run = "python3 -m pytest tests/acceptance/test_split.py -q"   # exit code 0 means pass
    files = ["tests/acceptance/test_split.py"]                    # the files this contract freezes
    timeout = 120                      # optional, seconds
    env = ["DATABASE_URL"]             # optional: variables passed through to the command
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SPEC_NAME = "necro.toml"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GLOB_CHARS = set("*?[")
SKIP_DIRS = {".git", ".necro"}
CONTRACT_KEYS = {"id", "says", "run", "files", "timeout", "env"}
TOP_KEYS = {"contract", "sources"}


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


def expand(root, patterns, strict: bool = False, where: str = "files") -> list:
    """Project-relative POSIX paths of the files the patterns name, sorted and unique.

    A pattern is a relative path or a glob (``**`` recurses). Matches inside .git or .necro are
    ignored. With ``strict`` a pattern that matches no file raises SpecError."""
    root = Path(root).resolve()
    found = set()
    for pattern in patterns:
        _check_pattern(pattern, where)
        if GLOB_CHARS & set(pattern):
            matches = [p for p in root.glob(pattern) if p.is_file()]
        else:
            candidate = root / pattern
            matches = [candidate] if candidate.is_file() else []
        hits = 0
        for match in matches:
            rel = match.relative_to(root)
            if SKIP_DIRS & set(rel.parts[:-1]) or rel.parts[0] in SKIP_DIRS:
                continue
            if not _inside(root, match):
                raise SpecError(f"{where}: '{rel.as_posix()}' points outside the project")
            found.add(rel.as_posix())
            hits += 1
        if strict and hits == 0:
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
    sources = data.get("sources")
    if sources is not None:
        if not isinstance(sources, list) or not sources:
            raise SpecError(f"{SPEC_NAME}: 'sources' must be a non-empty list of file patterns")
        for pattern in sources:
            _check_pattern(pattern, "sources")
    return Spec(contracts=contracts, sources=list(sources) if sources is not None else None)
