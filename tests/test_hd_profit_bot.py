import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import hd_profit_bot as bot

D = Decimal


class AwaitableValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def get():
            return self.value
        return get().__await__()


class ProfitMathTests(unittest.TestCase):
    def test_min_sell_price_matches_formula(self):
        floor = bot.min_sell_price_usd("1.20", "10", "0.02", "0.01")
        self.assertEqual(floor, D("1.20") + D("0.06") / 10)

    def test_selling_at_floor_yields_exactly_target_profit(self):
        floor = bot.min_sell_price_usd("1.20", "10", "0.02", "0.01")
        self.assertEqual(bot.net_profit_usd(floor, "1.20", "10", "0.02", "0.01"), D("0.03"))

    def test_never_sells_below_floor_or_without_holding(self):
        pos = {"token_balance": D(10), "buy_price_usd": D("1.20"),
               "buy_gas_usd": D("0.02"), "status": bot.HOLDING}
        self.assertFalse(bot.can_sell(pos, "1.205", "0.01")[0])
        self.assertTrue(bot.can_sell(pos, "1.206", "0.01")[0])
        self.assertFalse(bot.can_sell({**pos, "status": bot.IDLE}, "5", "0.01")[0])
        self.assertFalse(bot.can_sell({**pos, "token_balance": D(0)}, "5", "0.01")[0])

    def test_buy_only_when_idle_and_below_threshold(self):
        idle = {"status": bot.IDLE}
        self.assertTrue(bot.can_buy(idle, "0.9", "1.0"))
        self.assertFalse(bot.can_buy(idle, "1.0", "1.0"))
        self.assertFalse(bot.can_buy({"status": bot.HOLDING}, "0.5", "1.0"))

    def test_gas_cost_usd(self):
        self.assertEqual(bot.gas_cost_usd(150_000, 10**9, 3000), D("0.45"))


class PositionStoreTests(unittest.TestCase):
    def test_buys_average_price_and_accumulate_gas(self):
        store = bot.PositionStore()
        store.record_buy("0xAbC", 10, "1.0", "0.01")
        store.record_buy("0xabc", 10, "2.0", "0.02")
        pos = store.get("0xABC")
        self.assertEqual(pos["token_balance"], 20)
        self.assertEqual(pos["buy_price_usd"], D("1.5"))
        self.assertEqual(pos["buy_gas_usd"], D("0.03"))
        self.assertEqual(pos["status"], bot.HOLDING)

    def test_sell_resets_position_and_empty_balance_syncs_to_idle(self):
        store = bot.PositionStore()
        store.record_buy("0x1", 5, "1", "0.01")
        store.sync_balance("0x1", 0)
        self.assertEqual(store.get("0x1")["status"], bot.IDLE)


class SpeedUpTests(unittest.TestCase):
    def make_w3(self, wait_side_effect):
        w3 = MagicMock()
        w3.eth.get_transaction_count = AsyncMock(return_value=7)
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1})
        w3.eth.max_priority_fee = AwaitableValue(1)
        w3.eth.send_raw_transaction = AsyncMock(
            side_effect=[bytes([i]) * 32 for i in range(1, 6)]
        )
        w3.eth.wait_for_transaction_receipt = AsyncMock(side_effect=wait_side_effect)
        w3.eth.get_transaction_receipt = AsyncMock(side_effect=Exception("not found"))
        return w3

    def base_tx(self):
        return {"chainId": 1, "to": "0x" + "1" * 40, "value": 0, "data": b"", "gas": 200000,
                "maxFeePerGas": 1_000_000_000, "maxPriorityFeePerGas": 100_000_000}

    def run_send(self, w3, **kwargs):
        signed_txs = []

        def sign(tx):
            signed_txs.append(dict(tx))
            return SimpleNamespace(raw_transaction=b"raw")

        result = asyncio.run(bot.send_with_speed_up(
            w3, "0x" + "2" * 40, self.base_tx(), sign, timeout_seconds=0.01, **kwargs))
        return result, signed_txs

    def test_replacement_keeps_nonce_and_bumps_both_fees_at_least_10_percent(self):
        receipt = {"status": 1, "blockNumber": 1}
        w3 = self.make_w3([asyncio.TimeoutError(), receipt])
        result, txs = self.run_send(w3)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual({t["nonce"] for t in txs}, {7})
        self.assertGreaterEqual(txs[1]["maxFeePerGas"], txs[0]["maxFeePerGas"] * 1.10)
        self.assertGreaterEqual(txs[1]["maxPriorityFeePerGas"], txs[0]["maxPriorityFeePerGas"] * 1.10)

    def test_rejects_bump_below_10_percent(self):
        w3 = self.make_w3([])
        with self.assertRaises(ValueError):
            self.run_send(w3, fee_bump=D("1.05"))

    def test_earlier_attempt_mined_is_detected_instead_of_replacing(self):
        w3 = self.make_w3([asyncio.TimeoutError()])
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1})
        result, txs = self.run_send(w3)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(len(txs), 1)

    def sell_ctx(self, native_price):
        pos = {"token_balance": D(10), "buy_price_usd": D("1.20"),
               "buy_gas_usd": D("0.02"), "status": bot.HOLDING}
        return bot.SellContext(pos, D("1.25"), D(native_price), 200_000)

    def test_unprofitable_first_attempt_sends_nothing(self):
        w3 = self.make_w3([])
        result, txs = self.run_send(w3, sell=self.sell_ctx(1_000_000))
        self.assertEqual(result["status"], "SKIPPED_UNPROFITABLE")
        self.assertEqual(txs, [])
        w3.eth.send_raw_transaction.assert_not_called()

    def test_bump_that_destroys_profit_cancels_nonce(self):
        # 0.5 USD zysku brutto - 0.02 gazu zakupu: zysk 0.48; gaz sprzedaży rośnie po podbiciu.
        w3 = self.make_w3([asyncio.TimeoutError()])
        sell = self.sell_ctx(2000)  # gaz 0.40 USD przy 1 Gwei, po +15% już 0.46 > margines 0.45
        result, txs = self.run_send(w3, sell=sell)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(txs[-1]["gas"], 21000)
        self.assertEqual(txs[-1]["nonce"], 7)
        self.assertEqual(txs[-1]["to"], "0x" + "2" * 40)


if __name__ == "__main__":
    unittest.main()
