"""Receipts: what they pin, their hash, and re-verifying them later."""

import json
import subprocess
import unittest

from helpers import BUGGY, FIXED, HALF_FIX, Project

from necro import lock, receipts, runner, verify


def passing_receipt(p, **kw):
    lk = lock.freeze(p.dir)
    results = runner.run_contracts(p.dir, lk)
    assert runner.all_pass(results), [(r.id, r.status, r.output_tail) for r in results]
    rc = receipts.build_receipt(p.dir, lk, results, trigger=kw.get("trigger", "test"))
    path = receipts.write_receipt(p.dir, rc)
    return lk, rc, path


class Build(unittest.TestCase):
    def test_a_receipt_pins_contracts_sources_and_results(self):
        with Project(code=FIXED) as p:
            lk, rc, path = passing_receipt(p)
            self.assertEqual(rc["lock"]["sha256"], lock.lock_hash(lk))
            self.assertEqual([c["id"] for c in rc["contracts"]], ["C1", "C2", "C3"])
            self.assertEqual({c["result"] for c in rc["contracts"]}, {"pass"})
            src = {s["path"] for s in rc["sources"]}
            self.assertIn("tipsplit.py", src)
            self.assertNotIn("contracts/test_fair.py", src)  # pinned under contracts, not twice
            self.assertNotIn("necro.toml", src)
            self.assertFalse(any(s.startswith(".necro/") for s in src))
            self.assertEqual(path.name, rc["receiptHash"] + ".json")
            self.assertEqual(json.loads(path.read_text()), rc)

    def test_the_hash_ignores_created_at_but_covers_everything_else(self):
        with Project(code=FIXED) as p:
            _, rc, _ = passing_receipt(p)
            again = dict(rc, createdAt="1999-01-01T00:00:00Z")
            self.assertEqual(receipts.receipt_hash(again), rc["receiptHash"])
            changed = json.loads(json.dumps(rc))
            changed["contracts"][0]["result"] = "fail"
            self.assertNotEqual(receipts.receipt_hash(changed), rc["receiptHash"])

    def test_python_caches_are_not_sources(self):
        with Project(code=FIXED) as p:
            p.write("__pycache__/tipsplit.cpython-314.pyc", "x")
            p.write(".pytest_cache/v/cache", "x")
            _, rc, _ = passing_receipt(p)
            self.assertEqual({s["path"] for s in rc["sources"]}, {"tipsplit.py"})

    def test_in_a_git_repo_files_git_ignores_are_pinned_and_marked(self):
        # Integrity review, Nov 10: ignored files can change a run, so they are pinned and marked "ignored";
        # a fresh clone may lack them, and verify's re-run decides.
        with Project(code=FIXED) as p:
            subprocess.run(["git", "init", "-q"], cwd=p.dir, check=True)
            p.write(".gitignore", "build/\n")
            p.write("build/out.txt", "generated")
            lk = lock.freeze(p.dir)
            pins = {e["path"]: e for e in receipts.pin(p.dir, receipts.source_files(p.dir, lk))}
            self.assertNotIn("ignored", pins["tipsplit.py"])
            self.assertNotIn("ignored", pins[".gitignore"])
            self.assertTrue(pins["build/out.txt"].get("ignored"))
            self.assertFalse(any(s.startswith(".git/") for s in pins))

    def test_declared_sources_narrow_what_is_pinned(self):
        with Project(code=FIXED) as p:
            p.write("notes.txt", "scratch")
            p.write("necro.toml", 'sources = ["*.py"]\n' + p.read("necro.toml"))
            _, rc, _ = passing_receipt(p)
            self.assertEqual({s["path"] for s in rc["sources"]}, {"tipsplit.py"})

    def test_list_receipts_newest_first(self):
        with Project(code=FIXED) as p:
            _, first, _ = passing_receipt(p)
            p.write("tipsplit.py", FIXED + "\n# touched\n")
            _, second, _ = passing_receipt(p)
            listed = receipts.list_receipts(p.dir)
            self.assertEqual(listed[0].stem, second["receiptHash"])
            self.assertEqual(listed[1].stem, first["receiptHash"])


