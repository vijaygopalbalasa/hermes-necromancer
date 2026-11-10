"""Contract Necromancer for Hermes.

Hermes cannot call coding work done while one of the project's frozen acceptance contracts fails.
A person freezes the contracts by hash (/necro freeze). At Hermes's verify gate (pre_verify) the
plugin runs them itself and keeps the agent working, naming what fails. Edits to frozen files are
blocked (pre_tool_call). The final reply is stamped "Not done." or carries a receipt that anyone
can re-check later (transform_llm_output, hermes necro verify).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import threading
import time
from pathlib import Path

from .necro import commands, gate, guard, lock

logger = logging.getLogger(__name__)

GATE_BUDGET_S = 25.0   # under Hermes's default 30 s hook timeout (plugins.hook_callback_timeout)
CHECK_BUDGET_S = 600.0
MAX_TURNS_TRACKED = 1000

STATUS_SCHEMA = {
    "name": "necro_status",
    "description": (
        "Show this project's frozen acceptance contracts (Contract Necromancer): what each one checks, the "
        "command that checks it, which files it freezes, and the latest receipt. Read-only. Use it to learn "
        "what 'done' means here before you start."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

CHECK_SCHEMA = {
    "name": "necro_check",
    "description": (
        "Run this project's frozen acceptance contracts now and report which pass and which fail, with the end "
        "of each failing contract's output. Writes a receipt when all pass. Run it before you say the work is "
        "done: Hermes will not let you finish while one fails. You cannot change the contracts; fix the code "
        "under test."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


class NecroPlugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cache: dict = {}
        self.turns: dict = {}   # session id -> project folders the gate checked this turn
        self.seen: dict = {}    # project folder -> digest at its last check
        self.session_env_ok = False  # set by register() when the agent-shell marker could be registered
        self._mutex = threading.Lock()
        self._run_locks: dict = {}
        try:
            state = ctx.state
        except Exception:  # an older Hermes without plugin state: no approval records, no approval check
            state = None
        self.approvals = commands.Approvals(state)
        try:
            self.store_dir = str(Path(state.path).parent) if state is not None else None
        except Exception:
            self.store_dir = None

    # ---- settings and context -------------------------------------------------------------
    def setting(self, key, default):
        try:
            value = self.ctx.get_config(key, default)
        except Exception:
            return default
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        if isinstance(default, float):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default
        return value

    def cwd(self) -> str:
        try:
            value = self.ctx.session_cwd()
        except Exception:
            value = ""
        return value or os.getcwd()

    def _root_lock(self, root: Path) -> threading.Lock:
        with self._mutex:
            return self._run_locks.setdefault(str(root), threading.Lock())

    def evaluate(self, root: Path, deadline: float, trigger: str) -> gate.Verdict:
        run_lock = self._root_lock(root)
        if not run_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return gate.Verdict(False, root, problems=["another contract run is still going in this project"])
        try:
            v = gate.evaluate(root, deadline=deadline, approved=self.approvals.mapping(), cache=self.cache,
                              trigger=trigger)
        finally:
            run_lock.release()
        if v.digest:
            self.seen[str(root)] = v.digest
        return v

    def _deadline(self, key: str, default: float) -> float:
        return time.monotonic() + self.setting(key, default)

    def _absolute(self, raw) -> Path:
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else Path(self.cwd()) / p

    def _roots(self, paths=(), include_cwd: bool = True) -> list:
        """Frozen project folders that hold any of these paths, plus the session folder's."""
        roots = []
        for raw in list(paths or []) + ([self.cwd()] if include_cwd else []):
            root = lock.find_root(self._absolute(raw))
            if root is not None and root not in roots:
                roots.append(root)
        return roots

    def _orphans(self, paths=()) -> list:
        """Folders a person froze (and never unfroze) whose lock is gone, around these paths. Uses the
        approval store and, in case that was emptied too, the folders seen approved in this process."""
        approved = set(self.approvals.mapping() or {}) | set(self.approvals.known)
        out = []
        for raw in list(paths or []) + [self.cwd()]:
            try:
                p = str(self._absolute(raw).resolve())
            except OSError:
                continue
            for root in approved:
                if (p == root or p.startswith(root.rstrip("/") + "/")) and not (Path(root) / lock.LOCK_REL).is_file():
                    if root not in out:
                        out.append(root)
        return out

    @staticmethod
    def _orphan_problem(roots) -> str:
        return (f"the lock of the frozen project in {', '.join(roots)} was deleted. Only a person unfreezes a project, "
                "with /necro unfreeze; put .necro/lock.json back (for example from git)")

    # ---- hooks ---------------------------------------------------------------------------
    def on_pre_llm_call(self, session_id="", **kwargs):
        """A new turn starts: forget which projects the previous turn's gate checked, and record a
        baseline for the session's frozen project the first time it is seen, so an edit made only
        through the shell, even in this first turn, is noticed when the reply is stamped."""
        if session_id:
            self.turns.pop(session_id, None)
        try:
            root = lock.find_root(self.cwd())
            if root is not None and str(root) not in self.seen:
                digest = gate.fingerprint(root, self._deadline("gate_budget_seconds", GATE_BUDGET_S))
                if digest is not None:
                    self.seen[str(root)] = digest
        except Exception:
            logger.debug("contract-necromancer: no baseline this turn", exc_info=True)
        return None

    def on_pre_verify(self, session_id="", changed_paths=None, attempt=0, **kwargs):
        """Hermes is about to accept a final answer after code edits. Keep it going while any
        frozen contract fails (Hermes bounds this with agent.max_verify_nudges)."""
        deadline = self._deadline("gate_budget_seconds", GATE_BUDGET_S)
        try:
            orphans = self._orphans(changed_paths)
            if orphans:
                return {"action": "continue", "message": f"[Contract Necromancer] You cannot finish yet: "
                        f"{self._orphan_problem(orphans)}."}
            roots = self._roots(changed_paths)
            if not roots:
                return None
            self._remember(session_id, roots)
            for root in roots:
                v = self.evaluate(root, deadline, "pre_verify")
                if not v.ok:
                    logger.info("contract-necromancer: refused done in %s (attempt %s): %s", root, attempt,
                                v.failing_ids() or v.problems)
                    return {"action": "continue", "message": gate.continue_message(v)}
                logger.info("contract-necromancer: %s passes, receipt %s", root, v.receipt_hash)
            return None
        except Exception as exc:  # a broken gate must not let a frozen project slip through silently
            logger.exception("contract-necromancer: pre_verify failed")
            return {"action": "continue", "message": f"[Contract Necromancer] The frozen contracts could not be "
                    f"checked ({type(exc).__name__}: {exc}). Run necro_check, fix what it reports, then finish."}

    def on_transform(self, response_text="", session_id="", **kwargs):
        """Stamp the final reply: 'Not done.' first when a contract fails, the receipt after a pass.

        A frozen project is checked when the gate looked at it this turn, when any of its files
        changed since the last check (edits made through the shell never reach pre_verify), or when
        the reply claims the work is done."""
        deadline = self._deadline("gate_budget_seconds", GATE_BUDGET_S)
        checked = self.turns.pop(session_id, []) if session_id else []
        roots = []
        try:
            orphans = self._orphans()
            if orphans:
                return (f"Not done. Contract Necromancer: {self._orphan_problem(orphans)}. "
                        f"No receipt was written.\n\n{response_text}")
            roots = [Path(r) for r in checked if (Path(r) / lock.LOCK_REL).is_file()]
            for root in self._roots(include_cwd=True):
                if root not in roots:
                    roots.append(root)
            claim = self.setting("stamp_done_claims", True) and gate.looks_like_done(response_text)
            verdicts = []
            for root in roots:
                if str(root) not in checked and not claim and not self._changed(root, deadline):
                    continue
                verdicts.append(self.evaluate(root, deadline, "final_reply"))
            if not verdicts:
                return None
            failing = [v for v in verdicts if not v.ok]
            return gate.stamp(failing[0] if failing else verdicts[0], response_text)
        except Exception as exc:
            logger.exception("contract-necromancer: transform_llm_output failed")
            if roots or checked:
                return (f"Not done. Contract Necromancer could not check the frozen contracts "
                        f"({type(exc).__name__}). No receipt was written.\n\n{response_text}")
            return None

    def _changed(self, root: Path, deadline: float) -> bool:
        """True when the project's files differ from its last check. The first look only records a
        baseline, so a reply in a project nobody touched stays unstamped."""
        last = self.seen.get(str(root))
        try:
            now = gate.fingerprint(root, deadline)
        except Exception:
            return True
        if last is None:
            if now is not None:
                self.seen[str(root)] = now
            return False
        return now != last

    def on_pre_tool_call(self, tool_name="", args=None, **kwargs):
        """Block the agent's ordinary routes to rewriting a frozen contract, the lock or the approvals."""
        try:
            frozen = set(self.approvals.mapping() or {}) | set(self.approvals.known)
            msg = guard.check_tool_call(tool_name, args if isinstance(args, dict) else {}, self.cwd(), self.store_dir,
                                        frozen_roots=frozen)
        except Exception:
            logger.exception("contract-necromancer: pre_tool_call failed")
            return None
        return {"action": "block", "message": msg} if msg else None

    def _remember(self, session_id, roots) -> None:
        if not session_id:
            return
        self.turns[session_id] = [str(r) for r in roots]
        while len(self.turns) > MAX_TURNS_TRACKED:
            self.turns.pop(next(iter(self.turns)))

    # ---- tools ---------------------------------------------------------------------------
    def tool_status(self, args=None, **kwargs) -> str:
        root = lock.find_root(self.cwd())
        if root is None:
            orphans = self._orphans()
            if orphans:
                return json.dumps({"frozen": True, "error": self._orphan_problem(orphans)})
            return json.dumps({"frozen": False, "message": "No frozen acceptance contracts in this project "
                               "(no .necro/lock.json). Nothing gates done here."})
        try:
            lk = lock.read_lock(root)
        except lock.LockError as exc:
            return json.dumps({"frozen": True, "project": str(root), "error": str(exc)})
        d = lock.drift(root, lk)
        return json.dumps({
            "frozen": True,
            "project": str(root),
            "lock": lock.lock_hash(lk),
            "contracts": [
                {"id": c["id"], "says": c.get("says", ""), "run": c["run"],
                 "files": [f["path"] for f in c["files"]], "drift": d.contracts.get(c["id"], [])}
                for c in lk["contracts"]
            ],
            "spec_drift": d.spec,
            "rule": "You cannot finish coding work here while any contract fails. Fix the code, not the contracts.",
        })

    def tool_check(self, args=None, **kwargs) -> str:
        root = lock.find_root(self.cwd())
        if root is None:
            return json.dumps({"frozen": False, "message": "No frozen acceptance contracts in this project."})
        v = self.evaluate(root, self._deadline("check_budget_seconds", CHECK_BUDGET_S), "necro_check")
        return json.dumps(v.to_dict())

    # ---- person-facing commands -------------------------------------------------------------
    def slash(self, raw_args="", **kwargs) -> str:
        text, _code = commands.dispatch(raw_args or "", self.cwd(), self.approvals, self.cache)
        return text

    def cli_setup(self, parser) -> None:
        parser.add_argument("action", nargs="?", default="status",
                            choices=["status", "freeze", "approve", "unfreeze", "check", "verify", "receipts", "demo", "help"])
        parser.add_argument("receipt", nargs="?", help="receipt file for verify (default: the newest)")
        parser.add_argument("--dir", default=None, help="project folder (default: the current folder)")
        parser.add_argument("--run", action="store_true",
                            help="verify: re-run the contracts even though this lock is not approved here")

    def cli_run(self, args) -> int:
        cwd = args.dir or os.getcwd()
        words = [args.action] + ([args.receipt] if getattr(args, "receipt", None) else [])
        if getattr(args, "run", False):
            words.append("--run")
        text, code = commands.dispatch(shlex.join(words), cwd, self.approvals, self.cache, from_shell=True,
                                       session_env_ok=self.session_env_ok)
        print(text)
        return code


