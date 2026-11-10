"""The e2e harness must never run Hermes against the person's own home (Nov 10)."""

import importlib.util
import unittest

from helpers import REPO


def harness():
    spec = importlib.util.spec_from_file_location("necro_e2e_harness", REPO / "tests" / "e2e" / "run_in_hermes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealHomeGuard(unittest.TestCase):
    def test_the_real_hermes_home_and_anything_under_it_is_refused(self):
        m = harness()
        for home in ("~/.hermes", "~/.hermes/", "~/.hermes/profiles/work"):
            with self.assertRaises(SystemExit, msg=home):
                m.refuse_real_home(home)

    def test_every_hermes_call_goes_through_the_guard(self):
        m = harness()
        with self.assertRaises(SystemExit):
            m.run(["hermes", "--version"], env={})  # no HERMES_HOME means ~/.hermes: refused before running

    def test_a_throwaway_home_is_allowed(self):
        self.assertTrue(str(harness().refuse_real_home("/tmp/necro-throwaway-home")).endswith("necro-throwaway-home"))


if __name__ == "__main__":
    unittest.main()
