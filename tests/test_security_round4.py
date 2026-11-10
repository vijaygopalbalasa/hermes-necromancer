"""The items still open after review rounds 1 to 4 (Nov 10), each reproduced before its fix."""

import logging
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import FIXED, REPO, Project

from necro import commands, gate, guard, lock, spec
from test_plugin import FakeCtx, load_plugin


class Outputs(unittest.TestCase):
    def test_declared_outputs_are_part_of_the_lock_hash(self):
        with Project(code=FIXED) as p:
            base = p.read("necro.toml")
            p.write("necro.toml", 'outputs = ["a.xml"]\n' + base)
            a = lock.lock_hash(lock.freeze(p.dir))
            p.write("necro.toml", 'outputs = ["b.xml"]\n' + base)
            b = lock.lock_hash(lock.freeze(p.dir))
            self.assertNotEqual(a, b)

    def test_tool_caches_a_run_creates_do_not_fail_the_pass(self):
        with Project(code=FIXED) as p:
            p.write("contracts/test_refuses_zero.py",
                    "import os, unittest\nclass T(unittest.TestCase):\n    def test_caches(self):\n"
                    "        for d in ('.pytest_cache/v', '__pycache__', 'node_modules/.cache'):\n"
                    "            os.makedirs(d, exist_ok=True)\n"
                    "            open(os.path.join(d, 'x'), 'w').write('generated')\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)

    def test_the_default_ignore_list_is_documented(self):
        readme = (REPO / "README.md").read_text()
        for name in sorted(spec.SKIP_DIRS):
            self.assertIn(f"`{name}`", readme, name)


class OverLongInput(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def test_code_over_the_limit_that_touches_a_frozen_file_is_blocked(self):
        code = "x = 1\n" * (guard.MAX_TEXT // 6 + 10) + "open('contracts/test_fair.py', 'w').write('')\n"
        self.assertIsNotNone(guard.check_tool_call("execute_code", {"code": code}, str(self.p.dir)))

    def test_a_command_over_the_limit_that_names_a_frozen_file_is_blocked(self):
        cmd = "echo " + "a" * (guard.MAX_TEXT + 10) + " > contracts/test_fair.py"
        self.assertIsNotNone(guard.check_tool_call("terminal", {"command": cmd}, str(self.p.dir)))


class MarkerWarning(unittest.TestCase):
    def test_a_missing_register_session_env_is_a_warning_not_a_debug_line(self):
        with Project() as p:
            ctx = FakeCtx(p.dir)
            module = load_plugin()
            with self.assertLogs(module.logger.name, level="WARNING") as logs:
                module.register(ctx)
            self.assertTrue(any("register_session_env" in line for line in logs.output))


class RefusalSaysWhatToDo(unittest.TestCase):
    def refusal(self, verb, cwd):
        os.environ["HERMES_AGENT"] = "true"
        try:
            return commands.dispatch(verb, cwd, commands.Approvals(None), from_shell=True)
        finally:
            del os.environ["HERMES_AGENT"]

    def test_a_refused_freeze_gives_two_copy_paste_ways_forward(self):
        with Project(code=FIXED) as p:
            text, code = self.refusal("freeze", p.dir)
            self.assertEqual(code, 3)
            self.assertIn("type /necro freeze in the Hermes chat", text)
            self.assertIn(f"python3 {REPO / 'necro'} freeze", text.replace("'", ""))

    def test_a_refused_approve_or_unfreeze_points_to_the_slash_command(self):
        with Project(code=FIXED) as p:
            for verb in ("approve", "unfreeze"):
                text, code = self.refusal(verb, p.dir)
                self.assertEqual(code, 3)
                self.assertIn(f"type /necro {verb} in the Hermes chat", text)

    def test_standalone_approve_and_unfreeze_send_the_person_to_hermes(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            text, code = commands.do_approve(p.dir, commands.Approvals(None))
            self.assertNotEqual(code, 0)
            self.assertIn("/necro approve", text)
            text, code = commands.do_unfreeze(p.dir, commands.Approvals(None))
            self.assertNotEqual(code, 0)
            self.assertIn("/necro unfreeze", text)
            self.assertIsNotNone(lock.read_lock(p.dir))

    def test_the_readme_never_tells_anyone_to_run_a_command_that_refuses(self):
        readme = (REPO / "README.md").read_text()
        self.assertEqual(re.findall(r"(?<![/\w])necro\s+(?:freeze|approve|unfreeze)\b", readme), [])

    def test_the_demo_leads_with_the_slash_command(self):
        from necro.demo import run_demo
        out = run_demo(keep=False)
        self.assertIn("> /necro freeze", out)
        self.assertEqual(re.findall(r"(?<![/\w])necro\s+(?:freeze|approve|unfreeze)\b", out), [])


class AncestorWalk(unittest.TestCase):
    def test_a_ps_failure_part_way_refuses(self):
        calls = []

        def fake_ps(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 0, stdout="  4242 -zsh\n", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ps: no such process")

        self.assertIsNone(commands.hermes_ancestors(run=fake_ps))
        self.assertIsNotNone(commands.person_problem("freeze", environ={}, stdin_isatty=True,
                                                     ancestors=commands.hermes_ancestors(run=fake_ps)))


class FindOverAFrozenProject(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def blocked(self, cmd, wd=None, roots=()):
        return guard.check_tool_call("terminal", {"command": cmd}, str(wd or self.p.dir), None,
                                     frozen_roots=roots) is not None

    def test_find_up_from_a_subfolder_is_blocked(self):
        self.assertTrue(self.blocked("find .. -name lock.json -delete", wd=self.p.dir / "contracts"))

    def test_any_destructive_find_over_the_project_is_blocked_whatever_its_filters(self):
        self.assertTrue(self.blocked("find . -name '*.tmp' -delete"))
        self.assertTrue(self.blocked("find build -name '*.o' -exec rm {} +"))
        self.assertTrue(self.blocked("find . -name '*.py' -exec sed -i '' s/a/b/ {} +"))

    def test_a_destructive_find_from_a_folder_that_contains_the_project_is_blocked(self):
        self.assertTrue(self.blocked("find . -name '*.tmp' -delete", wd=self.p.dir.parent, roots=[str(self.p.dir)]))

    def test_read_only_finds_and_finds_elsewhere_are_allowed(self):
        self.assertFalse(self.blocked("find . -name '*.py' -print"))
        self.assertFalse(self.blocked("find . -name '*.py' -exec grep -n split {} +"))
        with Project() as other:
            self.assertFalse(self.blocked(f"find {other.dir} -name '*.tmp' -delete"))


if __name__ == "__main__":
    unittest.main()
