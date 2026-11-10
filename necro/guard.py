"""The pre_tool_call guard: stop the agent's ordinary routes to rewriting a frozen contract.

This is a guard rail, not a sandbox. A determined shell command can still change a file; the gate
then sees the hash differ and counts the contract as failing. The guard makes the honest path the
easy one and says why. It only blocks when a frozen file (or .necro/, or the approval store) is the
thing being written; reading, running and git restores stay allowed, and the hash check is the
arbiter either way. Paths compare without regard to case, because macOS and Windows disks do."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

from .lock import LOCK_REL, LockError, find_root, frozen_paths, frozen_patterns, read_lock
from .spec import GLOB_CHARS, matches

FILE_TOOLS = {"write_file", "patch"}
V4A_FILE = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
V4A_MOVE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)
PERSON_ONLY = {"freeze", "unfreeze", "approve"}
SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&\n]")
REDIRECT = re.compile(r"(?<![<>&0-9])(?:\d|&)?>>?\|?\s*([^\s;|&<>]+)")
WRITE_COMMANDS = {
    "rm", "mv", "tee", "truncate", "chmod", "chown", "ln", "unlink", "dd", "touch", "shred", "patch",
    "rmdir", "ed", "ex", "vi", "vim", "nvim", "nano", "emacs", "trash", "srm",
}
# Detaching or scrubbing the environment next to a necro command is how a person-only command would be
# smuggled past the agent-shell marker, so it is refused outright.
EVASION = re.compile(
    r"\benv\s+(?:-[a-z]*[iu]\b|--ignore-environment|--unset)|\bunset\b|\bsetsid\b|\bnohup\b|\bdisown\b|"
    r"\(\s*[^()]*&\s*\)|\bdaemon(?:ize)?\b|\bstart-stop-daemon\b|\bsystemd-run\b|\blaunchctl\b|\bat\s+now\b")
LAST_ARG_COMMANDS = {"cp", "install", "rsync", "scp"}
GIT_WRITES = {"rm", "mv"}
MAX_TEXT = 500_000

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


def _expand(text: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(text).strip()))


def _resolve(path_str: str, cwd: str) -> Path:
    p = Path(_expand(path_str))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    return Path(os.path.realpath(p))


def _rel(root: Path, p: Path) -> str | None:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        pass
    low_root, low_p = str(root).lower().rstrip("/") + "/", str(p).lower()
    if low_p.startswith(low_root):
        return str(p)[len(low_root):].replace(os.sep, "/")
    return None


def _is_frozen(rel: str, lk: dict) -> bool:
    low = rel.lower()
    if low == ".necro" or low.startswith(".necro/"):
        return True
    if low in {p.lower() for p in frozen_paths(lk)}:
        return True
    return any(GLOB_CHARS & set(pattern) and matches(rel, pattern, ignore_case=True) for pattern in frozen_patterns(lk))


def _block(rel: str) -> str:
    if rel.lower() == ".necro" or rel.lower().startswith(".necro/"):
        what = f"{rel} belongs to Contract Necromancer (the frozen lock and the receipts)"
    else:
        what = f"{rel} is a frozen acceptance contract"
    return (f"Blocked by Contract Necromancer: {what}. A person froze it so that \"done\" means the same thing "
            "before and after your work. Change the code under test, not the contract. If the contract itself is "
            "wrong, say so and ask the person to change it and run /necro freeze again.")


TOO_LONG_BLOCK = ("Blocked by Contract Necromancer: this is too long to check inside a frozen project (or it names "
                  "necro or a frozen file). Split it into shorter pieces.")
EVASION_BLOCK = ("Blocked by Contract Necromancer: detaching a process or changing its environment next to a necro "
                 "command is not allowed from the agent's shell.")
STORE_BLOCK = ("Blocked by Contract Necromancer: that is where a person's approvals of frozen contracts are kept. "
               "Only a person changes them, with /necro freeze, /necro approve or /necro unfreeze.")
PERSON_BLOCK = ("Blocked by Contract Necromancer: freezing, unfreezing or approving contracts is a person's job, so it "
                "cannot run from the agent's shell. Ask the person to run it.")


def _in_store(p: Path, store_dir: str | None) -> bool:
    if not store_dir:
        return False
    low = str(p).lower()
    for store in {os.path.realpath(store_dir).lower().rstrip("/"), str(store_dir).lower().rstrip("/")}:
        if low == store or low.startswith(store + "/"):
            return True
    return False


def _store_needles(store_dir: str | None) -> set:
    """Spellings of the approval store's folder that a command or a snippet of code could use."""
    if not store_dir:
        return set()
    out = set()
    home = os.path.realpath(os.path.expanduser("~"))
    for base in {os.path.realpath(store_dir), str(store_dir)}:
        out.add(base.lower())
        if base.startswith(home + "/"):
            tail = base[len(home) + 1:]
            out |= {f"~/{tail}".lower(), f"$home/{tail}".lower(), f"${{home}}/{tail}".lower()}
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        for base in {os.path.realpath(store_dir), str(store_dir)}:
            hh = os.path.realpath(hermes_home)
            if base.startswith(hh + "/"):
                tail = base[len(hh) + 1:]
                out |= {f"$hermes_home/{tail}".lower(), f"${{hermes_home}}/{tail}".lower()}
    return out