def register(ctx):
    plugin = NecroPlugin(ctx)
    ctx.register_tool(name="necro_status", toolset="necromancer", schema=STATUS_SCHEMA, handler=plugin.tool_status,
                      description="Show the frozen acceptance contracts")
    ctx.register_tool(name="necro_check", toolset="necromancer", schema=CHECK_SCHEMA, handler=plugin.tool_check,
                      description="Run the frozen acceptance contracts")
    ctx.register_hook("pre_verify", plugin.on_pre_verify)
    ctx.register_hook("transform_llm_output", plugin.on_transform, hold_stream=plugin.setting("hold_stream", False))
    ctx.register_hook("pre_tool_call", plugin.on_pre_tool_call)
    ctx.register_hook("pre_llm_call", plugin.on_pre_llm_call)
    try:  # mark the agent's own commands, so person-only `necro freeze/approve/unfreeze` refuse to run there
        ctx.register_session_env([commands.AGENT_MARKER], lambda session_id: {commands.AGENT_MARKER: session_id or "1"})
        plugin.session_env_ok = True
    except Exception:
        # Not just a log line: without the marker the shell commands freeze, approve and unfreeze refuse
        # to run at all (see commands.person_problem); /necro inside Hermes still works.
        plugin.session_env_ok = False
        logger.warning("contract-necromancer: register_session_env is unavailable (Hermes before 0.23.1); "
                       "`hermes necro freeze/approve/unfreeze` will refuse, use /necro instead")
    ctx.register_command("necro", plugin.slash, description="Frozen acceptance contracts: status, freeze, check, verify",
                         args_hint="[status|freeze|approve|unfreeze|check|verify|receipts|demo]")
    ctx.register_cli_command(name="necro", help="Frozen acceptance contracts and receipts (Contract Necromancer)",
                             setup_fn=plugin.cli_setup, handler_fn=plugin.cli_run,
                             description=commands.USAGE)
    return plugin
