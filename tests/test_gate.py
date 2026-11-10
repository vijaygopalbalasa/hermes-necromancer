"""The gate decision, the message that keeps the agent going, and the stamp on the final reply."""

import unittest

from helpers import BUGGY, FIXED, HALF_FIX, Project

from necro import gate, lock, receipts


class Evaluate(unittest.TestCase):
    def test_failing_work_is_refused_and_names_the_failing_contract(self):
        with Project(code=HALF_FIX) as p:
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertFalse(v.ok)
            self.assertEqual(v.failing_ids(), ["C2"])
            self.assertIsNone(v.receipt_path)
            self.assertEqual(receipts.list_receipts(p.dir), [])

    def test_passing_work_writes_a_receipt(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertTrue(v.ok, v.problems)
            self.assertTrue(v.receipt_path.is_file())
            self.assertEqual(receipts.list_receipts(p.dir), [v.receipt_path])

    def test_the_same_state_gives_the_same_receipt(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            a = gate.evaluate(p.dir, budget_s=60)
            b = gate.evaluate(p.dir, budget_s=60)
            self.assertEqual(a.receipt_hash, b.receipt_hash)
            self.assertEqual(len(receipts.list_receipts(p.dir)), 1)

    def test_an_unapproved_lock_is_refused_without_running_anything(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60, approved=set())
            self.assertFalse(v.ok)
            self.assertEqual(v.results, [])
            self.assertTrue(any("not approved" in x for x in v.problems), v.problems)

    def test_an_approved_lock_is_accepted(self):
        with Project(code=FIXED) as p:
            lk = lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60, approved={lock.lock_hash(lk)})
            self.assertTrue(v.ok, v.problems)

    def test_an_edited_spec_is_refused_until_a_person_freezes_again(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            p.write("necro.toml", p.read("necro.toml").replace("Zero people", "Nobody"))
            v = gate.evaluate(p.dir, budget_s=60)
            self.assertFalse(v.ok)
            self.assertIn("necro.toml changed since freeze", v.problems)

    def test_a_cached_result_is_reused_while_nothing_changed(self):
        with Project(code=BUGGY) as p:
            lock.freeze(p.dir)
            cache = {}
            first = gate.evaluate(p.dir, budget_s=60, cache=cache)
            second = gate.evaluate(p.dir, budget_s=60, cache=cache)
            self.assertTrue(second.cached)
            self.assertEqual(second.failing_ids(), first.failing_ids())

    def test_the_cache_is_dropped_when_a_source_changes(self):
        with Project(code=BUGGY) as p:
            lock.freeze(p.dir)
            cache = {}
            gate.evaluate(p.dir, budget_s=60, cache=cache)
            p.write("tipsplit.py", FIXED)
            v = gate.evaluate(p.dir, budget_s=60, cache=cache)
            self.assertFalse(v.cached)
            self.assertTrue(v.ok, v.problems)

    def test_no_lock_means_no_verdict(self):
        with Project(code=FIXED) as p:
            self.assertIsNone(lock.find_root(p.dir))
            with self.assertRaises(FileNotFoundError):
                gate.evaluate(p.dir, budget_s=60)


class Messages(unittest.TestCase):
    def test_the_continue_message_names_failures_and_says_not_to_edit_contracts(self):
        with Project(code=BUGGY) as p:
            lock.freeze(p.dir)
            msg = gate.continue_message(gate.evaluate(p.dir, budget_s=60))
            self.assertIn("C1", msg)
            self.assertIn("Shares add up to the total", msg)
            self.assertIn("not the contracts", msg)
            self.assertIn("necro_check", msg)
            self.assertIn("1 of 3", msg)
            self.assertNotIn("C2", msg)  # C2 and C3 pass with equal shares; only failures are listed

    def test_the_continue_message_explains_tampering(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            p.write("contracts/test_fair.py", "import unittest\n")
            msg = gate.continue_message(gate.evaluate(p.dir, budget_s=60))
            self.assertIn("C2", msg)
            self.assertIn("contracts/test_fair.py", msg)
            self.assertIn("changed since freeze", msg)

    def test_stamp_on_failure_leads_with_not_done(self):
        with Project(code=HALF_FIX) as p:
            lock.freeze(p.dir)
            out = gate.stamp(gate.evaluate(p.dir, budget_s=60), "All done, the bill splits correctly.")
            self.assertTrue(out.startswith("Not done."), out)
            self.assertIn("C2", out)
            self.assertIn("All done, the bill splits correctly.", out)
            self.assertNotIn("\u2014", out)  # no em dash

    def test_stamp_on_success_appends_the_receipt(self):
        with Project(code=FIXED) as p:
            lock.freeze(p.dir)
            v = gate.evaluate(p.dir, budget_s=60)
            out = gate.stamp(v, "Fixed it.")
            self.assertTrue(out.startswith("Fixed it."))
            self.assertIn(v.receipt_hash[:12], out)
            self.assertIn("necro verify", out)
            self.assertNotIn("\u2014", out)  # no em dash


class DoneClaims(unittest.TestCase):
    def test_recognises_done_claims(self):
        for text in [
            "Done! The shares add up now.",
            "I've fixed the rounding bug.",
            "The task is complete.",
            "All tests pass.",
            "Implemented the change; it's ready for review.",
            "Everything is working now.",
        ]:
            self.assertTrue(gate.looks_like_done(text), text)

    def test_ignores_ordinary_answers(self):
        for text in [
            "The split_bill function divides the total by the number of people.",
            "Which file should I look at?",
            "Here is how the tests are organised.",
            "It is not done yet: C2 still fails.",
        ]:
            self.assertFalse(gate.looks_like_done(text), text)


if __name__ == "__main__":
    unittest.main()
