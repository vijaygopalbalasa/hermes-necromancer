"""The gate: decide whether a "done" may stand, and say why in words the agent and the person can act on."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .canon import BudgetExceeded, canonical_json, check_deadline, hash_file, sha256_text
from .lock import LOCK_REL, LockError, build_lock, drift, frozen_paths, lock_hash, read_lock
from .receipts import SourcesError, build_receipt, git_ignored, pin_entry, source_files, write_receipt
from .runner import all_pass, run_contracts
from .spec import SpecError, expand, walk_files

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


APPROVAL_TAMPERED = "!tampered"
TAMPERED_PROBLEM = ("approvals were changed outside necro: the record for this folder failed its check (or the key "
                    "beside it is gone). A person freezes again with /necro freeze")
BUDGET_PROBLEM = ("reading the project's files took longer than the time budget. A person can list the files "
                  "that matter under sources = [...] in necro.toml and freeze again")
POST_RUN_GRACE_S = 3.0


def snapshot(root: Path, lk: dict, files: list, deadline=None) -> tuple:
    """Hashes of every file a verdict depends on: the frozen files (and any file now under a frozen
    pattern) and every other walked file, ignored by git or not, except declared outputs. Returns
    (path -> sha256, source paths). A contract run must leave every one of them alone."""
    frozen = set(frozen_paths(lk))
    for c in lk.get("contracts", []):
        frozen.update(expand(root, c.get("patterns") or [], strict=False, files=files))
    sources = source_files(root, lk, files=files)
    snap = {}
    for rel in sorted(frozen | set(sources)):
        snap[rel] = hash_file(root / rel, deadline)
        check_deadline(deadline)
    return snap, sources


def _digest(lh: str, snap: dict) -> str:
    return sha256_text(canonical_json({"lock": lh, "files": snap}))


def _approval_problem(approved, root: Path, lh: str) -> str | None:
    """None when a person accepted this exact lock for this exact folder. ``approved`` maps a folder
    to its lock hash (APPROVAL_TAMPERED when its record failed the MAC); None skips the check."""
    if approved is None:
        return None
    if not isinstance(approved, dict):
        raise TypeError("approved must map a project folder to the lock hash a person accepted there")
    got = approved.get(str(root))
    if got == APPROVAL_TAMPERED:
        return TAMPERED_PROBLEM
    if got != lh:
        return (f"the lock {LOCK_REL} ({lh[:12]}) is not approved for this folder. A person freezes contracts with "
                "/necro freeze, or accepts a committed lock after reading it with /necro approve")
    return None


def fingerprint(root, deadline=None) -> str | None:
    """The same digest a verdict records, without running anything: None when there is no usable
    lock. Raises BudgetExceeded when reading the files outruns the deadline."""
    root = Path(root).resolve()
    try:
        lk = read_lock(root)
    except LockError:
        return None
    if lk is None:
        return None
    files = walk_files(root, deadline)
    snap, _ = snapshot(root, lk, files, deadline)
    return _digest(lock_hash(lk), snap)


def evaluate(root, *, budget_s: float | None = None, deadline: float | None = None, approved=None,
             cache: dict | None = None, trigger: str | None = None) -> Verdict:
    """Run (or reuse) the frozen contracts of the project at ``root`` and write a receipt if all pass.

    ``deadline`` (a time.monotonic() value) or ``budget_s`` bounds everything: reading files and
    running contracts. ``approved`` maps a project folder to the one lock hash a person accepted
    there (None skips the check). ``cache`` keeps the last complete run per project and reuses it
    only while every file it depends on is byte-for-byte the same."""
    root = Path(root).resolve()
    if deadline is None and budget_s:
        deadline = time.monotonic() + budget_s
    if not (root / LOCK_REL).is_file():
        raise FileNotFoundError(f"no {LOCK_REL} in {root}")
    try:
        lk = read_lock(root)
    except LockError as exc:
        return Verdict(False, root, problems=[f"the lock cannot be used: {exc}"])
    lh = lock_hash(lk)
    refused = _approval_problem(approved, root, lh)
    if refused:
        return Verdict(False, root, lh, problems=[refused])
    try:
        files = walk_files(root, deadline)
        d = drift(root, lk, files=files, deadline=deadline)
        problems = list(d.spec)
        if not d.any() and lock_hash(build_lock(root, files=files)) != lh:
            problems.append(f"{LOCK_REL} does not match necro.toml and the frozen files, so it was not written by "
                            "freezing them. A person checks `git diff .necro/` and freezes again")
        before, _ = snapshot(root, lk, files, deadline)
        key = str(root)
        hit = cache.get(key) if cache is not None else None
        if hit and hit["lock"] == lh and _digest(lh, before) == hit["digest"]:
            results, after, cached = hit["results"], before, True
            sources = source_files(root, lk, files=files)
        else:
            cached = False
            results = run_contracts(root, lk, deadline=deadline, drift_result=d)
            grace = None if deadline is None else max(deadline, time.monotonic()) + POST_RUN_GRACE_S
            after, sources = snapshot(root, lk, walk_files(root, grace), grace)
            changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
            if changed:
                shown = ", ".join(changed[:10]) + (" and more" if len(changed) > 10 else "")
                problems.append(f"the contract run changed {shown}. Code under test must not change project files; "
                                "a person can declare files the contracts write under outputs = [...] in necro.toml")
            elif cache is not None and not any(r.status in ("skipped", "unfinished") for r in results):
                cache[key] = {"lock": lh, "digest": _digest(lh, after), "results": results}
    except BudgetExceeded:
        return Verdict(False, root, lh, problems=[BUDGET_PROBLEM])
    except (SourcesError, SpecError) as exc:
        return Verdict(False, root, lh, problems=[str(exc)])
    ok = not problems and all_pass(results)
    v = Verdict(ok, root, lh, results, problems, cached=cached, digest=_digest(lh, after))
    if ok:
        ignored = git_ignored(root, sources)
        pins = [pin_entry(p, after.get(p), p in ignored) for p in sources]
        receipt = build_receipt(root, lk, results, trigger=trigger, pins=pins)
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
        return f"- {r.id} NOT CHECKED{says}: the gate's time budget ran out. Run the necro_check tool, which has more time."
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
        "edits to frozen files are blocked, and any change to them counts as a failure. The necro_check tool re-runs "
        "them (find it with tool_search if it is not in your list). When every contract passes, finish again."
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
    r"(?:all\s+(?:the\s+|\d+\s+)?)?tests?\s+(?:now\s+)?pass(?:es|ed|ing)?|"
    r"tests?\s+(?:are|is)\s+(?:now\s+)?(?:green|passing)|"
    r"everything\s+(?:is\s+)?(?:now\s+)?(?:works|working|passes|passing)|"
    r"ready\s+(?:for\s+review|to\s+(?:merge|ship))|lgtm|good\s+to\s+go|ship\s+it|works\s+now|all\s+set|"
    r"shipped|should\s+(?:now\s+)?(?:work|fix\s+it)|this\s+fixes)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"(?:\bnot(?: yet)?|n't|\bnever|\bno longer|\bnothing|\bcan't say(?: it's| it is)?)\s+(?:\w+\s+){0,2}$",
                      re.IGNORECASE)
_ADDRESSED = re.compile(r"\b(?:when|once|if|until|unless|whether)\b[^.!?;:]{0,40}$", re.IGNORECASE)


# "finished reading", "done looking", "completed the review": the agent finished looking, not the work.
_JUST_LOOKED = re.compile(
    r"^\s*(?:(?:reading|looking|reviewing|going\s+through|checking|scanning|skimming|analy[sz]ing|investigating|"
    r"exploring|inspecting|studying|examining)\b|(?:the|my|a|your)\s+(?:review|reading|look|investigation|"
    r"analysis|audit|read-through)\b)",
    re.IGNORECASE,
)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
# A sentence that is itself a yes/no question ("Is it done?", "Should I mark it done?") claims nothing.
_YES_NO_QUESTION = re.compile(
    r"^\s*(?:is|are|was|were|am|should|shall|can|could|do|does|did|would|will|may|might|have|has|had)\b[^?]*\?\s*$",
    re.IGNORECASE)


def _claims_done(sentence: str) -> bool:
    if _YES_NO_QUESTION.match(sentence):
        return False
    for m in _DONE.finditer(sentence):
        before = sentence[max(0, m.start() - 40): m.start()]
        after = sentence[m.end(): m.end() + 40]
        if _NEGATED.search(before) or _ADDRESSED.search(before) or _JUST_LOOKED.search(after):
            continue
        return True
    return False


def looks_like_done(text: str) -> bool:
    """True when any sentence of a reply claims the work is finished, whatever the reply ends with.
    A sentence does not count when it denies it, talks about the person being done, says it finished
    reading or reviewing, or is itself a yes/no question."""
    return any(_claims_done(sentence) for sentence in _SENTENCE_END.split(text or "") if sentence.strip())
