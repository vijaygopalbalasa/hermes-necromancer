"""Receipts: a content-addressed record that every frozen contract passed on exactly these files."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .canon import canonical_json, hash_file, sha256_text, utc_now, write_json_atomic
from .lock import frozen_paths, lock_hash
from .runner import all_pass
from .spec import expand

RECEIPTS_REL = ".necro/receipts"
RECEIPT_KIND = "hermes-necromancer/receipt"
UNHASHED = ("receiptHash", "createdAt", "trigger")
SKIP_DIRS = {
    ".git", ".necro", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".idea", ".vscode", ".gradle", ".next", ".turbo",
}
SKIP_SUFFIXES = (".pyc", ".pyo")
SKIP_NAMES = {".DS_Store"}
MAX_SOURCES = 20000


class SourcesError(ValueError):
    """The project has too many files to pin; narrow them with sources = [...]."""


def _git(root: Path, *args) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def git_head(root: Path) -> str | None:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if not out:
        return None
    return out.strip() or None


def _listing(root: Path) -> list:
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside and inside.strip() == "true":
        # Paths come back relative to root, which is git's working directory here.
        out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if out is not None:
            return [name for name in out.split("\0") if name and (root / name).is_file()]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            files.append(rel)
            if len(files) > MAX_SOURCES * 2:
                break
    return files


def source_files(root, lk: dict) -> list:
    """Project files a receipt pins: the declared ``sources`` globs, else every file in the project
    (git's view when it is a repository: tracked plus untracked, minus ignored), minus caches, the
    .necro folder and the frozen contract files, which are pinned on their own."""
    root = Path(root).resolve()
    frozen = frozen_paths(lk)
    if lk.get("sources"):
        files = expand(root, lk["sources"], strict=False, where="sources")
    else:
        files = _listing(root)
    out = set()
    for rel in files:
        parts = rel.split("/")
        if SKIP_DIRS & set(parts[:-1]) or parts[-1] in SKIP_NAMES or rel.endswith(SKIP_SUFFIXES):
            continue
        if rel in frozen:
            continue
        out.add(rel)
    if len(out) > MAX_SOURCES:
        raise SourcesError(
            f"{len(out)} files would be pinned; add sources = [\"src/**/*.py\", ...] to necro.toml to name the ones that matter"
        )
    return sorted(out)


def pin(root: Path, rels) -> list:
    return [{"path": rel, "sha256": hash_file(root / rel)} for rel in rels]


def receipt_hash(receipt: dict) -> str:
    return sha256_text(canonical_json({k: v for k, v in receipt.items() if k not in UNHASHED}))


def build_receipt(root, lk: dict, results, trigger: str | None = None, now: str | None = None) -> dict:
    """A receipt for a run in which every frozen contract passed. Refuses anything less."""
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
        "sources": pin(root, source_files(root, lk)),
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