class Verify(unittest.TestCase):
    def test_confirmed_when_nothing_changed(self):
        with Project(code=FIXED) as p:
            _, rc, path = passing_receipt(p)
            v = verify.verify_receipt(path, run=True)
            self.assertEqual(v.verdict, "CONFIRMED", v.message)
            self.assertIn(rc["receiptHash"], v.message)

    def test_mismatch_when_the_receipt_was_altered(self):
        with Project(code=FIXED) as p:
            _, rc, path = passing_receipt(p)
            rc["sources"][0]["sha256"] = "0" * 64
            path.write_text(json.dumps(rc))
            v = verify.verify_receipt(path)
            self.assertEqual(v.verdict, "MISMATCH")
            self.assertIn("altered", v.message)

    def test_mismatch_names_a_changed_source_file(self):
        with Project(code=FIXED) as p:
            _, _, path = passing_receipt(p)
            p.write("tipsplit.py", HALF_FIX)
            v = verify.verify_receipt(path)
            self.assertEqual(v.verdict, "MISMATCH")
            self.assertIn("tipsplit.py", v.message)

    def test_mismatch_names_an_added_source_file(self):
        with Project(code=FIXED) as p:
            _, _, path = passing_receipt(p)
            p.write("sitecustomize.py", "x = 1\n")
            v = verify.verify_receipt(path)
            self.assertEqual(v.verdict, "MISMATCH")
            self.assertIn("sitecustomize.py", v.message)

    def test_mismatch_when_a_contract_file_changed(self):
        with Project(code=FIXED) as p:
            _, _, path = passing_receipt(p)
            p.write("contracts/test_fair.py", "import unittest\n")
            v = verify.verify_receipt(path)
            self.assertEqual(v.verdict, "MISMATCH")
            self.assertIn("contracts/test_fair.py", v.message)

    def test_mismatch_when_a_rerun_gives_a_different_result(self):
        with Project(code=FIXED) as p:
            p.write("flaky_input.txt", "ok")
            p.write("necro.toml", 'sources = ["tipsplit.py"]\n' + p.read("necro.toml"))
            p.write(
                "contracts/test_refuses_zero.py",
                "import unittest, pathlib\n"
                "class T(unittest.TestCase):\n"
                "    def test_input(self):\n"
                "        self.assertEqual(pathlib.Path('flaky_input.txt').read_text(), 'ok')\n",
            )
            _, _, path = passing_receipt(p)
            p.write("flaky_input.txt", "changed outside the pinned sources")
            v = verify.verify_receipt(path, run=True)
            self.assertEqual(v.verdict, "MISMATCH")
            self.assertIn("C3", v.message)

    def test_superseded_after_a_person_freezes_again(self):
        with Project(code=FIXED) as p:
            _, _, path = passing_receipt(p)
            p.write("necro.toml", p.read("necro.toml").replace("Zero people is refused", "Zero people is refused, always"))
            lock.freeze(p.dir)
            v = verify.verify_receipt(path)
            self.assertEqual(v.verdict, "SUPERSEDED", v.message)

    def test_verify_works_from_another_working_directory(self):
        with Project(code=FIXED) as p, Project() as elsewhere:
            _, _, path = passing_receipt(p)
            v = verify.verify_receipt(str(path), cwd=elsewhere.dir, run=True)
            self.assertEqual(v.verdict, "CONFIRMED", v.message)

    def test_a_receipt_for_failing_work_cannot_be_built(self):
        with Project(code=BUGGY) as p:
            lk = lock.freeze(p.dir)
            results = runner.run_contracts(p.dir, lk)
            with self.assertRaises(ValueError):
                receipts.build_receipt(p.dir, lk, results, trigger="test")


if __name__ == "__main__":
    unittest.main()
