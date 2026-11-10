"""Regression tests for the issues an independent adversarial review confirmed (Nov 10)."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest

from helpers import BUGGY, FIXED, HALF_FIX, Project

from necro import canon, gate, guard, lock, receipts, runner, verify
from test_plugin import FakeCtx, load_plugin


def ctx_for(p, **config):
    ctx = FakeCtx(p.dir, config)
    load_plugin().register(ctx)
    return ctx


def fire_pre_verify(ctx, paths, session="s1"):
    return ctx.hooks["pre_verify"](session_id=session, platform="cli", model="m", coding=True, attempt=0,
                                   final_response="Done!", changed_paths=[str(x) for x in paths])


def transform(ctx, text, session="s1"):
    return ctx.hooks["transform_llm_output"](response_text=text, session_id=session, model="m", platform="cli")


class UnpinnedFilesCannotDecide(unittest.TestCase):
    def test_a_shadow_module_hidden_from_git_is_pinned_and_visible(self):
        with Project(code=BUGGY) as p:
            subprocess.run(["git", "init", "-q"], cwd=p.dir, check=True)
            lock.freeze(p.dir)
            p.write("unittest.py", "raise SystemExit(0)\n")
            (p.dir / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (p.dir / ".git" / "info" / "exclude").write_text("unittest.py\n")
            self.assertIn("unittest.py", receipts.source_files(p.dir, lock.read_lock(p.dir)))

    def test_stale_bytecode_in_pycache_is_not_used(self):
        with Project(code=FIXED) as p:
            lk = lock.freeze(p.dir)
            # Compile the fixed module into the project's __pycache__, then plant the bug in the source
            # with the same size and an unchanged mtime so a normal import would trust the stale .pyc.
            size = max(len(BUGGY), len(FIXED))
            p.write("tipsplit.py", FIXED + "#" * (size - len(FIXED)))
            subprocess.run([sys.executable, "-c", "import tipsplit"], cwd=p.dir, check=True)
            st = os.stat(p.path("tipsplit.py"))
            p.write("tipsplit.py", BUGGY + "#" * (size - len(BUGGY)))
            os.utime(p.path("tipsplit.py"), ns=(st.st_atime_ns, st.st_mtime_ns))
            results = {r.id: r.status for r in runner.run_contracts(p.dir, lk)}
            self.assertEqual(results["C1"], "fail", "the contract must run the source on disk, not a cached .pyc")

    def test_contracts_leave_no_bytecode_in_the_project(self):
        with Project(code=FIXED) as p:
            lk = lock.freeze(p.dir)
            runner.run_contracts(p.dir, lk)
            self.assertEqual(list(p.dir.rglob("__pycache__")), [])

    def test_a_change_to_a_gitignored_file_drops_the_cached_pass(self):
        with Project(code=FIXED) as p:
            subprocess.run(["git", "init", "-q"], cwd=p.dir, check=True)
            p.write(".gitignore", "rules.json\n")
            p.write("rules.json", '{"ok": true}')
            p.write("contracts/test_refuses_zero.py",
                    "import json, unittest\nclass T(unittest.TestCase):\n    def test_rules(self):\n"
                    "        self.assertTrue(json.load(open('rules.json'))['ok'])\n")
            lock.freeze(p.dir)
            cache = {}
            self.assertTrue(gate.evaluate(p.dir, budget_s=60, cache=cache).ok)
            p.write("rules.json", '{"ok": false}')
            v = gate.evaluate(p.dir, budget_s=60, cache=cache)
            self.assertFalse(v.cached)
            self.assertFalse(v.ok)


class Approvals(unittest.TestCase):
    def test_a_copied_lock_in_another_folder_is_not_approved(self):
        with Project(code=BUGGY) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            decoy = p.dir / "aaa"
            decoy.mkdir()
            for name in ("necro.toml", "contracts", ".necro"):
                src = p.dir / name
                (shutil.copytree if src.is_dir() else shutil.copy2)(src, decoy / name)
            p.write("aaa/tipsplit.py", FIXED)
            out = fire_pre_verify(ctx, [decoy / "tipsplit.py"])
            self.assertIsNotNone(out)
            self.assertEqual(out["action"], "continue")

    def test_the_real_project_is_checked_even_when_only_a_decoy_was_edited(self):
        with Project(code=BUGGY) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            p.write("scratch/notes.py", "x = 1\n")
            out = fire_pre_verify(ctx, [p.dir / "scratch" / "notes.py"])
            self.assertEqual(out["action"], "continue")
            self.assertIn("C1", out["message"])

    def test_rolling_back_to_an_older_lock_is_refused(self):
        with Project(code=HALF_FIX) as p:
            p.write("necro.toml", p.read("necro.toml").split("[[contract]]\nid = \"C2\"")[0])  # only C1
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            old_spec, old_lock = p.read("necro.toml"), p.read(".necro/lock.json")
            from helpers import spec_text
            p.write("necro.toml", spec_text())
            ctx.commands["necro"]("freeze")
            p.write("necro.toml", old_spec)
            p.write(".necro/lock.json", old_lock)
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")
            self.assertIn("not approved", out["message"])

    def test_deleting_the_lock_does_not_unfreeze_the_project(self):
        with Project(code=BUGGY) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            (p.dir / ".necro" / "lock.json").unlink()
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")
            self.assertIn("deleted", out["message"])
            self.assertTrue(transform(ctx, "Done!", session="t2").startswith("Not done."))

    def test_unfreeze_by_a_person_really_unfreezes(self):
        with Project(code=BUGGY) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            ctx.commands["necro"]("unfreeze")
            self.assertIsNone(fire_pre_verify(ctx, [p.dir / "tipsplit.py"]))


class VerifyTrustsOnlyAConsistentLock(unittest.TestCase):
    def test_a_lock_that_does_not_match_necro_toml_is_a_mismatch(self):
        with Project(code=BUGGY) as p:
            lk = lock.freeze(p.dir)
            for c in lk["contracts"]:
                c["run"] = f"{sys.executable} -c pass"
            canon.write_json_atomic(p.dir / ".necro" / "lock.json", lk)
            results = runner.run_contracts(p.dir, lk)
            rc = receipts.build_receipt(p.dir, lk, results)
            path = receipts.write_receipt(p.dir, rc)
            v = verify.verify_receipt(path)
            self.assertEqual(v.verdict, "MISMATCH")
            self.assertIn("necro.toml", v.message)

    def test_a_malformed_but_self_consistent_receipt_is_a_mismatch_not_a_crash(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            rc = {"kind": receipts.RECEIPT_KIND, "version": 1, "lock": None, "contracts": [{"id": "C1"}], "sources": []}
            rc["receiptHash"] = receipts.receipt_hash(rc)
            path = p.write(".necro/receipts/x.json", json.dumps(rc))
            self.assertEqual(verify.verify_receipt(path).verdict, "MISMATCH")

    def test_verify_is_stable_when_a_contract_writes_a_declared_report(self):
        with Project(code=FIXED) as p:
            p.write("contracts/test_refuses_zero.py",
                    "import time, unittest\nfrom tipsplit import split_bill\n"
                    "class T(unittest.TestCase):\n    def test_zero(self):\n"
                    "        open('junit-report.xml', 'w').write(str(time.time()))\n"
                    "        self.assertRaises(ValueError, split_bill, 1, 0)\n")
            p.write("necro.toml", 'outputs = ["junit-report.xml"]\n' + p.read("necro.toml"))
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            self.assertEqual(verify.verify_receipt(v.receipt_path, run=True).verdict, "CONFIRMED")
            self.assertEqual(verify.verify_receipt(v.receipt_path, run=True).verdict, "CONFIRMED")


class NeverHangNeverCrash(unittest.TestCase):
    def test_a_fifo_or_a_symlink_loop_in_the_project_does_not_hang_or_crash(self):
        with Project(code=FIXED) as p:
            os.mkfifo(p.dir / "pipe")
            os.symlink("loop", p.dir / "loop")
            lock.freeze(p.dir)
            t0 = time.monotonic()
            v = gate.evaluate(p.dir, budget_s=20)
            self.assertLess(time.monotonic() - t0, 15)
            self.assertTrue(v.ok, v.problems)

    def test_a_symlink_out_of_the_project_under_sources_does_not_crash(self):
        with Project(code=FIXED) as p, Project() as other:
            p.write("necro.toml", 'sources = ["*.py"]\n' + p.read("necro.toml"))
            lock.freeze(p.dir)
            os.symlink(other.path("tipsplit.py"), p.dir / "notes.py")
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertIsInstance(v.ok, bool)

    def test_an_absurd_number_in_the_lock_is_a_lock_error(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            raw = p.read(".necro/lock.json").replace('"version": 1', '"version": ' + "9" * 5000)
            p.write(".necro/lock.json", raw)
            with self.assertRaises(lock.LockError):
                lock.read_lock(p.dir)
            ctx = ctx_for(p)
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")

    def test_hashing_that_outruns_the_budget_refuses_in_time(self):
        with Project(code=FIXED) as p:
            with open(p.dir / "data.bin", "wb") as fh:
                fh.truncate(3 * 1024 ** 3)  # a sparse 3 GB file
            lock.freeze(p.dir)
            t0 = time.monotonic()
            v = gate.evaluate(p.dir, budget_s=0.5)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertFalse(v.ok)
            self.assertTrue(any("sources" in x for x in v.problems), v.problems)

    def test_a_recursive_glob_does_not_freeze_bytecode(self):
        with Project(code=FIXED) as p:
            p.write("necro.toml", p.read("necro.toml").replace('files = ["contracts/test_fair.py"]', 'files = ["contracts/**"]'))
            lock.freeze(p.dir)
            subprocess.run([sys.executable, "-c", "import contracts.test_fair"], cwd=p.dir, check=True)
            self.assertTrue(gate.evaluate(p.dir, budget_s=60).ok)


class GuardPrecision(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        self.p.write("necro.toml", self.p.read("necro.toml").replace('files = ["contracts/test_fair.py"]', 'files = ["contracts/test_f*.py"]'))
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def check(self, tool, args):
        return guard.check_tool_call(tool, args, str(self.p.dir))

    def test_case_tricks_do_not_get_past_the_guard(self):
        self.assertIsNotNone(self.check("write_file", {"path": ".NECRO/lock.json", "content": "{}"}))
        self.assertIsNotNone(self.check("write_file", {"path": "Contracts/Test_Adds_Up.py", "content": ""}))

    def test_quoting_tricks_do_not_hide_a_person_only_command(self):
        self.assertIsNotNone(self.check("terminal", {"command": "hermes nec''ro approve"}))
        self.assertIsNotNone(self.check("terminal", {"command": 'python3 ~/.hermes/plugins/contract-necromancer/necro "freeze"'}))

    def test_harmless_commands_are_not_blocked(self):
        # Trade-off chosen in security round 2: any command where "necro" is followed by freeze, unfreeze or
        # approve is blocked, even a harmless grep, so spliced person-only commands cannot slip through.
        self.assertIsNotNone(self.check("terminal", {"command": "cat necro.toml && grep -rn 'def approve' ."}))
        self.assertIsNone(self.check("terminal", {"command": "cat necro.toml && grep -rn 'def split_bill' ."}))
        self.assertIsNone(self.check("execute_code", {"code": "src = open('contracts/test_fair.py').read()\nopen('tipsplit.py', 'w').write('x')"}))
        self.assertIsNone(self.check("execute_code", {"code": "print(open('contracts/test_fair.py').read().replace('a', 'b'))"}))

    def test_a_glob_matches_from_the_project_root_only(self):
        self.assertIsNone(self.check("write_file", {"path": "vendor/lib/contracts/test_fun.py", "content": "x"}))
        self.assertIsNotNone(self.check("write_file", {"path": "contracts/test_fun.py", "content": "x"}))

    def test_obvious_code_writes_to_a_frozen_file_are_still_blocked(self):
        self.assertIsNotNone(self.check("execute_code", {"code": "open('contracts/test_fair.py', 'w').write('')"}))
        self.assertIsNotNone(self.check("execute_code", {"code": "import pathlib\npathlib.Path('contracts/test_fair.py').write_text('')"}))
        self.assertIsNotNone(self.check("terminal", {"command": "python3 -c \"import os; os.remove('contracts/test_fair.py')\""}))

    def test_a_huge_command_is_checked_quickly(self):
        cmd = "echo " + " ".join(["necro"] * 30000) + " approve-ish"
        t0 = time.monotonic()
        self.check("terminal", {"command": cmd})
        self.assertLess(time.monotonic() - t0, 1.0)


class StampWhenItMatters(unittest.TestCase):
    def test_a_shell_only_edit_is_still_checked_at_the_end(self):
        with Project(code=FIXED) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            transform(ctx, "Here is the plan.", session="a")  # the first reply in a process records a baseline
            p.write("tipsplit.py", BUGGY)  # e.g. sed -i from the terminal: pre_verify never fires
            out = transform(ctx, "The shares are handled now.", session="a")
            self.assertTrue(out and out.startswith("Not done."), out)

    def test_more_done_claims_are_recognised(self):
        for text in ["The tests pass.", "Tests are green.", "LGTM, ready to merge.", "All 3 tests pass now."]:
            self.assertTrue(gate.looks_like_done(text), text)

    def test_talking_about_the_person_being_done_is_not_a_claim(self):
        for text in ["Tell me when you're done reviewing.", "Once you are done, run the tests."]:
            self.assertFalse(gate.looks_like_done(text), text)

    def test_a_turn_that_ended_without_a_stamp_does_not_leak_into_the_next(self):
        with Project(code=HALF_FIX) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            fire_pre_verify(ctx, [p.dir / "tipsplit.py"], session="s9")
            ctx.hooks["pre_llm_call"](session_id="s9", user_message="What is the capital of France?",
                                      conversation_history=[], is_first_turn=False, model="m", platform="cli")
            self.assertIsNone(transform(ctx, "Paris is the capital of France.", session="s9"))

    def test_a_crash_inside_the_check_is_stamped_not_done(self):
        with Project(code=FIXED) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            fire_pre_verify(ctx, [p.dir / "tipsplit.py"], session="c")
            p.write(".necro/lock.json", "{broken")
            out = transform(ctx, "Done.", session="c")
            self.assertTrue(out and out.startswith("Not done."), out)


class SecondRound(unittest.TestCase):
    """Gaps left after the first fixes, found by re-running the reviewer's own scripts."""

    def test_person_only_commands_refuse_to_run_in_an_agent_shell(self):
        from necro import commands
        with Project(code=FIXED) as p:
            os.environ["NECRO_AGENT_SESSION"] = "s1"
            try:
                text, code = commands.dispatch("freeze", p.dir, commands.Approvals(None), from_shell=True)
            finally:
                del os.environ["NECRO_AGENT_SESSION"]
            self.assertEqual(code, 3)
            self.assertIn("person", text)
            self.assertIsNone(lock.read_lock(p.dir))

    def test_hermes_own_markers_refuse_the_shell_command(self):
        # Security round 3: AI_AGENT or HERMES_AGENT refuses the shell command. Hermes sets both in every hermes
        # process, so `hermes necro freeze` always refuses and /necro freeze inside Hermes is the way to freeze.
        from necro import commands
        with Project(code=FIXED) as p:
            os.environ["AI_AGENT"], os.environ["HERMES_AGENT"] = "hermes-agent", "true"
            try:
                text, code = commands.dispatch("freeze", p.dir, commands.Approvals(None), from_shell=True)
            finally:
                del os.environ["AI_AGENT"], os.environ["HERMES_AGENT"]
            self.assertEqual(code, 3, text)
            self.assertIn("/necro freeze", text)

    def test_the_marker_is_registered_for_the_agent_shell(self):
        with Project() as p:
            ctx = FakeCtx(p.dir)
            ctx.session_env = {}
            ctx.register_session_env = lambda names, resolver: ctx.session_env.update({n: resolver for n in names})
            load_plugin().register(ctx)
            self.assertEqual(ctx.session_env["NECRO_AGENT_SESSION"]("abc"), {"NECRO_AGENT_SESSION": "abc"})

    def test_the_agent_cannot_unset_the_marker_through_the_guard(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            self.assertIsNotNone(guard.check_tool_call("terminal", {"command": "unset NECRO_AGENT_SESSION; true"}, str(p.dir)))

    def test_a_forged_lock_that_a_person_did_not_freeze_is_refused_even_if_approved(self):
        with Project(code=BUGGY) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            lk = lock.read_lock(p.dir)
            for c in lk["contracts"]:
                c["run"] = f"{sys.executable} -c pass"
            canon.write_json_atomic(p.dir / ".necro" / "lock.json", lk)
            from necro import commands
            commands.Approvals(ctx.state).approve(p.dir, lock.lock_hash(lk))  # as if the agent got past the guard
            out = fire_pre_verify(ctx, [p.dir / "tipsplit.py"])
            self.assertEqual(out["action"], "continue")
            self.assertIn("does not match necro.toml", out["message"])

    def test_quotes_do_not_hide_the_necro_folder_from_the_guard(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            self.assertIsNotNone(guard.check_tool_call("terminal", {"command": "rm -r .ne''cro"}, str(p.dir)))


class FirstTurnBaseline(unittest.TestCase):
    def test_a_shell_only_edit_in_the_first_turn_is_checked(self):
        with Project(code=FIXED) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            ctx.hooks["pre_llm_call"](session_id="f1", user_message="tidy up tipsplit.py", conversation_history=[],
                                      is_first_turn=True, model="m", platform="cli")
            p.write("tipsplit.py", BUGGY)  # a sed -i from the terminal during the first turn
            out = transform(ctx, "Tidied it up.", session="f1")
            self.assertTrue(out and out.startswith("Not done."), out)

    def test_an_untouched_project_stays_unstamped_in_the_first_turn(self):
        with Project(code=FIXED) as p:
            ctx = ctx_for(p)
            ctx.commands["necro"]("freeze")
            ctx.hooks["pre_llm_call"](session_id="f2", user_message="what does split_bill do?", conversation_history=[],
                                      is_first_turn=True, model="m", platform="cli")
            self.assertIsNone(transform(ctx, "It splits a bill into shares.", session="f2"))


if __name__ == "__main__":
    unittest.main()
