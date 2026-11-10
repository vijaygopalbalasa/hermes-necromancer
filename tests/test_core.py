"""Canonical hashing, the contract spec, freezing, drift and running contracts."""

import json
import os
import time
import unittest
from pathlib import Path

from helpers import FIXED, PY, Project, spec_text

from necro import canon, lock, runner, spec


class Canon(unittest.TestCase):
    def test_canonical_json_sorts_keys_and_drops_whitespace(self):
        self.assertEqual(canon.canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}), '{"a":[2,{"c":4,"d":3}],"b":1}')

    def test_canonical_json_keeps_unicode(self):
        self.assertEqual(canon.canonical_json({"k": "é"}), '{"k":"é"}')

    def test_sha256_known_vector(self):
        self.assertEqual(canon.sha256_text("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_hash_file_is_none_for_a_missing_file(self):
        self.assertIsNone(canon.hash_file(Path("/nonexistent/necro/file")))


class Spec(unittest.TestCase):
    def test_reads_three_contracts(self):
        with Project() as p:
            s = spec.load_spec(p.dir)
            self.assertEqual([c.id for c in s.contracts], ["C1", "C2", "C3"])
            self.assertEqual(s.contracts[0].files, ["contracts/test_adds_up.py"])
            self.assertIn("unittest", s.contracts[0].run)

    def test_refuses_a_duplicate_id(self):
        with Project() as p:
            p.write("necro.toml", spec_text() + '\n[[contract]]\nid = "C1"\nsays = "x"\nrun = "true"\nfiles = ["tipsplit.py"]\n')
            with self.assertRaisesRegex(spec.SpecError, "C1"):
                spec.load_spec(p.dir)

    def test_refuses_a_contract_without_files(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "C1"\nsays = "x"\nrun = "true"\n')
            with self.assertRaisesRegex(spec.SpecError, "files"):
                spec.load_spec(p.dir)

    def test_refuses_a_file_outside_the_project(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "C1"\nsays = "x"\nrun = "true"\nfiles = ["../elsewhere.py"]\n')
            with self.assertRaisesRegex(spec.SpecError, "outside"):
                spec.load_spec(p.dir)

    def test_refuses_a_pattern_that_matches_nothing(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "C1"\nsays = "x"\nrun = "true"\nfiles = ["contracts/nope_*.py"]\n')
            with self.assertRaisesRegex(spec.SpecError, "matches no file"):
                spec.load_spec(p.dir)

    def test_refuses_a_missing_run(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "C1"\nsays = "x"\nfiles = ["tipsplit.py"]\n')
            with self.assertRaisesRegex(spec.SpecError, "run"):
                spec.load_spec(p.dir)

    def test_refuses_bad_toml(self):
        with Project() as p:
            p.write("necro.toml", "[[contract]\n")
            with self.assertRaises(spec.SpecError):
                spec.load_spec(p.dir)

    def test_a_glob_pattern_expands_to_files(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "ALL"\nsays = "x"\nrun = "true"\nfiles = ["contracts/test_*.py"]\n')
            s = spec.load_spec(p.dir)
            self.assertEqual(
                spec.expand(p.dir, s.contracts[0].files),
                ["contracts/test_adds_up.py", "contracts/test_fair.py", "contracts/test_refuses_zero.py"],
            )


class Freeze(unittest.TestCase):
    def test_freeze_writes_a_lock_with_file_hashes(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            on_disk = json.loads(p.read(".necro/lock.json"))
            self.assertEqual(on_disk, lk)
            c1 = lk["contracts"][0]
            self.assertEqual(c1["id"], "C1")
            self.assertEqual(c1["files"][0]["path"], "contracts/test_adds_up.py")
            self.assertEqual(c1["files"][0]["sha256"], canon.hash_file(p.path("contracts/test_adds_up.py")))
            self.assertEqual(lk["spec"]["sha256"], canon.hash_file(p.path("necro.toml")))

    def test_lock_hash_ignores_the_freeze_time(self):
        with Project() as p:
            a = lock.freeze(p.dir, now="2026-01-01T00:00:00Z")
            b = lock.freeze(p.dir, now="2026-02-02T00:00:00Z")
            self.assertNotEqual(a["frozenAt"], b["frozenAt"])
            self.assertEqual(lock.lock_hash(a), lock.lock_hash(b))
            p.write("contracts/test_fair.py", p.read("contracts/test_fair.py") + "\n# changed\n")
            self.assertNotEqual(lock.lock_hash(lock.freeze(p.dir)), lock.lock_hash(a))

    def test_find_root_walks_up_from_a_nested_path(self):
        with Project() as p:
            lock.freeze(p.dir)
            self.assertEqual(lock.find_root(p.path("contracts/test_fair.py")), p.dir)
            self.assertEqual(lock.find_root(p.dir / "contracts"), p.dir)

    def test_find_root_is_none_without_a_lock(self):
        with Project() as p:
            self.assertIsNone(lock.find_root(p.dir))

    def test_frozen_paths_include_contract_files_and_the_spec(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            self.assertEqual(
                lock.frozen_paths(lk),
                {"necro.toml", "contracts/test_adds_up.py", "contracts/test_fair.py", "contracts/test_refuses_zero.py"},
            )


class Drift(unittest.TestCase):
    def test_no_drift_right_after_freeze(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            d = lock.drift(p.dir, lk)
            self.assertEqual(d.spec, [])
            self.assertEqual(d.contracts, {})

    def test_a_changed_contract_file_is_drift_for_that_contract_only(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            p.write("contracts/test_fair.py", "import unittest\n")
            d = lock.drift(p.dir, lk)
            self.assertEqual(list(d.contracts), ["C2"])
            self.assertIn("changed since freeze: contracts/test_fair.py", d.contracts["C2"])

    def test_a_deleted_contract_file_is_drift(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            p.path("contracts/test_adds_up.py").unlink()
            d = lock.drift(p.dir, lk)
            self.assertIn("missing: contracts/test_adds_up.py", d.contracts["C1"])

    def test_a_new_file_matching_a_frozen_pattern_is_drift(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "ALL"\nsays = "x"\nrun = "true"\nfiles = ["contracts/test_*.py"]\n')
            lk = lock.freeze(p.dir)
            p.write("contracts/test_zzz.py", "x = 1\n")
            d = lock.drift(p.dir, lk)
            self.assertIn("added after freeze: contracts/test_zzz.py", d.contracts["ALL"])

    def test_an_edited_spec_is_drift(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            p.write("necro.toml", p.read("necro.toml").replace("C3", "C9"))
            d = lock.drift(p.dir, lk)
            self.assertEqual(d.spec, ["necro.toml changed since freeze"])


class Run(unittest.TestCase):
    def test_buggy_code_fails_c1_and_c2_and_passes_c3(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            res = {r.id: r for r in runner.run_contracts(p.dir, lk)}
            self.assertEqual(res["C1"].status, "fail")
            self.assertEqual(res["C2"].status, "pass")  # equal shares differ by 0
            self.assertEqual(res["C3"].status, "pass")
            self.assertIn("AssertionError", res["C1"].output_tail)
            self.assertNotEqual(res["C1"].exit_code, 0)

    def test_fixed_code_passes_everything(self):
        with Project(code=FIXED) as p:
            lk = lock.freeze(p.dir)
            self.assertEqual([r.status for r in runner.run_contracts(p.dir, lk)], ["pass", "pass", "pass"])

    def test_a_tampered_contract_is_not_run_and_counts_as_failing(self):
        with Project(code=FIXED) as p:
            lk = lock.freeze(p.dir)
            p.write("contracts/test_fair.py", "raise SystemExit(0)\n")
            res = {r.id: r for r in runner.run_contracts(p.dir, lk)}
            self.assertEqual(res["C2"].status, "tampered")
            self.assertIsNone(res["C2"].exit_code)
            self.assertEqual(res["C1"].status, "pass")

    def test_the_locked_command_runs_even_if_the_spec_was_edited(self):
        with Project() as p:
            lk = lock.freeze(p.dir)
            p.write("necro.toml", p.read("necro.toml").replace("-m unittest -q contracts.test_adds_up", "-c pass"))
            res = {r.id: r for r in runner.run_contracts(p.dir, lk)}
            self.assertEqual(res["C1"].status, "fail")

    def test_a_slow_contract_times_out(self):
        with Project() as p:
            p.write("slow.py", "import time\ntime.sleep(30)\n")
            p.write("necro.toml", f'[[contract]]\nid = "S"\nsays = "x"\nrun = "{PY} slow.py"\nfiles = ["slow.py"]\ntimeout = 1\n')
            lk = lock.freeze(p.dir)
            t0 = time.monotonic()
            res = runner.run_contracts(p.dir, lk)
            self.assertLess(time.monotonic() - t0, 10)
            self.assertEqual(res[0].status, "timeout")

    def test_when_the_budget_runs_out_later_contracts_are_skipped_not_passed(self):
        with Project() as p:
            p.write("slow.py", "import time\ntime.sleep(30)\n")
            p.write(
                "necro.toml",
                f'[[contract]]\nid = "S"\nsays = "x"\nrun = "{PY} slow.py"\nfiles = ["slow.py"]\n'
                f'[[contract]]\nid = "T"\nsays = "y"\nrun = "{PY} -c pass"\nfiles = ["tipsplit.py"]\n',
            )
            lk = lock.freeze(p.dir)
            res = {r.id: r for r in runner.run_contracts(p.dir, lk, budget_s=1.5)}
            self.assertEqual(res["S"].status, "unfinished")  # cut by the budget, not by its own limit
            self.assertEqual(res["T"].status, "skipped")
            self.assertFalse(runner.all_pass(res.values()))

    def test_contracts_get_a_minimal_environment(self):
        with Project() as p:
            p.write("envcheck.py", "import os, sys\nsys.exit(1 if os.environ.get('NECRO_TEST_SECRET') else 0)\n")
            p.write("necro.toml", f'[[contract]]\nid = "E"\nsays = "x"\nrun = "{PY} envcheck.py"\nfiles = ["envcheck.py"]\n')
            lk = lock.freeze(p.dir)
            os.environ["NECRO_TEST_SECRET"] = "s3cret"
            try:
                self.assertEqual(runner.run_contracts(p.dir, lk)[0].status, "pass")
            finally:
                del os.environ["NECRO_TEST_SECRET"]

    def test_a_declared_variable_is_passed_through(self):
        with Project() as p:
            p.write("envcheck.py", "import os, sys\nsys.exit(0 if os.environ.get('NECRO_TEST_DB') == 'x' else 1)\n")
            p.write(
                "necro.toml",
                f'[[contract]]\nid = "E"\nsays = "x"\nrun = "{PY} envcheck.py"\nfiles = ["envcheck.py"]\nenv = ["NECRO_TEST_DB"]\n',
            )
            lk = lock.freeze(p.dir)
            os.environ["NECRO_TEST_DB"] = "x"
            try:
                self.assertEqual(runner.run_contracts(p.dir, lk)[0].status, "pass")
            finally:
                del os.environ["NECRO_TEST_DB"]

    def test_a_command_that_cannot_start_is_an_error_not_a_pass(self):
        with Project() as p:
            p.write("necro.toml", '[[contract]]\nid = "X"\nsays = "x"\nrun = "no-such-program-necro-xyz"\nfiles = ["tipsplit.py"]\n')
            lk = lock.freeze(p.dir)
            r = runner.run_contracts(p.dir, lk)[0]
            self.assertEqual(r.status, "error")
            self.assertIn("could not start", r.output_tail)


if __name__ == "__main__":
    unittest.main()
