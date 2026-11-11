"""The catalog review of Nov 11 2026 (NousResearch/hermes-agent#152908).

1. Every child process the plugin starts gets the small contract environment, never the full one, so provider
   keys never reach git, ps or the demo's helper.
2. The guard answers fast on huge or whitespace-heavy tool calls. Hermes gives up on a hook after 30 seconds and
   carries on without it, so a slow guard would be a way around it."""

import ast
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import FIXED, Project

from necro import commands, guard, lock, receipts, runner

ROOT = Path(__file__).resolve().parents[1]
GIT_SWITCHES = {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"}
SECRETS = {"OPENAI_API_KEY": "sk-test-not-real", "ANTHROPIC_API_KEY": "sk-ant-test-not-real",
           "NOUS_API_KEY": "test-not-real", "GIT_DIR": "/elsewhere/.git"}


class ChildProcessesGetTheContractEnvironment(unittest.TestCase):
    def test_git_gets_only_the_contract_environment_and_two_switches(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(kwargs.get("env"))
            return subprocess.CompletedProcess(argv, 0, b"true\n", b"")

        with mock.patch.dict(os.environ, SECRETS), mock.patch.object(receipts.subprocess, "run", fake_run):
            receipts._git(Path("."), "rev-parse", "--is-inside-work-tree")
        env = seen[0]
        self.assertIsNotNone(env, "git must not inherit the full environment")
        self.assertFalse(set(SECRETS) & set(env), sorted(set(SECRETS) & set(env)))
        self.assertLessEqual(set(env), set(runner.PASS_ENV) | GIT_SWITCHES)
        self.assertEqual((env["GIT_OPTIONAL_LOCKS"], env["GIT_TERMINAL_PROMPT"]), ("0", "0"))

    def test_git_still_lists_a_real_repository_with_it(self):
        p = Project(code=FIXED)
        self.addCleanup(p.cleanup)
        subprocess.run(["git", "init", "-q", str(p.dir)], check=True, env=runner.contract_env(),
                       stdin=subprocess.DEVNULL, capture_output=True)
        with mock.patch.dict(os.environ, SECRETS):
            listing = receipts.git_listing(p.dir)
        self.assertIsNotNone(listing)
        self.assertIn("tipsplit.py", listing)
        self.assertIn("contracts/test_fair.py", listing)

    def test_ps_for_the_person_check_gets_it_too(self):
        seen = []

        def fake_ps(argv, **kwargs):
            seen.append(kwargs.get("env"))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        with mock.patch.dict(os.environ, SECRETS):
            commands.hermes_ancestors(run=fake_ps)
        self.assertIsNotNone(seen[0], "ps must not inherit the full environment")
        self.assertFalse(set(SECRETS) & set(seen[0]))

    def test_every_subprocess_call_in_the_plugin_names_its_environment(self):
        missing = []
        for path in sorted((ROOT / "necro").glob("*.py")) + [ROOT / "__init__.py"]:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                        and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
                        and not any(k.arg == "env" for k in node.keywords)):
                    missing.append(f"{path.name}:{node.lineno}")
        self.assertEqual(missing, [])


class TheGuardIsFastOnHugeInput(unittest.TestCase):
    SIZE = 200_000  # patches: any size is read line by line
    SHELL = guard.MAX_TEXT - 1_000  # shell commands: just under the size where the guard stops splitting words
    BUDGET = 2.0  # seconds for one call, far under Hermes's 30 second hook limit; these took minutes before

    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)
        self.addCleanup(self.p.cleanup)

    def check(self, tool, args):
        start = time.perf_counter()
        out = guard.check_tool_call(tool, args, str(self.p.dir))
        took = time.perf_counter() - start
        self.assertLess(took, self.BUDGET, f"{tool} took {took:.2f} s")
        return out

    def test_a_v4a_move_line_of_spaces(self):
        self.check("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Move File: " + " " * self.SIZE
                             + "\n*** End Patch\n"})

    def test_a_v4a_move_line_of_spaces_and_line_breaks(self):
        self.check("patch", {"mode": "patch", "patch": "*** Move File:" + " \n" * (self.SIZE // 2)})

    def test_a_v4a_move_with_many_arrows(self):
        self.check("patch", {"mode": "patch", "patch": "*** Move File: a" + " ->" * (self.SIZE // 3)})

    def test_an_open_subshell_of_spaces_next_to_necro(self):
        self.check("terminal", {"command": "necro status; (" + " " * self.SHELL})

    def test_many_open_subshells_next_to_necro(self):
        self.check("terminal", {"command": "necro status; " + "( &" * (self.SHELL // 3)})

    def test_many_shutil_calls_that_never_close(self):
        self.check("terminal", {"command": "python3 -c 'contracts/test_fair.py' " + "shutil.a(" * (self.SHELL // 9)})

    def test_a_huge_command_naming_a_frozen_file_is_refused_quickly(self):
        msg = self.check("terminal", {"command": "python3 -c 'contracts/test_fair.py' " + "shutil.a(" * (self.SIZE // 9)})
        self.assertEqual(msg, guard.TOO_LONG_BLOCK)

    def test_a_long_fs_method_name(self):
        self.check("terminal", {"command": "node -e 'contracts/test_fair.py' fs." + "write" * (self.SHELL // 5)})


class TheFasterGuardStillBlocks(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)
        self.addCleanup(self.p.cleanup)

    def check(self, tool, args):
        return guard.check_tool_call(tool, args, str(self.p.dir))

    def test_a_v4a_move_of_a_frozen_file_with_extra_spaces(self):
        body = "*** Begin Patch\n*** Move File:   contracts/test_fair.py   ->   elsewhere.py  \n*** End Patch\n"
        self.assertIsNotNone(self.check("patch", {"mode": "patch", "patch": body}))

    def test_a_v4a_move_onto_a_frozen_file(self):
        body = "*** Begin Patch\n*** Move File: notes.py -> contracts/test_fair.py\n*** End Patch\n"
        self.assertIsNotNone(self.check("patch", {"mode": "patch", "patch": body}))

    def test_v4a_move_targets_are_read_from_the_first_arrow(self):
        body = "*** Move File:  a.py  ->  b.py -> c.py\n"
        self.assertEqual(guard.file_targets("patch", {"mode": "patch", "patch": body}), ["a.py", "b.py -> c.py"])

    def test_a_detached_subshell_next_to_necro(self):
        for cmd in ("necro status; ( sleep 1 & )", "necro status && (sleep 1 &)", "( necro status &)",
                    "necro status; (\n  sleep 1 &\n)"):
            self.assertEqual(self.check("terminal", {"command": cmd}), guard.EVASION_BLOCK, cmd)

    def test_a_plain_subshell_next_to_necro_is_allowed(self):
        self.assertIsNone(self.check("terminal", {"command": "necro status; (cd docs && ls)"}))

    def test_a_frozen_path_wrapped_in_a_call_inside_shutil_or_os(self):
        for cmd in ("python3 -c \"import shutil; from pathlib import Path; shutil.copy('x.py', Path('contracts/test_fair.py'))\"",
                    "python3 -c \"import shutil; shutil.move('x.py', str('contracts/test_fair.py'))\"",
                    "python3 -c \"import os; os.replace('x.py', ('contracts/test_fair.py'))\""):
            self.assertIsNotNone(self.check("terminal", {"command": cmd}), cmd)

    def test_reading_a_frozen_file_after_an_unrelated_shutil_call_is_allowed(self):
        cmd = "python3 -c \"import shutil; shutil.which('python3'); print(open('contracts/test_fair.py').read())\""
        self.assertIsNone(self.check("terminal", {"command": cmd}))

    def test_shutil_and_fs_writes_to_a_frozen_file(self):
        for cmd in ("python3 -c \"import shutil; shutil.copy('x.py', 'contracts/test_fair.py')\"",
                    "node -e \"const fs = require('fs'); fs.writeFileSync('contracts/test_fair.py', '')\"",
                    "node -e \"const fs = require('fs'); fs.promises.appendFile('contracts/test_fair.py', 'x')\""):
            self.assertIsNotNone(self.check("terminal", {"command": cmd}), cmd)


if __name__ == "__main__":
    unittest.main()
