import asyncio
import unittest
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

import hd_profit_api
import hd_profit_bot as bot
from hd_profit_runner import BotConfig, HdProfitRunner

D = Decimal
TOKEN = "0x8031a70628207ed93d7037ea6b1444912993c715"
WNATIVE = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
W1, W2 = "0x" + "a" * 40, "0x" + "b" * 40


class FakeExecutor:
    def __init__(self, balances, sell_status="SUCCESS"):
        self._balances = dict(balances)
        self.sell_status = sell_status
        self.buys, self.sells = [], []

    async def buy(self, wallet, spend_native_wei):
        self.buys.append(wallet)
        self._balances[wallet] = D(100)
        return {"qty": D(100), "spent_native_wei": spend_native_wei, "gas_native_wei": 10**13}

    async def sell(self, wallet, qty, ctx, min_out_native_wei):
        self.sells.append((wallet, qty, min_out_native_wei))
        if self.sell_status == "SUCCESS":
            self._balances[wallet] = D(0)
        return {"status": self.sell_status}

    async def balances(self, wallets):
        return {w: self._balances.get(w, D(0)) for w in wallets}

    async def network_state(self):
        return {"max_fee_per_gas": 10**9}


def make_runner(executor, prices, **overrides):
    async def price_fn(network, addresses):
        return prices

    config = BotConfig(
        token_address=TOKEN, wallets=[W1, W2], wrapped_native=WNATIVE,
        buy_below_usd=D("1.0"), buy_native_wei=10**15, target_profit_usd=D("0.05"),
        sell_gas_units=200_000, **overrides,
    )
    return HdProfitRunner(config, executor, bot.PositionStore(), price_fn=price_fn)


def prices(token):
    return {TOKEN.lower(): D(token), WNATIVE.lower(): D(3000)}


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_buys_below_threshold_on_all_idle_wallets(self):
        ex = FakeExecutor({})
        runner = make_runner(ex, prices("0.5"))
        await runner.tick()
        self.assertCountEqual(ex.buys, [W1, W2])
        pos = runner.store.get(W1)
        self.assertEqual(pos["status"], bot.HOLDING)
        self.assertEqual(pos["token_balance"], 100)
        self.assertEqual(pos["buy_price_usd"], D("0.03"))  # 0.001 ETH * 3000 / 100
        self.assertEqual(pos["buy_gas_usd"], D("0.03"))

    async def test_does_not_buy_at_or_above_threshold(self):
        ex = FakeExecutor({})
        await make_runner(ex, prices("1.0")).tick()
        self.assertEqual(ex.buys, [])

    async def test_no_sell_without_net_profit_then_sells_with_amount_out_min(self):
        ex = FakeExecutor({})
        runner = make_runner(ex, prices("0.5"))
        await runner.tick()
        # P_buy 0.03, Q 100, G_buy 0.03, G_sell 0.2*... = 200k*1gwei*3000 = 0.6 USD
        floor = bot.min_sell_price_usd("0.03", 100, "0.03", "0.6", D("0.05"))
        runner.price_fn = self._const(prices(floor - D("0.0001")))
        await runner.tick()
        self.assertEqual(ex.sells, [])
        runner.price_fn = self._const(prices(floor))
        await runner.tick()
        self.assertEqual(len(ex.sells), 2)
        wallet, qty, min_out = ex.sells[0]
        expected_min_out = int(floor * 100 / 3000 * 10**18)
        self.assertAlmostEqual(min_out / expected_min_out, 1, places=9)
        self.assertEqual(runner.store.get(W1)["status"], bot.IDLE)
        self.assertEqual(runner.state.counters["sells"], 2)

    async def test_cancelled_sell_returns_wallet_to_holding(self):
        ex = FakeExecutor({}, sell_status="CANCELLED")
        runner = make_runner(ex, prices("0.5"))
        await runner.tick()
        runner.price_fn = self._const(prices("50"))
        await runner.tick()
        self.assertEqual(runner.store.get(W1)["status"], bot.HOLDING)
        self.assertEqual(runner.state.counters["cancelled"], 2)

    async def test_unreadable_balance_is_skipped(self):
        class Blind(FakeExecutor):
            async def balances(self, wallets):
                return {w: None for w in wallets}

        ex = Blind({})
        await make_runner(ex, prices("0.5")).tick()
        self.assertEqual(ex.buys, [])

    async def test_untracked_tokens_are_never_sold(self):
        ex = FakeExecutor({W1: D(1000)})
        runner = make_runner(ex, prices("999"))
        await runner.tick()
        self.assertEqual(ex.sells, [])

    @staticmethod
    def _const(value):
        async def fn(network, addresses):
            return value
        return fn

    def test_config_rejects_profit_below_three_cents(self):
        with self.assertRaisesRegex(ValueError, "0.03"):
            BotConfig(token_address=TOKEN, wallets=[W1], wrapped_native=WNATIVE,
                      buy_below_usd=D(1), buy_native_wei=1, target_profit_usd=D("0.02")).validate()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor({})
        app = FastAPI()
        app.include_router(hd_profit_api.create_router(
            executor_factory=lambda token: self.executor,
            wallets_provider=lambda start, end: [W1, W2],
            db_path=":memory:",
        ))
        self.client = TestClient(app)
        self.body = {"buy_below_usd": "1", "buy_native": "0.001", "confirmed": True}

    def test_requires_confirmation_and_rejects_low_profit(self):
        self.assertEqual(self.client.post("/api/hd-bot/start", json={**self.body, "confirmed": False}).status_code, 400)
        low = self.client.post("/api/hd-bot/start", json={**self.body, "target_profit_usd": "0.01"})
        self.assertEqual(low.status_code, 400)

    def test_status_is_empty_before_start(self):
        self.assertEqual(self.client.get("/api/hd-bot").json(), {"bot": None})


if __name__ == "__main__":
    unittest.main()
