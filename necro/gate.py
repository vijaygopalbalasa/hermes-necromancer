"""The gate: decide whether a "done" may stand, and say why in words the agent and the person can act on."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .canon import BudgetExceeded, canonical_json, check_deadline, file_pin, sha256_text
from .lock import LOCK_REL, LockError, build_lock, drift, frozen_paths, lock_hash, read_lock
from .pins import PinError, observe, pinned_file
from .receipts import SourcesError, build_receipt, escaping_links, git_ignored, pin_entry, source_files, write_receipt
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
    """Pins of every file a verdict depends on: the frozen files (and any file now under a frozen
    pattern) and every other walked file, ignored by git or not, except declared outputs. Returns
    (path -> sha256, or {"size", "mtimeNs"} for a credential file, which is never opened; source
    paths). A contract run must leave every one of them alone."""
    frozen = set(frozen_paths(lk))
    for c in lk.get("contracts", []):
        frozen.update(expand(root, c.get("patterns") or [], strict=False, files=files))
    sources = source_files(root, lk, files=files)
    snap = {}
    for rel in sorted(frozen | set(sources)):
        snap[rel] = file_pin(root / rel, deadline)
        check_deadline(deadline)
    return snap, sources


def pin_snapshot(root: Path, lk: dict, files: list) -> dict:
    """What each pin sees now, kept apart from file paths (a file may be named like anything): so a changed
    setting in a file outside sources = [...] still changes the digest, and pins never reach a receipt."""
    out = {}
    for c in lk.get("contracts", []):
        for entry in c.get("pins") or []:
            text = entry.get("pin", "")
            try:
                out[text] = sha256_text(canonical_json(observe(root, text, files)))
            except PinError as exc:
                out[text] = f"unreadable: {exc}"
    return out


def _digest(lh: str, snap: dict, pins=None) -> str:
    return sha256_text(canonical_json({"lock": lh, "files": snap, "pins": pins or {}}))


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
    return _digest(lock_hash(lk), snap, pin_snapshot(root, lk, files))


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
        pins_before = pin_snapshot(root, lk, files)
        escapes = escaping_links(root, before, before)
        if escapes:
            shown = ", ".join(escapes[:10]) + (" and more" if len(escapes) > 10 else "")
            problems.append(f"a link points outside what is frozen: {shown}. A link is pinned by its text only, so "
                            "its target must be a file in the project; a person removes the link or points it at one")
        key = str(root)
        hit = cache.get(key) if cache is not None else None
        if hit and hit["lock"] == lh and not d.contracts and _digest(lh, before, pins_before) == hit["digest"]:
            results, after, pins_after, cached = hit["results"], before, pins_before, True
            sources = source_files(root, lk, files=files)
        else:
            cached = False
            results = run_contracts(root, lk, deadline=deadline, drift_result=d)
            grace = None if deadline is None else max(deadline, time.monotonic()) + POST_RUN_GRACE_S
            files_after = walk_files(root, grace)
            after, sources = snapshot(root, lk, files_after, grace)
            pins_after = pin_snapshot(root, lk, files_after)
            changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
            changed += sorted({pinned_file(t) for t in set(pins_before) | set(pins_after)
                               if pins_before.get(t) != pins_after.get(t)} - set(changed))
            if changed:
                shown = ", ".join(changed[:10]) + (" and more" if len(changed) > 10 else "")
                problems.append(f"the contract run changed {shown}. Code under test must not change project files; "
                                "a person can declare files the contracts write under outputs = [...] in necro.toml")
            elif cache is not None and not any(r.status in ("skipped", "unfinished") for r in results):
                cache[key] = {"lock": lh, "digest": _digest(lh, after, pins_after), "results": results}
    except BudgetExceeded:
        return Verdict(False, root, lh, problems=[BUDGET_PROBLEM])
    except (SourcesError, SpecError) as exc:
        return Verdict(False, root, lh, problems=[str(exc)])
    ok = not problems and all_pass(results)
    v = Verdict(ok, root, lh, results, problems, cached=cached, digest=_digest(lh, after, pins_after))
    if ok:
        ignored = git_ignored(root, sources)
        pins = [pin_entry(p, after.get(p), p in ignored) for p in sources]
        receipt = build_receipt(root, lk, results, trigger=trigger, pins=pins)
        try:
            v.receipt_path = write_receipt(root, receipt)
            v.receipt_hash = receipt["receiptHash"]
        except OSError as exc:  # a link where the receipts go, say: no receipt, so not done
            v.ok = False
            v.problems.append(f"the receipt could not be written ({exc})")
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


DEFAULT_SAYS = "Your tests pass"  # what /necro freeze writes; it names no requirement of its own
NAME_MAX = 48
_ERROR_LINE = re.compile(r"^[A-Za-z_][\w.]{0,80}(?:Error|Exception|Failure)\b")


def _cut(name: str, limit: int = NAME_MAX) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "\u2026"


def failing_tests(output: str) -> list:
    """[(test name, error line or None)] read from the end of a run's output: unittest, pytest, jest, vitest,
    node --test, go test and cargo test. Line by line, no backtracking; best effort."""
    out, seen = [], set()
    lines = (output or "").splitlines()

    def add(name, error=None):
        name = (name or "").strip()
        if name and name not in seen and len(out) < 20:
            seen.add(name)
            out.append((name, error))

    for i, line in enumerate(lines):
        if line.startswith(("FAIL: ", "ERROR: ")) and " (" in line:  # unittest
            error = None
            for later in lines[i + 1:]:
                if later.startswith(("=====", "-----")) and error:
                    break
                if later.startswith("====="):
                    break
                if _ERROR_LINE.match(later):
                    error = later.strip()
            add(line.split(": ", 1)[1].split(" (", 1)[0], error)
        elif line.startswith(("FAILED ", "ERROR ")) and "::" in line:  # pytest
            nodeid, _, error = line.split(" ", 1)[1].partition(" - ")
            add(nodeid.split("::")[-1].split("[", 1)[0], error.strip() or None)
        elif line.lstrip().startswith("\u25cf ") and "Test suite failed to run" not in line:  # jest
            add(line.lstrip()[2:].split(" \u203a ")[-1])
        elif line.lstrip().startswith("FAIL") and " > " in line:  # vitest
            add(line.rsplit(" > ", 1)[1])
        elif line.lstrip().startswith("\u2716 ") and " (" in line:  # node --test
            add(line.lstrip()[2:].rsplit(" (", 1)[0])
        elif line.lstrip().startswith("--- FAIL: "):  # go test
            add(line.lstrip()[10:].split(" ", 1)[0])
        elif line.startswith("test ") and line.endswith(" ... FAILED"):  # cargo test
            add(line[5:-11].split("::")[-1])
    return out


def humanize(name: str) -> str:
    """test_a_negative_total_is_refused -> 'a negative total is refused'; TestNegativeTotal -> 'negative total'."""
    words = name.strip()
    for prefix in ("test_", "Test_", "test", "Test"):
        if words.startswith(prefix) and len(words) > len(prefix):
            words = words[len(prefix):]
            break
    if "_" in words:
        words = words.replace("_", " ")
    elif " " not in words:
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", words).lower()
    return words.strip() or name


def _counts(results):
    from .detect import test_counts
    counts = [test_counts(r.output_tail) for r in results]
    if not counts or not all(counts):
        return None
    return sum(c[0] for c in counts), sum(c[1] for c in counts)


def describe_failures(v: Verdict) -> str:
    """What failed, in a few words: the one failing test's name, else how many tests failed, else the contract."""
    fails = v.failing()
    if not fails:
        return "the check"
    if any(r.status == "tampered" for r in fails):
        return "the check (a locked file changed)"
    names = []
    for r in fails:
        names += [n for n, _ in failing_tests(r.output_tail) if n not in names]
    if len(names) == 1:
        return _cut(names[0])
    counts = _counts(v.results)
    if counts and counts[0]:
        return f"{counts[0]} of {counts[1]} tests"
    if len(fails) == 1:
        says = fails[0].says
        return "the tests" if says == DEFAULT_SAYS else (says or fails[0].id)
    return " and ".join(r.id for r in fails)


