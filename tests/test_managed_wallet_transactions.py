import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from web3 import Web3

import main


class ManagedWalletTransactionTests(unittest.TestCase):
    def setUp(self):
        self.web3 = Web3()
        self.account = "0x0000000000000000000000000000000000000003"
        self.token = "0x0000000000000000000000000000000000000004"
        self.network = {
            "router": "0x0000000000000000000000000000000000000010",
            "v3_router": "0x0000000000000000000000000000000000000011",
            "universal_router": "0x0000000000000000000000000000000000000012",
            "wrapped_native": "0x0000000000000000000000000000000000000020",
            "permit2": main.PERMIT2_ADDRESS,
        }

    def make_transaction(self, address, signature, types, values, value="0"):
        selector = Web3.keccak(text=signature)[:4]
        encoded = self.web3.codec.encode(types, values)
        return main.ManagedWalletTransaction(
            network="robinhood",
            to=address,
            data="0x" + (selector + encoded).hex(),
            value=value,
            confirmed=True,
        )

    def test_accepts_confirmed_native_v2_swap_to_selected_wallet(self):
        tx = self.make_transaction(
            self.network["router"],
            "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
            ["uint256", "address[]", "address", "uint256"],
            [
                100,
                [self.network["wrapped_native"], self.token],
                self.account,
                int(time.time()) + 300,
            ],
            value="250",
        )

        validated = main.validate_managed_wallet_transaction(
            self.web3, self.network, self.account, tx
        )

        self.assertEqual(validated["value"], 250)
        self.assertEqual(validated["to"].lower(), self.network["router"].lower())

    def test_rejects_swap_with_different_recipient(self):
        tx = self.make_transaction(
            self.network["router"],
            "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
            ["uint256", "address[]", "address", "uint256"],
            [
                100,
                [self.network["wrapped_native"], self.token],
                "0x0000000000000000000000000000000000000005",
                int(time.time()) + 300,
            ],
            value="250",
        )

        with self.assertRaisesRegex(ValueError, "odbiorcy"):
            main.validate_managed_wallet_transaction(
                self.web3, self.network, self.account, tx
            )

    def test_limits_token_approval_to_approved_spender_and_amount(self):
        tx = self.make_transaction(
            self.token,
            "approve(address,uint256)",
            ["address", "uint256"],
            [self.network["router"], 1000],
        )

        self.assertEqual(
            main.validate_managed_wallet_transaction(
                self.web3, self.network, self.account, tx
            )["value"],
            0,
        )

        excessive_approval = self.make_transaction(
            self.token,
            "approve(address,uint256)",
            ["address", "uint256"],
            [self.network["router"], 2**160],
        )
        with self.assertRaisesRegex(ValueError, "autoryzacja"):
            main.validate_managed_wallet_transaction(
                self.web3, self.network, self.account, excessive_approval
            )

    def test_accepts_supported_uniswap_v3_exact_input_swap(self):
        tx = self.make_transaction(
            self.network["v3_router"],
            "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(
                self.token,
                self.network["wrapped_native"],
                3000,
                self.account,
                1000,
                100,
                0,
            )],
        )

        main.validate_managed_wallet_transaction(
            self.web3, self.network, self.account, tx
        )

    def test_accepts_restricted_uniswap_v4_swap_actions(self):
        zero_address = main.NATIVE_TOKEN_ADDRESS
        pool_key = (
            zero_address,
            self.token,
            3000,
            60,
            "0x0000000000000000000000000000000000000000",
        )
        swap_params = self.web3.codec.encode(
            ["((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)"],
            [(pool_key, True, 1000, 900, 0, b"")],
        )
        settle_params = self.web3.codec.encode(
            ["address", "uint256"], [zero_address, 1000]
        )
        take_params = self.web3.codec.encode(
            ["address", "uint256"], [self.token, 900]
        )
        action_input = self.web3.codec.encode(
            ["bytes", "bytes[]"],
            [b"\x06\x0c\x0f", [swap_params, settle_params, take_params]],
        )
        execute_data = self.web3.codec.encode(
            ["bytes", "bytes[]", "uint256"],
            [b"\x10", [action_input], int(time.time()) + 300],
        )
        tx = main.ManagedWalletTransaction(
            network="robinhood",
            to=self.network["universal_router"],
            data="0x" + (
                Web3.keccak(text="execute(bytes,bytes[],uint256)")[:4]
                + execute_data
            ).hex(),
            value="1000",
            confirmed=True,
        )

        validated = main.validate_managed_wallet_transaction(
            self.web3, self.network, self.account, tx
        )

        self.assertEqual(validated["value"], 1000)

    def test_v4_swap_preflight_finds_erc20_input_and_router_spender(self):
        pool_key = (
            self.token,
            self.network["wrapped_native"],
            3000,
            60,
            "0x0000000000000000000000000000000000000000",
        )
        swap_params = self.web3.codec.encode(
            ["((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)"],
            [(pool_key, True, 2500, 2000, 0, b"")],
        )
        settle_params = self.web3.codec.encode(
            ["address", "uint256"], [self.token, 2500]
        )
        take_params = self.web3.codec.encode(
            ["address", "uint256"], [self.network["wrapped_native"], 2000]
        )
        action_input = self.web3.codec.encode(
            ["bytes", "bytes[]"],
            [b"\x06\x0c\x0f", [swap_params, settle_params, take_params]],
        )
        execute_data = self.web3.codec.encode(
            ["bytes", "bytes[]", "uint256"],
            [b"\x10", [action_input], int(time.time()) + 300],
        )
        tx = main.ManagedWalletTransaction(
            network="robinhood",
            to=self.network["universal_router"],
            data="0x" + (
                Web3.keccak(text="execute(bytes,bytes[],uint256)")[:4]
                + execute_data
            ).hex(),
            value="0",
            confirmed=True,
        )
        validated = main.validate_managed_wallet_transaction(
            self.web3, self.network, self.account, tx
        )

        requirement = main.managed_wallet_input_requirement(
            self.web3, self.network, validated
        )

        self.assertEqual(
            requirement,
            (self.token.lower(), 2500, self.network["universal_router"]),
        )

    def test_v4_native_input_does_not_require_token_allowance(self):
        pool_key = (
            main.NATIVE_TOKEN_ADDRESS,
            self.token,
            3000,
            60,
            "0x0000000000000000000000000000000000000000",
        )
        swap_params = self.web3.codec.encode(
            ["((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)"],
            [(pool_key, True, 2500, 2000, 0, b"")],
        )
        settle_params = self.web3.codec.encode(
            ["address", "uint256"], [main.NATIVE_TOKEN_ADDRESS, 2500]
        )
        take_params = self.web3.codec.encode(
            ["address", "uint256"], [self.token, 2000]
        )
        action_input = self.web3.codec.encode(
            ["bytes", "bytes[]"],
            [b"\x06\x0c\x0f", [swap_params, settle_params, take_params]],
        )
        execute_data = self.web3.codec.encode(
            ["bytes", "bytes[]", "uint256"],
            [b"\x10", [action_input], int(time.time()) + 300],
        )
        tx = main.ManagedWalletTransaction(
            network="robinhood",
            to=self.network["universal_router"],
            data="0x" + (
                Web3.keccak(text="execute(bytes,bytes[],uint256)")[:4]
                + execute_data
            ).hex(),
            value="2500",
            confirmed=True,
        )
        validated = main.validate_managed_wallet_transaction(
            self.web3, self.network, self.account, tx
        )

        requirement = main.managed_wallet_input_requirement(
            self.web3, self.network, validated
        )

        self.assertIsNone(requirement)

    def test_transaction_fees_allow_base_fee_headroom(self):
        web3 = Mock()
        web3.eth.get_block.return_value = {"baseFeePerGas": 20_118_000}
        web3.eth.max_priority_fee = 2_000_000

        fees = main.transaction_fee_fields(web3)

        self.assertEqual(fees["type"], 2)
        self.assertEqual(fees["maxPriorityFeePerGas"], 2_000_000)
        self.assertEqual(fees["maxFeePerGas"], 42_236_000)

    def test_transaction_fees_use_legacy_gas_price_without_base_fee(self):
        web3 = Mock()
        web3.eth.get_block.return_value = {}
        web3.eth.gas_price = 7

        self.assertEqual(main.transaction_fee_fields(web3), {"gasPrice": 7})

    def test_holder_fetch_follows_blockscout_pagination(self):
        token_address = "0x0000000000000000000000000000000000000001"
        first_holder = "0x0000000000000000000000000000000000000002"
        second_holder = "0x0000000000000000000000000000000000000003"

        def response(payload):
            result = Mock()
            result.json.return_value = payload
            return result

        responses = [
            response({"decimals": "18", "symbol": "TEST"}),
            response({
                "items": [{"address": {"hash": first_holder}, "value": "100"}],
                "next_page_params": {"block_number": 10, "index": 1},
            }),
            response({
                "items": [{"address": {"hash": second_holder}, "value": "200"}],
                "next_page_params": None,
            }),
        ]
        with patch("main.requests.get", side_effect=responses) as get:
            result = main.fetch_robinhood_token_holders(token_address, limit=2)

        self.assertEqual([holder["address"] for holder in result["holders"]], [
            Web3.to_checksum_address(first_holder),
            Web3.to_checksum_address(second_holder),
        ])
        self.assertEqual([holder["balance_raw"] for holder in result["holders"]], ["100", "200"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(get.call_count, 3)

    def test_holder_fetch_returns_cursor_for_the_next_batch(self):
        token_address = "0x0000000000000000000000000000000000000001"
        holder_addresses = [
            "0x0000000000000000000000000000000000000002",
            "0x0000000000000000000000000000000000000003",
        ]

        def response(payload):
            result = Mock()
            result.json.return_value = payload
            return result

        cursor = {"block_number": 100, "index": 1, "items_count": 100}
        responses = [
            response({"decimals": "18", "symbol": "TEST"}),
            response({
                "items": [{"address": {"hash": holder_addresses[0]}, "value": "1"}],
                "next_page_params": cursor,
            }),
            response({"decimals": "18", "symbol": "TEST"}),
            response({
                "items": [{"address": {"hash": holder_addresses[1]}, "value": "2"}],
                "next_page_params": None,
            }),
        ]
        with patch("main.requests.get", side_effect=responses) as get:
            first_batch = main.fetch_robinhood_token_holders(token_address, limit=1)
            second_batch = main.fetch_robinhood_token_holders(
                token_address, limit=1, cursor=first_batch["next_cursor"]
            )

        self.assertEqual(first_batch["next_cursor"], cursor)
        self.assertEqual(second_batch["holders"][0]["address"], Web3.to_checksum_address(holder_addresses[1]))
        self.assertEqual(get.call_args_list[3].kwargs["params"], cursor)

    def test_holder_distribution_preflight_checks_manual_amount_against_token_decimals(self):
        wallet_id = "holder-test-wallet"
        account = main.Account.create()
        token = "0x0000000000000000000000000000000000000004"
        fake_web3 = Mock()
        fake_web3.is_connected.return_value = True
        fake_web3.eth.chain_id = main.NETWORK_CONFIG["robinhood"]["chain_id"]
        fake_web3.eth.get_code.return_value = b"\x01"
        fake_contract = Mock()
        fake_contract.functions.decimals.return_value.call.return_value = 6
        fake_contract.functions.symbol.return_value.call.return_value = "TEST"
        fake_contract.functions.balanceOf.return_value.call.return_value = 5_000_000
        fake_web3.eth.contract.return_value = fake_contract
        web3_factory = Mock(return_value=fake_web3)
        web3_factory.HTTPProvider = Mock(return_value=object())
        web3_factory.to_checksum_address = Web3.to_checksum_address

        main.wallet_store[wallet_id] = {"address": account.address, "network": "robinhood"}
        main.unlocked_wallet_keys[wallet_id] = account.key.hex()
        try:
            with (
                patch("main.Web3", web3_factory),
                patch("main.secrets.randbelow", side_effect=[0, 0, 0, 500_000]),
            ):
                result = main.holder_distribution_preflight(
                    wallet_id, token, Decimal("1.25"), 2
                )
                randomized = main.holder_distribution_preflight(
                    wallet_id, token, Decimal("0.25"), 2, Decimal("0.75")
                )
                with self.assertRaisesRegex(ValueError, "precyzję tokena"):
                    main.holder_distribution_preflight(
                        wallet_id, token, Decimal("0.0000001"), 1
                    )
        finally:
            main.wallet_store.pop(wallet_id, None)
            main.unlocked_wallet_keys.pop(wallet_id, None)

        self.assertEqual(result, {
            "decimals": 6,
            "symbol": "TEST",
            "amount_raw": 1_250_000,
            "amount_raws": [1_250_000, 1_250_000],
        })
        self.assertEqual(randomized["amount_raws"], [250_000, 750_000])

    def test_transaction_cost_details_sums_native_value_and_receipt_fee(self):
        costs = main.transaction_cost_details(
            {"value": 2 * 10**18},
            {"gasUsed": 21_000, "effectiveGasPrice": 3 * 10**9},
            "ETH",
        )

        self.assertEqual(costs, {
            "value": "2",
            "transaction_fee": "0.000063",
            "gas_price": "3 Gwei",
            "total": "2.000063",
            "symbol": "ETH",
        })
        self.assertIn(
            "Razem (Value + Fee): 2.000063 ETH",
            main.format_trade_cost_log({"transaction_cost": costs}),
        )

    def test_transaction_explorer_links_use_network_explorer_and_require_hash(self):
        tx_hash = "0x" + "ab" * 32

        self.assertEqual(
            main.transaction_explorer_url("robinhood", tx_hash),
            f"https://robinhoodchain.blockscout.com/tx/{tx_hash}",
        )
        self.assertEqual(
            main.transaction_explorer_url("eth", tx_hash),
            f"https://etherscan.io/tx/{tx_hash}",
        )
        self.assertIsNone(main.transaction_explorer_url("robinhood", "0x1234"))
        self.assertEqual(
            main.transaction_hash_from_error(
                RuntimeError(f"Transaction {tx_hash} was reverted")
            ),
            tx_hash,
        )
        self.assertIsNone(main.transaction_hash_from_error(RuntimeError("RPC offline")))

    def test_blockscout_holder_endpoint_accepts_unmonitored_contract_address(self):
        token = "0x0000000000000000000000000000000000000005"
        result = {
            "token_address": token,
            "symbol": "TEST",
            "decimals": 18,
            "holders": [],
            "fetched_at": time.time(),
        }
        main.robinhood_holder_cache.pop(f"{token.lower()}:{{}}", None)
        with patch("main.fetch_robinhood_token_holders", return_value=result) as fetch:
            response = asyncio.run(main.list_robinhood_token_holders(token, limit=500))
        fetch.assert_called_once_with(token, 500, None)
        self.assertEqual(response["token_address"], token)

    def test_holder_distribution_accepts_more_than_500_recipients(self):
        wallet_id = "unlimited-holder-test-wallet"
        account = main.Account.create()
        recipients = [f"0x{number:040x}" for number in range(1000, 1501)]
        request = main.Request({
            "type": "http",
            "scheme": "https",
            "path": "/api/wallets/test/holder-distributions",
            "raw_path": b"/api/wallets/test/holder-distributions",
            "query_string": b"",
            "server": ("testserver", 443),
            "root_path": "",
            "http_version": "1.1",
            "client": ("127.0.0.1", 1234),
            "headers": [],
        })
        distribution = main.HolderDistributionStart(
            token_address="0x0000000000000000000000000000000000000005",
            recipients=recipients,
            amount_min="1",
            amount_max="2",
            confirmed=True,
        )

        async def start_distribution():
            with patch(
                "main.holder_distribution_preflight",
                return_value={
                    "decimals": 18,
                    "symbol": "TEST",
                    "amount_raw": 10**18,
                    "amount_raws": [10**18] * 500 + [2 * 10**18],
                },
            ), patch("main.asyncio.create_task", side_effect=lambda coroutine: coroutine.close()):
                return await main.start_holder_distribution(
                    wallet_id, distribution, request
                )

        main.wallet_store[wallet_id] = {"address": account.address, "network": "robinhood"}
        main.unlocked_wallet_keys[wallet_id] = account.key.hex()
        try:
            result = asyncio.run(start_distribution())
        finally:
            main.wallet_store.pop(wallet_id, None)
            main.unlocked_wallet_keys.pop(wallet_id, None)
            for job_id, job in list(main.holder_distribution_jobs.items()):
                if job["wallet_id"] == wallet_id:
                    main.holder_distribution_jobs.pop(job_id, None)

        self.assertEqual(result["recipient_count"], 501)
        self.assertEqual(result["amount_min"], "1")
        self.assertEqual(result["amount_max"], "2")
        self.assertEqual(result["total_amount"], "502")

    def test_confirmed_managed_wallet_swap_returns_canonical_hash_and_records_history(self):
        wallet_id = "manual-swap-history-test-wallet"
        pair_id = "manual-swap-history-pair"
        account = main.Account.create()
        token = "0x0000000000000000000000000000000000000004"
        quote_token = "0x0000000000000000000000000000000000000005"
        tx_hash = bytes.fromhex("ab" * 32)
        transaction = main.ManagedWalletTransaction(
            network="robinhood",
            to="0x0000000000000000000000000000000000000010",
            data="0x12345678",
            value="0",
            confirmed=True,
            trade=main.ManagedWalletTradeHistory(
                pair_id=pair_id,
                trade_type="buy",
                token_address=token,
                token_symbol="TEST",
                token_amount="5",
                quote_token_address=quote_token,
                quote_symbol="ETH",
                quote_amount="0.01",
            ),
        )
        request = main.Request({
            "type": "http",
            "scheme": "https",
            "path": "/api/wallets/test/transaction",
            "raw_path": b"/api/wallets/test/transaction",
            "query_string": b"",
            "server": ("testserver", 443),
            "root_path": "",
            "http_version": "1.1",
            "client": ("127.0.0.1", 1234),
            "headers": [],
        })
        fake_web3 = Mock()
        fake_web3.is_connected.return_value = True
        fake_web3.eth.chain_id = main.NETWORK_CONFIG["robinhood"]["chain_id"]
        fake_web3.eth.estimate_gas.return_value = 100_000
        fake_web3.eth.get_transaction_count.return_value = 7
        fake_web3.eth.send_raw_transaction.return_value = tx_hash
        receipt = Mock()
        receipt.status = 1
        receipt.get.side_effect = lambda key, default=None: {
            "transactionHash": tx_hash
        }.get(key, default)
        fake_web3.eth.wait_for_transaction_receipt.return_value = receipt
        web3_factory = Mock(return_value=fake_web3)
        web3_factory.HTTPProvider = Mock(return_value=object())
        web3_factory.to_checksum_address = Web3.to_checksum_address
        web3_factory.is_address = Web3.is_address
        web3_factory.to_hex = Web3.to_hex
        pair_snapshot = {"network": "robinhood", "token_address": token}
        main.wallet_store[wallet_id] = {"address": account.address, "network": "robinhood"}
        main.unlocked_wallet_keys[wallet_id] = account.key.hex()
        main.active_pairs[pair_id] = pair_snapshot
        try:
            with (
                patch("main.Web3", web3_factory),
                patch("main.validate_managed_wallet_transaction", return_value={"value": 0}),
                patch("main.managed_wallet_input_requirement", return_value=None),
                patch("main.ensure_managed_wallet_input_allowance"),
                patch("main.transaction_fee_fields", return_value={}),
                patch("main.Account.sign_transaction", return_value=Mock(raw_transaction=b"signed")),
                patch("main.record_wallet_trade") as record_trade,
            ):
                result = main.send_managed_wallet_transaction(wallet_id, transaction, request)
        finally:
            main.wallet_store.pop(wallet_id, None)
            main.unlocked_wallet_keys.pop(wallet_id, None)
            main.active_pairs.pop(pair_id, None)
            main.active_pairs.pop(pair_id, None)

        expected_hash = "0x" + tx_hash.hex()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["tx_hash"], expected_hash)
        self.assertEqual(result["hash"], expected_hash)
        record_trade.assert_called_once()
        saved_trade = record_trade.call_args.args[1]
        self.assertEqual(saved_trade["type"], "buy")
        self.assertEqual(saved_trade["source"], "manual_swap")
        self.assertEqual(saved_trade["tx_hash"], expected_hash)


if __name__ == "__main__":
    unittest.main()
