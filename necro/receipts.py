"""Receipts: a content-addressed record that every frozen contract passed on exactly these files."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from .canon import canonical_json, hash_file, sha256_text, utc_now, write_json_atomic
from .lock import frozen_paths, lock_hash
from .runner import all_pass
from .spec import expand, matches, skipped, walk_files

RECEIPTS_REL = ".necro/receipts"
RECEIPT_KIND = "hermes-necromancer/receipt"
UNHASHED = ("receiptHash", "createdAt", "trigger")
MAX_SOURCES = 20000


class SourcesError(ValueError):
    """The project has too many files to pin; narrow them with sources = [...]."""


# Repository config must not run anything while necro reads a project (an untrusted clone, say).
_GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null"]


def _git(root: Path, *args) -> str | None:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        out = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *args], capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def git_listing(root: Path) -> list | None:
    """Inside a git work tree: tracked files plus untracked files git does not ignore, relative to root
    (git runs with fsmonitor and hooks off). None outside git or when git is not available."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.strip() != "true":
        return None
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if out is None:
        return None
    files = []
    for name in out.split("\0"):
        if not name:
            continue
        try:
            if stat.S_ISREG(os.stat(root / name).st_mode):
                files.append(name)
        except OSError:
            continue
    return files


def git_head(root: Path) -> str | None:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if not out:
        return None
    return out.strip() or None


def source_files(root, lk: dict, files=None, deadline=None) -> list:
    """Project files a receipt pins: the declared ``sources`` globs; else, in a git work tree, what git
    tracks plus untracked files it does not ignore (so a fresh clone verifies); else every regular file
    in the project. Always minus caches, installed dependencies, credential files, the .necro folder,
    the files a person declared under outputs and the frozen contract files, pinned on their own."""
    root = Path(root).resolve()
    frozen = frozen_paths(lk)
    if lk.get("sources"):
        if files is None:
            files = walk_files(root, deadline)
        files = expand(root, lk["sources"], strict=False, where="sources", files=files)
    else:
        listed = git_listing(root)
        if listed is not None:
            files = listed
        elif files is None:
            files = walk_files(root, deadline)
    outputs = lk.get("outputs") or []
    out = sorted(rel for rel in files
                 if rel not in frozen and not skipped(rel) and not any(matches(rel, pat) for pat in outputs))
    if len(out) > MAX_SOURCES:
        raise SourcesError(
            f"{len(out)} files would be pinned; a person should add sources = [\"src/**/*.py\", ...] to "
            "necro.toml to name the ones that matter"
        )
    return out


def pin(root: Path, rels, deadline=None) -> list:
    return [{"path": rel, "sha256": hash_file(root / rel, deadline)} for rel in rels]


def receipt_hash(receipt: dict) -> str:
    return sha256_text(canonical_json({k: v for k, v in receipt.items() if k not in UNHASHED}))


def build_receipt(root, lk: dict, results, trigger: str | None = None, now: str | None = None,
                  pins=None) -> dict:
    """A receipt for a run in which every frozen contract passed. Refuses anything less.

    ``pins`` are the source hashes to record (taken by the caller after the run)."""
    root = Path(root).resolve()
    results = list(results)
    by_id = {r.id: r for r in results}
    ids = [c["id"] for c in lk["contracts"]]
    if not all_pass(results) or sorted(by_id) != sorted(ids):
        raise ValueError("a receipt is written only when every frozen contract passed")
    receipt = {
        "kind": RECEIPT_KIND,
        "version": 1,
        "lock": {"sha256": lock_hash(lk), "spec": lk.get("spec")},
        "contracts": [
            {
                "id": c["id"],
                "says": c.get("says", ""),
                "run": c["run"],
                "files": [{"path": f["path"], "sha256": f["sha256"]} for f in c["files"]],
                "result": by_id[c["id"]].status,
                "exitCode": by_id[c["id"]].exit_code,
            }
            for c in lk["contracts"]
        ],
        "sources": list(pins) if pins is not None else pin(root, source_files(root, lk)),
        "gitCommit": git_head(root),
    }
    receipt["receiptHash"] = receipt_hash(receipt)
    receipt["createdAt"] = now or utc_now()
    receipt["trigger"] = trigger
    return receipt


def receipt_file(root, receipt_hash_hex: str) -> Path:
    return Path(root) / RECEIPTS_REL / f"{receipt_hash_hex}.json"


def write_receipt(root, receipt: dict) -> Path:
    """Write the receipt under its own hash. The same state written twice keeps the first file."""
    path = receipt_file(root, receipt["receiptHash"])
    if not path.is_file():
        write_json_atomic(path, receipt)
    return path


def list_receipts(root) -> list:
    """Receipt files, newest first."""
    folder = Path(root) / RECEIPTS_REL
    if not folder.is_dir():
        return []
    entries = []
    for path in folder.glob("*.json"):
        try:
            created = json.loads(path.read_text(encoding="utf-8")).get("createdAt") or ""
        except (OSError, ValueError):
            created = ""
        entries.append((created, path.stat().st_mtime, path))
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    return [e[2] for e in entries]
