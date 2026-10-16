import unittest


class MockDexPool:
    def __init__(self, name):
        self.name = name
        self.bober_balance = 0
        self.trades = []

    def buy_bober(self, amount):
        self.bober_balance += amount
        self.trades.append(("buy", amount))

    def sell_bober(self, amount):
        if amount > self.bober_balance:
            raise ValueError(f"Insufficient mock BOBER balance in {self.name}")
        self.bober_balance -= amount
        self.trades.append(("sell", amount))


class MockTradeCycleTests(unittest.TestCase):
    def test_buys_ten_and_sells_four_bober_in_each_mock_dex_pool(self):
        pools = [
            MockDexPool("Uniswap V3"),
            MockDexPool("Uniswap V4"),
            MockDexPool("Other DEX"),
        ]

        for pool in pools:
            pool.buy_bober(10)
            pool.sell_bober(4)

        for pool in pools:
            with self.subTest(pool=pool.name):
                self.assertEqual(pool.trades, [("buy", 10), ("sell", 4)])
                self.assertEqual(pool.bober_balance, 6)


if __name__ == "__main__":
    unittest.main()
