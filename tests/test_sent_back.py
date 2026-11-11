"""The person sees each refusal (Nov 11 judge review): Hermes hides the pre_verify note, so the final reply's
stamp says how many times Hermes was sent back and for what, then the usual pass line or Not done."""

import unittest

from helpers import BUGGY, FIXED, HALF_FIX, Project

from test_plugin import plugin_for


def fire(ctx, p, session="s1", attempt=0):
    return ctx.hooks["pre_verify"](session_id=session, platform="cli", model="m", coding=True, attempt=attempt,
                                   final_response="Done!", changed_paths=[str(p.path("tipsplit.py"))])


def transform(ctx, text, session="s1"):
    return ctx.hooks["transform_llm_output"](response_text=text, session_id=session, model="m", platform="cli")


class SentBack(unittest.TestCase):
    def test_one_refusal_then_a_pass(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            self.assertEqual(fire(ctx, p)["action"], "continue")
            p.write("tipsplit.py", FIXED)
            self.assertIsNone(fire(ctx, p, attempt=1))
            out = transform(ctx, "Fixed.")
            self.assertIn("Fixed.\n\nSent back once: C2 failed after Hermes said done. Contract Necromancer: all 3 "
                          "frozen acceptance contracts pass. Receipt", out)

    def test_two_refusals_then_a_pass(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            fire(ctx, p)
            fire(ctx, p, attempt=1)
            p.write("tipsplit.py", FIXED)
            fire(ctx, p, attempt=2)
            self.assertIn("Sent back 2 times: C2 failed, then C2 again. Contract Necromancer: all 3",
                          transform(ctx, "Fixed."))

    def test_a_refusal_then_out_of_nudges_is_not_done(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            fire(ctx, p)
            out = transform(ctx, "The fix is complete.")
            self.assertTrue(out.startswith("Not done. Sent back once: C2 failed after Hermes said done. Contract "
                                           "Necromancer: 1 of 3"), out)

    def test_different_failures_are_named_in_order(self):
        with Project(code=BUGGY) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            fire(ctx, p)
            p.write("tipsplit.py", HALF_FIX)
            fire(ctx, p, attempt=1)
            p.write("tipsplit.py", FIXED)
            fire(ctx, p, attempt=2)
            self.assertIn("Sent back 2 times: C1 failed, then C2.", transform(ctx, "Fixed."))

    def test_a_new_turn_starts_clean(self):
        with Project(code=HALF_FIX) as p:
            ctx = plugin_for(p)
            ctx.commands["necro"]("freeze")
            fire(ctx, p)
            ctx.hooks["pre_llm_call"](session_id="s1")  # that turn ended without a stamp; a new one starts
            p.write("tipsplit.py", FIXED)
            fire(ctx, p)
            out = transform(ctx, "Fixed.")
            self.assertIn("Receipt", out)
            self.assertNotIn("Sent back", out)



class SharedFrozenList(unittest.TestCase):
    """Judge review item B: contracts that freeze the same files list them once."""

    def test_a_shared_frozen_list_is_printed_once(self):
        from helpers import spec_text
        shared = spec_text().replace('files = ["contracts/test_adds_up.py"]', 'files = ["contracts/**"]') \
            .replace('files = ["contracts/test_fair.py"]', 'files = ["contracts/**"]') \
            .replace('files = ["contracts/test_refuses_zero.py"]', 'files = ["contracts/**"]')
        with Project(code=FIXED, spec=shared) as p:
            out = plugin_for(p).commands["necro"]("freeze")
            self.assertEqual(out.count("contracts/test_fair.py"), 1, out)
            self.assertIn("frozen, shared by all 3: contracts/test_adds_up.py, contracts/test_fair.py, "
                          "contracts/test_refuses_zero.py", out)

    def test_different_lists_stay_under_each_contract(self):
        with Project(code=FIXED) as p:
            out = plugin_for(p).commands["necro"]("freeze")
            self.assertEqual(out.count("frozen: "), 3, out)


if __name__ == "__main__":
    unittest.main()
