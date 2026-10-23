import unittest
from unittest.mock import patch

from BotAl.gecko_sl_tp_bot import GeckoSLTPBot


def make_bot(token_address="0xabc"):
    return GeckoSLTPBot(
        network="eth",
        pool_address="0xpool",
        token_address=token_address,
        entry_price=100.0,
        stop_loss_pct=5.0,
        take_profit_pct=10.0,
    )


def mock_pool_response(base_price, quote_price):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "attributes": {
                        "base_token_price_usd": str(base_price),
                        "quote_token_price_usd": str(quote_price),
                    },
                    "relationships": {
                        "base_token": {"data": {"id": "eth_0xbase"}},
                        "quote_token": {"data": {"id": "eth_0xabc"}},
                    },
                }
            }

    def get(*args, **kwargs):
        if kwargs["timeout"] != 10:
            raise AssertionError("Expected a bounded API timeout.")
        return Response()

    return get


class GeckoSLTPBotTests(unittest.TestCase):
    def test_get_current_price_uses_position_token_when_quote_token(self):
        with patch(
            "BotAl.gecko_sl_tp_bot.requests.get",
            side_effect=mock_pool_response(2000.0, 100.0),
        ):
            self.assertEqual(make_bot().get_current_price(), 100.0)

    def test_get_current_price_uses_position_token_when_base_token(self):
        with patch(
            "BotAl.gecko_sl_tp_bot.requests.get",
            side_effect=mock_pool_response(2000.0, 100.0),
        ):
            self.assertEqual(make_bot("0xbase").get_current_price(), 2000.0)

    def test_get_trigger_reason_checks_both_thresholds(self):
        bot = make_bot()

        self.assertEqual(bot.get_trigger_reason(95.0), "STOP-LOSS")
        self.assertEqual(bot.get_trigger_reason(110.0), "TAKE-PROFIT")
        self.assertIsNone(bot.get_trigger_reason(100.0))

    def test_invalid_position_parameters_are_rejected(self):
        parameters = {
            "network": "eth",
            "pool_address": "0xpool",
            "entry_price": 100.0,
            "stop_loss_pct": 5.0,
            "take_profit_pct": 10.0,
        }
        invalid_values = (
            {"entry_price": 0.0},
            {"stop_loss_pct": 100.0},
            {"take_profit_pct": 0.0},
            {"pool_address": "pool/other"},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                invalid_parameters = parameters.copy()
                invalid_parameters.update(overrides)
                with self.assertRaises(ValueError):
                    GeckoSLTPBot(**invalid_parameters)


if __name__ == "__main__":
    unittest.main()
