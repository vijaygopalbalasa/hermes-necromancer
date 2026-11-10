"""The pre_tool_call guard: stop the agent's ordinary routes to rewriting a frozen contract.

This is a guard rail, not a sandbox. A determined shell command can still change a file; the gate
then sees the hash differ and counts the contract as failing. The guard just makes the honest path
the easy one and says why."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .lock import LOCK_REL, LockError, find_root, frozen_paths, frozen_patterns, read_lock

FILE_TOOLS = {"write_file", "patch"}
V4A_FILE = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
V4A_MOVE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)
PERSON_ONLY = re.compile(r"\bnecro\b[\s\S]*?\b(?:freeze|unfreeze|approve)\b", re.IGNORECASE)
SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
REDIRECT = re.compile(r"(?<![<>&0-9])(?:\d|&)?>>?\|?\s*([^\s;|&<>]+)")
WRITE_COMMANDS = {
    "rm", "mv", "tee", "truncate", "chmod", "chown", "ln", "unlink", "dd", "touch", "shred", "patch",
    "rmdir", "ed", "ex", "vi", "vim", "nvim", "nano", "emacs",
}
LAST_ARG_COMMANDS = {"cp", "install", "rsync", "scp"}
GIT_WRITES = {"checkout", "restore", "rm", "mv", "reset", "stash", "apply", "am", "clean", "switch"}
CODE_WRITES = re.compile(
    r"open\([^,)]*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+]|write_text|write_bytes|\.unlink\(|\bos\.(?:remove|unlink|"
    r"rename|replace|truncate|chmod)\b|\bshutil\.|\.rename\(|\.replace\(|writeFile|unlinkSync|rmSync|renameSync|"
    r"truncateSync|\bfs\.(?:rm|write|unlink|rename|truncate)",
)

_LOCK_CACHE: dict = {}


def _lock_for(root: Path):
    path = root / LOCK_REL
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(root)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _LOCK_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        lk = read_lock(root)
    except LockError:
        lk = {"contracts": [], "spec": {}}
    _LOCK_CACHE[key] = (stamp, lk)
    return lk


def _resolve(path_str: str, cwd: str) -> Path:
    p = Path(os.path.expanduser(str(path_str).strip()))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    return Path(os.path.realpath(p))


def _rel(root: Path, p: Path) -> str | None:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


def _is_frozen(root: Path, rel: str, lk: dict) -> bool:
    if rel == ".necro" or rel.startswith(".necro/"):
        return True
    if rel in frozen_paths(lk):
        return True
    for pattern in frozen_patterns(lk):
        if any(ch in pattern for ch in "*?[") and Path(rel).match(pattern):
            return True
        if pattern.endswith("/**") and rel.startswith(pattern[:-3] + "/"):
            return True
    return False


def _block(rel: str) -> str:
    if rel == ".necro" or rel.startswith(".necro/"):
        what = f"{rel} belongs to Contract Necromancer (the frozen lock and the receipts)"
    else:
        what = f"{rel} is a frozen acceptance contract"
    return (f"Blocked by Contract Necromancer: {what}. A person froze it so that \"done\" means the same thing "
            "before and after your work. Change the code under test, not the contract. If the contract itself is "
            "wrong, say so and ask the person to change it and run /necro freeze again.")


def _frozen_target(path_str: str, cwd: str) -> str | None:
    if not path_str:
        return None
    p = _resolve(path_str, cwd)
    root = find_root(p)
    if root is None:
        return None
    rel = _rel(root, p)
    lk = _lock_for(root)
    if rel is None or lk is None:
        return None
    return _block(rel) if _is_frozen(root, rel, lk) else None


def file_targets(tool_name: str, args: dict) -> list:
    if tool_name not in FILE_TOOLS:
        return []
    mode = "replace" if tool_name == "write_file" else (args.get("mode") or "replace")
    if mode == "patch":
        body = args.get("patch") or ""
        if not isinstance(body, str):
            return []
        paths = [m.group(1).strip() for m in V4A_FILE.finditer(body)]
        for m in V4A_MOVE.finditer(body):
            paths += [m.group(1).strip(), m.group(2).strip()]
        return [p for p in paths if p]
    p = args.get("path")
    return [str(p)] if p else []


def _mentions(text: str, root: Path, lk: dict, wd: str) -> list:
    """Frozen paths (or the .necro folder) that a command or snippet names."""
    hits = []
    candidates = sorted(frozen_paths(lk)) + [".necro"]
    for rel in candidates:
        absolute = str(root / rel)
        spellings = {rel, absolute, os.path.relpath(absolute, wd) if wd else rel}
        if any(s and s in text for s in spellings):
            hits.append(rel)
    return hits


def _words(segment: str) -> list:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _command_name(words: list) -> tuple:
    i = 0
    while i < len(words) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]) or words[i] in ("sudo", "env", "command", "exec", "nohup", "time")):
        i += 1
    if i >= len(words):
        return "", []
    return os.path.basename(words[i]), words[i + 1:]


def _shell_block(cmd: str, wd: str) -> str | None:
    if PERSON_ONLY.search(cmd):
        return ("Blocked by Contract Necromancer: freezing, unfreezing or approving contracts is a person's job, so it "
                "cannot run from the agent's shell. Ask the person to run it.")
    roots = []
    root = find_root(wd) if wd else None
    if root:
        roots.append(root)
    for token in _words(cmd):
        if token.startswith(("/", "~")):
            r = find_root(_resolve(token, wd))
            if r and r not in roots:
                roots.append(r)
    for root in roots:
        lk = _lock_for(root)
        if not lk:
            continue
        if not _mentions(cmd, root, lk, wd):
            continue
        for m in REDIRECT.finditer(cmd):
            target = m.group(1)
            if target.startswith("&"):
                continue
            rel = _rel(root, _resolve(target, wd))
            if rel is not None and _is_frozen(root, rel, lk):
                return _block(rel)
        for segment in SEGMENT_SPLIT.split(cmd):
            name, rest = _command_name(_words(segment))
            if not name:
                continue
            named = _mentions(segment, root, lk, wd)
            if not named:
                continue
            args = [a for a in rest if not a.startswith("-")]
            if name in WRITE_COMMANDS:
                return _block(named[0])
            if name in ("sed", "perl", "gsed") and any(re.match(r"^-[A-Za-z]*i", a) or a.startswith("--in-place") for a in rest):
                return _block(named[0])
            if name in LAST_ARG_COMMANDS and args:
                rel = _rel(root, _resolve(args[-1], wd))
                if rel is not None and _is_frozen(root, rel, lk):
                    return _block(rel)
            if name == "git" and args and args[0] in GIT_WRITES:
                return _block(named[0])
            if CODE_WRITES.search(segment):
                return _block(named[0])
    return None


def _code_block(code: str, cwd: str) -> str | None:
    root = find_root(cwd) if cwd else None
    if root is None:
        return None
    lk = _lock_for(root)
    if not lk:
        return None
    named = _mentions(code, root, lk, cwd)
    if named and CODE_WRITES.search(code):
        return _block(named[0])
    return None


def check_tool_call(tool_name: str, args, cwd: str) -> str | None:
    """A block message when a tool call would rewrite a frozen contract, else None. Never raises."""
    if not isinstance(args, dict):
        return None
    if tool_name in FILE_TOOLS:
        for target in file_targets(tool_name, args):
            msg = _frozen_target(target, cwd)
            if msg:
                return msg
        return None
    if tool_name == "terminal":
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        wd = args.get("workdir") or cwd
        return _shell_block(cmd, str(_resolve(wd, cwd)) if wd else cwd)
    if tool_name == "execute_code":
        code = args.get("code")
        return _code_block(code, cwd) if isinstance(code, str) else None
    return None