def _one_contract_result(v: Verdict) -> str:
    """How a one-contract lock (what /necro freeze writes) stands, for the person: '1 of 4 tests fails'."""
    r = v.results[0]
    if r.status == "tampered":
        return "a locked file changed (" + "; ".join(r.reasons) + ")"
    if r.status == "timeout":
        return "the tests timed out"
    if r.status in ("unfinished", "skipped"):
        return "the tests were not checked in time"
    if r.status == "error":
        return "the test command could not run"
    counts = _counts(v.results)
    if counts and counts[0]:
        return f"{counts[0]} of {counts[1]} tests {'fails' if counts[0] == 1 else 'fail'}"
    names = [n for n, _ in failing_tests(r.output_tail)]
    if len(names) == 1:
        return f"{_cut(names[0])} fails"
    return "the tests fail"


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
    needs = []
    for r in v.failing():
        for name, error in failing_tests(r.output_tail):
            needs.append(f"- Failing requirement: {humanize(name)} ({name})" + (f": {error.rstrip('.')}" if error else "") + ".")
    if needs:
        lines += needs[:12]
        lines.append("Each failing test is a requirement the person wrote down and locked. Change the code under test "
                     "until it passes; do not argue that a locked test is wrong.")
    for r in v.failing():
        lines.append(_describe(r))
    lines.append(
        "A person froze these contracts and they are locked by hash, so fix the code under test, not the contracts: "
        "edits to frozen files are blocked, and any change to them counts as a failure. The necro_check tool re-runs "
        "them (find it with tool_search if it is not in your list). When every contract passes, finish again."
    )
    return "\n".join(lines)