def _touches_store(text_low: str, words: list, wd: str, store_dir: str | None) -> bool:
    if not store_dir:
        return False
    if any(n in text_low for n in _store_needles(store_dir)):
        return True
    for token in words:
        if "/" in token or token.startswith(("~", ".", "$")):
            try:
                if _in_store(_resolve(token, wd), store_dir):
                    return True
            except (OSError, ValueError):
                continue
    return False


def _frozen_target(path_str: str, cwd: str, store_dir: str | None = None) -> str | None:
    if not path_str:
        return None
    p = _resolve(path_str, cwd)
    if _in_store(p, store_dir):
        return STORE_BLOCK
    root = find_root(p)
    if root is None:
        return None
    rel = _rel(root, p)
    lk = _lock_for(root)
    if rel is None or lk is None:
        return None
    return _block(rel) if _is_frozen(rel, lk) else None


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


def _spellings(root: Path, rel: str, wd: str) -> set:
    absolute = str(root / rel)
    out = {rel, absolute}
    if wd:
        out.add(os.path.relpath(absolute, wd))
    return {s.lower() for s in out if s}


def _mentions(low_text: str, root: Path, lk: dict, wd: str) -> list:
    """Frozen paths (or the .necro folder) a lower-cased command or snippet names."""
    hits = []
    for rel in sorted(frozen_paths(lk)) + [".necro"]:
        if any(s in low_text for s in _spellings(root, rel, wd)):
            hits.append(rel)
    return hits


def _words(text: str) -> list:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _person_only_text(low: str) -> bool:
    """The raw-text check: "necro" followed anywhere later by freeze, unfreeze or approve. Linear."""
    at = low.find("necro")
    return at >= 0 and ("freeze" in low[at:] or "approve" in low[at:])


def _person_only(words: list) -> bool:
    """True when the words run necro's freeze, unfreeze or approve (quotes already removed)."""
    for i, word in enumerate(words[:-1]):
        w = word.lower().rstrip("/")
        if w == "necro" or w.endswith("/necro") or w.endswith("/necro/__main__.py"):
            for nxt in words[i + 1:]:
                if not nxt.startswith("-"):
                    if nxt.lower() in PERSON_ONLY:
                        return True
                    break
    return False


def _command_name(words: list) -> tuple:
    i = 0
    while i < len(words) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]) or words[i] in ("sudo", "env", "command", "exec", "nohup", "time")):
        i += 1
    if i >= len(words):
        return "", []
    return os.path.basename(_bare(words[i])), words[i + 1:]


