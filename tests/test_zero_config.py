"""Zero-config freeze (Nov 11): /necro freeze in a folder with no necro.toml finds the tests and the
command that runs them, writes necro.toml and locks it. Only a person's freeze writes anything."""

import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, Project

from necro import commands, detect, guard, lock
from test_integrity import forbidden_opens
from test_plugin import FakeCtx, FakeState, load_plugin

CALC_BUGGY = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
UNITTEST_TEST = (
    "import unittest\n"
    "from calc import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_adds(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
PYTEST_IMPORT_TEST = "import pytest\nfrom calc import add\n\n\ndef test_adds():\n    assert add(2, 3) == 5\n"
PYTEST_BARE_TEST = "from calc import add\n\n\ndef test_adds():\n    assert add(2, 3) == 5\n"
NPM_DEFAULT = 'echo "Error: no test specified" && exit 1'
SECRET = "DUMMY_TOKEN_FOR_THIS_TEST_ONLY=necro-zero-config-5521"


class Folder:
    """A throwaway folder holding exactly the files given (relative path -> text)."""

    def __init__(self, files=None):
        self.dir = Path(tempfile.mkdtemp(prefix="necro-zc-")).resolve()
        for rel, text in (files or {}).items():
            self.write(rel, text)

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def link(self, rel, target):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
        return p

    def path(self, rel):
        return self.dir / rel

    def read(self, rel):
        return (self.dir / rel).read_text(encoding="utf-8")

    def state(self):
        """Every entry under the folder: a link's text, or a file's bytes (this test's own files only)."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.dir):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.dir)
                if os.path.islink(full):
                    out[rel] = ("link", os.readlink(full))
                elif os.path.isfile(full):
                    with open(full, "rb") as fh:
                        out[rel] = ("file", fh.read())
                else:
                    out[rel] = ("dir", None)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def unittest_project():
    return Folder({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST})


def run(f, text, approvals=None, **kw):
    """/necro <text> as the person types it in the Hermes chat."""
    approvals = approvals if approvals is not None else commands.Approvals(FakeState())
    return commands.dispatch(text, f.dir, approvals, **kw)


def no_runner_problem(*args, **kwargs):
    return None


class Detection(unittest.TestCase):
    def found(self, files):
        with Folder(files) as f:
            return detect.detect(f.dir)

    def test_pytest_named_in_pyproject(self):
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST,
                          "pyproject.toml": '[project]\nname = "calc"\n\n[project.optional-dependencies]\n'
                                            'test = ["pytest>=8"]\n'})
        self.assertEqual((got.kind, got.run, got.patterns), ("pytest", "python3 -m pytest -q", ["tests/**"]))

    def test_pytest_named_in_each_other_config_file(self):
        for name, text in (("setup.cfg", "[tool:pytest]\naddopts = -q\n"), ("tox.ini", "[testenv]\ncommands = pytest\n"),
                           ("requirements-dev.txt", "pytest==8.3.3\n"), ("requirements.txt", "pytest\n"),
                           ("pytest.ini", "[pytest]\n")):
            with self.subTest(name=name):
                got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST, name: text})
                self.assertEqual(got.run, "python3 -m pytest -q")

    def test_pytest_from_a_conftest(self):
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST, "conftest.py": "import sys\n"})
        self.assertEqual(got.run, "python3 -m pytest -q")
        self.assertEqual(got.patterns, ["tests/**", "**/conftest.py"])
        self.assertEqual(got.files, ["conftest.py", "tests/test_calc.py"])

    def test_pytest_from_a_test_file_that_imports_it(self):
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": PYTEST_IMPORT_TEST})
        self.assertEqual(got.run, "python3 -m pytest -q")

    def test_pytest_from_bare_test_functions(self):
        # A test function outside any TestCase runs only under pytest.
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": PYTEST_BARE_TEST})
        self.assertEqual(got.run, "python3 -m pytest -q")

    def test_unittest_in_a_tests_folder(self):
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST})
        self.assertEqual((got.kind, got.run), ("unittest", "python3 -m unittest discover -q -s tests"))
        self.assertEqual(got.patterns, ["tests/**"])
        self.assertIn(".coverage", got.outputs)

    def test_unittest_at_the_top(self):
        got = self.found({"calc.py": CALC_BUGGY, "test_calc.py": UNITTEST_TEST})
        self.assertEqual(got.run, "python3 -m unittest discover -q")
        self.assertEqual(got.patterns, ["**/test_*.py"])

    def test_unittest_in_a_package_needs_no_start_folder(self):
        got = self.found({"calc.py": CALC_BUGGY, "tests/__init__.py": "", "tests/test_calc.py": UNITTEST_TEST})
        self.assertEqual(got.run, "python3 -m unittest discover -q")

    def test_unittest_in_any_one_folder(self):
        # The demo's layout: the tests sit in contracts/, which has no __init__.py.
        got = self.found({"calc.py": CALC_BUGGY, "contracts/test_add.py": UNITTEST_TEST,
                          "contracts/test_more.py": UNITTEST_TEST})
        self.assertEqual(got.run, "python3 -m unittest discover -q -s contracts")
        self.assertEqual(got.patterns, ["**/test_*.py"])

    def test_unittest_files_named_x_test(self):
        got = self.found({"calc.py": CALC_BUGGY, "calc_test.py": UNITTEST_TEST})
        self.assertEqual(got.run, "python3 -m unittest discover -q -p '*_test.py'")
        self.assertEqual(shlex.split(got.run)[-1], "*_test.py")
        self.assertEqual(got.patterns, ["**/*_test.py"])

    def test_a_layout_one_discover_cannot_run_is_not_guessed(self):
        # tests/unit has no __init__.py, so unittest discover would skip it and could run zero tests.
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_a.py": UNITTEST_TEST, "tests/unit/test_b.py": UNITTEST_TEST})
        self.assertIsNone(got.run)
        self.assertEqual(got.patterns, ["tests/**"])

    def test_node_with_a_test_script(self):
        got = self.found({"package.json": json.dumps({"scripts": {"test": "jest"}}),
                          "src/sum.js": "module.exports = (a, b) => a + b\n",
                          "src/sum.test.js": "test('adds', () => {})\n"})
        self.assertEqual((got.kind, got.run, got.patterns), ("npm", "npm test --silent", ["**/*.test.*"]))

    def test_node_jest_folders_next_to_the_code(self):
        got = self.found({"package.json": json.dumps({"scripts": {"test": "jest"}}),
                          "src/ui/__tests__/button.js": "test('x', () => {})\n",
                          "src/ui/__tests__/__snapshots__/button.js.snap": "exports[`x`] = `1`;\n"})
        self.assertEqual(got.patterns, ["**/__tests__/**"])
        self.assertEqual(len(got.files), 2)

    def test_npm_default_test_script_is_not_a_test_command(self):
        got = self.found({"package.json": json.dumps({"scripts": {"test": NPM_DEFAULT}}),
                          "src/sum.test.js": "test('adds', () => {})\n"})
        self.assertIsNone(got.run)
        self.assertEqual(got.patterns, ["**/*.test.*"])

    def test_go(self):
        got = self.found({"go.mod": "module example.com/calc\n\ngo 1.22\n", "calc.go": "package calc\n",
                          "calc_test.go": "package calc\n", "testdata/golden.txt": "5\n"})
        self.assertEqual((got.kind, got.run), ("go", "go test ./..."))
        self.assertEqual(got.patterns, ["**/*_test.go", "**/testdata/**"])

    def test_rust(self):
        got = self.found({"Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\n', "src/lib.rs": "",
                          "tests/adds.rs": "#[test]\nfn adds() {}\n"})
        self.assertEqual((got.kind, got.run, got.patterns), ("cargo", "cargo test --quiet", ["tests/**"]))
        self.assertEqual(got.outputs, ["Cargo.lock"])  # cargo test writes it when it is missing

    def test_python_comes_before_node(self):
        got = self.found({"package.json": json.dumps({"scripts": {"test": "jest"}}), "calc.py": CALC_BUGGY,
                          "tests/test_calc.py": UNITTEST_TEST})
        self.assertEqual(got.kind, "unittest")

    def test_a_glob_is_used_only_when_it_adds_files(self):
        got = self.found({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST, "tests/__init__.py": "",
                          "pkg/__init__.py": "", "pkg/tests/__init__.py": "", "pkg/tests/test_more.py": UNITTEST_TEST})
        self.assertEqual(got.patterns, ["tests/**", "**/test_*.py"])
        self.assertEqual(got.files, ["pkg/tests/test_more.py", "tests/__init__.py", "tests/test_calc.py"])

    def test_a_credential_file_or_link_among_the_matches_freezes_the_others_by_name(self):
        with Folder({"package.json": json.dumps({"scripts": {"test": "jest"}}), ".env.test.local": SECRET,
                     "src/a.test.js": "test('a', () => {})\n", "src/b.test.js": "test('b', () => {})\n",
                     "tests/server.pem": "not a real key\n", "tests/helper.js": "\n"}) as f:
            f.link("tests/data.json", "../src/a.test.js")
            got = detect.detect(f.dir)
            self.assertEqual(got.patterns, ["tests/helper.js", "src/a.test.js", "src/b.test.js"])
            self.assertNotIn(".env.test.local", got.files)
            with mock.patch.object(detect, "runner_problem", no_runner_problem):
                text, code = run(f, "freeze")
            self.assertEqual(code, 0, text)  # strict freezing accepts what detection chose
            self.assertIn("Frozen: tests/helper.js, src/a.test.js, src/b.test.js (3 files).", text)

    def test_the_home_folder_is_not_a_project(self):
        with Folder({"tests/test_calc.py": UNITTEST_TEST}) as f:
            with mock.patch.object(Path, "home", return_value=f.dir):
                got = detect.detect(f.dir)
            self.assertIn("home folder", got.problem)

    def test_a_folder_with_too_many_files_is_not_guessed(self):
        with Folder({"tests/test_calc.py": UNITTEST_TEST, "a.py": "", "b.py": ""}) as f:
            with mock.patch.object(detect, "MAX_SOURCES", 2):
                got = detect.detect(f.dir)
            self.assertIn("too many", got.problem)


class NeverOpened(unittest.TestCase):
    def test_detection_status_and_freeze_never_open_a_credential_file_or_a_link(self):
        with Folder({"calc.py": CALC_BUGGY, "tests/test_calc.py": UNITTEST_TEST, ".env": SECRET + "\n",
                     "tests/fixtures/server.pem": "not a real key\n", "real.json": json.dumps({"scripts": {"test": "jest"}}),
                     "real.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n'}) as f:
            f.link("package.json", "real.json")
            f.link("pyproject.toml", "real.toml")
            with forbidden_opens(f.dir) as opened:
                got = detect.detect(f.dir)
                run(f, "status")
                text, code = run(f, "freeze")
            self.assertEqual(opened, [])
            # The linked pyproject.toml was not read, so nothing says pytest; tests/** holds a .pem, so its
            # other files are frozen by name.
            self.assertEqual(got.run, "python3 -m unittest discover -q -s tests")
            self.assertEqual(code, 0, text)
            self.assertIn("tests/test_calc.py", lock.read_lock(f.dir)["contracts"][0]["patterns"])


class FreezeWithoutNecroToml(unittest.TestCase):
    def test_one_command_locks_a_unittest_project_and_the_gate_then_works(self):
        with unittest_project() as f:
            approvals = commands.Approvals(FakeState())
            text, code = run(f, "freeze", approvals)
            self.assertEqual(code, 0, text)
            self.assertEqual(text, "Locked your tests: `python3 -m unittest discover -q -s tests` must pass. Frozen: "
                                   "tests/** (1 file). Hermes can't say \"done\" until this passes, and it can't edit "
                                   "these files. Undo: /necro unfreeze. Change what counts as done: edit necro.toml, "
                                   "then /necro freeze. See where it stands: /necro check.")
            spec_text = f.read("necro.toml")
            self.assertTrue(spec_text.startswith("# Written by /necro freeze"), spec_text)
            lk = lock.read_lock(f.dir)
            self.assertEqual([c["id"] for c in lk["contracts"]], ["C1"])
            self.assertEqual(lk["contracts"][0]["says"], "Your tests pass")
            self.assertTrue(approvals.approved_here(f.dir, lock.lock_hash(lk)))
            out, code = commands.do_check(f.dir, approvals)
            self.assertEqual(code, 2, out)
            self.assertIn("0 of 1", out)
            f.write("calc.py", CALC_FIXED)
            out, code = commands.do_check(f.dir, approvals)
            self.assertEqual(code, 0, out)
            self.assertIn("Receipt", out)

    def test_the_slash_command_inside_hermes_does_it_and_the_guard_then_blocks_the_test(self):
        with unittest_project() as f:
            ctx = FakeCtx(f.dir)
            load_plugin().register(ctx)
            self.assertTrue(ctx.commands["necro"]("freeze").startswith("Locked your tests:"))
            out = ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "tests/test_calc.py", "content": ""},
                                             task_id="t", session_id="s")
            self.assertEqual(out["action"], "block")
            out = ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "necro.toml", "content": ""},
                                             task_id="t", session_id="s")
            self.assertEqual(out["action"], "block")
            verdict = ctx.hooks["pre_verify"](session_id="s", platform="cli", model="m", coding=True, attempt=0,
                                              final_response="Done!", changed_paths=[str(f.path("calc.py"))])
            self.assertEqual(verdict["action"], "continue")
            self.assertIn("C1", verdict["message"])

    def test_pytest_project(self):
        with Folder({"calc.py": CALC_BUGGY, "tests/test_calc.py": PYTEST_BARE_TEST, "tests/conftest.py": ""}) as f:
            with mock.patch.object(detect, "runner_problem", no_runner_problem):
                text, code = run(f, "freeze")
            self.assertEqual(code, 0, text)
            self.assertTrue(text.startswith("Locked your tests: `python3 -m pytest -q` must pass. Frozen: tests/** "
                                            "(2 files)."), text)
            self.assertEqual(tomllib.loads(f.read("necro.toml"))["contract"][0]["run"], "python3 -m pytest -q")

    def test_node_project(self):
        with Folder({"package.json": json.dumps({"scripts": {"test": "jest"}}), "src/sum.js": "",
                     "src/sum.test.js": "test('adds', () => {})\n"}) as f:
            with mock.patch.object(detect, "runner_problem", no_runner_problem):
                text, code = run(f, "freeze")
            self.assertEqual(code, 0, text)
            self.assertTrue(text.startswith("Locked your tests: `npm test --silent` must pass. Frozen: **/*.test.* "
                                            "(1 file)."), text)

    def test_go_and_rust_projects(self):
        cases = (({"go.mod": "module x\n", "x_test.go": "package x\n"}, "go test ./..."),
                 ({"Cargo.toml": '[package]\nname = "x"\n', "tests/it.rs": ""}, "cargo test --quiet"))
        for files, want in cases:
            with self.subTest(run=want), Folder(files) as f:
                with mock.patch.object(detect, "runner_problem", no_runner_problem):
                    text, code = run(f, "freeze")
                self.assertEqual(code, 0, text)
                self.assertEqual(lock.read_lock(f.dir)["contracts"][0]["run"], want)

    def test_no_tests_locks_nothing_and_says_the_two_ways_forward(self):
        with Folder({"calc.py": CALC_BUGGY}) as f:
            before = f.state()
            text, code = run(f, "freeze")
            self.assertNotEqual(code, 0)
            self.assertIn("Nothing locked", text)
            self.assertIn("/necro freeze <your test command>", text)
            self.assertIn("[[contract]]", text)
            self.assertIn("files = [", text)
            self.assertEqual(f.state(), before)

    def test_tests_without_a_known_runner_lock_nothing(self):
        with Folder({"src/sum.test.js": "test('adds', () => {})\n"}) as f:
            before = f.state()
            text, code = run(f, "freeze")
            self.assertNotEqual(code, 0)
            self.assertIn("not the command that runs them", text)
            self.assertIn("/necro freeze <your test command>", text)
            self.assertEqual(f.state(), before)

    def test_a_runner_that_is_not_installed_locks_nothing(self):
        with Folder({"package.json": json.dumps({"scripts": {"test": "jest"}}), "src/sum.test.js": ""}) as f:
            before = f.state()
            with mock.patch.object(detect.shutil, "which", return_value=None):
                text, code = run(f, "freeze")
            self.assertNotEqual(code, 0)
            self.assertIn("`npm` is not installed", text)
            self.assertEqual(f.state(), before)

    def test_python3_without_pytest_locks_nothing(self):
        with Folder({"calc.py": CALC_BUGGY, "tests/test_calc.py": PYTEST_BARE_TEST, ".venv/bin/python": ""}) as f:
            before = f.state()
            with mock.patch.object(detect, "_imports", return_value=False):
                text, code = run(f, "freeze")
            self.assertNotEqual(code, 0)
            self.assertIn("cannot import pytest", text)
            self.assertIn("/necro freeze .venv/bin/python -m pytest -q", text)
            self.assertEqual(f.state(), before)

    def test_a_failed_freeze_leaves_no_necro_toml_behind(self):
        with unittest_project() as f:
            before = f.state()
            with mock.patch.object(commands, "freeze", side_effect=commands.SpecError("tests/x.py changed")):
                text, code = run(f, "freeze")
            self.assertNotEqual(code, 0)
            self.assertIn("tests/x.py changed", text)
            self.assertEqual(f.state(), before)

    def test_a_dangling_necro_toml_link_is_not_written_through(self):
        with unittest_project() as f:
            f.link("necro.toml", "elsewhere.toml")
            text, code = run(f, "freeze")
            self.assertNotEqual(code, 0)
            self.assertIn("link", text)
            self.assertFalse(os.path.lexists(f.path("elsewhere.toml")))
            self.assertIsNone(lock.read_lock(f.dir))

    def test_the_standalone_freeze_still_asks_for_approve_in_hermes(self):
        with unittest_project() as f:
            text, code = run(f, "freeze", commands.Approvals(None))
            self.assertEqual(code, 0, text)
            self.assertIn("/necro approve", text)


class FreezeWithACommand(unittest.TestCase):
    def test_the_persons_command_is_used(self):
        with unittest_project() as f:
            text, code = run(f, "freeze python3 -m unittest discover -q -s tests")
            self.assertEqual(code, 0, text)
            self.assertTrue(text.startswith("Locked your tests: `python3 -m unittest discover -q -s tests` must pass."))

    def test_a_quoted_argument_survives_into_the_lock(self):
        with unittest_project() as f:
            approvals = commands.Approvals(FakeState())
            text, code = run(f, "freeze python3 -m unittest discover -q -s tests -p 'test_*.py'", approvals)
            self.assertEqual(code, 0, text)
            got = lock.read_lock(f.dir)["contracts"][0]["run"]
            self.assertEqual(shlex.split(got), ["python3", "-m", "unittest", "discover", "-q", "-s", "tests", "-p",
                                                "test_*.py"])
            f.write("calc.py", CALC_FIXED)
            self.assertIn("1 of 1", commands.do_check(f.dir, approvals)[0])

    def test_a_whole_quoted_command_is_unwrapped(self):
        with unittest_project() as f:
            text, code = run(f, 'freeze "python3 -m unittest discover -q -s tests"')
            self.assertEqual(code, 0, text)
            self.assertEqual(lock.read_lock(f.dir)["contracts"][0]["run"], "python3 -m unittest discover -q -s tests")

    def test_shell_syntax_runs_through_sh(self):
        with unittest_project() as f:
            approvals = commands.Approvals(FakeState())
            text, code = run(f, "freeze true && python3 -m unittest discover -q -s tests", approvals)
            self.assertEqual(code, 0, text)
            got = lock.read_lock(f.dir)["contracts"][0]["run"]
            self.assertEqual(shlex.split(got), ["sh", "-c", "true && python3 -m unittest discover -q -s tests"])
            f.write("calc.py", CALC_FIXED)
            out, code = commands.do_check(f.dir, approvals)
            self.assertEqual(code, 0, out)

    def test_a_script_the_command_names_is_frozen_too(self):
        with Folder({"calc.py": CALC_BUGGY, "check.sh": "#!/bin/sh\nexit 0\n"}) as f:
            os.chmod(f.path("check.sh"), 0o755)
            text, code = run(f, "freeze ./check.sh")
            self.assertEqual(code, 0, text)
            self.assertIn("Frozen: check.sh (1 file)", text)
            msg = guard.check_tool_call("terminal", {"command": "echo 'exit 0' > check.sh"}, str(f.dir))
            self.assertIn("Blocked", msg or "")

    def test_a_command_that_names_no_file_and_no_tests_locks_nothing(self):
        with Folder({"calc.py": CALC_BUGGY}) as f:
            before = f.state()
            text, code = run(f, "freeze true")
            self.assertNotEqual(code, 0)
            self.assertIn("Nothing locked", text)
            self.assertIn("[[contract]]", text)
            self.assertEqual(f.state(), before)

    def test_a_command_when_necro_toml_exists_changes_nothing(self):
        with Project() as p:
            before = p.read("necro.toml")
            text, code = commands.dispatch("freeze python3 -m pytest -q", p.dir, commands.Approvals(FakeState()))
            self.assertNotEqual(code, 0)
            self.assertIn("already", text)
            self.assertEqual(p.read("necro.toml"), before)
            self.assertIsNone(lock.read_lock(p.dir))

    def test_a_lock_without_its_necro_toml_keeps_the_old_answer(self):
        with Project() as p:
            commands.dispatch("freeze", p.dir, commands.Approvals(FakeState()))
            p.path("necro.toml").unlink()
            text, code = commands.dispatch("freeze", p.dir, commands.Approvals(FakeState()))
            self.assertNotEqual(code, 0)
            self.assertIn("Could not freeze", text)
            self.assertFalse(p.path("necro.toml").exists())

    def test_an_existing_necro_toml_freezes_as_before(self):
        with Project() as p:
            text, code = commands.dispatch("freeze", p.dir, commands.Approvals(FakeState()))
            self.assertEqual(code, 0, text)
            self.assertIn("Froze 3 contracts", text)


class PersonOnly(unittest.TestCase):
    MARKERS = ({"NECRO_AGENT_SESSION": "s1"}, {"HERMES_AGENT": "true"}, {"AI_AGENT": "hermes-agent"})

    def test_a_freeze_from_a_shell_never_writes_necro_toml(self):
        for marker in self.MARKERS:
            for text in ("freeze", "freeze python3 -m unittest discover -q -s tests"):
                with self.subTest(marker=marker, text=text), unittest_project() as f:
                    before = f.state()
                    with mock.patch.dict(os.environ, marker):
                        out, code = commands.dispatch(text, f.dir, commands.Approvals(None), from_shell=True)
                    self.assertEqual(code, 3, out)
                    self.assertEqual(f.state(), before)

    def test_the_standalone_command_without_a_terminal_writes_nothing(self):
        with unittest_project() as f:
            before = f.state()
            out = subprocess.run([sys.executable, str(REPO / "necro"), "freeze", "--dir", str(f.dir)],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(out.returncode, 3, out.stdout)
            self.assertEqual(f.state(), before)

    def test_hermes_cli_freeze_writes_nothing(self):
        with unittest_project() as f:
            before = f.state()
            ctx = FakeCtx(f.dir)
            plugin = load_plugin().register(ctx)
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"HERMES_AGENT": "true"}):
                code = plugin.cli_run(argparse.Namespace(action="freeze", receipt=None, dir=str(f.dir), run=False))
            self.assertEqual(code, 3)
            self.assertEqual(f.state(), before)


class StatusBeforeAnything(unittest.TestCase):
    def test_it_says_what_was_found_in_one_line(self):
        with unittest_project() as f:
            before = f.state()
            text, code = run(f, "status")
            self.assertEqual(code, 0)
            self.assertEqual(len(text.splitlines()), 1, text)
            self.assertIn("tests/** (1 file)", text)
            self.assertIn("`python3 -m unittest discover -q -s tests`", text)
            self.assertTrue(text.endswith("Type /necro freeze to lock them."), text)
            self.assertEqual(run(f, "")[0], text)
            self.assertEqual(f.state(), before)

    def test_nothing_found(self):
        with Folder({"calc.py": CALC_BUGGY}) as f:
            text, code = run(f, "status")
            self.assertEqual(code, 0)
            self.assertEqual(len(text.splitlines()), 1, text)
            self.assertIn("no tests were found", text)
            self.assertIn("/necro freeze <your test command>", text)

    def test_test_files_without_a_runner(self):
        with Folder({"src/sum.test.js": ""}) as f:
            text, _ = run(f, "status")
            self.assertIn("not the command that runs them", text)

    def test_cli_status_slash_status_and_the_agent_tool_change_nothing(self):
        with unittest_project() as f:
            before = f.state()
            ctx = FakeCtx(f.dir)
            plugin = load_plugin().register(ctx)
            ctx.commands["necro"]("status")
            ctx.commands["necro"]("")
            self.assertFalse(json.loads(ctx.tools["necro_status"]["handler"]({}))["frozen"])
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                plugin.cli_run(argparse.Namespace(action="status", receipt=None, dir=str(f.dir), run=False))
            self.assertIn("Type /necro freeze to lock them.", shown.getvalue())
            self.assertEqual(f.state(), before)


class GeneratedToml(unittest.TestCase):
    def test_it_round_trips_quotes_and_backslashes_and_keeps_outputs_at_the_top(self):
        run_cmd = 'python3 -c "import sys; sys.exit(0)" C:\\x'
        text = detect.toml_text(run_cmd, ["tests/**", "it's.py"], [".coverage", "htmlcov/**"])
        data = tomllib.loads(text)
        self.assertEqual(data["contract"][0]["run"], run_cmd)
        self.assertEqual(data["contract"][0]["files"], ["tests/**", "it's.py"])
        self.assertEqual(data["outputs"], [".coverage", "htmlcov/**"])
        self.assertTrue(text.startswith("# "))
        self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()
