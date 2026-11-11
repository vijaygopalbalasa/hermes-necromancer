"""Name the failing test (Nov 11, a real-model run): with one zero-config contract, "C1 failed" says nothing, and a
small model argued the locked test was wrong. The note to the model names each failing test as a requirement;
the person's stamp and the Sent back line name the test, or count them."""

import unittest

from helpers import FIXED, HALF_FIX

from necro import gate
from necro.runner import Result
from test_plugin import FakeCtx, load_plugin
from test_zero_config import Folder, run

FILM_TESTS = '''import unittest

from tipsplit import split_bill


class SplitBill(unittest.TestCase):
    def test_shares_add_up_to_the_total(self):
        for total, people in [(1000, 3), (1001, 4), (7, 5)]:
            self.assertEqual(sum(split_bill(total, people)), total)

    def test_no_share_differs_by_more_than_one_cent(self):
        shares = split_bill(1001, 4)
        self.assertLessEqual(max(shares) - min(shares), 1)

    def test_zero_people_is_refused(self):
        with self.assertRaises(ValueError):
            split_bill(100, 0)

    def test_a_negative_total_is_refused(self):
        with self.assertRaises(ValueError):
            split_bill(-500, 2)
'''

UNITTEST_ONE = """F...
======================================================================
FAIL: test_a_negative_total_is_refused (test_tipsplit.SplitBill.test_a_negative_total_is_refused)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_tipsplit.py", line 21, in test_a_negative_total_is_refused
    with self.assertRaises(ValueError):
AssertionError: ValueError not raised

----------------------------------------------------------------------
Ran 4 tests in 0.001s

FAILED (failures=1)
"""
PYTEST_ONE = "..F.\nFAILED tests/test_calc.py::test_adds - assert -1 == 5\n1 failed, 3 passed in 0.12s\n"
UNITTEST_TWO = UNITTEST_ONE.replace("FAILED (failures=1)", "FAILED (failures=2)").replace(
    "======================================================================\nFAIL: test_a",
    "======================================================================\nFAIL: test_shares_add_up_to_the_total (x.X.y)\n"
    "AssertionError: 999 != 1000\n======================================================================\nFAIL: test_a")


def verdict(*results):
    return gate.Verdict(False, root=None, results=list(results))


def failed(output, says="Your tests pass", cid="C1"):
    return Result(cid, says, "fail", 1, 10, output)


class Describer(unittest.TestCase):
    def test_one_unittest_name(self):
        self.assertEqual(gate.describe_failures(verdict(failed(UNITTEST_ONE))), "test_a_negative_total_is_refused")
        self.assertEqual(gate.sent_back_line([gate.describe_failures(verdict(failed(UNITTEST_ONE)))]),
                         "Sent back once: test_a_negative_total_is_refused failed after Hermes said done.")

    def test_one_pytest_name(self):
        self.assertEqual(gate.describe_failures(verdict(failed(PYTEST_ONE))), "test_adds")

    def test_counts_when_more_than_one_test_fails(self):
        self.assertEqual(gate.describe_failures(verdict(failed(UNITTEST_TWO))), "2 of 4 tests")

    def test_the_contract_when_the_output_says_nothing(self):
        self.assertEqual(gate.describe_failures(verdict(failed("boom", says="Shares add up"))), "Shares add up")
        self.assertEqual(gate.describe_failures(verdict(failed("boom", says=""))), "C1")
        self.assertEqual(gate.describe_failures(verdict(failed("boom"))), "the tests")

    def test_two_and_three_refusals(self):
        self.assertEqual(gate.sent_back_line(["2 of 4 tests", "test_x"]),
                         "Sent back 2 times: 2 of 4 tests failed, then test_x still failed.")
        self.assertEqual(gate.sent_back_line(["test_x"] * 3), "Sent back 3 times: test_x still failed.")

    def test_a_long_name_is_cut_and_the_line_stays_short(self):
        long = "test_" + "very_" * 30 + "long"
        out = gate.describe_failures(verdict(failed(UNITTEST_ONE.replace("test_a_negative_total_is_refused", long))))
        self.assertTrue(out.endswith("…"), out)
        self.assertLessEqual(len(gate.sent_back_line([out, "2 of 4 tests", out])), 125)

    def test_the_note_names_each_failing_requirement(self):
        note = gate.continue_message(gate.Verdict(False, root="/p", results=[failed(UNITTEST_ONE)]))
        self.assertIn("- Failing requirement: a negative total is refused (test_a_negative_total_is_refused): "
                      "AssertionError: ValueError not raised.", note)
        self.assertIn("Each failing test is a requirement the person wrote down and locked. Change the code under "
                      "test until it passes; do not argue that a locked test is wrong.", note)
        self.assertIn("FAILED (failures=1)", note)  # the output tail is still there


class OneContractStamps(unittest.TestCase):
    """A zero-config lock has one contract: the person's stamp names the test or counts them."""

    def project(self, code):
        f = Folder({"tipsplit.py": code, "tests/test_tipsplit.py": FILM_TESTS})
        text, status = run(f, "freeze")
        self.assertEqual(status, 0, text)
        ctx = FakeCtx(f.dir)
        load_plugin().register(ctx)
        ctx.commands["necro"]("freeze")  # this context's own approval
        return f, ctx

    def fire(self, ctx, f, attempt=0):
        return ctx.hooks["pre_verify"](session_id="s", platform="cli", model="m", coding=True, attempt=attempt,
                                       final_response="Done", changed_paths=[str(f.path("tipsplit.py"))])

    def stamp(self, ctx, text):
        return ctx.hooks["transform_llm_output"](response_text=text, session_id="s", model="m", platform="cli")

    def test_sent_back_then_a_pass(self):
        f, ctx = self.project(HALF_FIX)  # only the negative-total test fails
        with f:
            note = self.fire(ctx, f)["message"]
            self.assertIn("Failing requirement: a negative total is refused (test_a_negative_total_is_refused)", note)
            f.write("tipsplit.py", FIXED)
            self.assertIsNone(self.fire(ctx, f, 1))
            out = self.stamp(ctx, "Fixed.")
            self.assertIn("Sent back once: test_a_negative_total_is_refused failed after Hermes said done. Contract "
                          "Necromancer: all 4 tests pass. Receipt ", out)

    def test_out_of_nudges_never_says_c1_fails_your_tests_pass(self):
        f, ctx = self.project(HALF_FIX)
        with f:
            for attempt in range(3):
                self.fire(ctx, f, attempt)
            out = self.stamp(ctx, "The locked test is invalid.")
            self.assertTrue(out.startswith("Not done. Sent back 3 times: test_a_negative_total_is_refused still "
                                           "failed. Contract Necromancer: 1 of 4 tests fails, so no receipt was "
                                           "written."), out)
            self.assertNotIn("Your tests pass", out)


if __name__ == "__main__":
    unittest.main()
