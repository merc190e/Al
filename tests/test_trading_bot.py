import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from bot import main
from bot.swap_executor import _to_base_units, execute_swap


class TradingBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_reports_signal_without_submitting_trade(self):
        pair = main.TradingPair(
            id="test-pair",
            network="eth",
            token_address="0x" + "1" * 40,
            token_symbol="TEST",
            quote_token_address="0x" + "2" * 40,
            quote_symbol="ETH",
            entry_price=2,
            amount_held=3,
            target_buy_price=2,
        )
        main.active_pairs.clear()
        main.active_pairs["test-pair"] = pair.model_dump()
        sleep_calls = 0

        async def stop_after_two_cycles(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                raise asyncio.CancelledError

        with (
            patch.object(main.asyncio, "to_thread", new_callable=AsyncMock, return_value={
                "price_usd": 1,
                "change_24h": None,
                "logo_url": None,
                "token_symbol": "TEST",
                "website": None,
                "twitter": None,
                "pool_address": None,
                "dex_pairs_list": [],
            }),
            patch("bot.swap_executor.execute_swap") as execute,
            patch.object(main.manager, "broadcast", new_callable=AsyncMock) as broadcast,
            patch.object(main.asyncio, "sleep", side_effect=stop_after_two_cycles),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.trading_bot_loop()

        execute.assert_not_called()
        update = broadcast.await_args_list[0].args[0]["data"][0]
        self.assertEqual(update["status"], "TRIGGER_BUY")
        self.assertEqual(update["unrealized_pnl_usd"], -3)
        self.assertEqual(broadcast.await_args_list[0].args[0]["stats"]["total_pnl_percent"], -50)
        main.active_pairs.clear()

    async def test_pair_mutations_do_not_require_api_token(self):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "id": "",
                "network": "eth",
                "token_address": "0x" + "1" * 40,
                "quote_token_address": "0x" + "2" * 40,
                "token_symbol": "TEST",
                "quote_symbol": "ETH",
                "trade_amount": 0.5,
                "target_buy_price": 1,
            }
            dashboard = await client.get("/")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("Bilans Bota & PnL", dashboard.text)
            self.assertIn("quote_token_address", dashboard.text)
            self.assertIn("function selectPairPool", dashboard.text)
            self.assertIn("function loadOHLCVChart", dashboard.text)
            self.assertIn("cdn.plot.ly/plotly", dashboard.text)
            self.assertNotIn("BOT_API_TOKEN", dashboard.text)

            added = await client.post("/api/pairs", json=payload)
            self.assertEqual(added.status_code, 200)
            pair_id = added.json()["pair_id"]

            pool_address = "0x" + "3" * 40
            main.active_pairs[pair_id]["dex_pairs_list"] = [
                {"pool_address": pool_address},
            ]
            selected = await client.put(
                f"/api/pairs/{pair_id}/pool",
                json={"pool_address": pool_address},
            )
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(
                main.active_pairs[pair_id]["pair_contract_address"],
                pool_address,
            )

            rejected = await client.put(
                f"/api/pairs/{pair_id}/pool",
                json={"pool_address": "unrelated-pool"},
            )
            self.assertEqual(rejected.status_code, 422)

            removed = await client.delete(f"/api/pairs/{pair_id}")
            self.assertEqual(removed.status_code, 200)

    def test_amount_conversion_rejects_precision_loss(self):
        self.assertEqual(_to_base_units(0.125, 6), 125000)
        with self.assertRaises(ValueError):
            _to_base_units(0.0000001, 6)

    def test_pair_requires_token_address(self):
        with self.assertRaises(ValueError):
            main.TradingPair(
                network="eth",
                token_symbol="TEST",
                quote_token_address="0x" + "2" * 40,
                quote_symbol="ETH",
            )

    def test_geckoterminal_data_includes_pool_price_and_socials(self):
        token_address = "0x" + "1" * 40
        pool_address = "0x" + "3" * 40

        def mock_response(payload):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = payload
            return response

        pools = mock_response({
            "data": [{
                "id": f"eth_{pool_address}",
                "attributes": {
                    "name": "TEST / USDC",
                    "dex_id": "uniswap-v3",
                    "base_token_price_usd": "0.25",
                    "price_change_percentage": {"h24": "12.5"},
                },
                "relationships": {
                    "base_token": {"data": {"id": f"eth_{token_address}"}},
                },
            }]
        })
        selected_pool = mock_response({
            "data": {
                "attributes": {
                    "base_token_price_usd": "0.25",
                    "price_change_percentage": {"h24": "12.5"},
                },
                "relationships": {
                    "base_token": {"data": {"id": f"eth_{token_address}"}},
                },
            }
        })
        manually_selected_pool = mock_response({
            "data": {
                "attributes": {
                    "base_token_price_usd": "0.5",
                    "price_change_percentage": {"h24": "-4.25"},
                },
                "relationships": {
                    "base_token": {"data": {"id": f"eth_{token_address}"}},
                },
            }
        })
        token = mock_response({
            "data": {"attributes": {
                "price_usd": "0.25",
                "symbol": "TEST",
                "image_url": "https://example.test/logo.png",
            }}
        })
        info = mock_response({
            "data": {"attributes": {
                "symbol": "TEST",
                "image_url": "https://example.test/logo.png",
                "websites": ["https://example.test"],
                "twitter_handle": "@testcoin",
            }}
        })
        with patch.object(main.requests, "get", side_effect=[pools, selected_pool, token, info]) as get:
            result = main.fetch_geckoterminal_details("eth", token_address)

        self.assertEqual(result["price_usd"], 0.25)
        self.assertEqual(result["logo_url"], "https://example.test/logo.png")
        self.assertEqual(result["website"], "https://example.test")
        self.assertEqual(result["twitter"], "https://x.com/testcoin")
        self.assertEqual(result["change_24h"], 12.5)
        self.assertEqual(result["pool_address"], pool_address)
        self.assertEqual(result["dex_pairs_list"][0]["price_usd"], 0.25)
        self.assertEqual(get.call_count, 4)

        selected_pool = mock_response({
            "data": {
                "attributes": {
                    "base_token_price_usd": "0.5",
                    "price_change_percentage": {"h24": "-4.25"},
                },
                "relationships": {
                    "base_token": {"data": {"id": f"eth_{token_address}"}},
                },
            }
        })
        with patch.object(
            main.requests,
            "get",
            side_effect=[pools, manually_selected_pool, token, info],
        ):
            selected_result = main.fetch_geckoterminal_details(
                "eth",
                token_address,
                custom_pair_contract=pool_address,
            )

        self.assertEqual(selected_result["pool_address"], pool_address)
        self.assertEqual(selected_result["price_usd"], 0.5)
        self.assertEqual(selected_result["change_24h"], -4.25)

    def test_native_buy_uses_router_quote_and_two_percent_slippage(self):
        pair = SimpleNamespace(
            chain="ethereum",
            token_address="0x" + "1" * 40,
            quote_address=None,
            buy_amount="0.01",
            sell_amount_percent=100,
        )
        token = MagicMock()
        token.functions.decimals.return_value.call.return_value = 18
        context = MagicMock()
        context.account.address = "0x" + "2" * 40
        context.wrapped_native_address = "0x" + "3" * 40
        context.router.functions.getAmountsOut.return_value.call.return_value = [10**16, 1000]
        with (
            patch("bot.swap_executor._chain_context", return_value=context),
            patch("bot.swap_executor._token_contract", return_value=token),
            patch("bot.swap_executor._send_transaction", return_value="0xswap") as send,
        ):
            self.assertEqual(execute_swap(pair, "buy"), "0xswap")

        swap_function = context.router.functions.swapExactETHForTokens
        self.assertEqual(swap_function.call_args.args[0], 980)
        swap = swap_function.return_value
        send.assert_called_once_with(context, swap, value=10**16)

    def test_websocket_connects_without_api_token(self):
        with TestClient(main.app) as client:
            with client.websocket_connect("/ws"):
                pass

    def test_manual_trade_requires_api_token_and_uses_existing_executor(self):
        pair_id = "eth_12345678_87654321"
        main.active_pairs[pair_id] = {
            "network": "eth",
            "token_address": "0x" + "1" * 40,
            "quote_token_address": "0x" + "2" * 40,
            "token_symbol": "TEST",
        }
        try:
            with patch.dict("os.environ", {"TRADING_API_TOKEN": "test-secret"}):
                with TestClient(main.app) as client:
                    unauthorized = client.post(
                        f"/api/pairs/{pair_id}/trade",
                        json={"side": "buy", "buy_amount": 0.01},
                    )
                    self.assertEqual(unauthorized.status_code, 401)

                    with patch("bot.main.execute_swap", return_value="0x" + "a" * 64) as execute:
                        response = client.post(
                            f"/api/pairs/{pair_id}/trade",
                            headers={"X-Trading-API-Token": "test-secret"},
                            json={"side": "buy", "buy_amount": 0.01},
                        )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "submitted")
                    executor_pair = execute.call_args.args[0]
                    self.assertEqual(executor_pair.chain, "ethereum")
                    self.assertEqual(executor_pair.quote_address, "0x" + "2" * 40)
                    self.assertEqual(executor_pair.buy_amount, 0.01)
        finally:
            main.active_pairs.pop(pair_id, None)

    def test_ohlcv_fetch_returns_ascending_15_minute_candles(self):
        response = MagicMock()
        response.json.return_value = {
            "data": {"attributes": {"ohlcv_list": [
                [1200, "2", "3", "1", "2.5", "20"],
                [300, "1", "2", "0.5", "1.5", "10"],
            ]}}
        }
        with patch.object(main.requests, "get", return_value=response) as get:
            candles = main.fetch_geckoterminal_ohlcv("eth", "0xpool")

        self.assertEqual([candle["timestamp"] for candle in candles], [300, 1200])
        self.assertEqual(candles[0]["close"], 1.5)
        self.assertIn("aggregate=15&limit=96", get.call_args.args[0])

    async def test_token_details_endpoint_returns_metadata_and_24h_candles(self):
        candles = [{"timestamp": 300, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]
        details = {
            "token_name": "Test",
            "token_symbol": "TEST",
            "price_usd": 1.5,
            "logo_url": "https://example.test/logo.png",
            "website": "https://example.test",
            "twitter": "https://x.com/test",
            "pool_address": "0xpool",
            "dex_pairs_list": [],
            "change_24h": 2.5,
        }
        with patch.object(
            main.asyncio,
            "to_thread",
            new_callable=AsyncMock,
            side_effect=[details, candles],
        ) as to_thread:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/tokens/eth/0xtoken")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name"], "Test")
        self.assertEqual(payload["ohlcv_24h"], candles)
        self.assertEqual(to_thread.await_count, 2)


if __name__ == "__main__":
    unittest.main()
