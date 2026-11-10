import unittest
from tipsplit import split_bill

class Fair(unittest.TestCase):
    def test_no_share_differs_by_more_than_a_cent(self):
        for total, people in [(1001, 3), (99, 7), (10, 4)]:
            shares = split_bill(total, people)
            self.assertLessEqual(max(shares) - min(shares), 1)
