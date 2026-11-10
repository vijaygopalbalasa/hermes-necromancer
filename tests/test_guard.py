"""The pre_tool_call guard: the agent may read frozen contracts but not rewrite them."""

import unittest

from helpers import FIXED, Project

from necro import guard, lock


class FileTools(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def check(self, tool, args, cwd=None):
        return guard.check_tool_call(tool, args, str(cwd or self.p.dir))

    def test_write_file_to_a_frozen_contract_is_blocked(self):
        msg = self.check("write_file", {"path": "contracts/test_fair.py", "content": "x"})
        self.assertIsNotNone(msg)
        self.assertIn("contracts/test_fair.py", msg)
        self.assertIn("frozen", msg)

    def test_an_absolute_path_is_blocked_too(self):
        msg = self.check("write_file", {"path": str(self.p.path("contracts/test_fair.py")), "content": "x"}, cwd="/")
        self.assertIsNotNone(msg)

    def test_a_dot_dot_path_is_blocked_too(self):
        msg = self.check("patch", {"path": "../contracts/test_adds_up.py", "old_string": "a", "new_string": "b"}, cwd=self.p.dir / "contracts")
        self.assertIsNotNone(msg)

    def test_patch_on_the_spec_is_blocked(self):
        self.assertIsNotNone(self.check("patch", {"path": "necro.toml", "old_string": "a", "new_string": "b"}))

    def test_a_v4a_patch_touching_a_frozen_file_is_blocked(self):
        body = "*** Begin Patch\n*** Update File: contracts/test_adds_up.py\n@@\n-x\n+y\n*** End Patch\n"
        self.assertIsNotNone(self.check("patch", {"mode": "patch", "patch": body}))

    def test_writing_into_the_necro_folder_is_blocked(self):
        self.assertIsNotNone(self.check("write_file", {"path": ".necro/lock.json", "content": "{}"}))
        self.assertIsNotNone(self.check("write_file", {"path": ".necro/receipts/x.json", "content": "{}"}))

    def test_editing_the_code_under_test_is_allowed(self):
        self.assertIsNone(self.check("write_file", {"path": "tipsplit.py", "content": FIXED}))
        self.assertIsNone(self.check("patch", {"path": "tipsplit.py", "old_string": "a", "new_string": "b"}))

    def test_a_new_file_next_to_the_contracts_is_allowed(self):
        self.assertIsNone(self.check("write_file", {"path": "contracts/helpers_scratch.py", "content": "x"}))

    def test_reading_a_frozen_contract_is_allowed(self):
        self.assertIsNone(self.check("read_file", {"path": "contracts/test_fair.py"}))

    def test_a_project_without_a_lock_is_left_alone(self):
        with Project() as other:
            self.assertIsNone(guard.check_tool_call("write_file", {"path": "contracts/test_fair.py", "content": "x"}, str(other.dir)))


class Shell(unittest.TestCase):
    def setUp(self):
        self.p = Project(code=FIXED)
        lock.freeze(self.p.dir)

    def tearDown(self):
        self.p.cleanup()

    def check(self, cmd, **extra):
        return guard.check_tool_call("terminal", {"command": cmd, **extra}, str(self.p.dir))

    def test_sed_in_place_on_a_frozen_contract_is_blocked(self):
        self.assertIsNotNone(self.check("sed -i '' 's/1)/99)/' contracts/test_fair.py"))

    def test_redirecting_into_a_frozen_contract_is_blocked(self):
        self.assertIsNotNone(self.check("echo 'pass' > contracts/test_fair.py"))

    def test_removing_or_moving_a_frozen_contract_is_blocked(self):
        self.assertIsNotNone(self.check("rm contracts/test_fair.py"))
        self.assertIsNotNone(self.check("mv contracts/test_fair.py /tmp/x"))

    def test_touching_the_lock_from_the_shell_is_blocked(self):
        self.assertIsNotNone(self.check("cat new.json > .necro/lock.json"))
        self.assertIsNotNone(self.check("rm -rf .necro"))

    def test_freezing_from_the_agent_shell_is_blocked(self):
        msg = self.check("hermes necro freeze")
        self.assertIsNotNone(msg)
        self.assertIn("person", msg)
        self.assertIsNotNone(self.check("hermes   necro approve"))

    def test_reading_and_running_contracts_is_allowed(self):
        self.assertIsNone(self.check("cat contracts/test_fair.py"))
        self.assertIsNone(self.check("python3 -m unittest contracts.test_fair"))
        self.assertIsNone(self.check("hermes necro check"))
        self.assertIsNone(self.check("git diff contracts/test_fair.py"))

    def test_editing_code_from_the_shell_is_allowed(self):
        self.assertIsNone(self.check("sed -i '' 's/a/b/' tipsplit.py"))

    def test_workdir_is_honoured(self):
        msg = guard.check_tool_call("terminal", {"command": "rm test_fair.py", "workdir": str(self.p.dir / "contracts")}, "/")
        self.assertIsNotNone(msg)

    def test_execute_code_writing_a_frozen_contract_is_blocked(self):
        code = "open('contracts/test_fair.py', 'w').write('')"
        self.assertIsNotNone(guard.check_tool_call("execute_code", {"code": code}, str(self.p.dir)))

    def test_execute_code_reading_a_frozen_contract_is_allowed(self):
        code = "print(open('contracts/test_fair.py').read())"
        self.assertIsNone(guard.check_tool_call("execute_code", {"code": code}, str(self.p.dir)))


if __name__ == "__main__":
    unittest.main()
