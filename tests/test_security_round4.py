"""The items still open after review rounds 1 to 4 (Nov 10), each reproduced before its fix."""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import FIXED, REPO, Project

from necro import commands, gate, guard, lock, spec
from test_plugin import FakeCtx, OldHermesCtx, load_plugin


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
            ctx = OldHermesCtx(p.dir)
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
        with Project() as other:  # typed from outside any frozen project, pointing elsewhere
            self.assertFalse(self.blocked(f"find {other.dir} -name '*.tmp' -delete", wd=other.dir))


class Round5(unittest.TestCase):
    """Round 5: the find guard becomes an allowlist and looks inside every segment; long input in a
    frozen project is refused outright."""

    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def blocked(self, tool, payload, wd=None):
        key = "command" if tool == "terminal" else "code"
        return guard.check_tool_call(tool, {key: payload}, str(wd or self.p.dir)) is not None

    def test_find_runs_only_read_only_programs(self):
        self.assertTrue(self.blocked("terminal", "find . -name '*.py' -exec sh -c 'cat > \"$1\"' _ {} \\;"))
        self.assertTrue(self.blocked("terminal", "find . -name '*.py' -fprint out.txt"))
        self.assertFalse(self.blocked("terminal", "find . -name '*.py' -exec grep -n split {} +"))
        self.assertFalse(self.blocked("terminal", "find . -name '*.py' -exec wc -l {} +"))

    def test_any_over_long_input_inside_a_frozen_project_is_refused(self):
        long_text = "echo " + "a" * (guard.MAX_TEXT + 10)
        self.assertTrue(self.blocked("terminal", long_text))
        self.assertTrue(self.blocked("execute_code", "x = 1\n" * (guard.MAX_TEXT // 6 + 10)))
        with Project() as elsewhere:
            self.assertFalse(self.blocked("terminal", long_text, wd=elsewhere.dir))

    def test_find_is_recognised_inside_other_commands(self):
        for cmd in ("gfind . -delete", "busybox find . -delete", "echo x | xargs find . -delete",
                    "sh -c 'find . -name \"*.py\" -delete'", "bash -c \"cd . && find . -delete\""):
            self.assertTrue(self.blocked("terminal", cmd), cmd)


class FindTokenEdges(unittest.TestCase):
    """Last guard holes (Nov 10): a path as the -exec program, upper-case FIND, and find wrapped in shell syntax."""

    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def blocked(self, cmd):
        return guard.check_tool_call("terminal", {"command": cmd}, str(self.p.dir)) is not None

    def test_a_path_as_the_exec_program_is_not_a_reader(self):
        self.assertTrue(self.blocked("find . -name '*.py' -exec ./grep {} ;"))
        self.assertTrue(self.blocked("find . -name '*.py' -exec /tmp/cat {} +"))
        self.assertFalse(self.blocked("find . -name '*.py' -exec grep -n split {} +"))

    def test_find_is_matched_without_regard_to_case(self):
        self.assertTrue(self.blocked("FIND . -delete"))
        self.assertTrue(self.blocked("GFind . -delete"))

    def test_find_wrapped_in_shell_syntax_is_recognised(self):
        for cmd in ("echo $(find . -delete)", "echo `find . -delete`", "(find . -delete)", "{ find . -delete; }",
                    "echo \"$(find . -delete)\""):
            self.assertTrue(self.blocked(cmd), cmd)


class FindTextRule(unittest.TestCase):
    """The final guard rule (Nov 10): no token parsing for find, one fail-closed text check."""

    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def blocked(self, cmd):
        return guard.check_tool_call("terminal", {"command": cmd}, str(self.p.dir)) is not None

    def test_command_substitution(self):
        self.assertTrue(self.blocked("echo $(find . -delete)"))

    def test_no_spaces_around_and(self):
        self.assertTrue(self.blocked("x=1&&find . -delete"))

    def test_read_only_names_are_exact_and_case_sensitive(self):
        self.assertTrue(self.blocked("find . -name '*.py' -exec GREP -n x {} +"))
        self.assertFalse(self.blocked("find . -name '*.py' -exec grep -n x {} +"))

    def test_a_path_is_never_a_read_only_name(self):
        self.assertTrue(self.blocked("find . -name '*.py' -exec ./grep -n x {} +"))

    def test_quotes_and_backslashes_inside_words_do_not_hide_find(self):
        for cmd in ("fi''nd . -de''lete", '"fi"nd . -de"le"te', "f\\ind . -de\\lete", "find . -exec r''m {} +"):
            self.assertTrue(self.blocked(cmd), cmd)
        self.assertFalse(self.blocked("find . -name '*.py' -exec 'grep' -n x {} +"))
        self.assertTrue(self.blocked("find . -name '*.py' -exec 'GREP' -n x {} +"))

    def test_a_find_from_outside_that_names_the_frozen_project_is_blocked(self):
        # The plain-text rule (review, Nov 10 late): the project's path, or a folder above it, anywhere in the text
        # once quotes and backslashes are dropped. Quoting and globs cannot hide it.
        outside = tempfile.mkdtemp(prefix="necro-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        roots = [str(self.p.dir)]
        d, up = self.p.dir, self.p.dir.parent
        for cmd in (f"find {d} -delete", f"find '{d}'/contracts -name '*.py' -exec rm {{}} +", f'find "{up}" -delete',
                    f"find {up}/ -delete", f"find {up}/* -delete", f"find {up}/{d.name[:6]}* -delete"):
            self.assertIsNotNone(guard.check_tool_call("terminal", {"command": cmd}, outside, frozen_roots=roots), cmd)
        code = f"import subprocess\nsubprocess.run(['find', '{d}', '-delete'])\n"
        self.assertIsNotNone(guard.check_tool_call("execute_code", {"code": code}, outside, frozen_roots=roots))
        code = f"import subprocess\nsubprocess.run(['find', '{up}', '-delete'])\n"
        self.assertIsNotNone(guard.check_tool_call("execute_code", {"code": code}, outside, frozen_roots=roots))
        self.assertIsNone(guard.check_tool_call("terminal", {"command": f"find {outside} -delete"}, outside,
                                                frozen_roots=roots))


if __name__ == "__main__":
    unittest.main()
