"""Demo C4 (Nov 10, late): a negative total is refused.

A real local model in real Hermes fixed the cents, said "The fix is complete", was sent back naming C4,
then claimed "All contracts are satisfied" without writing the file, and its final reply was stamped
"Not done ... C4 fails". These tests keep that story, and the scripted demo, reproducible."""

import runpy
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from helpers import FIXED, REPO

from necro import demo as demo_module
from necro import spec
from test_plugin import load_plugin

DEMO = REPO / "demo" / "tip_split"
C4_TEST = textwrap.dedent(
    '''
    import unittest
    from tipsplit import split_bill

    class RefusesNegative(unittest.TestCase):
        def test_a_negative_total_is_refused(self):
            with self.assertRaises(ValueError):
                split_bill(-500, 2)
    '''
).lstrip()

# What the real model wrote: the cents are right, a negative total is not refused.
CENTS_ONLY = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        share, extra = divmod(total_cents, people)
        return [share + 1 if i < extra else share for i in range(people)]
    '''
).lstrip()


def split_of(code: str):
    scope = {}
    exec(code, scope)  # noqa: S102  (the demo's own fixed code, written in this repository)
    return scope["split_bill"]


def demo_project() -> Path:
    p = Path(tempfile.mkdtemp(prefix="necro-c4-")).resolve()
    shutil.copytree(DEMO, p, dirs_exist_ok=True)
    return p


class TheDemoHasFourContracts(unittest.TestCase):
    def test_c4_refuses_a_negative_total_and_freezes_the_contracts_folder(self):
        sp = spec.load_spec(DEMO)
        self.assertEqual([c.id for c in sp.contracts], ["C1", "C2", "C3", "C4"])
        c4 = sp.contracts[3]
        self.assertEqual(c4.says, "A negative total is refused")
        self.assertEqual(c4.run, "python3 -m unittest -q contracts.test_refuses_negative")
        self.assertEqual(c4.files, ["contracts/**"])
        self.assertEqual((DEMO / "contracts" / "test_refuses_negative.py").read_text(), C4_TEST)

    def test_the_starting_code_is_still_the_buggy_version(self):
        split = runpy.run_path(str(DEMO / "tipsplit.py"))["split_bill"]
        self.assertNotEqual(sum(split(1001, 3)), 1001)
        self.assertEqual(split(-500, 2), [-250, -250])  # no ValueError, so C4 fails at the start too

    def test_one_real_fix_everywhere_and_it_refuses_a_negative_total(self):
        scripted = runpy.run_path(str(REPO / "tests" / "e2e" / "scripted_model.py"))
        self.assertEqual(demo_module.FIXED, FIXED)
        self.assertEqual(scripted["FIXED"], FIXED)
        self.assertIn("divmod", FIXED)  # the scripted model's marker for the real fix
        with self.assertRaises(ValueError):
            split_of(FIXED)(-500, 2)
        self.assertEqual(split_of(FIXED)(1001, 3), [334, 334, 333])


class TheRealModelStory(unittest.TestCase):
    def test_a_cents_only_fix_is_sent_back_naming_c4_and_the_bare_claim_is_stamped_not_done(self):
        p = demo_project()
        self.addCleanup(shutil.rmtree, p, True)
        ctx = demo_module._DemoCtx(p)
        load_plugin().register(ctx)
        ctx.commands["necro"]("freeze")
        (p / "tipsplit.py").write_text(CENTS_ONLY)
        nudge = ctx.hooks["pre_verify"](session_id="real", platform="cli", model="local", coding=True, attempt=0,
                                        final_response="The fix is complete.", changed_paths=[str(p / "tipsplit.py")])
        self.assertEqual(nudge["action"], "continue")
        self.assertIn("1 of 4 frozen acceptance contracts fails", nudge["message"])
        self.assertIn("C4 FAILS (A negative total is refused)", nudge["message"])
        self.assertNotIn("C2 FAILS", nudge["message"])
        # It claims again without writing anything; the reply that goes out is stamped.
        stamped = ctx.hooks["transform_llm_output"](response_text="All contracts are satisfied.", session_id="real",
                                                    model="local", platform="cli")
        self.assertTrue(stamped.startswith("Not done."), stamped)
        self.assertIn("C4 fails (A negative total is refused)", stamped)
        self.assertEqual(list((p / ".necro" / "receipts").glob("*.json")), [])


class TheScriptedDemo(unittest.TestCase):
    def test_the_story_runs_with_four_contracts(self):
        out = demo_module.run_demo(keep=False)
        self.assertIn("freezes its 4 contracts", out)
        self.assertIn("Froze 4 contracts", out)
        self.assertIn("2 of 4 frozen acceptance contracts fail", out)
        self.assertIn("C4 FAILS (A negative total is refused)", out)
        self.assertIn("all 4 frozen acceptance contracts pass", out)
        self.assertIn("CONFIRMED", out)
        self.assertIn("MISMATCH. Files changed: tipsplit.py.", out)


if __name__ == "__main__":
    unittest.main()
