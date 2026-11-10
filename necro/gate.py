"""The gate: decide whether a "done" may stand, and say why in words the agent and the person can act on."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .canon import canonical_json, hash_file, sha256_text
from .lock import LOCK_REL, LockError, drift, frozen_paths, lock_hash, read_lock
from .receipts import SourcesError, build_receipt, source_files, write_receipt
from .runner import all_pass, run_contracts
from .spec import SpecError, expand

LABEL = "Contract Necromancer"
WORD = {
    "fail": "fails",
    "tampered": "was tampered with",
    "timeout": "timed out",
    "unfinished": "did not finish in time",
    "skipped": "was not run",
    "error": "could not run",
}


@dataclass
class Verdict:
    ok: bool
    root: Path
    lock_hash: str | None = None
    results: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    receipt_path: Path | None = None
    receipt_hash: str | None = None
    cached: bool = False
    digest: str | None = None

    def failing(self) -> list:
        return [r for r in self.results if r.status != "pass"]

    def failing_ids(self) -> list:
        return [r.id for r in self.failing()]

    def to_dict(self) -> dict:
        return {
            "all_pass": self.ok,
            "project": str(self.root),
            "lock": self.lock_hash,
            "problems": list(self.problems),
            "contracts": [r.to_dict() for r in self.results],
            "receipt": str(self.receipt_path) if self.receipt_path else None,
            "receipt_hash": self.receipt_hash,
        }


def state_digest(root: Path, lk: dict) -> str:
    """One hash over everything a verdict depends on: the lock, the frozen files (and any new file
    under a frozen pattern), and every source file the receipt would pin."""
    frozen = set(frozen_paths(lk))
    for c in lk.get("contracts", []):
        try:
            frozen.update(expand(root, c.get("patterns") or [], strict=False))
        except SpecError:
            frozen.add(f"!unexpandable:{c.get('id')}")
    sources = source_files(root, lk)
    return sha256_text(canonical_json({
        "lock": lock_hash(lk),
        "frozen": {p: hash_file(root / p) for p in sorted(frozen)},
        "sources": {p: hash_file(root / p) for p in sources},
    }))


def evaluate(root, *, budget_s: float | None = None, approved=None, cache: dict | None = None,
             trigger: str | None = None) -> Verdict:
    """Run (or reuse) the frozen contracts of the project at ``root`` and write a receipt if all pass.

    ``approved`` is the set of lock hashes a person accepted; None skips that check.
    ``cache`` maps a project to its last complete run, reused while nothing it depends on changed."""
    root = Path(root).resolve()
    if not (root / LOCK_REL).is_file():
        raise FileNotFoundError(f"no {LOCK_REL} in {root}")
    try:
        lk = read_lock(root)
    except LockError as exc:
        return Verdict(False, root, problems=[f"the lock cannot be used: {exc}"])
    lh = lock_hash(lk)
    if approved is not None and lh not in approved:
        return Verdict(False, root, lh, problems=[
            f"the lock {LOCK_REL} ({lh[:12]}) is not approved on this machine. A person freezes contracts with "
            "/necro freeze, or accepts a committed lock after reading it with /necro approve"
        ])
    problems = list(drift(root, lk).spec)
    try:
        digest = state_digest(root, lk)
    except SourcesError as exc:
        return Verdict(False, root, lh, problems=[str(exc)])
    key = str(root)
    hit = cache.get(key) if cache is not None else None
    if hit and hit["digest"] == digest:
        results, cached = hit["results"], True
    else:
        results, cached = run_contracts(root, lk, budget_s=budget_s), False
        if cache is not None and not any(r.status in ("skipped", "unfinished") for r in results):
            cache[key] = {"digest": digest, "results": results}
    ok = not problems and all_pass(results)
    v = Verdict(ok, root, lh, results, problems, cached=cached, digest=digest)
    if ok:
        receipt = build_receipt(root, lk, results, trigger=trigger)
        v.receipt_path = write_receipt(root, receipt)
        v.receipt_hash = receipt["receiptHash"]
    return v


def _last_lines(text: str, n: int = 12) -> str:
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join("    " + ln[:300] for ln in lines[-n:])


def _describe(r) -> str:
    says = f" ({r.says})" if r.says else ""
    if r.status == "fail":
        tail = _last_lines(r.output_tail)
        return f"- {r.id} FAILS{says}: exit code {r.exit_code}." + (f" End of its output:\n{tail}" if tail else "")
    if r.status == "tampered":
        return (f"- {r.id} TAMPERED{says}: {'; '.join(r.reasons)}. Put the frozen file back exactly as it was "
                "(undo your edit, or restore it from git); only a person can change contracts.")
    if r.status == "timeout":
        return f"- {r.id} TIMED OUT{says}: {'; '.join(r.reasons)}."
    if r.status in ("unfinished", "skipped"):
        return f"- {r.id} NOT CHECKED{says}: the gate's time budget ran out. Run necro_check, which has more time."
    tail = _last_lines(r.output_tail, 4)
    return f"- {r.id} COULD NOT RUN{says}." + (f"\n{tail}" if tail else "")


def continue_message(v: Verdict) -> str:
    """The note Hermes hands back to the agent instead of letting it finish."""
    lines = []
    if v.results:
        n, total = len(v.failing()), len(v.results)
        verb = "fails" if n == 1 else "fail"
        if n:
            lines.append(f"[{LABEL}] You cannot finish yet: {n} of {total} frozen acceptance contracts {verb} in {v.root}.")
        else:
            lines.append(f"[{LABEL}] You cannot finish yet in {v.root}.")
    else:
        lines.append(f"[{LABEL}] You cannot finish yet: the frozen acceptance contracts in {v.root} could not be checked.")
    for p in v.problems:
        lines.append(f"- {p}.")
    for r in v.failing():
        lines.append(_describe(r))
    lines.append(
        "A person froze these contracts and they are locked by hash, so fix the code under test, not the contracts: "
        "edits to frozen files are blocked, and any change to them counts as a failure. Run necro_check to re-run "
        "them. When every contract passes, finish again."
    )
    return "\n".join(lines)


def stamp(v: Verdict, text: str) -> str:
    """The final reply as the person sees it: failures lead, a receipt follows a pass."""
    text = text or ""
    if v.ok:
        rel = os.path.relpath(v.receipt_path, v.root) if v.receipt_path else ".necro/receipts"
        return (f"{text.rstrip()}\n\n{LABEL}: all {len(v.results)} frozen acceptance contracts pass. "
                f"Receipt {v.receipt_hash[:12]}; anyone can re-check it with `hermes necro verify {rel}`.")
    fails = v.failing()
    if fails:
        parts = "; ".join(f"{r.id} {WORD.get(r.status, r.status)}" + (f" ({r.says})" if r.says else "") for r in fails)
        n, total = len(fails), len(v.results)
        head = f"Not done. {LABEL}: {n} of {total} frozen acceptance contracts did not pass: {parts}."
        if v.problems:
            head += " Also: " + "; ".join(v.problems) + "."
    else:
        head = f"Not done. {LABEL} could not confirm the frozen contracts: " + "; ".join(v.problems) + "."
    return f"{head} No receipt was written.\n\n{text}"


_DONE = re.compile(
    r"\b(?:done|fixed|resolved|implemented|completed?|finished|"
    r"all (?:the )?tests (?:now )?pass(?:es|ing)?|"
    r"everything (?:is )?(?:now )?(?:works|working|passes|passing)|"
    r"ready for review|works now|should (?:now )?work)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"(?:\bnot(?: yet)?|n't|\bnever|\bno longer)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


def looks_like_done(text: str) -> bool:
    """True when a reply claims the work is finished (and does not deny it)."""
    for m in _DONE.finditer(text or ""):
        if not _NEGATED.search(text[max(0, m.start() - 40): m.start()]):
            return True
    return False
