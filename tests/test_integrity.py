"""Integrity of pins after the fresh-clone change (Nov 10): ignored and credential files are pinned and
marked, another checkout may lack them, and the re-run decides."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import BUGGY, FIXED, REPO, Project

from necro import gate, lock, receipts, verify

SECRET = "DUMMY_TOKEN_FOR_THIS_TEST_ONLY=necro-test-value-8231"


class GitWork:
    """A throwaway author repository and helpers to commit and clone it."""

    def __init__(self, code):
        self.work = Path(tempfile.mkdtemp(prefix="necro-int-")).resolve()
        self.author = self.work / "author"
        shutil.copytree(REPO / "demo" / "tip_split", self.author)
        (self.author / "tipsplit.py").write_text(code)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.work)}
        self.git("init", "-q")

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd or self.author, env=self.env, check=True, capture_output=True)

    def commit_and_clone(self) -> Path:
        self.git("add", "-A")
        self.git("commit", "-qm", "work, lock and receipt")
        clone = self.work / "reviewer"
        self.git("clone", "-q", str(self.author), str(clone), cwd=self.work)
        return clone

    def cleanup(self):
        shutil.rmtree(self.work, ignore_errors=True)


def pins_of(v) -> dict:
    return {s["path"]: s for s in json.loads(v.receipt_path.read_text())["sources"]}


class IgnoredPins(unittest.TestCase):
    def test_the_judges_env_case_is_confirmed_in_a_clone_with_a_note(self):
        g = GitWork(FIXED)
        try:
            (g.author / ".gitignore").write_text(".env\n")
            (g.author / ".env").write_text(SECRET + "\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            pin = pins_of(v)[".env"]
            self.assertTrue(pin.get("ignored"))
            self.assertTrue(pin.get("credential"))
            clone = g.commit_and_clone()
            out = verify.verify_receipt(clone / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            self.assertIn("1 local-only file not present here: .env", out.message)
        finally:
            g.cleanup()

    def test_a_gitignored_shadow_module_is_in_the_receipt_and_fails_the_clone_rerun(self):
        g = GitWork(BUGGY)
        try:
            (g.author / ".gitignore").write_text("unittest.py\n")
            (g.author / "unittest.py").write_text("raise SystemExit(0)\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(v.ok, "the shadow makes the local run pass")
            self.assertTrue(pins_of(v)["unittest.py"].get("ignored"), "the shadow must show up in the receipt")
            clone = g.commit_and_clone()
            out = verify.verify_receipt(clone / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("C1", out.message)
        finally:
            g.cleanup()

    def test_a_shadow_hidden_through_git_info_exclude_is_pinned_too(self):
        g = GitWork(BUGGY)
        try:
            lock.freeze(g.author)
            (g.author / "unittest.py").write_text("raise SystemExit(0)\n")
            (g.author / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (g.author / ".git" / "info" / "exclude").write_text("unittest.py\n")
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(pins_of(v)["unittest.py"].get("ignored"))
            clone = g.commit_and_clone()
            out = verify.verify_receipt(clone / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
        finally:
            g.cleanup()

    def test_an_ignored_pin_that_is_present_but_different_is_a_mismatch(self):
        g = GitWork(FIXED)
        try:
            (g.author / ".gitignore").write_text("local.cfg\n")
            (g.author / "local.cfg").write_text("a = 1\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            (g.author / "local.cfg").write_text("a = 2\n")
            out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("local.cfg", out.message)
        finally:
            g.cleanup()

    def test_a_tracked_pin_missing_in_a_checkout_is_a_mismatch(self):
        g = GitWork(FIXED)
        try:
            (g.author / "data.txt").write_text("x\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            clone = g.commit_and_clone()
            (clone / "data.txt").unlink()
            out = verify.verify_receipt(clone / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("data.txt", out.message)
        finally:
            g.cleanup()


class RunsMustLeaveIgnoredFilesAlone(unittest.TestCase):
    def test_a_run_that_writes_an_ignored_file_fails_naming_it(self):
        g = GitWork(FIXED)
        try:
            (g.author / ".gitignore").write_text("scratch.log\n")
            (g.author / "contracts" / "test_refuses_zero.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_w(self):\n"
                "        open('scratch.log', 'a').write('x')\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            self.assertFalse(v.ok)
            self.assertTrue(any("scratch.log" in p for p in v.problems), v.problems)
        finally:
            g.cleanup()


class CredentialFiles(unittest.TestCase):
    def test_credential_files_are_pinned_by_hash_and_their_contents_never_appear(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            raw = v.receipt_path.read_text()
            pin = {s["path"]: s for s in json.loads(raw)["sources"]}[".env"]
            self.assertTrue(pin.get("credential"))
            self.assertEqual(pin["sha256"], hashlib.sha256((SECRET + "\n").encode()).hexdigest())
            self.assertNotIn("necro-test-value-8231", raw)
            self.assertNotIn("necro-test-value-8231", gate.stamp(v, "Done."))

    def test_a_run_that_changes_a_credential_file_fails(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            p.write("contracts/test_refuses_zero.py",
                    "import unittest\nclass T(unittest.TestCase):\n    def test_w(self):\n"
                    "        open('.env', 'a').write('MORE=1\\n')\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertFalse(v.ok)
            self.assertTrue(any(".env" in x for x in v.problems), v.problems)
            self.assertFalse(any("necro-test-value-8231" in x for x in v.problems))


class DoneClaimsPerSentence(unittest.TestCase):
    def test_a_done_sentence_counts_whatever_the_reply_ends_with(self):
        for text in ["All done! Want me to add more tests?", "Fixed the rounding. Should I also update the docs?",
                     "All done, want me to add more tests?"]:
            self.assertTrue(gate.looks_like_done(text), text)

    def test_a_reply_with_no_done_sentence_is_exempt(self):
        for text in ["I finished reading tipsplit.py. Want me to fix it?", "Should I mark it done?",
                     "Is the task complete?", "Here is how the tests are organised. Which one first?"]:
            self.assertFalse(gate.looks_like_done(text), text)


if __name__ == "__main__":
    unittest.main()
