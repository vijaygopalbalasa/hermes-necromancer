import unittest
from tipsplit import split_bill

class AddsUp(unittest.TestCase):
    def test_shares_add_up_to_the_total(self):
        for total, people in [(1000, 3), (1001, 3), (5, 4), (99, 7)]:
            self.assertEqual(sum(split_bill(total, people)), total)
