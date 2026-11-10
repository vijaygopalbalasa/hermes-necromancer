import unittest
from tipsplit import split_bill

class RefusesNegative(unittest.TestCase):
    def test_a_negative_total_is_refused(self):
        with self.assertRaises(ValueError):
            split_bill(-500, 2)
