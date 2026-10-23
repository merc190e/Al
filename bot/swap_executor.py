import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from web3 import Web3

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SUPPORTED_CHAINS = {
    "ethereum": 1,
    "bsc": 56,
    "arbitrum": 42161,
    "polygon": 137,
    "robinhood": None,
}
SLIPPAGE_BPS = 200
DEADLINE_SECONDS = 300

ROUTER_ABI = [
    {
        "inputs": [],
        "name": "WETH",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForETH",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class ChainContext:
    web3: Web3
    account: Any
    router: Any
    wrapped_native_address: str


READ_ONLY_CONTEXTS: dict[str, ChainContext] = {}


def _chain_context(chain: str) -> ChainContext:
    normalized_chain = chain.lower()
    private_key = os.getenv("TRADING_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError("Live trading is disabled; no transaction signer is configured.")
    if re.fullmatch(r"[a-fA-F0-9]{64}", private_key):
        private_key = f"0x{private_key}"
    elif not re.fullmatch(r"0x[a-fA-F0-9]{64}", private_key):
        raise ValueError("TRADING_PRIVATE_KEY must be a 32-byte hexadecimal private key.")

    context = _read_only_chain_context(normalized_chain)
    return ChainContext(
        web3=context.web3,
        account=context.web3.eth.account.from_key(private_key),
        router=context.router,
        wrapped_native_address=context.wrapped_native_address,
    )


def _read_only_chain_context(chain: str) -> ChainContext:
    normalized_chain = chain.lower()
    if normalized_chain not in SUPPORTED_CHAINS:
        raise ValueError(f"Unsupported chain: {chain}")
    cached_context = READ_ONLY_CONTEXTS.get(normalized_chain)
    if cached_context:
        return cached_context
    expected_chain_id = SUPPORTED_CHAINS[normalized_chain]
    if expected_chain_id is None:
        configured_chain_id = os.getenv("TRADING_CHAIN_ID_ROBINHOOD", "").strip()
        if not configured_chain_id.isdecimal() or int(configured_chain_id) <= 0:
            raise RuntimeError("Set TRADING_CHAIN_ID_ROBINHOOD to the Robinhood Chain ID.")
        expected_chain_id = int(configured_chain_id)

    rpc_url = os.getenv(f"TRADING_RPC_URL_{normalized_chain.upper()}")
    router_address = os.getenv(f"DEX_ROUTER_{normalized_chain.upper()}")
    if not rpc_url or not router_address:
        raise RuntimeError(
            f"Price lookup is not configured for {normalized_chain}; set its RPC URL and DEX router."
        )

    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    if not web3.is_connected():
        raise RuntimeError(f"Could not connect to the configured {normalized_chain} RPC.")
    actual_chain_id = web3.eth.chain_id
    if actual_chain_id != expected_chain_id:
        raise RuntimeError(
            f"RPC chain ID mismatch for {normalized_chain}: expected {expected_chain_id}, "
            f"received {actual_chain_id}."
        )
    if not Web3.is_address(router_address):
        raise ValueError(f"Configured DEX router address for {normalized_chain} is invalid.")

    router_address = Web3.to_checksum_address(router_address)
    if not web3.eth.get_code(router_address):
        raise RuntimeError(f"No contract code found at the configured {normalized_chain} DEX router.")
    router = web3.eth.contract(address=router_address, abi=ROUTER_ABI)
    wrapped_native_address = router.functions.WETH().call()
    if not Web3.is_address(wrapped_native_address):
        raise RuntimeError(f"The configured {normalized_chain} router returned an invalid WETH address.")
    context = ChainContext(
        web3=web3,
        account=None,
        router=router,
        wrapped_native_address=Web3.to_checksum_address(wrapped_native_address),
    )
    READ_ONLY_CONTEXTS[normalized_chain] = context
    return context


def _token_contract(context: ChainContext, address: str) -> Any:
    if not Web3.is_address(address) or int(address, 16) == 0:
        raise ValueError(f"Invalid ERC-20 contract address: {address}")
    return context.web3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=ERC20_ABI,
    )


def _token_decimals(token: Any) -> int:
    decimals = token.functions.decimals().call()
    if not isinstance(decimals, int) or not 0 <= decimals <= 36:
        raise ValueError("Token returned an unsupported decimals value.")
    return decimals


def _to_base_units(amount: Decimal | float, decimals: int) -> int:
    decimal_amount = Decimal(str(amount))
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        raise ValueError("Amount must be a finite positive number.")
    scaled_amount = decimal_amount.scaleb(decimals)
    integral_amount = scaled_amount.to_integral_value()
    if scaled_amount != integral_amount:
        raise ValueError(f"Amount has more than {decimals} decimal places.")
    base_units = int(integral_amount)
    if base_units >= 2**256:
        raise ValueError("Amount exceeds the maximum value supported by the router.")
    return base_units


def _send_transaction(context: ChainContext, function: Any, value: int = 0) -> str:
    web3 = context.web3
    tx = function.build_transaction(
        {
            "from": context.account.address,
            "nonce": web3.eth.get_transaction_count(context.account.address, "pending"),
            "chainId": web3.eth.chain_id,
            "value": value,
            "gasPrice": web3.eth.gas_price,
        }
    )
    tx["gas"] = web3.eth.estimate_gas(tx) * 120 // 100
    signed = context.account.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    return web3.to_hex(tx_hash)


def _approve_if_needed(context: ChainContext, token: Any, amount: int) -> None:
    current_allowance = token.functions.allowance(
        context.account.address,
        context.router.address,
    ).call()
    if current_allowance >= amount:
        return

    tx_hash = _send_transaction(
        context,
        token.functions.approve(context.router.address, amount),
    )
    receipt = context.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"Token approval transaction reverted: {tx_hash}")


def execute_swap(pair: Any, side: str) -> str:
    if side not in {"buy", "sell"}:
        raise ValueError(f"Unsupported swap side: {side}")

    if side == "buy" and (pair.buy_amount is None or not Decimal(str(pair.buy_amount)) > 0):
        raise ValueError("Set a positive per-pair buy amount before enabling a buy trigger.")

    context = _chain_context(pair.chain)
    web3 = context.web3
    token_address = Web3.to_checksum_address(pair.token_address)
    quote_address = (
        Web3.to_checksum_address(pair.quote_address)
        if pair.quote_address
        else None
    )
    if quote_address and quote_address == token_address:
        raise ValueError("Quote token and traded token must be different addresses.")

    traded_token = _token_contract(context, token_address)
    token_decimals = _token_decimals(traded_token)
    native_quote = quote_address is None
    approval_token = None

    if side == "buy":
        if native_quote:
            amount_in = _to_base_units(pair.buy_amount, 18)
            path = [context.wrapped_native_address, token_address]
        else:
            quote_token = _token_contract(context, quote_address)
            quote_decimals = _token_decimals(quote_token)
            amount_in = _to_base_units(pair.buy_amount, quote_decimals)
            path = [quote_address, token_address]
            approval_token = quote_token
    else:
        balance = traded_token.functions.balanceOf(context.account.address).call()
        amount_in = int(balance * Decimal(str(pair.sell_amount_percent)) // Decimal("100"))
        if amount_in <= 0:
            raise ValueError("Token balance is too small to sell the configured percentage.")
        path = (
            [token_address, context.wrapped_native_address]
            if native_quote
            else [token_address, quote_address]
        )
        approval_token = traded_token

    quoted_amounts = context.router.functions.getAmountsOut(amount_in, path).call()
    quoted_output = quoted_amounts[-1]
    minimum_output = quoted_output * (10_000 - SLIPPAGE_BPS) // 10_000
    if quoted_output <= 0 or minimum_output <= 0:
        raise RuntimeError("Router returned an unusable output quote; swap was not submitted.")
    if approval_token is not None:
        _approve_if_needed(context, approval_token, amount_in)

    deadline = int(time.time()) + DEADLINE_SECONDS
    if side == "buy" and native_quote:
        swap = context.router.functions.swapExactETHForTokens(
            minimum_output,
            path,
            context.account.address,
            deadline,
        )
        return _send_transaction(context, swap, value=amount_in)
    if side == "sell" and native_quote:
        swap = context.router.functions.swapExactTokensForETH(
            amount_in,
            minimum_output,
            path,
            context.account.address,
            deadline,
        )
        return _send_transaction(context, swap)

    swap = context.router.functions.swapExactTokensForTokens(
        amount_in,
        minimum_output,
        path,
        context.account.address,
        deadline,
    )
    return _send_transaction(context, swap)
