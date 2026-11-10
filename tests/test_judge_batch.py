"""Findings from an independent judge-style review (Nov 10), reproduced from its probes before the fixes."""

import contextlib
import io
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import FIXED, REPO, Project

from necro import commands, gate, guard, lock, receipts, verify
from necro import demo as demo_module
from test_plugin import load_plugin


def demo_copy() -> Path:
    p = Path(tempfile.mkdtemp(prefix="judge-")).resolve()
    shutil.copytree(REPO / "demo" / "tip_split", p, dirs_exist_ok=True)
    return p


class DemoCannotBeCheated(unittest.TestCase):
    """Judge probe B: a new contracts/__init__.py that no-ops assertLessEqual."""

    def test_the_judges_cheat_is_blocked_and_caught(self):
        p = demo_copy()
        try:
            ctx = demo_module._DemoCtx(p)
            load_plugin().register(ctx)
            ctx.commands["necro"]("freeze")
            (p / "tipsplit.py").write_text(demo_module.HALF_FIX)
            r = ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "contracts/__init__.py", "content": "x"})
            self.assertIsNotNone(r, "the guard must block creating a file under the frozen contracts folder")
            (p / "contracts" / "__init__.py").write_text(
                "import unittest\nunittest.TestCase.assertLessEqual = lambda *a, **k: None\n")
            v = ctx.hooks["pre_verify"](session_id="b", changed_paths=[str(p / "contracts/__init__.py")])
            self.assertIsNotNone(v, "the gate passed code that splits 10 cents as [4, 2, 2, 2]")
            self.assertEqual(v["action"], "continue")
            self.assertIn("contracts/__init__.py", v["message"])
            t = ctx.hooks["transform_llm_output"](response_text="Done! All tests pass.", session_id="b")
            self.assertTrue(t.startswith("Not done."), t)
            self.assertEqual(runpy.run_path(str(p / "tipsplit.py"))["split_bill"](10, 4), [4, 2, 2, 2])
        finally:
            shutil.rmtree(p, ignore_errors=True)

    def test_the_demo_freezes_the_whole_contracts_folder(self):
        lk_spec = (REPO / "demo" / "tip_split" / "necro.toml").read_text()
        self.assertIn('"contracts/**"', lk_spec)


class VerifyInAFreshClone(unittest.TestCase):
    """Judge probe C: the author had an ignored .env; a reviewer verifies in a fresh git clone."""

    def git(self, *args, cwd):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)}
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    def test_an_ignored_env_file_does_not_break_verify_in_a_clone(self):
        work = Path(tempfile.mkdtemp(prefix="judge-git-")).resolve()
        try:
            a = work / "author"
            shutil.copytree(REPO / "demo" / "tip_split", a)
            (a / "tipsplit.py").write_text(FIXED)
            (a / ".gitignore").write_text(".env\n")
            (a / ".env").write_text("DUMMY_SETTING_FOR_THIS_TEST=1\n")  # the test's own dummy file
            self.git("init", "-q", cwd=a)
            lock.freeze(a)
            v = gate.evaluate(a, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            self.git("add", "-A", cwd=a)
            self.git("commit", "-qm", "work, lock and receipt", cwd=a)
            self.git("clone", "-q", str(a), str(work / "reviewer"), cwd=work)
            receipt = work / "reviewer" / v.receipt_path.relative_to(a)
            out = verify.verify_receipt(receipt, run=True)
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_credential_files_are_pinned_by_hash_and_marked(self):
        # Since the integrity review: hashed (never stored or printed) and marked, so another checkout may lack them.
        with Project(code=FIXED) as p:
            for name in (".env", ".env.local", "deploy/server.pem", ".netrc"):
                p.write(name, "dummy")
            lk = lock.freeze(p.dir)
            pins = {e["path"]: e for e in receipts.pin(p.dir, receipts.source_files(p.dir, lk))}
            for name in (".env", ".env.local", "deploy/server.pem", ".netrc"):
                self.assertTrue(pins[name].get("credential"), name)
            self.assertNotIn("credential", pins["tipsplit.py"])


class DoneClaims(unittest.TestCase):
    """Judge probe D: 'I finished reading tipsplit.py ... want me to fix it?' was stamped Not done."""

    def test_reading_reviewing_or_asking_is_not_a_done_claim(self):
        for text in [
            "I finished reading tipsplit.py. split_bill drops the leftover cents; want me to fix it?",
            "Done looking through the tests.",
            "I've finished reviewing the code.",
            "I completed the review of the rounding code.",
        ]:
            self.assertFalse(gate.looks_like_done(text), text)

    def test_real_done_claims_still_count(self):
        for text in [
            "Done! The shares add up now.",
            "I've fixed the rounding bug.",
            "Finished: split_bill now spreads the leftover cents.",
            "All tests pass.",
            "The task is complete.",
        ]:
            self.assertTrue(gate.looks_like_done(text), text)

    def test_the_judges_reply_is_not_stamped(self):
        p = demo_copy()
        try:
            ctx = demo_module._DemoCtx(p)
            load_plugin().register(ctx)
            ctx.commands["necro"]("freeze")
            ctx.hooks["pre_llm_call"](session_id="d")
            t = ctx.hooks["transform_llm_output"](
                response_text="I finished reading tipsplit.py. split_bill drops the leftover cents; want me to fix it?",
                session_id="d")
            self.assertIsNone(t)
        finally:
            shutil.rmtree(p, ignore_errors=True)


class NoStrayOutput(unittest.TestCase):
    def test_the_demo_prints_no_warnings(self):
        out = subprocess.run([sys.executable, str(REPO / "necro"), "demo"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=300)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr.strip(), "")

    def test_registering_with_a_full_context_logs_nothing(self):
        p = demo_copy()
        try:
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream):
                load_plugin().register(demo_module._DemoCtx(p))
            self.assertEqual(stream.getvalue(), "")
        finally:
            shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
