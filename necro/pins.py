"""Pins: the settings that shape a test run, locked without freezing the whole file they live in.

    pins = ["package.json#scripts.test", "pyproject.toml#tool.pytest", "setup.cfg#[tool:pytest]",
            "pytest.ini", "**/conftest.py"]

A pin names a file, or a field in one. A field is a dotted path in a .json or .toml file
("scripts.test", "tool.pytest", "bin.*.test", where * means every item and a segment in double quotes
may hold a dot) or a [section] of a .cfg or .ini file. The lock keeps a hash of what the pin sees: the
field's value, or that it is missing; a whole file's bytes, or that it is absent, so adding it later
counts; for a glob, the files it matches. A change to any of them counts as tampering, while edits
elsewhere in the same file (dependencies, say) stay allowed. A file that cannot be parsed fails closed,
and a pin never opens a credential file or reads through a link."""

from __future__ import annotations

import configparser
import json
import os
import re
import stat
import tomllib
from pathlib import Path

from .canon import canonical_json, hash_file, is_credential, sha256_text

GLOB_CHARS = set("*?[")
FIELD_FORMATS = {".json": "JSON", ".toml": "TOML", ".cfg": "INI", ".ini": "INI"}
MAX_BYTES = 1 << 20
_SECTION = re.compile(r"^\[([^\[\]]+)\]$")
_SEGMENT = re.compile(r'"((?:[^"\\]|\\.)*)"|([^.]+)')


class PinError(ValueError):
    """A pin cannot be read: a bad pin, a file that does not parse, a link or a credential file."""


def split_pin(text: str) -> tuple:
    """(file, field or None). Raises PinError for a malformed pin."""
    if not isinstance(text, str) or not text.strip():
        raise PinError("a pin must be a non-empty string")
    file, _, field = text.partition("#")
    if not file or file.startswith(("/", "~")) or "\\" in file or ".." in Path(file).parts:
        raise PinError(f"'{text}' must name a file inside the project")
    if not field:
        return file, None
    if GLOB_CHARS & set(file):
        raise PinError(f"'{text}': a field pin names one file, not a glob")
    fmt = FIELD_FORMATS.get(Path(file).suffix.lower())
    if fmt is None:
        raise PinError(f"'{text}': only .json, .toml, .cfg and .ini files can have a field pinned")
    if fmt == "INI" and not _SECTION.match(field):
        raise PinError(f"'{text}': in a .cfg or .ini file, pin a whole [section]")
    if fmt != "INI":
        _segments(field, text)
    return file, field


def _segments(field: str, text: str) -> list:
    out, i = [], 0
    while i <= len(field):
        m = _SEGMENT.match(field, i)
        if not m:
            raise PinError(f"'{text}': '{field}' is not a dotted path")
        out.append(json.loads(f'"{m.group(1)}"') if m.group(1) is not None else m.group(2))
        i = m.end()
        if i == len(field):
            return out
        if field[i] != ".":
            raise PinError(f"'{text}': '{field}' is not a dotted path")
        i += 1
    raise PinError(f"'{text}': '{field}' is not a dotted path")


def _dig(data, segs):
    if not segs:
        return data
    head, rest = segs[0], segs[1:]
    if head == "*":
        if isinstance(data, list):
            return [_dig(x, rest) for x in data]
        if isinstance(data, dict):
            return {k: _dig(v, rest) for k, v in sorted(data.items())}
        return None
    if isinstance(data, dict) and head in data:
        return _dig(data[head], rest)
    return None


def _read_text(path: Path, rel: str) -> str | None:
    """The file's text, None when it is absent. Never a credential file, never through a link."""
    if is_credential(os.path.basename(rel)):
        raise PinError(f"{rel} is a credential file, which Contract Necromancer never opens")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PinError(f"{rel} cannot be read ({exc.strerror or exc})") from None
    if stat.S_ISLNK(st.st_mode):
        raise PinError(f"{rel} is a link, which Contract Necromancer never follows")
    if not stat.S_ISREG(st.st_mode):
        raise PinError(f"{rel} is not a regular file")
    if st.st_size > MAX_BYTES:
        raise PinError(f"{rel} is larger than 1 MB")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            return handle.read(MAX_BYTES + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PinError(f"{rel} cannot be read ({exc})") from None


def _field_value(root: Path, file: str, field: str):
    text = _read_text(root / file, file)
    if text is None:
        return ["absent"]
    fmt = FIELD_FORMATS[Path(file).suffix.lower()]
    try:
        if fmt == "JSON":
            data = json.loads(text)
        elif fmt == "TOML":
            data = tomllib.loads(text)
        else:
            parser = configparser.RawConfigParser(strict=False, interpolation=None)
            parser.read_string(text, source=file)
            section = _SECTION.match(field).group(1)
            if not parser.has_section(section):
                return ["field", None]
            return ["field", {k: parser.get(section, k, raw=True) for k in parser.options(section)}]
    except (ValueError, configparser.Error) as exc:
        raise PinError(f"{file} cannot be read as {fmt} ({str(exc)[:160]})") from None
    return ["field", _dig(data, _segments(field, file + "#" + field))]


def _file_value(root: Path, rel: str):
    path = root / rel
    if os.path.islink(path):
        return ["link", os.readlink(path)]
    if not os.path.lexists(path):
        return ["absent"]
    if is_credential(os.path.basename(rel)):
        raise PinError(f"{rel} is a credential file, which Contract Necromancer never opens")
    digest = hash_file(path)
    if digest is None:
        raise PinError(f"{rel} cannot be read")
    return ["file", digest]


def observe(root, text: str, files=None):
    """What a pin sees now, as plain data (see the module note). Raises PinError."""
    from .spec import glob_regex, walk_files  # spec imports this module
    root = Path(root)
    file, field = split_pin(text)
    if field is not None:
        return _field_value(root, file, field)
    if GLOB_CHARS & set(file):
        listing = files if files is not None else walk_files(root)
        rx = glob_regex(file)
        out = []
        for rel in sorted(r for r in listing if rx.match(r)):
            if is_credential(os.path.basename(rel)) and not os.path.islink(root / rel):
                out.append([rel, "credential"])
            else:
                out.append([rel] + _file_value(root, rel))
        return ["files", out]
    return _file_value(root, file)


def value_hash(root, text: str, files=None) -> str:
    return sha256_text(canonical_json(observe(root, text, files)))


def pinned_file(text: str) -> str:
    return text.partition("#")[0]


ABSENT_HASH = sha256_text(canonical_json(["absent"]))


def changed_message(text: str, stored: str, now) -> str:
    """Why a pin no longer matches its stored hash, in plain words; ``now`` is what observe() sees now."""
    file, _, field = text.partition("#")
    name = Path(file).name
    if field:
        if name == "package.json" and field.startswith("scripts."):
            what = "the test command"
        elif name in ("package.json", "Cargo.toml"):
            what = "the test settings"
        elif field.startswith("tool.pytest") or (name, field) in {("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")}:
            what = "the pytest settings"
        else:
            what = "a locked setting"
        return f"{what} in {file} changed ({field})"
    if GLOB_CHARS & set(file):
        matched = [entry[0] for entry in now[1]] if isinstance(now, list) and now[:1] == ["files"] else []
        return (f"the files matching {file}, which shape the test run, changed (now: "
                f"{', '.join(matched[:5]) or 'none'}{' and more' if len(matched) > 5 else ''})")
    if stored == ABSENT_HASH:
        return f"{file} was added, and it can change how the tests run"
    if now == ["absent"]:
        return f"missing: {file}"
    return f"changed since freeze: {file}"
