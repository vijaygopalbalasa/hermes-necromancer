"""Lock what shapes the run (Nov 11): field pins on the settings that pick or change the tests (package.json
scripts, pytest's sections, Cargo's test keys), test-only config files frozen whole or kept absent, runner files
(Makefile and the like) for a person's command, sh -c inner commands, and one run at freeze."""

import json
import os
import sys
import textwrap
import unittest
from unittest import mock

from helpers import REPO  # noqa: F401  (puts the plugin on sys.path)

from necro import commands, detect, gate, lock, verify
from test_plugin import FakeState
from test_zero_config import CALC_BUGGY, CALC_FIXED, UNITTEST_TEST, Folder, no_runner_problem, not_run, run

PY = sys.executable
PASSES = f'{json.dumps(PY)[1:-1]} -c pass'


def spec(pins, files='["tests/**"]'):
    return textwrap.dedent(f'''
        [[contract]]
        id = "C1"
        says = "Your tests pass"
        run = "{PASSES}"
        files = {files}
        pins = {json.dumps(pins)}
        ''')


def frozen(f):
    """Freeze necro.toml as a person would and return approvals for the folder."""
    approvals = commands.Approvals(FakeState())
    text, code = commands.dispatch("freeze", f.dir, approvals)
    assert code == 0, text
    return approvals


def verdict(f, approvals):
    return gate.evaluate(f.dir, budget_s=60, approved=approvals.mapping())


def reasons(v):
    return [reason for r in v.results for reason in r.reasons]


PACKAGE = {"name": "calc", "scripts": {"test": "jest"}, "devDependencies": {"jest": "29.7.0"}}


class FieldPins(unittest.TestCase):
    def test_npm_test_script_is_pinned_and_other_edits_stay_allowed(self):
        with Folder({"tests/a.test.js": "", "package.json": json.dumps(PACKAGE),
                     "necro.toml": spec(["package.json#scripts.test"])}) as f:
            approvals = frozen(f)
            self.assertTrue(verdict(f, approvals).ok)
            f.write("package.json", json.dumps({**PACKAGE, "devDependencies": {"jest": "30.0.0", "x": "1"}}))
            self.assertTrue(verdict(f, approvals).ok, "a dependency edit is allowed")
            f.write("package.json", json.dumps({**PACKAGE, "scripts": {"test": "exit 0"}}))
            v = verdict(f, approvals)
            self.assertFalse(v.ok)
            self.assertEqual(v.results[0].status, "tampered")
            self.assertIn("the test command in package.json changed (scripts.test)", reasons(v))
            self.assertIn("the test command in package.json changed (scripts.test)", gate.continue_message(v))

    def test_verify_says_mismatch_after_a_pinned_change(self):
        with Folder({"tests/a.test.js": "", "package.json": json.dumps(PACKAGE),
                     "necro.toml": spec(["package.json#scripts.test"])}) as f:
            approvals = frozen(f)
            self.assertTrue(verdict(f, approvals).ok)
            f.write("package.json", json.dumps({**PACKAGE, "scripts": {"test": "exit 0"}}))
            out = verify.verify_receipt(next((f.dir / ".necro" / "receipts").glob("*.json")), approved=approvals.mapping())
            self.assertEqual(out.verdict, "MISMATCH")
            self.assertIn("package.json", out.message)

    def test_pytest_settings_in_pyproject_are_pinned(self):
        pyproject = '[project]\nname = "calc"\ndependencies = []\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
        with Folder({"tests/test_a.py": "", "pyproject.toml": pyproject,
                     "necro.toml": spec(["pyproject.toml#tool.pytest"])}) as f:
            approvals = frozen(f)
            f.write("pyproject.toml", pyproject.replace("dependencies = []", 'dependencies = ["attrs"]'))
            self.assertTrue(verdict(f, approvals).ok, "a dependency edit is allowed")
            f.write("pyproject.toml", pyproject.replace('"-q"', '"-q -k nothing"'))
            v = verdict(f, approvals)
            self.assertFalse(v.ok)
            self.assertIn("the pytest settings in pyproject.toml changed (tool.pytest)", reasons(v))

    def test_cargo_test_keys_are_pinned(self):
        cargo = '[package]\nname = "calc"\nversion = "0.1.0"\n\n[dependencies]\n'
        with Folder({"tests/it.rs": "", "Cargo.toml": cargo, "necro.toml": spec(list(detect.CARGO_PINS))}) as f:
            approvals = frozen(f)
            f.write("Cargo.toml", cargo + 'serde = "1"\n')
            self.assertTrue(verdict(f, approvals).ok, "a dependency edit is allowed")
            f.write("Cargo.toml", cargo.replace('version = "0.1.0"', 'version = "0.1.0"\nautotests = false'))
            v = verdict(f, approvals)
            self.assertIn("the test settings in Cargo.toml changed (package.autotests)", reasons(v))

    def test_a_pinned_file_that_does_not_parse_fails_closed(self):
        with Folder({"tests/a.test.js": "", "package.json": json.dumps(PACKAGE),
                     "necro.toml": spec(["package.json#scripts.test"])}) as f:
            approvals = frozen(f)
            f.write("package.json", "{not json")
            v = verdict(f, approvals)
            self.assertFalse(v.ok)
            self.assertTrue(any("package.json cannot be read as JSON" in r for r in reasons(v)), reasons(v))
        with Folder({"tests/a.test.js": "", "package.json": "{not json",
                     "necro.toml": spec(["package.json#scripts.test"])}) as f:
            text, code = commands.dispatch("freeze", f.dir, commands.Approvals(FakeState()))
            self.assertNotEqual(code, 0)
            self.assertIn("package.json cannot be read as JSON", text)

    def test_an_absent_config_file_must_stay_absent(self):
        with Folder({"tests/test_a.py": "", "necro.toml": spec(["pytest.ini"])}) as f:
            approvals = frozen(f)
            f.write("pytest.ini", "[pytest]\naddopts = -k nothing\n")
            self.assertIn("pytest.ini was added, and it can change how the tests run", reasons(verdict(f, approvals)))

    def test_a_glob_pin_catches_a_new_conftest_anywhere(self):
        with Folder({"tests/test_a.py": "", "necro.toml": spec(["**/conftest.py"])}) as f:
            approvals = frozen(f)
            f.write("src/conftest.py", "def pytest_collection_modifyitems(items):\n    items.clear()\n")
            self.assertTrue(any("src/conftest.py" in r for r in reasons(verdict(f, approvals))))