FIND_READ_ONLY = {"grep", "cat", "head", "tail", "wc", "ls", "stat", "file"}
FIND_WORD = re.compile(r"(?:^|[^a-z0-9_./-])g?find\b")
FIND_WRITES = re.compile(r"-(?:delete|fprint0|fprintf|fprint|fls)\b")
FIND_RUNS = re.compile(r"(?:^|\s)-(?:execdir|exec|okdir|ok)\b(?:\s+(\S+))?")
FIND_BLOCK = ("Blocked by Contract Necromancer: a find that deletes, writes or runs a program other than a read-only "
              "one is not allowed in or over a frozen project ({root}). Name the files you mean one by one; the frozen "
              "contracts and .necro stay.")


def _find_text_writes(text: str) -> bool:
    """One fail-closed text rule, no parsing: the word find (or gfind) anywhere, together with -delete or a
    -fprint/-fls action, or with -exec/-execdir/-ok/-okdir not followed by exactly grep, cat, head, tail,
    wc, ls, stat or file (case-sensitive, so a path or GREP counts as writing)."""
    low = text.lower()
    if not FIND_WORD.search(low):
        return False
    if FIND_WRITES.search(low):
        return True
    return any(m.group(1) not in FIND_READ_ONLY for m in FIND_RUNS.finditer(text))


def _frozen_near(wd: str, frozen_roots=()) -> Path | None:
    """The frozen project that ``wd`` sits in, or one below it (a find from above would reach it)."""
    if not wd:
        return None
    here = Path(os.path.realpath(wd))
    root = find_root(here)
    if root is not None:
        return root
    for r in frozen_roots or ():
        r = Path(r)
        if here == r or here in r.parents or r in here.parents:
            return r
    return None


def _bare(token: str) -> str:
    """A token without the shell syntax wrapped around it: $( ( ) { } backticks, quotes and ;. Used only to
    spot a command's name."""
    while True:
        before = token
        if token.startswith("$("):
            token = token[2:]
        token = token.lstrip("({`\"'").rstrip(")}`\"';")
        if token == before:
            return token


def _code_writes(low_text: str, spellings: set) -> bool:
    """True when code (Python or JavaScript) writes, deletes or moves one of these exact paths."""
    for s in spellings:
        q = r"""['"]""" + re.escape(s) + r"""['"]"""
        patterns = (
            r"open\(\s*" + q + r"\s*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+]",
            r"path\(\s*" + q + r"\s*\)\s*\.\s*(?:write_text|write_bytes|unlink|rename|replace|touch|chmod|open\(\s*['\"][^'\"]*[wax+])",
            r"os\.(?:remove|unlink|rename|replace|truncate|chmod)\(\s*" + q,
            r"(?:os\.(?:rename|replace)|shutil\.\w+)\([^)]*" + q,
            r"fs(?:\.promises)?\.\w*(?:write|unlink|rm|rename|truncate|append|copy)\w*\(\s*" + q,
        )
        if any(re.search(p, low_text) for p in patterns):
            return True
    return False


