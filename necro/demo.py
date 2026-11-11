"""`hermes necro demo`: the whole story on a throwaway copy of demo/tip_split.

A scripted agent drives the plugin's real hook functions, the same ones Hermes calls: a half fix
claimed as done, a blocked edit to a frozen contract, a sneaky shell edit caught by hash, the real
fix, a receipt, and a later re-check that catches a change."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = PLUGIN_DIR / "demo" / "tip_split"

HALF_FIX = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        share = total_cents // people
        shares = [share] * people
        shares[0] += total_cents - share * people  # give the leftover cents to the first person
        return shares
    '''
).lstrip()

FIXED = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        if total_cents < 0:
            raise ValueError("the total cannot be negative")
        share, extra = divmod(total_cents, people)
        return [share + 1 if i < extra else share for i in range(people)]
    '''
).lstrip()

# The path is assembled at run time, so the guard's text match cannot see it. The hash check can.
SNEAKY = ("import pathlib; p = pathlib.Path('contracts') / ('test_' + 'fair.py'); "
          "p.write_text(p.read_text().replace('min(shares), 1)', 'min(shares), 99)'))")


def _plugin_module():
    try:
        from .. import NecroPlugin  # loaded by Hermes as a package
        return sys.modules[NecroPlugin.__module__]
    except (ImportError, ValueError):
        name = "contract_necromancer_demo_plugin"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


class _State:
    def __init__(self):
        self.data = {}
        self.path = Path(tempfile.mkdtemp(prefix="necro-demo-state-")) / "state.json"

    def get(self, key, default=None):
        return json.loads(json.dumps(self.data.get(key, default)))

    def set(self, key, value):
        self.data[key] = json.loads(json.dumps(value))


class _DemoCtx:
    """Just enough of Hermes's PluginContext for the story (no model, no network)."""

    def __init__(self, cwd: Path):
        self.cwd = str(cwd)
        self.state = _State()
        self.hooks, self.tools, self.commands = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, callback, *, hold_stream=False):
        self.hooks[name] = callback

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, *a, **kw):
        pass

    def register_session_env(self, names, resolver):
        self.session_env = {name: resolver for name in names}

    def get_config(self, key, default=None):
        return default

    def session_cwd(self):
        return self.cwd


def _indent(text: str, pad: str = "     ") -> str:
    return "\n".join(pad + line for line in text.strip().splitlines())


def run_demo(keep: bool = True) -> str:
    project = Path(tempfile.mkdtemp(prefix="necro-demo-")).resolve()
    shutil.copytree(TEMPLATE, project, dirs_exist_ok=True)
    ctx = _DemoCtx(project)
    _plugin_module().register(ctx)
    out = []
    say = out.append

    def gate(reply: str, session: str) -> None:
        verdict = ctx.hooks["pre_verify"](session_id=session, platform="cli", model="scripted", coding=True,
                                          attempt=0, final_response=reply, changed_paths=[str(project / "tipsplit.py")])
        if verdict:
            say("   Hermes verify gate: REFUSED. The agent is told:")
            say(_indent(verdict["message"]))
        else:
            say("   Hermes verify gate: passed.")
        stamped = ctx.hooks["transform_llm_output"](response_text=reply, session_id=session, model="scripted",
                                                    platform="cli")
        say("   The person sees:")
        say(_indent(stamped or reply))

    say("Contract Necromancer demo: a scripted agent, the plugin's real hooks, a throwaway project.")
    say(f"Project: {project} (a tip splitter whose split_bill loses the leftover cents)\n")

    from .runner import contract_env
    from .spec import load_spec
    say(f"1. A person reads necro.toml and freezes its {len(load_spec(project).contracts)} contracts.")
    say("   > /necro freeze")
    say(_indent(ctx.commands["necro"]("freeze")))

    say("\n2. The agent makes a half fix and claims it is done.")
    (project / "tipsplit.py").write_text(HALF_FIX)
    gate("Done! The shares add up to the total now.", "demo-1")

    say("\n3. The agent tries to loosen the failing contract with write_file.")
    blocked = ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "contracts/test_fair.py", "content": "# relaxed"},
                                         task_id="demo", session_id="demo-2")
    say("   " + ("BLOCKED. " + blocked["message"] if blocked else "allowed (unexpected)"))

    say("\n4. It edits the contract from the shell instead, hiding the path from the guard.")
    shell = {"command": f"python3 -c \"{SNEAKY}\""}
    decision = ctx.hooks["pre_tool_call"](tool_name="terminal", args=shell, task_id="demo", session_id="demo-2")
    say("   The guard " + ("blocked it." if decision else "did not catch it, so the edit lands."))
    if not decision:
        subprocess.run([sys.executable, "-c", SNEAKY], cwd=project, check=True, env=contract_env())
    gate("Done. The fairness check passes now.", "demo-2")
    shutil.copy2(TEMPLATE / "contracts" / "test_fair.py", project / "contracts" / "test_fair.py")
    say("   (the person restores the contract file)")

    say("\n5. The agent writes the real fix.")
    (project / "tipsplit.py").write_text(FIXED)
    gate("Fixed: leftover cents go one each to the first people, so shares add up and differ by at most a cent, "
         "and a negative total is refused.", "demo-3")

    from .commands import Approvals, do_verify
    approvals = Approvals(ctx.state)
    say("\n6. Anyone re-checks the receipt later.")
    say("   > /necro verify")
    say(_indent(do_verify(project, approvals=approvals)[0]))

    say("\n7. Someone changes tipsplit.py after the receipt was written.")
    (project / "tipsplit.py").write_text(FIXED + "\n# a later edit\n")
    say("   > /necro verify")
    say(_indent(do_verify(project, approvals=approvals)[0]))

    if not keep:
        shutil.rmtree(project, ignore_errors=True)
    else:
        say(f"\nThe project, its lock and its receipt are in {project}")
    return "\n".join(out)


if __name__ == "__main__":
    print(run_demo())