class GeneratedPins(unittest.TestCase):
    def freeze(self, f, text="freeze"):
        with mock.patch.object(detect, "runner_problem", no_runner_problem), \
                mock.patch.object(commands, "right_now", not_run):
            out, code = run(f, text)
        self.assertEqual(code, 0, out)
        return out, lock.read_lock(f.dir)["contracts"][0]

    def test_npm_pins_the_test_script_the_scripts_it_runs_and_files_they_name(self):
        package = {"scripts": {"test": "npm run unit && node scripts/check.js", "unit": "jest --ci"}}
        with Folder({"package.json": json.dumps(package), "src/a.test.js": "", "scripts/check.js": ""}) as f:
            _, c = self.freeze(f)
            pins = [p["pin"] for p in c["pins"]]
            for want in ("package.json#scripts.test", "package.json#scripts.pretest", "package.json#scripts.unit",
                         "package.json#scripts.preunit", "package.json#scripts.postunit", "package.json#jest",
                         "jest.config.*"):
                self.assertIn(want, pins)
            self.assertIn("scripts/check.js", c["patterns"])

    def test_pytest_freezes_pytest_ini_whole_and_pins_the_rest(self):
        with Folder({"calc.py": CALC_BUGGY, "tests/test_a.py": "import pytest\n", "pytest.ini": "[pytest]\n"}) as f:
            _, c = self.freeze(f)
            self.assertIn("pytest.ini", c["patterns"])
            pins = [p["pin"] for p in c["pins"]]
            for want in ("pyproject.toml#tool.pytest", "setup.cfg#[tool:pytest]", "tox.ini#[pytest]", "pytest.toml",
                         "**/conftest.py"):
                self.assertIn(want, pins)
            self.assertIn("pins lock the settings that shape the run", f.read("necro.toml"))

    def test_cargo_pins_its_test_keys(self):
        with Folder({"Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\n', "tests/it.rs": ""}) as f:
            _, c = self.freeze(f)
            pins = [p["pin"] for p in c["pins"]]
            for want in detect.CARGO_PINS:
                self.assertIn(want, pins)


class PersonsCommand(unittest.TestCase):
    def freeze(self, f, text):
        with mock.patch.object(detect, "runner_problem", no_runner_problem), \
                mock.patch.object(commands, "right_now", not_run):
            return run(f, text)

    def test_a_known_runner_freezes_its_file_whole(self):
        for command, name in (("make test", "Makefile"), ("tox -e py", "tox.ini"), ("nox -s tests", "noxfile.py"),
                              ("just test", "justfile"), ("task test", "Taskfile.yml")):
            with self.subTest(command=command), Folder({"tests/test_a.py": UNITTEST_TEST, name: "x\n"}) as f:
                text, code = self.freeze(f, f"freeze {command}")
                self.assertEqual(code, 0, text)
                self.assertIn(name, lock.read_lock(f.dir)["contracts"][0]["patterns"])

    def test_sh_c_inner_commands_name_files_too(self):
        with Folder({"tests/test_a.py": UNITTEST_TEST, "scripts/check.py": ""}) as f:
            text, code = self.freeze(f, "freeze sh -c 'python3 scripts/check.py && true'")
            self.assertEqual(code, 0, text)
            self.assertIn("scripts/check.py", lock.read_lock(f.dir)["contracts"][0]["patterns"])

    def test_a_named_file_that_cannot_be_locked_is_said_plainly(self):
        with Folder({"tests/test_a.py": UNITTEST_TEST, "check.py": "", "fixture.pem": "not a real key\n"}) as f:
            text, code = self.freeze(f, "freeze python3 check.py fixture.pem")
            self.assertEqual(code, 0, text)
            self.assertIn("Not locked: fixture.pem is a credential file, which Contract Necromancer never opens.", text)

    def test_a_program_inside_a_skipped_folder_is_refused(self):
        with Folder({"tests/test_a.py": UNITTEST_TEST, ".venv/bin/pytest": ""}) as f:
            before = f.state()
            text, code = self.freeze(f, "freeze .venv/bin/pytest -q")
            self.assertNotEqual(code, 0)
            self.assertIn("that program (.venv/bin/pytest) lives in .venv, which is not locked; use python3 -m pytest "
                          "-q, or lock it yourself in necro.toml", text)
            self.assertEqual(f.state(), before)


class RunAtFreeze(unittest.TestCase):
    TWO = UNITTEST_TEST + "\n    def test_subtracts_nothing(self):\n        self.assertEqual(add(0, 0), 0)\n"

    def test_it_says_how_many_fail_right_now(self):
        with Folder({"calc.py": CALC_BUGGY, "tests/test_calc.py": self.TWO}) as f:
            text, code = run(f, "freeze")
            self.assertEqual(code, 0, text)
            self.assertTrue(text.endswith("\nRight now: 1 of 2 tests fails. Hermes can't say done until they pass."),
                            text)

    def test_it_says_when_all_pass(self):
        with Folder({"calc.py": CALC_FIXED, "tests/test_calc.py": self.TWO}) as f:
            text, _ = run(f, "freeze")
            self.assertTrue(text.endswith("\nRight now: all 2 tests pass."), text)

    def test_a_run_longer_than_the_budget_points_to_necro_check(self):
        slow = "import time, unittest\n\nclass Slow(unittest.TestCase):\n    def test_slow(self):\n        time.sleep(5)\n"
        with Folder({"tests/test_slow.py": slow}) as f:
            text, code = commands.dispatch("freeze", f.dir, commands.Approvals(FakeState()), run_budget_s=1.0)
            self.assertEqual(code, 0, text)
            self.assertTrue(text.endswith("\nRun /necro check to see where it stands."), text)

    def test_counts_from_each_runner(self):
        samples = {
            "1 failed, 3 passed in 0.12s\n": (1, 4),
            "Ran 3 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n": (2, 3),
            "Tests:       2 failed, 2 passed, 4 total\n": (2, 4),
            "      Tests  2 failed | 2 passed (4)\n": (2, 4),
            "# tests 4\n# pass 2\n# fail 2\n": (2, 4),
            "\u2139 tests 2\n\u2139 pass 1\n\u2139 fail 1\n\u2716 failing tests:\n": (1, 2),
            "test result: FAILED. 1 passed; 1 failed; 0 ignored\n": (1, 2),
            "nothing to see\n": None,
        }
        for text, want in samples.items():
            self.assertEqual(detect.test_counts(text), want, text)



class PinsAndTheCache(unittest.TestCase):
    """Review, Nov 11: a pinned file outside sources = [...] is not in the gate's snapshot, so a cached pass
    must not survive a change to a pin."""

    def test_a_pin_change_is_caught_even_when_sources_leave_the_file_out(self):
        from test_plugin import FakeCtx, load_plugin
        toml = 'sources = ["src/**"]\n' + spec(["package.json#scripts.test"])
        with Folder({"src/a.js": "", "tests/a.test.js": "", "package.json": json.dumps(PACKAGE), "necro.toml": toml}) as f:
            ctx = FakeCtx(f.dir)
            load_plugin().register(ctx)
            self.assertIn("Froze 1 contract", ctx.commands["necro"]("freeze"))
            fire = lambda: ctx.hooks["pre_verify"](session_id="s", platform="cli", model="m", coding=True,  # noqa: E731
                                                   attempt=0, final_response="Done", changed_paths=[str(f.path("src/a.js"))])
            self.assertIsNone(fire())  # passes, and the pass is cached
            f.write("package.json", json.dumps({**PACKAGE, "scripts": {"test": "exit 0"}}))
            out = fire()
            self.assertIsNotNone(out, "a cached pass outlived a changed pin")
            self.assertIn("the test command in package.json changed (scripts.test)", out["message"])

    def test_a_date_in_a_pinned_toml_table_is_fine(self):
        pyproject = '[tool.pytest.ini_options]\naddopts = "-q"\nsince = 2026-11-11\n'
        with Folder({"tests/test_a.py": "", "pyproject.toml": pyproject,
                     "necro.toml": spec(["pyproject.toml#tool.pytest"])}) as f:
            approvals = frozen(f)
            self.assertTrue(verdict(f, approvals).ok)
            f.write("pyproject.toml", pyproject.replace("2026-11-11", "2026-11-12"))
            self.assertIn("the pytest settings in pyproject.toml changed (tool.pytest)", reasons(verdict(f, approvals)))


class PinKeysStandApart(unittest.TestCase):
    """Security review, Nov 11: pin observations live apart from file paths, so a file named like a pin key never
    collides with one, and pin keys never reach a receipt or the list of files a run changed."""

    def test_a_file_named_like_a_pin_key_is_hashed_as_a_file(self):
        import hashlib
        weird = "pin package.json#scripts.test"
        with Folder({"tests/a.test.js": "", "package.json": json.dumps(PACKAGE), weird: "hello\n",
                     "necro.toml": spec(["package.json#scripts.test"])}) as f:
            approvals = frozen(f)
            v = verdict(f, approvals)
            self.assertTrue(v.ok, v.problems)
            rc = json.loads(v.receipt_path.read_text())
            entry = {s["path"]: s for s in rc["sources"]}[weird]
            self.assertEqual(entry["sha256"], hashlib.sha256(b"hello\n").hexdigest())
            self.assertFalse(any(s["path"].startswith("pin ") and s["path"] != weird for s in rc["sources"]))
            cache = {}
            gate.evaluate(f.dir, budget_s=60, approved=approvals.mapping(), cache=cache)
            f.write(weird, "changed\n")
            again = gate.evaluate(f.dir, budget_s=60, approved=approvals.mapping(), cache=cache)
            self.assertFalse(again.cached, "an edit to that file must not reuse the cached run")


class MoreRunnerFiles(unittest.TestCase):
    """Security review, Nov 11: vitest reads vite.config.* when there is no vitest.config.*; cargo reads
    .cargo/config(.toml) and build.rs, and package.build names another build script."""

    def freeze(self, f):
        with mock.patch.object(detect, "runner_problem", no_runner_problem), \
                mock.patch.object(commands, "right_now", not_run):
            out, code = run(f, "freeze")
        self.assertEqual(code, 0, out)
        c = lock.read_lock(f.dir)["contracts"][0]
        return c["patterns"], [p["pin"] for p in c.get("pins", [])]

    def test_vite_config_is_frozen_when_there_and_kept_absent_when_not(self):
        package = json.dumps({"scripts": {"test": "vitest run"}})
        with Folder({"package.json": package, "src/a.test.ts": "", "vite.config.ts": "export default {}\n"}) as f:
            patterns, pins = self.freeze(f)
            self.assertIn("vite.config.*", patterns)
        with Folder({"package.json": package, "src/a.test.ts": ""}) as f:
            patterns, pins = self.freeze(f)
            self.assertIn("vite.config.*", pins)

    def test_cargo_build_files_and_package_build(self):
        cargo = '[package]\nname = "x"\nversion = "0.1.0"\n'
        with Folder({"Cargo.toml": cargo, "tests/it.rs": "", "build.rs": "fn main() {}\n"}) as f:
            patterns, pins = self.freeze(f)
            self.assertIn("build.rs", patterns)
            for want in (".cargo/config.toml", ".cargo/config", "Cargo.toml#package.build"):
                self.assertIn(want, pins)


if __name__ == "__main__":
    unittest.main()
