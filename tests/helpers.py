"""Shared helpers: build small throwaway projects with frozen contracts."""

import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PY = sys.executable

BUGGY = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        share = total_cents // people
        return [share] * people
    '''
).lstrip()

HALF_FIX = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        share = total_cents // people
        shares = [share] * people
        shares[0] += total_cents - share * people
        return shares
    '''
).lstrip()

FIXED = textwrap.dedent(
    '''
    def split_bill(total_cents, people):
        """Split a bill into whole-cent shares that add up to the total."""
        if people < 1:
            raise ValueError("people must be at least 1")
        share, extra = divmod(total_cents, people)
        return [share + 1 if i < extra else share for i in range(people)]
    '''
).lstrip()

ADDS_UP = textwrap.dedent(
    '''
    import unittest
    from tipsplit import split_bill

    class AddsUp(unittest.TestCase):
        def test_shares_add_up_to_the_total(self):
            for total, people in [(1000, 3), (1001, 3), (5, 4), (99, 7)]:
                self.assertEqual(sum(split_bill(total, people)), total)
    '''
).lstrip()

FAIR = textwrap.dedent(
    '''
    import unittest
    from tipsplit import split_bill

    class Fair(unittest.TestCase):
        def test_no_share_differs_by_more_than_a_cent(self):
            for total, people in [(1001, 3), (99, 7), (10, 4)]:
                shares = split_bill(total, people)
                self.assertLessEqual(max(shares) - min(shares), 1)
    '''
).lstrip()

REFUSES_ZERO = textwrap.dedent(
    '''
    import unittest
    from tipsplit import split_bill

    class RefusesZero(unittest.TestCase):
        def test_zero_people_is_refused(self):
            with self.assertRaises(ValueError):
                split_bill(100, 0)
    '''
).lstrip()


def spec_text(py=PY, extra=""):
    q = py.replace("\\", "\\\\")
    return textwrap.dedent(
        f'''
        # Acceptance contracts for the tip splitter.
        [[contract]]
        id = "C1"
        says = "Shares add up to the total"
        run = "{q} -m unittest -q contracts.test_adds_up"
        files = ["contracts/test_adds_up.py"]

        [[contract]]
        id = "C2"
        says = "No share differs from another by more than one cent"
        run = "{q} -m unittest -q contracts.test_fair"
        files = ["contracts/test_fair.py"]

        [[contract]]
        id = "C3"
        says = "Zero people is refused"
        run = "{q} -m unittest -q contracts.test_refuses_zero"
        files = ["contracts/test_refuses_zero.py"]
        {extra}
        '''
    ).lstrip()


class Project:
    """A temporary tip-splitter project. Use as a context manager."""

    def __init__(self, code=BUGGY, spec=None):
        self.dir = Path(tempfile.mkdtemp(prefix="necro-test-")).resolve()
        self.write("tipsplit.py", code)
        self.write("contracts/test_adds_up.py", ADDS_UP)
        self.write("contracts/test_fair.py", FAIR)
        self.write("contracts/test_refuses_zero.py", REFUSES_ZERO)
        self.write("necro.toml", spec if spec is not None else spec_text())

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel):
        return (self.dir / rel).read_text(encoding="utf-8")

    def path(self, rel):
        return self.dir / rel

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


def env_without_git():
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(Path(tempfile.gettempdir()).resolve())
    return env
