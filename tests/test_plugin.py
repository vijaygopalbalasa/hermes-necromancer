"""The Hermes glue: register(ctx), the hooks, the tools and the /necro command, against a recording context."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import BUGGY, FIXED, HALF_FIX, REPO, Project

from necro import lock, receipts


def load_plugin():
    name = "necro_plugin_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "__init__.py", submodule_search_locations=[str(REPO)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeState:
    """Like Hermes's PluginState: get/set JSON values, with a path inside the plugin's data folder."""

    def __init__(self):
        self.data = {}
        self.path = Path(tempfile.mkdtemp(prefix="necro-state-")) / "state.json"

    def get(self, key, default=None):
        return json.loads(json.dumps(self.data.get(key, default)))

    def set(self, key, value):
        self.data[key] = json.loads(json.dumps(value))


class FakeCtx:
    def __init__(self, cwd, config=None):
        self.cwd = str(cwd)
        self.config = config or {}
        self.state = FakeState()
        self.tools, self.hooks, self.commands, self.cli = {}, {}, {}, {}
        self.hold = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_hook(self, name, callback, *, hold_stream=False):
        self.hooks[name] = callback
        self.hold[name] = hold_stream

    def register_command(self, name, handler, description="", args_hint="", **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def session_cwd(self):
        return self.cwd


def plugin_for(project, **config):
    ctx = FakeCtx(project.dir, config)
    load_plugin().register(ctx)
    return ctx


class Registration(unittest.TestCase):
    def test_registers_exactly_what_plugin_yaml_declares(self):
        with Project() as p:
            ctx = plugin_for(p)
            manifest = (REPO / "plugin.yaml").read_text()
            self.assertEqual(set(ctx.tools), {"necro_status", "necro_check"})
            self.assertEqual(set(ctx.hooks), {"pre_verify", "transform_llm_output", "pre_tool_call", "pre_llm_call"})
            for name in list(ctx.tools) + list(ctx.hooks):
                self.assertIn(f"- {name}", manifest)
            self.assertEqual(set(ctx.commands), {"necro"})
            self.assertEqual(set(ctx.cli), {"necro"})

    def test_the_reply_streams_unless_hold_stream_is_set(self):
        with Project() as p:
            self.assertFalse(plugin_for(p).hold["transform_llm_output"])
            self.assertTrue(plugin_for(p, hold_stream=True).hold["transform_llm_output"])

    def test_tool_schemas_are_well_formed(self):
        with Project() as p:
            for name, t in plugin_for(p).tools.items():
                self.assertEqual(t["schema"]["name"], name)
                self.assertEqual(t["schema"]["parameters"]["type"], "object")
                self.assertGreater(len(t["schema"]["description"]), 40)


class SlashCommand(unittest.TestCase):
    def test_freeze_writes_the_lock_and_approves_it(self):
        with Project() as p:
            ctx = plugin_for(p)
            out = ctx.commands["necro"]("freeze")
            self.assertIn("Froze 3 contracts", out)
            lk = lock.read_lock(p.dir)
            self.assertEqual(ctx.state.get("approved_roots")[str(p.dir)]["lock"], lock.lock_hash(lk))

    def test_status_before_and_after_freeze(self):
        with Project() as p:
            ctx = plugin_for(p)
            self.assertIn("not frozen", ctx.commands["necro"]("status"))
            ctx.commands["necro"]("freeze")
            out = ctx.commands["necro"]("")
            self.assertIn("C1", out)
            self.assertIn("Shares add up to the total", out)

    def test_check_reports_failures_and_verify_confirms_a_receipt(self):
        with Project(code=FIXED) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = ctx.commands["necro"]("check")
            self.assertIn("3 of 3", out)
            self.assertIn("Receipt", out)
            self.assertIn("CONFIRMED", ctx.commands["necro"]("verify"))

    def test_freeze_reports_a_bad_spec_plainly(self):
        with Project(spec='[[contract]]\nid = "C1"\n') as p:
            out = plugin_for(p).commands["necro"]("freeze")
            self.assertIn("Could not freeze", out)
            self.assertIsNone(lock.read_lock(p.dir))

    def test_approve_accepts_a_lock_frozen_elsewhere(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)  # e.g. committed by a teammate
            ctx = plugin_for(p)
            self.assertIn("not approved", ctx.commands["necro"]("check"))
            self.assertIn("Approved", ctx.commands["necro"]("approve"))
            self.assertIn("3 of 3", ctx.commands["necro"]("check"))


class PreVerify(unittest.TestCase):
    def fire(self, ctx, p, attempt=0, changed=("tipsplit.py",), session="s1", response="Done!"):
        return ctx.hooks["pre_verify"](
            session_id=session, platform="cli", model="m", coding=True, attempt=attempt,
            final_response=response, changed_paths=[str(p.path(c)) for c in changed],
        )

    def test_keeps_the_agent_going_while_a_contract_fails(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = self.fire(ctx, p)
            self.assertEqual(out["action"], "continue")
            self.assertIn("C2", out["message"])

    def test_keeps_gating_on_later_attempts(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            self.assertEqual(self.fire(ctx, p, attempt=2)["action"], "continue")

    def test_lets_the_turn_finish_when_everything_passes_and_writes_a_receipt(self):
        with Project(code=FIXED) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            self.assertIsNone(self.fire(ctx, p))
            self.assertEqual(len(receipts.list_receipts(p.dir)), 1)

    def test_ignores_edits_outside_a_frozen_project(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            self.assertIsNone(self.fire(ctx, p))

    def test_refuses_an_unapproved_lock(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            ctx = plugin_for(p)
            out = self.fire(ctx, p)
            self.assertEqual(out["action"], "continue")
            self.assertIn("not approved", out["message"])

    def test_finds_the_project_from_the_session_cwd_when_paths_are_relative(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = ctx.hooks["pre_verify"](
                session_id="s", platform="cli", model="m", coding=True, attempt=0,
                final_response="Done", changed_paths=["tipsplit.py"],
            )
            self.assertEqual(out["action"], "continue")

    def test_never_raises(self):
        with Project(code=FIXED) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            (p.dir / ".necro" / "lock.json").write_text("{not json")
            out = self.fire(ctx, p)
            self.assertEqual(out["action"], "continue")
            self.assertIn("lock", out["message"])


class Stamp(unittest.TestCase):
    def transform(self, ctx, text, session="s1"):
        return ctx.hooks["transform_llm_output"](response_text=text, session_id=session, model="m", platform="cli")

    def test_a_gated_failing_turn_is_stamped_not_done(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            PreVerify.fire(None, ctx, p, session="s1")
            out = self.transform(ctx, "Done, the shares add up.")
            self.assertTrue(out.startswith("Not done."), out)

    def test_a_gated_passing_turn_gets_the_receipt(self):
        with Project(code=FIXED) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            PreVerify.fire(None, ctx, p, session="s1")
            out = self.transform(ctx, "Fixed.")
            self.assertTrue(out.startswith("Fixed."))
            self.assertIn("Receipt", out)

    def test_a_done_claim_without_the_gate_is_still_checked(self):
        with Project(code=BUGGY) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = self.transform(ctx, "I've fixed the rounding bug, all done.", session="other")
            self.assertTrue(out.startswith("Not done."), out)

    def test_an_ordinary_answer_is_left_alone(self):
        with Project(code=BUGGY) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            self.assertIsNone(self.transform(ctx, "split_bill divides the total by the number of people.", session="other"))

    def test_no_lock_no_stamp(self):
        with Project(code=BUGGY) as p:
            self.assertIsNone(self.transform(plugin_for(p), "Done!"))

    def test_the_stamp_is_used_once_per_turn(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            PreVerify.fire(None, ctx, p, session="s1")
            self.transform(ctx, "Done.")
            self.assertIsNone(self.transform(ctx, "Here is an explanation of the code."))


class Guard(unittest.TestCase):
    def test_blocks_a_write_to_a_frozen_contract(self):
        with Project() as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "contracts/test_fair.py", "content": ""}, task_id="t", session_id="s")
            self.assertEqual(out["action"], "block")
            self.assertIn("frozen", out["message"])

    def test_allows_ordinary_edits(self):
        with Project() as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            self.assertIsNone(ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "tipsplit.py", "content": FIXED}, task_id="t"))


class Tools(unittest.TestCase):
    def test_necro_check_lists_results(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = json.loads(ctx.tools["necro_check"]["handler"]({}))
            self.assertFalse(out["all_pass"])
            self.assertEqual([c["id"] for c in out["contracts"] if c["status"] != "pass"], ["C2"])

    def test_necro_status_without_a_lock_says_so(self):
        with Project() as p:
            out = json.loads(plugin_for(p).tools["necro_status"]["handler"]({}))
            self.assertFalse(out["frozen"])

    def test_necro_status_lists_contracts(self):
        with Project() as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            out = json.loads(ctx.tools["necro_status"]["handler"]({}))
            self.assertTrue(out["frozen"])
            self.assertEqual([c["id"] for c in out["contracts"]], ["C1", "C2", "C3"])


if __name__ == "__main__":
    unittest.main()
