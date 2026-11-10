import unittest
from tipsplit import split_bill

class RefusesZero(unittest.TestCase):
    def test_zero_people_is_refused(self):
        with self.assertRaises(ValueError):
            split_bill(100, 0)
