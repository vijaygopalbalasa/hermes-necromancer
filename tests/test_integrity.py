"""Integrity of pins after the fresh-clone change (Nov 10): ignored and credential files are pinned and
marked, another checkout may lack them, and the re-run decides."""

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import BUGGY, FIXED, REPO, Project

from necro import canon, commands, gate, lock, receipts, spec, verify

SECRET = "DUMMY_TOKEN_FOR_THIS_TEST_ONLY=necro-test-value-8231"

# Every open() in this process (builtins.open, io.open, os.open, pathlib) raises the "open" audit event.
# While a test watches a project folder, any open of a credential file in it is recorded: by its own
# name, or by the name of the file a link points at.
_AUDIT = {"under": None, "opened": []}


def _audit(event, args):
    under = _AUDIT["under"]
    if under is None or event != "open" or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(args[0])
    if not os.path.abspath(path).startswith(under):
        return
    if spec.is_credential(os.path.basename(path)) or spec.is_credential(os.path.basename(os.path.realpath(path))):
        _AUDIT["opened"].append(path)


sys.addaudithook(_audit)


@contextlib.contextmanager
def credential_opens(folder):
    """Yields the list of credential files opened under ``folder`` while the block runs."""
    _AUDIT["opened"] = []
    _AUDIT["under"] = os.path.realpath(folder).rstrip("/") + "/"
    try:
        yield _AUDIT["opened"]
    finally:
        _AUDIT["under"] = None


