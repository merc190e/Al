import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import main
from main import format_raw_token_amount, summarize_wallet_trades


class WalletTradeHistoryTests(unittest.TestCase):
    def test_formats_raw_token_amount_without_losing_precision(self):
        self.assertEqual(format_raw_token_amount(123450000, 6), "123.45")
        self.assertEqual(format_raw_token_amount(42, 0), "42")
        self.assertEqual(format_raw_token_amount(7, 18), "0.000000000000000007")

    def test_summarizes_buy_and_sell_amounts_by_token_and_quote(self):
        summary = summarize_wallet_trades([
            {
                "type": "buy",
                "token_symbol": "BOBER",
                "quote_symbol": "ETH",
                "token_amount": "10.25",
                "quote_amount": "0.012",
            },
            {
                "type": "buy",
                "token_symbol": "BOBER",
                "quote_symbol": "ETH",
                "token_amount": "2.75",
                "quote_amount": "0.003",
            },
            {
                "type": "sell",
                "token_symbol": "BOBER",
                "quote_symbol": "ETH",
                "token_amount": "4",
                "quote_amount": "0.006",
            },
        ])

        self.assertEqual(summary["buy"], [{
            "token_symbol": "BOBER",
            "quote_symbol": "ETH",
            "token_amount": "13.00",
            "quote_amount": "0.015",
        }])
        self.assertEqual(summary["sell"], [{
            "token_symbol": "BOBER",
            "quote_symbol": "ETH",
            "token_amount": "4",
            "quote_amount": "0.006",
        }])

    def test_ignores_incomplete_trade_amounts_in_totals(self):
        summary = summarize_wallet_trades([{
            "type": "buy",
            "token_symbol": "BOBER",
            "quote_symbol": "ETH",
            "token_amount": None,
            "quote_amount": "0.01",
        }])

        self.assertEqual(summary["buy"], [])

    def test_records_trade_history_in_wallet_store_and_public_response(self):
        previous_wallet_store = main.wallet_store
        main.wallet_store = {
            "wallet-id": {
                "name": "Test wallet",
                "address": "0x0000000000000000000000000000000000000001",
                "network": "robinhood",
                "keystore": "encrypted",
            }
        }
        trade = {
            "type": "buy",
            "token_symbol": "BOBER",
            "quote_symbol": "ETH",
            "token_amount": "10",
            "quote_amount": "0.025",
        }
        try:
            with patch.object(main, "save_wallet_store") as save_store:
                main.record_wallet_trade("wallet-id", trade)

            save_store.assert_called_once_with()
            wallet = main.public_wallet("wallet-id", main.wallet_store["wallet-id"])
            self.assertEqual(wallet["trade_history"], [trade])
            self.assertEqual(wallet["trade_summary"]["buy"][0]["quote_amount"], "0.025")
        finally:
            main.wallet_store = previous_wallet_store

    def test_auto_buy_progress_is_scoped_to_the_current_campaign(self):
        previous_wallet_store = main.wallet_store
        main.wallet_store = {
            "wallet-id": {
                "trade_history": [
                    {
                        "type": "buy", "pair_id": "pair", "campaign_id": "old",
                        "token_amount": "50", "quote_amount": "2",
                    },
                    {
                        "type": "buy", "pair_id": "pair", "campaign_id": "new",
                        "token_amount": "3", "quote_amount": "0.2",
                    },
                ]
            }
        }
        try:
            count, bought, spent = main.wallet_pair_buy_totals(
                "wallet-id", "pair", "new"
            )
        finally:
            main.wallet_store = previous_wallet_store

        self.assertEqual((count, bought, spent), (1, main.Decimal("3"), main.Decimal("0.2")))

    def test_auto_buy_quantity_campaign_splits_remaining_target_across_transactions(self):
        self.assertEqual(
            main.auto_buy_quantity_tranche(main.Decimal("10"), 0, 10),
            main.Decimal("1"),
        )
        self.assertEqual(
            main.auto_buy_quantity_tranche(main.Decimal("9.2"), 1, 10),
            main.Decimal("9.2") / 9,
        )
        self.assertEqual(
            main.auto_buy_quantity_tranche(main.Decimal("1"), 10, 10),
            main.Decimal("0"),
        )

    def test_auto_sell_totals_are_scoped_to_campaign(self):
        previous_wallet_store = main.wallet_store
        main.wallet_store = {
            "wallet-id": {
                "trade_history": [
                    {
                        "type": "sell", "pair_id": "pair", "campaign_id": "old",
                        "token_amount": "50", "quote_amount": "2",
                    },
                    {
                        "type": "sell", "pair_id": "pair", "campaign_id": "new",
                        "token_amount": "3", "quote_amount": "0.2",
                    },
                    {
                        "type": "buy", "pair_id": "pair", "campaign_id": "new",
                        "token_amount": "10", "quote_amount": "0.5",
                    },
                ]
            }
        }
        try:
            count, sold, received = main.wallet_pair_trade_totals(
                "wallet-id", "pair", "sell", "new"
            )
        finally:
            main.wallet_store = previous_wallet_store

        self.assertEqual((count, sold, received), (1, main.Decimal("3"), main.Decimal("0.2")))

    def test_price_ladder_waits_for_each_ascending_level(self):
        level, crossed, complete = main.price_ladder_step(1, 1.3, 1.02, 0, 10)
        self.assertAlmostEqual(level, 1.03)
        self.assertFalse(crossed)
        self.assertFalse(complete)

        level, crossed, complete = main.price_ladder_step(1, 1.3, 1.03, 0, 10)
        self.assertAlmostEqual(level, 1.03)
        self.assertTrue(crossed)
        self.assertFalse(complete)

    def test_price_ladder_supports_descending_targets_and_completion(self):
        level, crossed, complete = main.price_ladder_step(1.3, 1, 1.2, 0, 3)
        self.assertAlmostEqual(level, 1.2)
        self.assertTrue(crossed)
        self.assertFalse(complete)

        level, crossed, complete = main.price_ladder_step(1, 1.3, 1.3, 10, 10)
        self.assertIsNone(level)
        self.assertFalse(crossed)
        self.assertTrue(complete)

    def test_price_ladder_transactions_are_spaced_over_configured_duration(self):
        self.assertTrue(main.price_ladder_trade_due(100, 100, 0, 10, 120))
        self.assertFalse(main.price_ladder_trade_due(100, 899, 1, 10, 120))
        self.assertTrue(main.price_ladder_trade_due(100, 900, 1, 10, 120))
        self.assertFalse(
            main.price_ladder_trade_due(100, 924, 1, 10, 120, last_trade_at=125)
        )
        self.assertTrue(
            main.price_ladder_trade_due(100, 925, 1, 10, 120, last_trade_at=125)
        )
        self.assertEqual(
            main.price_ladder_next_trade_at(100, 1, 10, 120, last_trade_at=125),
            925,
        )
        self.assertIsNone(main.price_ladder_next_trade_at(100, 10, 10, 120))

    def test_daily_schedule_uses_configured_timezone_and_skips_past_slot(self):
        after = datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc).timestamp()
        same_day = main.next_daily_schedule_at("09:00", "UTC", after)
        self.assertEqual(
            datetime.fromtimestamp(same_day, timezone.utc),
            datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
        )

        after_slot = datetime(2026, 1, 1, 9, 1, tzinfo=timezone.utc).timestamp()
        next_day = main.next_daily_schedule_at("09:00", "UTC", after_slot)
        self.assertEqual(
            datetime.fromtimestamp(next_day, timezone.utc),
            datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
        )

    def test_due_daily_schedule_runs_once_and_advances_to_tomorrow(self):
        previous_wallet_store = main.wallet_store
        previous_unlocked_wallet_keys = main.unlocked_wallet_keys
        previous_logs = list(main.trade_logs)
        now = datetime(2026, 1, 1, 9, 0, 5, tzinfo=timezone.utc).timestamp()
        main.wallet_store = {"wallet-id": {"network": "robinhood"}}
        main.unlocked_wallet_keys = {"wallet-id": "test-private-key"}
        main.trade_logs.clear()
        pair = {
            "auto_trade_enabled": True,
            "auto_sell_enabled": False,
            "auto_buy_schedule": [{
                "time": "09:00",
                "quantity": 2,
                "max_quote": 0.01,
                "next_run_at": now - 5,
            }],
            "schedule_timezone": "UTC",
            "wallet_id": "wallet-id",
            "token_symbol": "TEST",
        }
        trade = {
            "token_amount": "2",
            "token_symbol": "TEST",
            "tx_hash": "0xabc",
            "transaction_cost": {
                "value": "0.01",
                "transaction_fee": "0.0001",
                "gas_price": "2 Gwei",
                "total": "0.0101",
                "symbol": "ETH",
            },
        }
        try:
            with patch.object(main, "execute_auto_buy", return_value=trade) as execute, \
                    patch.object(main, "record_wallet_trade") as record:
                asyncio.run(main.run_due_daily_schedule("pair", pair, now))

            execute.assert_called_once()
            record.assert_called_once_with("wallet-id", trade)
            self.assertGreater(pair["auto_buy_schedule"][0]["next_run_at"], now)
            self.assertIn("HARMONOGRAM BUY POTWIERDZONY", main.trade_logs[-1])
        finally:
            main.wallet_store = previous_wallet_store
            main.unlocked_wallet_keys = previous_unlocked_wallet_keys
            main.trade_logs[:] = previous_logs

    def test_missed_daily_schedule_is_skipped_without_sending_a_trade(self):
        previous_wallet_store = main.wallet_store
        previous_unlocked_wallet_keys = main.unlocked_wallet_keys
        now = datetime(2026, 1, 1, 9, 2, tzinfo=timezone.utc).timestamp()
        main.wallet_store = {"wallet-id": {"network": "robinhood"}}
        main.unlocked_wallet_keys = {"wallet-id": "test-private-key"}
        pair = {
            "auto_trade_enabled": True,
            "auto_sell_enabled": False,
            "auto_buy_schedule": [{
                "time": "09:00",
                "quantity": 2,
                "max_quote": 0.01,
                "next_run_at": now - 120,
            }],
            "schedule_timezone": "UTC",
            "wallet_id": "wallet-id",
            "token_symbol": "TEST",
        }
        try:
            with patch.object(main, "execute_auto_buy") as execute:
                asyncio.run(main.run_due_daily_schedule("pair", pair, now))

            execute.assert_not_called()
            self.assertEqual(
                datetime.fromtimestamp(
                    pair["auto_buy_schedule"][0]["next_run_at"], timezone.utc
                ),
                datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
            )
        finally:
            main.wallet_store = previous_wallet_store
            main.unlocked_wallet_keys = previous_unlocked_wallet_keys

    def test_auto_swap_empty_reverts_include_stage_specific_diagnostics(self):
        pair = {"dex_pairs_list": [{"dex_version": "v3"}, {"dex_version": "v4"}]}
        quote_error = main.auto_swap_failure(
            "quote", RuntimeError("execution reverted: 0x"), pair
        )
        self.assertIn("Router V2 odrzucił wycenę", str(quote_error))
        self.assertIn("v3, v4", str(quote_error))
        self.assertIn("nie przełącza się", str(quote_error))

        simulation_error = main.auto_swap_failure(
            "simulation", RuntimeError("execution reverted: 0x"), pair
        )
        self.assertIn("przed wysłaniem transakcji", str(simulation_error))
        self.assertIn("limit poślizgu", str(simulation_error))

    def test_auto_swap_selects_matching_liquid_v3_pool_and_rejects_v4_only(self):
        network = main.NETWORK_CONFIG["robinhood"]
        wrapped_native = network["wrapped_native"]
        token = "0x0000000000000000000000000000000000000004"
        pair = {
            "dex_pairs_list": [
                {
                    "dex_version": "unknown",
                    "pool_address": "0x" + "11" * 20,
                    "base_token_address": wrapped_native,
                    "quote_token_address": token,
                    "liquidity_usd": 99999,
                },
                {
                    "dex_version": "v3",
                    "pool_address": "0x" + "22" * 20,
                    "base_token_address": token,
                    "quote_token_address": wrapped_native,
                    "liquidity_usd": 100,
                },
                {
                    "dex_version": "v3",
                    "pool_address": "0x" + "33" * 20,
                    "base_token_address": token,
                    "quote_token_address": wrapped_native,
                    "liquidity_usd": 200,
                },
                {
                    "dex_version": "v4",
                    "pool_address": "0x" + "44" * 32,
                    "base_token_address": token,
                    "quote_token_address": wrapped_native,
                    "liquidity_usd": 500,
                },
            ]
        }

        route = main.select_auto_swap_route(
            pair, main.NATIVE_TOKEN_ADDRESS, token, network
        )
        self.assertEqual(route["version"], "v3")
        self.assertEqual(route["pool"]["pool_address"], "0x" + "33" * 20)

        with self.assertRaisesRegex(RuntimeError, "wyłącznie pulę DEX V4"):
            main.select_auto_swap_route(
                {"dex_pairs_list": [pair["dex_pairs_list"][3]]},
                main.NATIVE_TOKEN_ADDRESS,
                token,
                network,
            )

    def test_price_ladder_duration_is_limited_to_30_days(self):
        with self.assertRaises(ValueError):
            main.TradingPair(
                token_address="0x0000000000000000000000000000000000000004",
                quote_token_address=main.NATIVE_TOKEN_ADDRESS,
                auto_buy_duration_minutes=43201,
            )
        with self.assertRaises(ValueError):
            main.TradingPair(
                token_address="0x0000000000000000000000000000000000000004",
                quote_token_address=main.NATIVE_TOKEN_ADDRESS,
                auto_sell_quantity_duration_minutes=0,
            )
        with self.assertRaises(ValueError):
            main.TradingPair(
                token_address="0x0000000000000000000000000000000000000004",
                quote_token_address=main.NATIVE_TOKEN_ADDRESS,
                auto_buy_quantity_duration_minutes=43201,
            )

    def test_price_campaigns_validate_and_create_independent_campaign_ids(self):
        previous_pairs = main.active_pairs
        previous_wallet_store = main.wallet_store
        main.active_pairs = {}
        main.wallet_store = {
            "wallet-id": {
                "network": "robinhood",
                "trade_history": [],
            }
        }
        previous_unlocked_wallet_keys = main.unlocked_wallet_keys
        main.unlocked_wallet_keys = {"wallet-id": "test-private-key"}
        pair = main.TradingPair(
            network="robinhood",
            token_address="0x0000000000000000000000000000000000000004",
            quote_token_address=main.NATIVE_TOKEN_ADDRESS,
            target_buy_price=1.3,
            target_sell_price=1.5,
            auto_trade_enabled=True,
            auto_buy_mode="target_price",
            auto_buy_max_transactions=10,
            auto_buy_duration_minutes=120,
            auto_sell_enabled=True,
            auto_sell_mode="target_price",
            auto_sell_target_quantity=25,
            auto_sell_max_transactions=5,
            auto_sell_duration_minutes=180,
            wallet_id="wallet-id",
        )
        try:
            result = asyncio.run(main.add_pair(pair))
            stored = main.active_pairs[result["pair_id"]]
            self.assertEqual(stored["auto_buy_mode"], "target_price")
            self.assertEqual(stored["auto_buy_duration_minutes"], 120)
            self.assertEqual(stored["auto_sell_mode"], "target_price")
            self.assertEqual(stored["auto_sell_target_quantity"], 25)
            self.assertEqual(stored["auto_sell_duration_minutes"], 180)
            self.assertNotEqual(
                stored["auto_buy_campaign_id"], stored["auto_sell_campaign_id"]
            )

            quantity_pair = main.TradingPair(
                network="robinhood",
                token_address="0x0000000000000000000000000000000000000005",
                quote_token_address=main.NATIVE_TOKEN_ADDRESS,
                target_buy_price=None,
                buy_percent=None,
                target_sell_price=2,
                auto_trade_enabled=True,
                auto_buy_mode="target_quantity",
                auto_buy_target_quantity=100,
                auto_buy_total_budget=1,
                auto_buy_max_transactions=5,
                auto_buy_quantity_duration_minutes=30,
                auto_sell_enabled=True,
                auto_sell_mode="target_quantity",
                auto_sell_target_quantity=50,
                auto_sell_max_transactions=4,
                auto_sell_quantity_duration_minutes=45,
                wallet_id="wallet-id",
            )
            quantity_result = asyncio.run(main.add_pair(quantity_pair))
            quantity_stored = main.active_pairs[quantity_result["pair_id"]]
            self.assertEqual(quantity_stored["auto_buy_quantity_duration_minutes"], 30)
            self.assertEqual(quantity_stored["auto_sell_quantity_duration_minutes"], 45)
            started_at = quantity_stored["auto_buy_quantity_started_at"]
            self.assertGreater(started_at, 0)
            self.assertEqual(quantity_stored["auto_buy_next_trade_at"], started_at)
            self.assertTrue(
                main.price_ladder_trade_due(started_at, started_at, 0, 5, 30)
            )

            schedule_pair = main.TradingPair(
                network="robinhood",
                token_address="0x0000000000000000000000000000000000000006",
                quote_token_address=main.NATIVE_TOKEN_ADDRESS,
                auto_trade_enabled=True,
                auto_buy_mode="schedule",
                auto_buy_schedule=[{
                    "time": "09:00",
                    "quantity": 2,
                    "max_quote": 0.01,
                }],
                auto_sell_enabled=True,
                auto_sell_mode="schedule",
                auto_sell_schedule=[{"time": "18:00", "quantity": 1}],
                schedule_timezone="UTC",
                wallet_id="wallet-id",
            )
            schedule_result = asyncio.run(main.add_pair(schedule_pair))
            schedule_stored = main.active_pairs[schedule_result["pair_id"]]
            self.assertEqual(schedule_stored["auto_buy_mode"], "schedule")
            self.assertEqual(schedule_stored["auto_sell_mode"], "schedule")
            self.assertGreater(
                schedule_stored["auto_buy_schedule"][0]["next_run_at"],
                main.time.time(),
            )

            conflicting_schedule_pair = main.TradingPair(
                network="robinhood",
                token_address="0x0000000000000000000000000000000000000007",
                quote_token_address=main.NATIVE_TOKEN_ADDRESS,
                auto_trade_enabled=True,
                auto_buy_mode="schedule",
                auto_buy_schedule=[{
                    "time": "09:00",
                    "quantity": 2,
                    "max_quote": 0.01,
                }],
                auto_sell_enabled=True,
                auto_sell_mode="schedule",
                auto_sell_schedule=[{"time": "09:00", "quantity": 1}],
                schedule_timezone="UTC",
                wallet_id="wallet-id",
            )
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.add_pair(conflicting_schedule_pair))
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("tę samą godzinę", raised.exception.detail)
        finally:
            main.active_pairs = previous_pairs
            main.wallet_store = previous_wallet_store
            main.unlocked_wallet_keys = previous_unlocked_wallet_keys

    def test_auto_trade_pair_rejects_a_locked_wallet(self):
        previous_pairs = main.active_pairs
        previous_wallet_store = main.wallet_store
        previous_unlocked_wallet_keys = main.unlocked_wallet_keys
        main.active_pairs = {}
        main.wallet_store = {
            "wallet-id": {
                "network": "robinhood",
                "trade_history": [],
            }
        }
        main.unlocked_wallet_keys = {}
        pair = main.TradingPair(
            network="robinhood",
            token_address="0x0000000000000000000000000000000000000004",
            quote_token_address=main.NATIVE_TOKEN_ADDRESS,
            target_buy_price=1,
            auto_trade_enabled=True,
            wallet_id="wallet-id",
        )
        try:
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.add_pair(pair))
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("zablokowany", raised.exception.detail)
        finally:
            main.active_pairs = previous_pairs
            main.wallet_store = previous_wallet_store
            main.unlocked_wallet_keys = previous_unlocked_wallet_keys

    def test_price_campaign_requires_a_positive_dex_target(self):
        pair = main.TradingPair(
            network="robinhood",
            token_address="0x0000000000000000000000000000000000000004",
            quote_token_address=main.NATIVE_TOKEN_ADDRESS,
            auto_trade_enabled=True,
            auto_buy_mode="target_price",
            wallet_id="wallet-id",
        )
        with self.assertRaises(main.HTTPException) as raised:
            asyncio.run(main.add_pair(pair))
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
