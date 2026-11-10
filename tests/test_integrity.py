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
from necro import demo

SECRET = "DUMMY_TOKEN_FOR_THIS_TEST_ONLY=necro-test-value-8231"

# Every open() in this process (builtins.open, io.open, os.open, pathlib) raises the "open" audit event.
# While a test watches a project folder, two kinds of open in it are recorded: a credential file (by its
# own name, or the name a link resolves to), and any path that is itself a link (necro never reads
# through a link).
_AUDIT = {"under": None, "opened": []}


def _audit(event, args):
    under = _AUDIT["under"]
    if under is None or event != "open" or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(args[0])
    if not (os.path.abspath(path).startswith(under) or os.path.realpath(path).startswith(under)):
        return
    if (os.path.islink(path) or spec.is_credential(os.path.basename(path))
            or spec.is_credential(os.path.basename(os.path.realpath(path)))):
        _AUDIT["opened"].append(path)


sys.addaudithook(_audit)


@contextlib.contextmanager
def forbidden_opens(folder):
    """Yields the list of credential files and links opened under ``folder`` while the block runs."""
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

    def test_credential_pins_hold_presence_size_and_stat_times_only_and_the_file_is_never_opened(self):
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
            self.assertEqual(pin, {"path": ".env", "credential": True, "size": st.st_size, "mtimeNs": st.st_mtime_ns,
                                   "ino": st.st_ino, "ctimeNs": st.st_ctime_ns})
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
        with forbidden_opens(p.dir) as opened:
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

    def test_a_rewrite_that_keeps_size_and_resets_mtime_is_caught_by_its_change_time(self):
        # The pin holds the inode and change time too, and touch cannot set a change time back.
        v = self.run_with("        st = os.stat('.env')\n        open('.env', 'w').write('X' * st.st_size)\n"
                          "        os.utime('.env', ns=(st.st_atime_ns, st.st_mtime_ns))\n")
        self.assertFalse(v.ok)
        self.assertTrue(any(".env" in x for x in v.problems), v.problems)

    def test_no_file_is_ever_opened_through_a_link_and_a_link_pin_holds_its_text(self):
        # Review, Nov 11: a link is never opened or followed, whatever it points to, a .env included; its pin
        # is the text os.readlink gives. A link to a file pinned on its own needs nothing more.
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            os.symlink(".env", p.path("settings.txt"))
            os.symlink(".env", p.path(".env.local"))
            os.symlink("tipsplit.py", p.path("alias.py"))
            os.symlink("contracts/test_fair.py", p.path("fair-link.py"))
            os.symlink("loop", p.path("loop"))
            self.assertFalse(canon.is_credential_path(p.path("settings.txt")))
            self.assertFalse(canon.is_credential_path(p.path(".env.local")))
            self.assertTrue(canon.is_credential_path(p.path(".env")))
            with forbidden_opens(p.dir) as opened:
                lock.freeze(p.dir)
                v = gate.evaluate(p.dir, budget_s=60)
                self.assertTrue(v.ok, v.problems)
                out = verify.verify_receipt(v.receipt_path, run=True)
                self.assertIsNone(canon.hash_file(p.path("alias.py")))
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            pins = pins_of(v)
            for name, text in (("settings.txt", ".env"), (".env.local", ".env"), ("alias.py", "tipsplit.py"),
                               ("fair-link.py", "contracts/test_fair.py"), ("loop", "loop")):
                self.assertEqual(pins[name], {"path": name, "link": text}, name)
            self.assertTrue(pins[".env"].get("credential"))

    def test_a_link_to_a_target_outside_the_project_fails_the_gate_and_verify(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="necro-elsewhere-")).resolve()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        (elsewhere / "x.py").write_text("X = 1\n")
        with Project(code=FIXED) as p:
            os.symlink(elsewhere / "x.py", p.path("helper.py"))
            lock.freeze(p.dir)
            with forbidden_opens(p.dir) as opened:
                v = gate.evaluate(p.dir, budget_s=60)
            self.assertEqual(opened, [])
            self.assertFalse(v.ok)
            self.assertTrue(any(f"a link points outside what is frozen: helper.py -> {elsewhere / 'x.py'}" in x
                                for x in v.problems), v.problems)
        with Project(code=FIXED) as p:
            os.symlink(p.path("tipsplit.py"), p.path("abs.py"))  # absolute text: fine here, outside in a copy
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            copy = Path(tempfile.mkdtemp(prefix="necro-copy-")).resolve() / "copy"
            self.addCleanup(shutil.rmtree, copy.parent, True)
            shutil.copytree(p.dir, copy, symlinks=True)
            out = verify.verify_receipt(copy / v.receipt_path.relative_to(p.dir), run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn(f"outside what is frozen: abs.py -> {p.path('tipsplit.py')}", out.message)

    def test_links_into_skipped_folders_necro_outputs_or_folders_fail_closed(self):
        with Project(code=FIXED) as p:
            p.write("necro.toml", 'outputs = ["reports/*.xml"]\n' + p.read("necro.toml"))
            p.write("node_modules/pkg/x.js", "module.exports = 1\n")
            p.write("reports/a.xml", "<a/>\n")
            os.symlink("node_modules/pkg/x.js", p.path("nm-link.js"))
            os.symlink(".necro/lock.json", p.path("lock-link.json"))
            os.symlink("reports/a.xml", p.path("report-link.xml"))
            os.symlink("contracts", p.path("tests-link"))
            lock.freeze(p.dir)
            with forbidden_opens(p.dir) as opened:
                v = gate.evaluate(p.dir, budget_s=60)
            self.assertEqual(opened, [])
            self.assertFalse(v.ok)
            problem = next((x for x in v.problems if "a link points outside what is frozen" in x), "")
            for shown in ("nm-link.js -> node_modules/pkg/x.js", "lock-link.json -> .necro/lock.json",
                          "report-link.xml -> reports/a.xml", "tests-link -> contracts"):
                self.assertIn(shown, problem, v.problems)

    def test_a_link_that_changes_appears_or_is_frozen_is_a_change_like_any_other(self):
        with Project(code=FIXED) as p:
            os.symlink("tipsplit.py", p.path("alias.py"))
            p.write("contracts/test_refuses_zero.py",
                    "import os, unittest\nclass T(unittest.TestCase):\n    def test_w(self):\n"
                    "        os.remove('alias.py'); os.symlink('necro.toml', 'alias.py')\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertFalse(v.ok)
            self.assertTrue(any("alias.py" in x for x in v.problems), v.problems)
        with Project(code=FIXED) as p:
            os.symlink("tipsplit.py", p.path("alias.py"))
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            os.remove(p.path("alias.py"))
            os.symlink("necro.toml", p.path("alias.py"))
            out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("Files changed: alias.py.", out.message)
            os.remove(p.path("alias.py"))
            os.symlink("tipsplit.py", p.path("alias.py"))
            os.symlink("tipsplit.py", p.path("extra.py"))
            out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("Files added: extra.py.", out.message)
        with Project(code=FIXED) as p:
            os.symlink("test_fair.py", p.path("contracts/test_fair_link.py"))
            p.write("necro.toml", p.read("necro.toml").replace('files = ["contracts/test_fair.py"]',
                                                                'files = ["contracts/**"]'))
            with self.assertRaises(spec.SpecError) as caught:
                lock.freeze(p.dir)
            self.assertIn("contracts/test_fair_link.py", str(caught.exception))
            self.assertIn("link", str(caught.exception))

    def test_committed_links_are_not_marked_as_ignored_by_git(self):
        g = GitWork(FIXED)
        try:
            os.symlink("tipsplit.py", g.author / "alias.py")
            os.symlink("contracts/test_fair.py", g.author / "fair-link.py")
            lock.freeze(g.author)
            g.git("add", "-A")
            g.git("commit", "-qm", "links")
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            for name in ("alias.py", "fair-link.py"):
                self.assertNotIn("ignored", pins_of(v)[name], name)
        finally:
            g.cleanup()

    def test_the_plugins_own_key_has_no_credential_name_and_the_old_one_moves_unread(self):
        from necro import commands as cmd
        state = demo._State()
        folder = Path(state.path).parent
        old_key = os.urandom(32)
        (folder / "approvals.key").write_text(old_key.hex())
        at, digest, root = "2026-11-10T00:00:00Z", "ab" * 32, "/some/project"
        state.set(cmd.Approvals.KEY, {root: {"lock": digest, "at": at, "mac": cmd.Approvals._mac(old_key, root, digest, at)}})
        with forbidden_opens(folder) as opened:
            mapping = cmd.Approvals(state).mapping()
        self.assertEqual(opened, [])
        self.assertEqual(mapping[root], digest)
        self.assertFalse((folder / "approvals.key").exists())
        self.assertEqual((folder / "approvals.hmac").read_text().strip(), old_key.hex())
        self.assertFalse(spec.is_credential("approvals.hmac"))
        fresh = demo._State()
        cmd.Approvals(fresh).approve(Path(tempfile.gettempdir()), "cd" * 64)
        self.assertTrue((Path(fresh.path).parent / "approvals.hmac").is_file())
        self.assertFalse((Path(fresh.path).parent / "approvals.key").exists())

    def test_names_python_can_import_or_run_never_get_credential_treatment(self):
        for name in (".env.py", ".env.pth", "conftest.pem", "Conftest.KEY", "sitecustomize.key", "usercustomize.pem",
                     "keys.pth"):
            self.assertFalse(spec.is_credential(name), name)
        with Project(code=FIXED) as p:
            p.write(".env.py", "X = 1\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            self.assertNotIn("credential", pins_of(v)[".env.py"])
            self.assertEqual(len(pins_of(v)[".env.py"]["sha256"]), 64)

    def test_necro_never_opens_a_credential_file_anywhere(self):
        with Project(code=FIXED) as p:
            for name in (".env", ".env.local", "deploy/server.pem", "certs/Site.KEY", ".netrc"):
                p.write(name, SECRET + "\n")
            with forbidden_opens(p.dir) as opened:
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
            for name in (".env", ".env.local", "deploy/server.pem", "certs/Site.KEY", ".netrc"):
                self.assertTrue(pins[name].get("credential"), name)
                self.assertNotIn("sha256", pins[name], name)
                self.assertNotIn("sha256", pins_of(v)[name], name)
            self.assertNotIn("credential", pins["tipsplit.py"])

    def test_an_untouched_credential_file_confirms_in_the_same_checkout(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            with forbidden_opens(p.dir) as opened:
                out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            self.assertNotIn("local-only", out.message)
            self.assertIn("(1 local credential file not compared: .env)", out.message)

    def test_a_credential_pin_is_never_reported_as_a_bare_match(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            out = verify.verify_receipt(v.receipt_path)
            self.assertEqual(out.verdict, "HASHES MATCH", out.message)
            self.assertTrue(out.message.startswith(f"HASHES MATCH {v.receipt_hash} (1 local credential file not "
                                                   "compared: .env)."), out.message)

    def test_a_credential_file_whose_size_changed_after_the_receipt_is_a_mismatch(self):
        # Its mtime, inode and change time differ in any copy and are ignored; a different size is a change.
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            with open(p.path(".env"), "a") as handle:
                handle.write("MORE=1\n")
            with forbidden_opens(p.dir) as opened:
                out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("Files changed: .env.", out.message)
            self.assertNotIn("necro-test-value-8231", out.message)

    def test_a_credential_change_that_breaks_a_contract_is_caught_by_the_rerun(self):
        with Project(code=FIXED) as p:
            p.write(".env", "MODE=good\n")
            p.write("contracts/test_refuses_zero.py",
                    "import unittest\nclass T(unittest.TestCase):\n    def test_mode(self):\n"
                    "        self.assertEqual(open('.env').read(), 'MODE=good\\n')\n")
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            p.write(".env", "MODE=evil\n")  # the same size, so only the re-run can tell
            out = verify.verify_receipt(v.receipt_path, run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("C3 (pass then, fail now)", out.message)

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
            with forbidden_opens(p.dir) as opened:
                out = verify.verify_receipt(forged, run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn(".env", out.message)

    def test_a_contract_cannot_freeze_a_credential_file(self):
        with Project(code=FIXED) as p:
            p.write("contracts/.env", SECRET + "\n")
            p.write("necro.toml", p.read("necro.toml").replace('files = ["contracts/test_fair.py"]',
                                                                'files = ["contracts/**"]'))
            with forbidden_opens(p.dir) as opened, self.assertRaises(spec.SpecError) as caught:
                lock.freeze(p.dir)
            self.assertEqual(opened, [])
            self.assertIn("contracts/.env", str(caught.exception))
            self.assertIn("credential", str(caught.exception))
            self.assertIsNone(lock.read_lock(p.dir))

    def test_verify_will_not_open_a_credential_file_given_as_the_receipt(self):
        with Project(code=FIXED) as p:
            p.write(".env", SECRET + "\n")
            lock.freeze(p.dir)
            with forbidden_opens(p.dir) as opened:
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

    def test_a_shadow_module_linked_to_a_credential_file_is_added(self):
        g = GitWork(FIXED)
        try:
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            copy = g.work / "tarball"
            shutil.copytree(g.author, copy)
            (copy / ".env").write_text("raise SystemExit(0)\n")
            os.symlink(".env", copy / "unittest.py")
            out = verify.verify_receipt(copy / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(out.verdict, "MISMATCH", out.message)
            self.assertIn("Files added: unittest.py.", out.message)
        finally:
            g.cleanup()

    def test_a_committed_credential_named_file_confirms_in_a_fresh_clone(self):
        # A committed .npmrc (registry settings, no secret) gets a new modification time in every clone.
        g = GitWork(FIXED)
        try:
            (g.author / ".npmrc").write_text("engine-strict=true\n")
            lock.freeze(g.author)
            v = gate.evaluate(g.author, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            clone = g.commit_and_clone()
            with forbidden_opens(clone) as opened:
                out = verify.verify_receipt(clone / v.receipt_path.relative_to(g.author), run=True)
            self.assertEqual(opened, [])
            self.assertEqual(out.verdict, "CONFIRMED", out.message)
            self.assertIn("(1 local credential file not compared: .npmrc)", out.message)
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
            with forbidden_opens(clone) as opened:
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


class VerifyUsesTheTrustedLockForLinks(unittest.TestCase):
    """A receipt is not signed, so a path inside it cannot widen what links may point to: the allow-set comes
    from the receipt's own pins and the trusted lock, never from the receipt's copy of the lock's spec path."""

    def test_a_forged_spec_path_in_the_receipt_does_not_widen_the_allowed_links(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=120)
            self.assertTrue(v.ok, v.problems)
            rc = json.loads(v.receipt_path.read_text())
            rc["lock"].setdefault("spec", {})["path"] = "forged_allowed.py"
            rc["receiptHash"] = receipts.receipt_hash(rc)
            v.receipt_path.write_text(json.dumps(rc))
            seen = {}
            real = verify.escaping_links

            def spy(root, rels, pinned):
                seen["pinned"] = set(pinned)
                return real(root, rels, pinned)

            with mock.patch.object(verify, "escaping_links", side_effect=spy):
                verify.verify_receipt(v.receipt_path, run=False)
            self.assertIn("pinned", seen)
            self.assertNotIn("forged_allowed.py", seen["pinned"])
            self.assertTrue(lock.frozen_paths(lock.read_lock(p.dir)) <= seen["pinned"])