def sent_back_line(refusals) -> str:
    """'Sent back once: test_x failed after Hermes said done.', 'Sent back 3 times: test_x still failed.' or
    'Sent back 2 times: 2 of 4 tests failed, then test_x still failed.' Each refusal is describe_failures()'s text."""
    refusals = [r if isinstance(r, str) else ("the check" if not r else " and ".join(r)) for r in refusals or []]
    if not refusals:
        return ""
    n = len(refusals)
    if n == 1:
        return f"Sent back once: {refusals[0]} failed after Hermes said done."
    runs = [d for i, d in enumerate(refusals) if i == 0 or d != refusals[i - 1]]
    if len(runs) == 1:
        return f"Sent back {n} times: {runs[0]} still failed."
    line = f"Sent back {n} times: {runs[0]} failed, " + ", ".join(f"then {d} still failed" for d in runs[1:]) + "."
    if len(line) > 120 and len(runs) > 2:
        line = f"Sent back {n} times: {runs[0]} failed, \u2026, then {runs[-1]} still failed."
    if len(line) > 120:
        first, last = _cut(runs[0], 30), _cut(runs[-1], 30)
        line = f"Sent back {n} times: {first} failed, " + ("\u2026, " if len(runs) > 2 else "") + f"then {last} still failed."
    return line


def stamp(v: Verdict, text: str, sent_back=()) -> str:
    """The final reply as the person sees it: failures lead, a receipt follows a pass. ``sent_back`` lists the
    turn's refusals, which the person would not otherwise see (Hermes keeps the pre_verify note to itself)."""
    text = text or ""
    line = sent_back_line(sent_back)
    lead = f"{line} " if line else ""
    if v.ok:
        rel = os.path.relpath(v.receipt_path, v.root) if v.receipt_path else ".necro/receipts"
        if len(v.results) == 1:
            counts = _counts(v.results)
            passed = ("the locked tests pass" if not counts or not counts[1] else
                      "the test passes" if counts[1] == 1 else f"all {counts[1]} tests pass")
        else:
            passed = f"all {len(v.results)} frozen acceptance contracts pass"
        return (f"{text.rstrip()}\n\n{lead}{LABEL}: {passed}. "
                f"Receipt {v.receipt_hash[:12]}; anyone can re-check it with `hermes necro verify {rel}`.")
    fails = v.failing()
    if fails and len(v.results) == 1:
        head = f"Not done. {lead}{LABEL}: {_one_contract_result(v)}, so no receipt was written."
        if v.problems:
            head += " Also: " + "; ".join(v.problems) + "."
        return f"{head}\n\n{text}"
    if fails:
        parts = "; ".join(f"{r.id} {WORD.get(r.status, r.status)}" + (f" ({r.says})" if r.says else "") for r in fails)
        n, total = len(fails), len(v.results)
        head = f"Not done. {lead}{LABEL}: {n} of {total} frozen acceptance contracts did not pass: {parts}."
        if v.problems:
            head += " Also: " + "; ".join(v.problems) + "."
    else:
        head = f"Not done. {lead}{LABEL} could not confirm the frozen contracts: " + "; ".join(v.problems) + "."
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
