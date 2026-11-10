"""Findings from three automated security review rounds (Nov 10), each reproduced before its fix."""

import argparse
import contextlib
import io
import shlex
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import FIXED, HALF_FIX, REPO, Project, spec_text

from necro import commands, gate, guard, lock, verify
from test_plugin import FakeCtx, OldHermesCtx, load_plugin


def ctx_for(p):
    ctx = FakeCtx(p.dir)
    plugin = load_plugin().register(ctx)
    return ctx, plugin


def fire_pre_verify(ctx, paths, session="s1"):
    return ctx.hooks["pre_verify"](session_id=session, platform="cli", model="m", coding=True, attempt=0,
                                   final_response="Done!", changed_paths=[str(x) for x in paths])


def transform(ctx, text, session="s1"):
    return ctx.hooks["transform_llm_output"](response_text=text, session_id=session, model="m", platform="cli")


class R1LockRemovalFailsClosed(unittest.TestCase):
    def test_a_renamed_lock_still_counts_as_frozen(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            os.rename(p.dir / ".necro" / "lock.json", p.dir / ".necro" / "lock.bak")
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")
            self.assertTrue(transform(ctx, "Done.", session="r").startswith("Not done."))

    def test_an_unreadable_lock_fails_closed(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            p.write(".necro/lock.json", "not json")
            self.assertEqual(fire_pre_verify(ctx, [p.dir / "tipsplit.py"])["action"], "continue")

    def test_losing_both_the_lock_and_the_approval_record_still_fails_closed_in_this_process(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            ctx.state.data.clear()
            (p.dir / ".necro" / "lock.json").unlink()
            self.assertEqual(fire_pre_verify(ctx, [p.dir / "tipsplit.py"])["action"], "continue")

    def test_ordinary_deletes_and_renames_are_blocked(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            for cmd in ("mv .necro/lock.json .necro/lock.bak", "rm .necro/lock.json",
                        "find .necro -name lock.json -delete", "find contracts -name test_fair.py -exec rm {} +",
                        "git mv contracts/test_fair.py contracts/old.py", "trash contracts/test_fair.py"):
                self.assertIsNotNone(guard.check_tool_call("terminal", {"command": cmd}, str(p.dir)), cmd)
            move = "*** Begin Patch\n*** Move File: contracts/test_fair.py -> contracts/x.py\n*** End Patch\n"
            self.assertIsNotNone(guard.check_tool_call("patch", {"mode": "patch", "patch": move}, str(p.dir)))


class R1VerifyDoesNotRunUnapprovedCommands(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        self.p.write("contracts/test_refuses_zero.py",
                     "import unittest\nfrom tipsplit import split_bill\n"
                     "class T(unittest.TestCase):\n    def test_zero(self):\n"
                     "        open('ran.txt', 'a').write('ran\\n')\n"
                     "        self.assertRaises(ValueError, split_bill, 1, 0)\n")
        self.p.write("necro.toml", 'outputs = ["ran.txt"]\n' + self.p.read("necro.toml"))
        self.lk = lock.freeze(self.p.dir)
        v = gate.evaluate(self.p.dir, budget_s=60)
        assert v.ok, v.problems
        self.receipt = v.receipt_path
        self.p.path("ran.txt").unlink()

    def tearDown(self):
        self.p.cleanup()

    def test_an_unapproved_lock_is_checked_by_hash_only(self):
        v = verify.verify_receipt(self.receipt, approved={})
        self.assertFalse(self.p.path("ran.txt").exists(), "verify ran the repo's commands")
        self.assertEqual(v.verdict, "HASHES MATCH")
        self.assertIn("not re-run", v.message)
        self.assertFalse(v.ok)

    def test_no_approvals_at_all_means_hash_only(self):
        verify.verify_receipt(self.receipt, approved=None)
        self.assertFalse(self.p.path("ran.txt").exists())

    def test_an_approved_lock_is_re_run(self):
        v = verify.verify_receipt(self.receipt, approved={str(self.p.dir): lock.lock_hash(self.lk)})
        self.assertTrue(self.p.path("ran.txt").exists())
        self.assertEqual(v.verdict, "CONFIRMED", v.message)

    def test_an_approval_for_another_folder_does_not_count(self):
        verify.verify_receipt(self.receipt, approved={"/somewhere/else": lock.lock_hash(self.lk)})
        self.assertFalse(self.p.path("ran.txt").exists())

    def test_an_explicit_run_re_runs(self):
        v = verify.verify_receipt(self.receipt, approved=None, run=True)
        self.assertTrue(self.p.path("ran.txt").exists())
        self.assertEqual(v.verdict, "CONFIRMED", v.message)

    def test_the_standalone_command_needs_run(self):
        out = subprocess.run([sys.executable, str(REPO / "necro"), "verify", str(self.receipt)],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not re-run", out.stdout)
        self.assertFalse(self.p.path("ran.txt").exists())
        out = subprocess.run([sys.executable, str(REPO / "necro"), "verify", str(self.receipt), "--run"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertIn("CONFIRMED", out.stdout)

    def test_gate_evaluate_binds_approval_to_the_folder(self):
        with self.assertRaises(TypeError):
            gate.evaluate(self.p.dir, budget_s=60, approved={lock.lock_hash(self.lk)})


class R2PersonOnlyTextCheck(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def blocked(self, cmd):
        return guard.check_tool_call("terminal", {"command": cmd}, str(self.p.dir)) is not None

    def test_a_python_import_of_the_freeze_code_is_blocked(self):
        self.assertTrue(self.blocked('python3 -c "from necro.commands import do_freeze; do_freeze(\'.\', None)"'))

    def test_a_long_command_that_mentions_necro_is_refused_not_truncated(self):
        self.assertTrue(self.blocked("echo " + "a" * 600_000 + " && hermes necro approve"))

    def test_a_long_command_outside_a_frozen_project_is_not_refused_for_length(self):
        # Round 5: inside a frozen project every over-long command is refused; elsewhere length alone is fine.
        with Project() as elsewhere:
            self.assertIsNone(guard.check_tool_call("terminal", {"command": "echo " + "a" * 600_000}, str(elsewhere.dir)))

    def test_evasion_next_to_necro_is_blocked(self):
        for cmd in ("env -i hermes necro check", "env -u NECRO_AGENT_SESSION hermes necro check",
                    "setsid hermes necro check", "nohup python3 necro check &", "(hermes necro check &)"):
            self.assertTrue(self.blocked(cmd), cmd)


class R2ApprovalStore(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="necro-home-")).resolve()
        self.store = self.home / ".hermes" / "plugin-data" / "contract-necromancer"
        self.store.mkdir(parents=True)
        (self.store / "state.json").write_text("{}")
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        os.environ["HOME"] = self.old_home
        self.p.cleanup()

    def check(self, tool, args, cwd=None):
        return guard.check_tool_call(tool, args, str(cwd or self.p.dir), str(self.store))

    def test_home_relative_spellings_are_resolved(self):
        rel = ".hermes/plugin-data/contract-necromancer/state.json"
        for cmd in (f"echo '{{}}' > ~/{rel}", f"cat $HOME/{rel}", f"cp x ${{HOME}}/{rel}"):
            self.assertIsNotNone(self.check("terminal", {"command": cmd}), cmd)
        self.assertIsNotNone(self.check("terminal", {"command": f"rm {rel}"}, cwd=self.home))
        self.assertIsNotNone(self.check("write_file", {"path": f"~/{rel}", "content": "{}"}))
        self.assertIsNotNone(self.check("write_file", {"path": f"$HOME/{rel}", "content": "{}"}))
        self.assertIsNotNone(self.check("execute_code", {"code": f"open('{self.store}/state.json','w')"}))

    def test_approvals_changed_outside_necro_are_refused(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            data = ctx.state.data["approved_roots"]
            data[str(p.dir)]["lock"] = "0" * 64  # forged without the key
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")
            self.assertIn("approvals were changed outside necro", out["message"])

    def test_an_approval_added_without_the_key_is_refused(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            lk = lock.freeze(p.dir)
            ctx.state.data["approved_roots"] = {str(p.dir): {"lock": lock.lock_hash(lk), "at": "2026-11-10"}}
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")
            self.assertIn("approvals were changed outside necro", out["message"])

    def test_status_says_when_the_approval_record_was_changed(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            ctx.state.data["approved_roots"][str(p.dir)]["lock"] = "0" * 64
            self.assertIn("changed outside necro", ctx.commands["necro"]("status"))

    def test_a_deleted_key_makes_every_approval_untrusted(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            (Path(ctx.state.path).parent / "approvals.hmac").unlink()
            self.assertEqual(fire_pre_verify(ctx, [p.dir / "tipsplit.py"])["action"], "continue")


class R2RunsMustNotChangeSources(unittest.TestCase):
    def test_code_that_rewrites_a_source_during_the_run_is_refused(self):
        with Project(code=FIXED + "\nopen('audit.log', 'a').write('touched\\n')\n") as p:
            p.write("audit.log", "start\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertFalse(v.ok)
            self.assertTrue(any("audit.log" in x for x in v.problems), v.problems)

    def test_an_undeclared_file_written_by_a_contract_is_refused(self):
        with Project(code=FIXED) as p:
            p.write("contracts/test_refuses_zero.py",
                    "import unittest\nclass T(unittest.TestCase):\n    def test_w(self):\n"
                    "        open('junit-report.xml', 'w').write('x')\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertFalse(v.ok)
            self.assertTrue(any("junit-report.xml" in x for x in v.problems), v.problems)

    def test_declared_outputs_are_allowed_and_not_pinned(self):
        with Project(code=FIXED) as p:
            p.write("contracts/test_refuses_zero.py",
                    "import time, unittest\nclass T(unittest.TestCase):\n    def test_w(self):\n"
                    "        open('reports/junit.xml', 'w').write(str(time.time()))\n")
            p.write("reports/.keep", "")
            p.write("necro.toml", 'outputs = ["reports/*.xml"]\n' + p.read("necro.toml"))
            lk = lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            rc = json.loads(v.receipt_path.read_text())
            self.assertNotIn("reports/junit.xml", {s["path"] for s in rc["sources"]})
            approved = {str(p.dir): lock.lock_hash(lk)}
            self.assertEqual(verify.verify_receipt(v.receipt_path, approved=approved).verdict, "CONFIRMED")
            self.assertEqual(verify.verify_receipt(v.receipt_path, approved=approved).verdict, "CONFIRMED")

    def test_verify_notices_a_run_that_changes_a_pinned_file(self):
        # The code under test writes notes.txt only when an unpinned flag exists (node_modules is never pinned).
        code = FIXED + "\nimport os\nif os.path.exists('node_modules/flag'):\n    open('notes.txt', 'a').write('x')\n"
        with Project(code=code) as p:
            p.write("notes.txt", "")
            lk = lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            p.write("node_modules/flag", "1")
            out = verify.verify_receipt(v.receipt_path, approved={str(p.dir): lock.lock_hash(lk)})
            self.assertEqual(out.verdict, "MISMATCH")
            self.assertIn("notes.txt", out.message)


class R3PersonOnlyCommands(unittest.TestCase):
    CLEAN = {"PATH": os.environ.get("PATH", "")}

    def problem(self, **kw):
        args = {"session_env_ok": True, "environ": dict(self.CLEAN), "stdin_isatty": True, "ancestors": ["-zsh"]}
        args.update(kw)
        return commands.person_problem("freeze", **args)

    def test_a_person_at_their_own_terminal_is_allowed(self):
        self.assertIsNone(self.problem())

    def test_each_condition_refuses(self):
        self.assertIn("terminal", self.problem(stdin_isatty=False))
        self.assertIn("agent", self.problem(environ={**self.CLEAN, "NECRO_AGENT_SESSION": "s1"}))
        self.assertIsNotNone(self.problem(environ={**self.CLEAN, "AI_AGENT": "hermes-agent"}))
        self.assertIsNotNone(self.problem(environ={**self.CLEAN, "HERMES_AGENT": "true"}))
        self.assertIn("Hermes", self.problem(ancestors=["bash", "/x/venv/bin/python3 /x/venv/bin/hermes chat", "-zsh"]))
        self.assertIsNotNone(self.problem(ancestors=None))  # cannot tell who started it
        self.assertIn("register_session_env", self.problem(session_env_ok=False))

    def test_a_cli_without_register_session_env_refuses_and_writes_nothing(self):
        with Project(code=FIXED) as p:
            ctx = OldHermesCtx(p.dir)  # no register_session_env, like Hermes before 0.23.1
            module = load_plugin()
            with self.assertLogs(module.logger.name, level="WARNING"):
                plugin = module.register(ctx)
            self.assertFalse(plugin.session_env_ok)
            with contextlib.redirect_stdout(io.StringIO()):
                code = plugin.cli_run(argparse.Namespace(action="freeze", receipt=None, dir=str(p.dir), run=False))
            self.assertEqual(code, 3)
            self.assertIsNone(lock.read_lock(p.dir))

    def test_the_standalone_freeze_refuses_without_a_terminal(self):
        with Project(code=FIXED) as p:
            out = subprocess.run([sys.executable, str(REPO / "necro"), "freeze", "--dir", str(p.dir)],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(out.returncode, 3, out.stdout)
            self.assertIsNone(lock.read_lock(p.dir))

    def test_the_slash_command_needs_none_of_this(self):
        with Project(code=FIXED) as p:
            ctx = FakeCtx(p.dir)
            load_plugin().register(ctx)
            self.assertIn("Froze 3 contracts", ctx.commands["necro"]("freeze"))


if __name__ == "__main__":
    unittest.main()


class R4AncestorWalkAndFind(unittest.TestCase):
    """Round 4: a ps failure part way up the parent chain must refuse, and a destructive find run from a
    subfolder must not reach the lock."""

    def test_a_ps_failure_part_way_up_refuses(self):
        real_run = subprocess.run
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(a[0], 0, stdout="999999 /bin/zsh -l\n", stderr="")
            return subprocess.CompletedProcess(a[0], 1, stdout="", stderr="no such process")

        commands.subprocess.run = flaky
        try:
            self.assertIsNone(commands.hermes_ancestors())
        finally:
            commands.subprocess.run = real_run

    def test_find_delete_from_a_subfolder_cannot_reach_the_lock(self):
        with Project(code=FIXED) as p:
            ctx, _ = ctx_for(p)
            ctx.commands["necro"]("freeze")
            sub = p.dir / "contracts"
            msg = guard.check_tool_call("terminal", {"command": "find .. -name lock.json -delete",
                                                     "workdir": str(sub)}, str(sub))
            self.assertIsNotNone(msg)
            self.assertTrue((p.dir / ".necro" / "lock.json").is_file())


class R4RefusalsSayWhatToDoInstead(unittest.TestCase):
    def test_the_agent_marker_refusal_names_the_real_plugin_path(self):
        why = commands.person_problem("freeze", environ={"AI_AGENT": "1"}, stdin_isatty=True, ancestors=[])
        self.assertIn("/necro freeze", why)
        self.assertIn("python3 " + shlex.quote(str(REPO / "necro")) + " freeze", why)
        self.assertNotIn("<plugin folder>", why)

    def test_nothing_suggests_a_shell_freeze_that_always_refuses(self):
        with Project(code=FIXED) as p:
            (p.dir / ".necro").mkdir(exist_ok=True)
            out, _ = commands.dispatch("status", p.dir, commands.Approvals(Path(tempfile.mkdtemp())))
            self.assertNotIn("hermes necro freeze", out)
        src = (REPO / "necro" / "commands.py").read_text()
        self.assertNotIn("or `hermes necro freeze`", src)
