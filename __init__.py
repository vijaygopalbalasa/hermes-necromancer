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
import threading
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
        self.turns: dict = {}
        self._mutex = threading.Lock()
        self._run_locks: dict = {}
        state = getattr(ctx, "state", None)
        self.approvals = commands.Approvals(state)

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

    def evaluate(self, root: Path, budget_s: float, trigger: str) -> gate.Verdict:
        run_lock = self._root_lock(root)
        if not run_lock.acquire(timeout=budget_s):
            return gate.Verdict(False, root, problems=["another contract run is still going in this project"])
        try:
            return gate.evaluate(root, budget_s=budget_s, approved=self.approvals.hashes(), cache=self.cache,
                                 trigger=trigger)
        finally:
            run_lock.release()

    def _root_from_paths(self, paths) -> Path | None:
        base = self.cwd()
        for raw in paths or []:
            p = Path(str(raw)).expanduser()
            if not p.is_absolute():
                p = Path(base) / p
            root = lock.find_root(p)
            if root is not None:
                return root
        return None

    # ---- hooks ---------------------------------------------------------------------------
    def on_pre_verify(self, session_id="", changed_paths=None, attempt=0, **kwargs):
        """Hermes is about to accept a final answer after code edits. Keep it going while any
        frozen contract fails (Hermes bounds this with agent.max_verify_nudges)."""
        try:
            root = self._root_from_paths(changed_paths)
            if root is None:
                return None
            v = self.evaluate(root, self.setting("gate_budget_seconds", GATE_BUDGET_S), "pre_verify")
            self._remember(session_id, root)
            if v.ok:
                logger.info("contract-necromancer: %s passes, receipt %s", root, v.receipt_hash)
                return None
            logger.info("contract-necromancer: refused done in %s (attempt %s): %s", root, attempt,
                        v.failing_ids() or v.problems)
            return {"action": "continue", "message": gate.continue_message(v)}
        except Exception as exc:  # a broken gate must not let a frozen project slip through silently
            logger.exception("contract-necromancer: pre_verify failed")
            return {"action": "continue", "message": f"[Contract Necromancer] The frozen contracts could not be "
                    f"checked ({type(exc).__name__}: {exc}). Run necro_check, fix what it reports, then finish."}

    def on_transform(self, response_text="", session_id="", **kwargs):
        """Stamp the final reply: 'Not done.' first when a contract fails, the receipt after a pass."""
        try:
            entry = self.turns.pop(session_id, None) if session_id else None
            if entry is not None:
                root = Path(entry)
            else:
                if not self.setting("stamp_done_claims", True) or not gate.looks_like_done(response_text):
                    return None
                root = lock.find_root(self.cwd())
                if root is None:
                    return None
            if not (root / lock.LOCK_REL).is_file():
                return None
            v = self.evaluate(root, self.setting("gate_budget_seconds", GATE_BUDGET_S), "final_reply")
            return gate.stamp(v, response_text)
        except Exception:
            logger.exception("contract-necromancer: transform_llm_output failed")
            return None

    def on_pre_tool_call(self, tool_name="", args=None, **kwargs):
        """Block the agent's ordinary routes to rewriting a frozen contract or the lock."""
        try:
            msg = guard.check_tool_call(tool_name, args if isinstance(args, dict) else {}, self.cwd())
        except Exception:
            logger.exception("contract-necromancer: pre_tool_call failed")
            return None
        return {"action": "block", "message": msg} if msg else None

    def _remember(self, session_id, root: Path) -> None:
        if not session_id:
            return
        self.turns[session_id] = str(root)
        while len(self.turns) > MAX_TURNS_TRACKED:
            self.turns.pop(next(iter(self.turns)))

    # ---- tools ---------------------------------------------------------------------------
    def tool_status(self, args=None, **kwargs) -> str:
        root = lock.find_root(self.cwd())
        if root is None:
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
        v = self.evaluate(root, self.setting("check_budget_seconds", CHECK_BUDGET_S), "necro_check")
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

    def cli_run(self, args) -> int:
        cwd = args.dir or os.getcwd()
        raw = args.action + (f" {args.receipt}" if getattr(args, "receipt", None) else "")
        text, code = commands.dispatch(raw, cwd, self.approvals, self.cache)
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
    ctx.register_command("necro", plugin.slash, description="Frozen acceptance contracts: status, freeze, check, verify",
                         args_hint="[status|freeze|approve|unfreeze|check|verify|receipts|demo]")
    ctx.register_cli_command(name="necro", help="Frozen acceptance contracts and receipts (Contract Necromancer)",
                             setup_fn=plugin.cli_setup, handler_fn=plugin.cli_run,
                             description=commands.USAGE)
    return plugin