def _shell_block(cmd: str, wd: str, store_dir: str | None, frozen_roots=()) -> str | None:
    full_low = cmd.lower()
    if len(cmd) > MAX_TEXT:
        root = find_root(wd) if wd else None
        lk = _lock_for(root) if root else None
        frozen = [rel for rel in (frozen_paths(lk) if lk else ()) if rel.lower() in full_low]
        if (_frozen_near(wd, frozen_roots) is not None or "necro" in full_low or frozen
                or any(n in full_low for n in _store_needles(store_dir))):
            return TOO_LONG_BLOCK
        cmd = cmd[:MAX_TEXT]
    low = cmd.lower()
    if _person_only_text(low) or "necro_agent_session" in low:  # cheap text checks first
        return PERSON_BLOCK
    words = _words(cmd)
    joined = " ".join(words).lower()
    if _person_only(words) or _person_only_text(joined) or "necro_agent_session" in joined:
        return PERSON_BLOCK
    if ("necro" in low or "necro" in joined) and (EVASION.search(low) or EVASION.search(joined)):
        return EVASION_BLOCK
    if _touches_store(low + "\n" + joined, words, wd, store_dir):
        return STORE_BLOCK
    low = low + "\n" + joined  # also the words with quotes removed: .ne''cro is .necro
    roots = []
    root = find_root(wd) if wd else None
    if root:
        roots.append(root)
    for token in words:
        if token.startswith(("/", "~")):
            r = find_root(_resolve(token, wd))
            if r and r not in roots:
                roots.append(r)
    near = _frozen_near(wd, frozen_roots)
    if near is not None and _find_text_writes(cmd):
        return FIND_BLOCK.format(root=near)
    for root in roots:
        lk = _lock_for(root)
        if not lk:
            continue
        if not _mentions(low, root, lk, wd):
            continue
        for m in REDIRECT.finditer(cmd):
            target = m.group(1)
            if target.startswith("&"):
                continue
            rel = _rel(root, _resolve(target, wd))
            if rel is not None and _is_frozen(rel, lk):
                return _block(rel)
        for segment in SEGMENT_SPLIT.split(cmd):
            name, rest = _command_name(_words(segment))
            named = _mentions(segment.lower() + "\n" + " ".join(_words(segment)).lower(), root, lk, wd)
            if not named:
                continue
            args = [a for a in rest if not a.startswith("-")]
            if name in WRITE_COMMANDS:
                return _block(named[0])
            if name in ("sed", "perl", "gsed") and any(re.match(r"^-[A-Za-z]*i", a) or a.startswith("--in-place") for a in rest):
                return _block(named[0])
            if name in LAST_ARG_COMMANDS and args:
                rel = _rel(root, _resolve(args[-1], wd))
                if rel is not None and _is_frozen(rel, lk):
                    return _block(rel)
            if name == "git" and args and args[0] in GIT_WRITES:
                return _block(named[0])
        for rel in _mentions(low, root, lk, wd):
            if _code_writes(low, _spellings(root, rel, wd)):
                return _block(rel)
    return None


def _code_block(code: str, cwd: str, store_dir: str | None, frozen_roots=()) -> str | None:
    low = code.lower()
    if any(n in low for n in _store_needles(store_dir)):
        return STORE_BLOCK
    root = find_root(cwd) if cwd else None
    lk = _lock_for(root) if root else None
    if len(code) > MAX_TEXT:
        named = lk and _mentions(low, root, lk, cwd)
        return TOO_LONG_BLOCK if (_frozen_near(cwd, frozen_roots) is not None or "necro" in low or named) else None
    if _person_only_text(low):
        return PERSON_BLOCK
    near = _frozen_near(cwd, frozen_roots)
    if near is not None and _find_text_writes(code):
        return FIND_BLOCK.format(root=near)
    if root is None or not lk:
        return None
    for rel in _mentions(low, root, lk, cwd):
        if _code_writes(low, _spellings(root, rel, cwd)):
            return _block(rel)
    return None


def check_tool_call(tool_name: str, args, cwd: str, store_dir: str | None = None, frozen_roots=()) -> str | None:
    """A block message when a tool call would rewrite a frozen contract, the .necro folder or the
    approval store, else None. ``frozen_roots`` are the project folders a person froze (from the
    approval store), so a find started above one of them is recognised."""
    if not isinstance(args, dict):
        return None
    if tool_name in FILE_TOOLS:
        for target in file_targets(tool_name, args):
            msg = _frozen_target(target, cwd, store_dir)
            if msg:
                return msg
        return None
    if tool_name == "terminal":
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        wd = args.get("workdir") or cwd
        return _shell_block(cmd, str(_resolve(wd, cwd)) if wd else cwd, store_dir, frozen_roots)
    if tool_name == "execute_code":
        code = args.get("code")
        return _code_block(code, cwd, store_dir, frozen_roots) if isinstance(code, str) else None
    return None