def rehash(receipt_path: Path, change) -> None:
    """Rewrite a receipt with ``change`` applied and a matching receiptHash, as a forger would."""
    rc = json.loads(receipt_path.read_text())
    change(rc)
    rc["receiptHash"] = receipts.receipt_hash(rc)
    receipt_path.write_text(json.dumps(rc))


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
    """Catalog rule 11 wins (Nov 10): credential files are never opened, not even to hash them."""

    def test_credential_pins_hold_presence_size_and_mtime_only_and_the_file_is_never_opened(self):
        real_open = open
        opened = []

        def spy(file, *args, **kwargs):
            if str(file).endswith(".env"):
                opened.append(str(file))
                raise AssertionError(f"necro opened a credential file: {file}")
            return real_open(file, *args, **kwargs)

        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            with mock.patch("builtins.open", side_effect=spy):
                v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            self.assertEqual(opened, [])
            raw = v.receipt_path.read_text()
            pin = {s["path"]: s for s in json.loads(raw)["sources"]}[".env"]
            st = os.stat(p.path(".env"))
            self.assertEqual(pin, {"path": ".env", "credential": True, "size": st.st_size, "mtimeNs": st.st_mtime_ns})
            self.assertNotIn("necro-test-value-8231", raw)
            self.assertNotIn(hashlib.sha256((SECRET + "\n").encode()).hexdigest(), raw)

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

    def run_with(self, body: str):
        """Evaluate a project with a .env whose C3 test body does something to credential files."""
        p = Project(code=FIXED)
        self.addCleanup(p.cleanup)
        p.write(".env", SECRET + "\n")
        p.write("contracts/test_refuses_zero.py",
                "import os, unittest\nclass T(unittest.TestCase):\n    def test_w(self):\n" + body)
        lock.freeze(p.dir)
        with credential_opens(p.dir) as opened:
            v = gate.evaluate(p.dir, budget_s=60)
        self.assertEqual(opened, [])
        return v

    def test_a_run_that_creates_or_deletes_a_credential_file_fails_naming_it(self):
        v = self.run_with("        open('.env.test', 'w').write('X=1\\n')\n")
        self.assertFalse(v.ok)
        self.assertTrue(any(".env.test" in x for x in v.problems), v.problems)
        v = self.run_with("        os.remove('.env')\n")
        self.assertFalse(v.ok)
        self.assertTrue(any(".env" in x for x in v.problems), v.problems)

    def test_a_run_that_only_touches_a_credential_file_fails(self):
        v = self.run_with("        st = os.stat('.env')\n        os.utime('.env', ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))\n")
        self.assertFalse(v.ok)
        self.assertTrue(any(".env" in x for x in v.problems), v.problems)

    def test_the_stated_limit_a_change_that_keeps_size_and_mtime_is_not_seen(self):
        # Never opening the file has this price, and the README says so plainly.
        v = self.run_with("        st = os.stat('.env')\n        open('.env', 'w').write('X' * st.st_size)\n"
                          "        os.utime('.env', ns=(st.st_atime_ns, st.st_mtime_ns))\n")
        self.assertTrue(v.ok, v.problems)

    def test_necro_never_opens_a_credential_file_anywhere(self):
        with Project(code=FIXED) as p:
            for name in (".env", ".env.local", "deploy/server.pem", "certs/Site.KEY", ".netrc"):
                p.write(name, SECRET + "\n")
            os.symlink(".env", p.path("settings.txt"))  # an innocent name for a link to a credential file
            with credential_opens(p.dir) as opened:
                lk = lock.freeze(p.dir)
                self.assertIsNotNone(gate.fingerprint(p.dir))
                v = gate.evaluate(p.dir, budget_s=60)
                self.assertTrue(v.ok, v.problems)
                pins = {e["path"]: e for e in receipts.pin(p.dir, receipts.source_files(p.dir, lk))}
                commands.status(p.dir, commands.Approvals(None))
                out = verify.verify_receipt(v.receipt_path, run=True)
                self.assertIsNone(canon.hash_file(p.path(".env")))
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            for name in (".env", ".env.local", "deploy/server.pem", "certs/Site.KEY", ".netrc", "settings.txt"):
                self.assertTrue(pins[name].get("credential"), name)
                self.assertNotIn("sha256", pins[name], name)
                self.assertNotIn("sha256", pins_of(v)[name], name)
            self.assertNotIn("credential", pins["tipsplit.py"])

    def test_an_untouched_credential_file_confirms_in_the_same_checkout(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            with credential_opens(p.dir) as opened:
                out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            self.assertNotIn("local-only", out.message)

    def test_a_credential_file_changed_after_the_receipt_is_a_mismatch_naming_it(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            with open(p.path(".env"), "a") as handle:
                handle.write("MORE=1\n")
            with credential_opens(p.dir) as opened:
                out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn(".env", out.message)
            self.assertNotIn("necro-test-value-8231", out.message)

    def test_a_copy_without_the_credential_file_is_confirmed_with_a_note(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            copy = Path(tempfile.mkdtemp(prefix="necro-copy-")).resolve()
            self.addCleanup(shutil.rmtree, copy, True)
            shutil.copytree(p.dir, copy, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".env"))
            out = verify.verify_receipt(copy / v.receipt_path.relative_to(p.dir), run=True)
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            self.assertIn("1 local-only file not present here: .env", out.message)

    def test_a_receipt_that_pins_a_credential_file_by_hash_is_a_mismatch_and_it_stays_closed(self):
        # A forged receipt names .env with its real SHA-256: verify decides by the file name, so it never
        # opens .env to compare, and the pin cannot match.
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            real = hashlib.sha256((SECRET + "\n").encode()).hexdigest()
            rehash(v.receipt_path, lambda rc: rc["sources"].__setitem__(
                [s["path"] for s in rc["sources"]].index(".env"), {"path": ".env", "sha256": real}))
            forged = receipts.receipt_file(p.dir, json.loads(v.receipt_path.read_text())["receiptHash"])
            v.receipt_path.rename(forged)
            with credential_opens(p.dir) as opened:
                out = verify.verify_receipt(forged, run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn(".env", out.message)

    def test_a_contract_cannot_freeze_a_credential_file(self):
        with Project(code=FIXED) as p:
            p.write("contracts/.env", SECRET + "\n")
            p.write("necro.toml", p.read("necro.toml").replace('files = ["contracts/test_fair.py"]',
                                                                'files = ["contracts/**"]'))
            with credential_opens(p.dir) as opened, self.assertRaises(spec.SpecError) as caught:
                lock.freeze(p.dir)
            self.assertEqual(opened, [])
            self.assertIn("contracts/.env", str(caught.exception))
            self.assertIn("credential", str(caught.exception))
            self.assertIsNone(lock.read_lock(p.dir))

    def test_verify_will_not_open_a_credential_file_given_as_the_receipt(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            with credential_opens(p.dir) as opened:
                out = verify.verify_receipt(p.path(".env"))
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "MISMATCH")
            self.assertNotIn("necro-test-value-8231", out.message)

    def test_credential_names_match_in_any_case(self):
        for name in (".ENV", ".Env.Local", "SERVER.PEM", "Id_Rsa", ".NETRC"):
            self.assertTrue(spec.is_credential(name), name)
        for name in ("environment.py", "key.py", "netrc.md", "envoy.yaml"):
            self.assertFalse(spec.is_credential(name), name)


class VerifyExtras(unittest.TestCase):
    """An unpinned file in the verifying checkout is ADDED (MISMATCH), ignored or not; only credential
    files get a note instead, and they are never opened."""

    def test_a_tarball_style_copy_with_an_ignored_shadow_module_is_a_mismatch(self):
        g = GitWork(FIXED)
        try:
            (g.author / ".gitignore").write_text("unittest.py\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            copy = g.work / "tarball"
            shutil.copytree(g.author, copy)
            (copy / "unittest.py").write_text("raise SystemExit(0)\n")
            out = verify.verify_receipt(copy / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("unittest.py", out.message)
        finally:
            g.cleanup()

    def test_a_clone_with_its_own_env_file_is_confirmed_with_a_note(self):
        g = GitWork(FIXED)
        try:
            (g.author / ".gitignore").write_text(".env\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            clone = g.commit_and_clone()
            (clone / ".env").write_text(SECRET + "\n")
            with credential_opens(clone) as opened:
                out = verify.verify_receipt(clone / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            self.assertIn("local credential file not in the receipt: .env", out.message)
            self.assertNotIn("necro-test-value-8231", out.message)
        finally:
            g.cleanup()


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
