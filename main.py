import asyncio
import copy
import json
import ipaddress
import logging
import math
import os
import re
import requests
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from eth_account import Account
from pydantic import BaseModel, Field, SecretStr
from web3 import Web3

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# --- MAPOWANIE SIEĆ -> GECKOTERMINAL SLUG & RPC ---
NETWORK_CONFIG = {
    "robinhood": {
        "slugs": ["robinhood"],
        "dex_chain_id": "robinhood",
        "chain_id": 4663,
        "rpc": os.getenv("ROBINHOOD_RPC_URL") or (
            f"https://robinhood-mainnet.g.alchemy.com/v2/{os.environ['ALCHEMY_API_KEY']}"
            if os.getenv("ALCHEMY_API_KEY")
            else "https://rpc.mainnet.chain.robinhood.com"
        ),
        "router": "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba",
        "router_type": "uniswap-v2",
        "v3_router": "0xcaf681a66d020601342297493863e78c959e5cb2",
        "v3_quoter": "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
        "v3_factory": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        "v4_pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "v4_quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "universal_router": "0x204faca1764b154221e35c0d20abb3c525710498",
        "wrapped_native": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
    },
    "eth": {
        "slugs": ["eth"],
        "dex_chain_id": "ethereum",
        "chain_id": 1,
        "rpc": "https://rpc.ankr.com/eth",
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "router_type": "uniswap-v2",
    },
    "bsc": {
        "slugs": ["bsc"],
        "dex_chain_id": "bsc",
        "chain_id": 56,
        "rpc": "https://bsc-dataseed.binance.org/",
        "router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "router_type": "pancake-v2",
    },
    "arbitrum": {
        "slugs": ["arbitrum"],
        "dex_chain_id": "arbitrum",
        "chain_id": 42161,
        "rpc": "https://arb1.arbitrum.io/rpc",
        "router": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
        "router_type": "sushi-v2",
    }
}

# --- KONTRAKT BOBER ORAZ TOKENY GLÓWNE ---
BOBER_CONTRACT = "0x8031a70628207ed93d7037ea6b1444912993c715"
NATIVE_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
WALLET_STORE_PATH = Path(
    os.getenv("AL_WALLETS_FILE", Path.home() / ".local" / "share" / "albot" / "wallets.json")
)
wallet_store_lock = threading.RLock()
wallet_store: Dict[str, dict] = {}
unlocked_wallet_keys: Dict[str, str] = {}
market_cache: Dict[tuple, tuple] = {}
robinhood_holder_cache: Dict[str, dict] = {}
holder_distribution_jobs: Dict[str, dict] = {}
holder_distribution_lock = threading.RLock()

class TradingPair(BaseModel):
    id: Optional[str] = None
    network: str = "robinhood"        # Domyślnie Robinhood Chain
    token_address: str               # Kupowany token (np. BOBER)
    token_symbol: Optional[str] = "BOBER"
    token_name: Optional[str] = None
    logo_url: Optional[str] = None
    quote_token_address: str         # Kontrakt płatności (np. ETH / USDT / USDC)
    quote_symbol: str = "ETH"
    trade_amount: float = 0.1        # Kwota w walucie płatności
    target_buy_price: Optional[float] = None
    target_sell_price: Optional[float] = None
    buy_percent: Optional[float] = None
    sell_percent: Optional[float] = None
    entry_price: Optional[float] = None
    amount_held: float = 0.0
    pair_contract_address: Optional[str] = None
    auto_trade_enabled: bool = False
    wallet_id: Optional[str] = None
    max_slippage_percent: float = Field(default=1.0, ge=0.1, le=50)
    auto_buy_mode: str = "single"
    auto_buy_target_quantity: Optional[float] = None
    auto_buy_total_budget: Optional[float] = None
    auto_buy_max_transactions: int = Field(default=10, ge=1, le=100)
    auto_buy_duration_minutes: int = Field(default=60, ge=1, le=43200)
    auto_buy_quantity_duration_minutes: Optional[int] = Field(default=None, ge=1, le=43200)
    auto_buy_schedule: List["BuyScheduleEntry"] = Field(default_factory=list, max_length=100)
    auto_sell_enabled: bool = False
    auto_sell_mode: str = "target_quantity"
    auto_sell_target_quantity: Optional[float] = None
    auto_sell_max_transactions: int = Field(default=10, ge=1, le=100)
    auto_sell_duration_minutes: int = Field(default=60, ge=1, le=43200)
    auto_sell_quantity_duration_minutes: Optional[int] = Field(default=None, ge=1, le=43200)
    auto_sell_schedule: List["SellScheduleEntry"] = Field(default_factory=list, max_length=100)
    schedule_timezone: str = "UTC"


class BuyScheduleEntry(BaseModel):
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quantity: float = Field(gt=0)
    max_quote: float = Field(gt=0)


class SellScheduleEntry(BaseModel):
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quantity: float = Field(gt=0)


TradingPair.model_rebuild()


class WalletCreate(BaseModel):
    name: str
    network: str
    private_key: SecretStr
    vault_password: SecretStr


class HDWalletCreate(BaseModel):
    name: str
    vault_password: SecretStr
    mnemonic: Optional[SecretStr] = None
    words: int = 12
    count: int = Field(default=10, ge=1, le=500)
    start_index: int = Field(default=0, ge=0, le=1_000_000)


class DisperseDeploy(BaseModel):
    wallet_id: str


class DisperseFund(BaseModel):
    wallet_id: str
    recipient_wallet_ids: List[str] = Field(min_length=1, max_length=500)
    amount_each: str
    confirmed: bool = False


class DisperseTokenFund(DisperseFund):
    token_address: str


class HDTradeJob(BaseModel):
    side: str
    token_address: str
    quantity: str
    max_quote: Optional[str] = None
    wallet_from: int = Field(ge=1, le=500)
    wallet_to: int = Field(ge=1, le=500)
    interval_value: float = Field(gt=0, le=100000)
    interval_unit: str = "seconds"
    jitter_percent: float = Field(default=0, ge=0, le=90)
    shuffle: bool = True
    rounds: int = Field(default=1, ge=1, le=20)
    start_time: Optional[str] = None
    timezone: str = "UTC"
    daily: bool = False
    confirmed: bool = False


class DisperseJob(BaseModel):
    wallet_id: str
    asset: str = "eth"
    token_address: Optional[str] = None
    amount_each: str
    wallet_from: int = Field(ge=1, le=500)
    wallet_to: int = Field(ge=1, le=500)
    batch_size: int = Field(default=10, ge=1, le=100)
    interval_value: float = Field(default=0, ge=0, le=100000)
    interval_unit: str = "seconds"
    jitter_percent: float = Field(default=0, ge=0, le=90)
    amount_jitter_percent: float = Field(default=0, ge=0, le=90)
    shuffle: bool = False
    start_time: Optional[str] = None
    timezone: str = "UTC"
    confirmed: bool = False


class WalletUnlock(BaseModel):
    vault_password: SecretStr


class WalletRemove(BaseModel):
    vault_password: SecretStr


class ManagedWalletTradeHistory(BaseModel):
    pair_id: str
    trade_type: str
    token_address: str
    token_symbol: str
    token_amount: str
    quote_token_address: str
    quote_symbol: str
    quote_amount: str


class ManagedWalletTransaction(BaseModel):
    network: str
    to: str
    data: str
    value: str = "0"
    confirmed: bool = False
    trade: Optional[ManagedWalletTradeHistory] = None


class HolderDistributionStart(BaseModel):
    token_address: str
    recipients: List[str]
    amount_per_recipient: Optional[str] = None
    amount_min: Optional[str] = None
    amount_max: Optional[str] = None
    confirmed: bool = False


V2_ROUTER_ABI = [
    {"inputs": [], "name": "WETH", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "address[]"}], "name": "getAmountsOut", "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "address[]"}, {"type": "address"}, {"type": "uint256"}], "name": "swapExactETHForTokensSupportingFeeOnTransferTokens", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "address[]"}, {"type": "address"}, {"type": "uint256"}], "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "address[]"}, {"type": "address"}, {"type": "uint256"}], "name": "swapExactTokensForETHSupportingFeeOnTransferTokens", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]
V3_POOL_FEE_ABI = [
    {"inputs": [], "name": "fee", "outputs": [{"type": "uint24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function"},
]
V3_FACTORY_ABI = [
    {"inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}], "name": "getPool", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]
V3_QUOTER_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "fee", "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"}],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
V3_ROUTER_AUTO_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"}],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {"inputs": [{"type": "bytes[]"}], "name": "multicall", "outputs": [{"type": "bytes[]"}], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "address"}], "name": "unwrapWETH9", "outputs": [], "stateMutability": "payable", "type": "function"},
]
ERC20_TRADE_ABI = [
    {"inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "transfer", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

active_pairs: Dict[str, dict] = {}
trade_logs: List[str] = []
latest_bot_stats = {
    "total_invested": 0.0,
    "total_pnl_usd": 0.0,
    "total_pnl_percent": 0.0,
}
ticker_prices: Dict[str, dict] = {
    "BOBER": {"price": 0.0, "change_24h": 0.0},
    "ETH": {"price": 0.0, "change_24h": 0.0},
    "BNB": {"price": 0.0, "change_24h": 0.0},
    "BTC": {"price": 0.0, "change_24h": 0.0}
}

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()


def load_wallet_store() -> None:
    global wallet_store
    with wallet_store_lock:
        if not WALLET_STORE_PATH.exists():
            wallet_store = {}
            return
        try:
            records = json.loads(WALLET_STORE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logging.exception("Nie można odczytać zaszyfrowanego magazynu portfeli")
            raise RuntimeError("Nie można odczytać magazynu portfeli.") from error
        if not isinstance(records, dict):
            raise RuntimeError("Magazyn portfeli ma nieprawidłowy format.")
        wallet_store = records


def save_wallet_store() -> None:
    WALLET_STORE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(WALLET_STORE_PATH.parent, 0o700)
    temp_path = WALLET_STORE_PATH.with_suffix(".tmp")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as wallet_file:
            json.dump(wallet_store, wallet_file, separators=(",", ":"))
            wallet_file.flush()
            os.fsync(wallet_file.fileno())
        os.replace(temp_path, WALLET_STORE_PATH)
        os.chmod(WALLET_STORE_PATH, 0o600)
    except OSError:
        logging.exception("Nie można zapisać zaszyfrowanego magazynu portfeli")
        raise


def public_wallet(wallet_id: str, wallet: dict) -> dict:
    return {
        "id": wallet_id,
        "name": wallet["name"],
        "address": wallet["address"],
        "network": wallet["network"],
        "derivation_path": wallet.get("derivation_path"),
        "unlocked": wallet_id in unlocked_wallet_keys,
        "trade_history": wallet.get("trade_history", []),
        "trade_summary": summarize_wallet_trades(wallet.get("trade_history", [])),
    }


def summarize_wallet_trades(trades: list) -> dict:
    summary = {"buy": {}, "sell": {}}
    with localcontext() as decimal_context:
        decimal_context.prec = 1000
        for trade in trades:
            trade_type = trade.get("type")
            if trade_type not in summary:
                continue
            token_symbol = str(trade.get("token_symbol") or "TOKEN")
            quote_symbol = str(trade.get("quote_symbol") or "QUOTE")
            key = "|".join((
                token_symbol,
                quote_symbol,
                str(trade.get("token_address") or "").lower(),
                str(trade.get("quote_token_address") or "").lower(),
            ))
            try:
                token_amount = Decimal(str(trade["token_amount"]))
                quote_amount = Decimal(str(trade["quote_amount"]))
            except (KeyError, InvalidOperation, TypeError):
                logging.warning("Pomijam nieprawidłowy wpis historii transakcji portfela")
                continue
            if not token_amount.is_finite() or not quote_amount.is_finite():
                logging.warning("Pomijam nieprawidłowy wpis historii transakcji portfela")
                continue
            totals = summary[trade_type].setdefault(
                key,
                {
                    "token_symbol": token_symbol,
                    "quote_symbol": quote_symbol,
                    "token_amount": Decimal(0),
                    "quote_amount": Decimal(0),
                },
            )
            totals["token_amount"] += token_amount
            totals["quote_amount"] += quote_amount
    return {
        trade_type: [
            {
                **totals,
                "token_amount": format(totals["token_amount"], "f"),
                "quote_amount": format(totals["quote_amount"], "f"),
            }
            for totals in groups.values()
        ]
        for trade_type, groups in summary.items()
    }


def format_raw_token_amount(raw_amount: int, decimals: int) -> str:
    digits = str(int(raw_amount)).rjust(decimals + 1, "0")
    if decimals == 0:
        return digits
    integer_part = digits[:-decimals]
    fractional_part = digits[-decimals:].rstrip("0")
    return f"{integer_part}.{fractional_part}" if fractional_part else integer_part


def record_wallet_trade(wallet_id: str, trade: dict) -> None:
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        if not wallet:
            logging.error("Nie można zapisać historii: portfel %s nie istnieje", wallet_id)
            return
        history = wallet.setdefault("trade_history", [])
        history.append(trade)
        del history[:-1000]
        try:
            save_wallet_store()
        except OSError:
            logging.exception(
                "Transakcja portfela %s została wykonana, ale nie udało się zapisać jej historii",
                wallet_id,
            )


def wallet_pair_buy_totals(
    wallet_id: str,
    pair_id: str,
    campaign_id: Optional[str] = None,
) -> tuple:
    return wallet_pair_trade_totals(wallet_id, pair_id, "buy", campaign_id)


def wallet_pair_trade_totals(
    wallet_id: str,
    pair_id: str,
    trade_type: str,
    campaign_id: Optional[str] = None,
) -> tuple:
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id, {})
        trades = [
            trade for trade in wallet.get("trade_history", [])
            if trade.get("type") == trade_type and trade.get("pair_id") == pair_id
            and (
                campaign_id is None
                or trade.get("campaign_id") == campaign_id
            )
        ]
    with localcontext() as decimal_context:
        decimal_context.prec = 1000
        token_amount = Decimal(0)
        quote_amount = Decimal(0)
        for trade in trades:
            try:
                token_value = Decimal(str(trade.get("token_amount") or "0"))
                quote_value = Decimal(str(trade.get("quote_amount") or "0"))
            except InvalidOperation:
                logging.warning("Pomijam nieprawidłową transakcję przy obliczaniu postępu kampanii")
                continue
            if token_value.is_finite() and quote_value.is_finite():
                token_amount += token_value
                quote_amount += quote_value
    return len(trades), token_amount, quote_amount


def price_ladder_step(
    start_price: float,
    target_price: float,
    current_price: float,
    completed_transactions: int,
    max_transactions: int,
) -> tuple:
    start = Decimal(str(start_price))
    target = Decimal(str(target_price))
    current = Decimal(str(current_price))
    if (
        not start.is_finite() or start <= 0
        or not target.is_finite() or target <= 0
        or not current.is_finite() or current <= 0
        or max_transactions < 1
    ):
        raise ValueError("Nieprawidłowe dane cenowe dla drabinki DEX.")
    if start == target:
        return None, False, True
    if completed_transactions >= max_transactions:
        return None, False, True
    next_index = completed_transactions + 1
    level = start + (target - start) * Decimal(next_index) / Decimal(max_transactions)
    crossed = current >= level if target > start else current <= level
    return float(level), crossed, False


def price_ladder_trade_due(
    started_at: float,
    current_time: float,
    completed_transactions: int,
    max_transactions: int,
    duration_minutes: int,
    last_trade_at: Optional[float] = None,
) -> bool:
    if (
        not math.isfinite(started_at)
        or not math.isfinite(current_time)
        or max_transactions < 1
        or completed_transactions < 0
        or duration_minutes < 1
        or (last_trade_at is not None and not math.isfinite(last_trade_at))
    ):
        raise ValueError("Nieprawidłowe ustawienia harmonogramu drabinki DEX.")
    if completed_transactions >= max_transactions:
        return False
    slot = 0 if max_transactions == 1 else completed_transactions / (max_transactions - 1)
    due_at = started_at + duration_minutes * 60 * slot
    if last_trade_at is not None and max_transactions > 1:
        interval = duration_minutes * 60 / (max_transactions - 1)
        due_at = max(due_at, last_trade_at + interval)
    return current_time >= due_at


def price_ladder_next_trade_at(
    started_at: float,
    completed_transactions: int,
    max_transactions: int,
    duration_minutes: int,
    last_trade_at: Optional[float] = None,
) -> Optional[float]:
    if completed_transactions >= max_transactions:
        return None
    slot = 0 if max_transactions == 1 else completed_transactions / (max_transactions - 1)
    due_at = started_at + duration_minutes * 60 * slot
    if last_trade_at is not None and max_transactions > 1:
        interval = duration_minutes * 60 / (max_transactions - 1)
        due_at = max(due_at, last_trade_at + interval)
    return due_at


def auto_buy_quantity_tranche(
    remaining_quantity: Decimal,
    completed_transactions: int,
    max_transactions: int,
) -> Decimal:
    remaining_transactions = max_transactions - completed_transactions
    if remaining_transactions <= 0 or remaining_quantity <= 0:
        return Decimal(0)
    return remaining_quantity / remaining_transactions


def next_daily_schedule_at(
    schedule_time: str,
    timezone_name: str,
    after_timestamp: float,
) -> float:
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Nieznana strefa czasowa harmonogramu.") from error
    try:
        local_time = datetime_time.fromisoformat(schedule_time)
    except ValueError as error:
        raise ValueError("Godzina harmonogramu musi być w formacie HH:MM.") from error
    after = datetime.fromtimestamp(after_timestamp, timezone)
    candidate = datetime.combine(after.date(), local_time, tzinfo=timezone)
    if candidate.timestamp() <= after_timestamp:
        candidate = datetime.combine(
            after.date() + timedelta(days=1), local_time, tzinfo=timezone
        )
    return candidate.timestamp()


def initialize_daily_schedule(
    entries: list,
    timezone_name: str,
    now: float,
) -> list:
    return [
        {
            **entry,
            "next_run_at": next_daily_schedule_at(
                entry["time"], timezone_name, now
            ),
        }
        for entry in entries
    ]


def decrypt_wallet_key(wallet_id: str, password: str) -> str:
    wallet = wallet_store[wallet_id]
    key_bytes = Account.decrypt(wallet["keystore"], password)
    account = Account.from_key(key_bytes)
    if account.address.lower() != wallet["address"].lower():
        raise ValueError("Klucz nie pasuje do adresu zapisanych danych.")
    return key_bytes.hex()


def require_secure_wallet_request(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host == "testclient"
    if request.url.scheme != "https" and not is_loopback:
        raise HTTPException(
            status_code=400,
            detail="Dodawanie i odblokowywanie kluczy wymaga HTTPS albo połączenia lokalnego.",
        )


def fetch_robinhood_token_holders(
    token_address: str,
    limit: int = 500,
    cursor: Optional[dict] = None,
) -> dict:
    token_address = Web3.to_checksum_address(token_address)
    limit = max(1, min(int(limit), 500))
    base_url = "https://robinhoodchain.blockscout.com/api/v2"
    try:
        token_response = requests.get(
            f"{base_url}/tokens/{token_address}",
            timeout=15,
            headers={"Accept": "application/json", "User-Agent": "RobinhoodDexDashboard/1.0"},
        )
        token_response.raise_for_status()
        token_info = token_response.json()
        decimals = int(token_info["decimals"])
        symbol = str(token_info.get("symbol") or "TOKEN")
        if not 0 <= decimals <= 255:
            raise ValueError("Nieprawidłowa liczba miejsc dziesiętnych tokena.")

        holders = []
        seen_addresses = set()
        params = dict(cursor) if cursor else {"items_count": min(limit, 100)}
        next_cursor = None
        while len(holders) < limit:
            response = requests.get(
                f"{base_url}/tokens/{token_address}/holders",
                params=params,
                timeout=15,
                headers={"Accept": "application/json", "User-Agent": "RobinhoodDexDashboard/1.0"},
            )
            response.raise_for_status()
            page = response.json()
            items = page.get("items")
            if not isinstance(items, list):
                raise ValueError("Blockscout zwrócił nieprawidłową listę posiadaczy.")
            for item in items:
                address_record = item.get("address_hash") or item.get("address")
                address = (
                    address_record.get("hash")
                    if isinstance(address_record, dict)
                    else address_record
                )
                if not isinstance(address, str) or not Web3.is_address(address):
                    continue
                address = Web3.to_checksum_address(address)
                normalized = address.lower()
                if normalized in seen_addresses or normalized == NATIVE_TOKEN_ADDRESS.lower():
                    continue
                seen_addresses.add(normalized)
                holders.append({
                    "address": address,
                    "balance_raw": str(item.get("value") or "0"),
                    "is_contract": bool(
                        address_record.get("is_contract", False)
                        if isinstance(address_record, dict)
                        else item.get("is_contract", False)
                    ),
                    "rank": len(holders) + 1,
                })
                if len(holders) >= limit:
                    break
            next_page = page.get("next_page_params")
            next_cursor = next_page if isinstance(next_page, dict) else None
            if len(holders) >= limit or not items or not isinstance(next_page, dict):
                break
            params = dict(next_page)
            params["items_count"] = min(100, limit - len(holders))
        if not holders:
            raise ValueError("Blockscout nie zwrócił żadnych adresów posiadaczy.")
    except requests.RequestException as error:
        logging.warning("Nie udało się pobrać posiadaczy z Blockscout: %s", error)
        raise RuntimeError(
            "Nie udało się pobrać posiadaczy z Blockscout. "
            "Usługa może być chwilowo niedostępna albo wymagać weryfikacji przeglądarkowej."
        ) from error
    except (KeyError, TypeError, ValueError) as error:
        logging.exception("Blockscout zwrócił nieprawidłowe dane posiadaczy tokena")
        raise RuntimeError(f"Nieprawidłowa odpowiedź Blockscout: {error}") from error

    result = {
        "token_address": token_address,
        "symbol": symbol,
        "decimals": decimals,
        "holders": holders,
        "next_cursor": next_cursor,
        "fetched_at": time.time(),
    }
    with wallet_store_lock:
        cursor_key = json.dumps(cursor or {}, sort_keys=True, separators=(",", ":"))
        robinhood_holder_cache[f"{token_address.lower()}:{cursor_key}"] = result
    return result


def transaction_fee_fields(web3: Web3) -> dict:
    latest_block = web3.eth.get_block("latest")
    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        return {"gasPrice": web3.eth.gas_price}
    try:
        priority_fee = int(web3.eth.max_priority_fee)
    except Exception:
        logging.warning("RPC nie podało max_priority_fee; używam 10%% opłaty bazowej.")
        priority_fee = max(int(base_fee) // 10, 1)
    priority_fee = max(priority_fee, 1)
    return {
        "type": 2,
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": int(base_fee) * 2 + priority_fee,
    }


def transaction_cost_details(
    transaction: dict,
    receipt,
    native_symbol: str,
) -> dict:
    raw_gas_price = receipt.get("effectiveGasPrice")
    if raw_gas_price is None:
        raw_gas_price = transaction.get(
            "gasPrice", transaction.get("maxFeePerGas")
        )
    if raw_gas_price is None:
        raise RuntimeError("Potwierdzenie transakcji nie zawiera ceny gazu.")
    gas_price = int(raw_gas_price)
    gas_used = int(receipt["gasUsed"])
    if gas_price <= 0 or gas_used < 0:
        raise RuntimeError("Potwierdzenie transakcji zawiera nieprawidłowe dane gazu.")
    gas_cost = gas_used * gas_price
    value = int(transaction.get("value") or 0)
    return {
        "value": format_raw_token_amount(value, 18),
        "transaction_fee": format_raw_token_amount(gas_cost, 18),
        "gas_price": f"{format_raw_token_amount(gas_price, 9)} Gwei",
        "total": format_raw_token_amount(value + gas_cost, 18),
        "symbol": native_symbol,
    }


def format_trade_cost_log(trade: dict) -> str:
    costs = trade["transaction_cost"]
    symbol = costs["symbol"]
    return (
        f"Value: {costs['value']} {symbol} · "
        f"Transaction Fee: {costs['transaction_fee']} {symbol} · "
        f"Gas Price: {costs['gas_price']} · "
        f"Razem (Value + Fee): {costs['total']} {symbol}"
    )


def transaction_explorer_url(network: str, tx_hash: str) -> Optional[str]:
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(tx_hash or "")):
        return None
    explorer_bases = {
        "robinhood": "https://robinhoodchain.blockscout.com",
        "eth": "https://etherscan.io",
        "bsc": "https://bscscan.com",
        "arbitrum": "https://arbiscan.io",
    }
    explorer = explorer_bases.get(network)
    return f"{explorer}/tx/{tx_hash}" if explorer else None


def transaction_explorer_link(network: str, tx_hash: str) -> str:
    url = transaction_explorer_url(network, tx_hash)
    return f" · {url}" if url else ""


def transaction_hash_from_error(error: Exception) -> Optional[str]:
    match = re.search(r"\b0x[0-9a-fA-F]{64}\b", str(error))
    return match.group(0) if match else None


def robinhood_token_metadata(token_address: str) -> dict:
    network = NETWORK_CONFIG["robinhood"]
    web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 20}))
    if not web3.is_connected() or web3.eth.chain_id != network["chain_id"]:
        raise RuntimeError("Nie można zweryfikować połączenia z Robinhood Chain.")
    checksum_address = Web3.to_checksum_address(token_address)
    if not web3.eth.get_code(checksum_address):
        raise ValueError("Podany adres nie jest kontraktem w Robinhood Chain.")
    contract = web3.eth.contract(address=checksum_address, abi=ERC20_TRADE_ABI)
    try:
        decimals = int(contract.functions.decimals().call())
        symbol = str(contract.functions.symbol().call())
    except Exception as error:
        raise ValueError("Podany kontrakt nie udostępnia prawidłowych metadanych ERC-20.") from error
    if not 0 <= decimals <= 255:
        raise ValueError("Kontrakt zwrócił nieprawidłową precyzję tokena.")
    return {"token_address": checksum_address, "decimals": decimals, "symbol": symbol or "TOKEN"}


def send_holder_token_transfer(
    wallet_id: str,
    token_address: str,
    recipient: str,
    amount_raw: int,
) -> str:
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise RuntimeError("Portfel nie istnieje albo został zablokowany.")
    if wallet.get("network") != "robinhood":
        raise RuntimeError("Rozsyłanie tokenów jest dostępne tylko w Robinhood Chain.")
    network = NETWORK_CONFIG["robinhood"]
    web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 20}))
    if not web3.is_connected():
        raise RuntimeError("Nie można połączyć się z RPC Robinhood Chain.")
    if web3.eth.chain_id != network["chain_id"]:
        raise RuntimeError("RPC zwróciło inny chain ID; przelew został wstrzymany.")
    account = Account.from_key(private_key)
    contract = web3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_TRADE_ABI,
    )
    transfer = contract.functions.transfer(
        Web3.to_checksum_address(recipient), int(amount_raw)
    )
    if transfer.call({"from": account.address}) is False:
        raise RuntimeError("Kontrakt tokena odrzucił transfer.")
    gas_estimate = transfer.estimate_gas({"from": account.address})
    transaction = transfer.build_transaction({
        "from": account.address,
        "nonce": web3.eth.get_transaction_count(account.address, "pending"),
        "chainId": network["chain_id"],
        "gas": int(gas_estimate * 1.2),
        **transaction_fee_fields(web3),
    })
    signed = Account.sign_transaction(transaction, private_key)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"Przelew został odrzucony przez sieć: {tx_hash.hex()}")
    return tx_hash.hex()


def pair_snapshot(pair_id: str, pair: dict, gt_data: Optional[dict] = None) -> dict:
    gt_data = gt_data or {}
    amount_held = float(pair.get("amount_held") or 0.0)
    entry_price = pair.get("entry_price")
    current_price = gt_data.get("price_usd") or pair.get("last_price")
    holding_value_usd = amount_held * current_price if current_price else 0.0
    cost_basis_usd = amount_held * entry_price if entry_price else 0.0
    unrealized_pnl_usd = holding_value_usd - cost_basis_usd
    unrealized_pnl_percent = (
        (current_price - entry_price) / entry_price * 100
        if current_price and entry_price
        else 0.0
    )
    if pair.get("wallet_id"):
        auto_buy_trade_count, auto_buy_amount, auto_buy_spent = wallet_pair_buy_totals(
            pair["wallet_id"],
            pair_id,
            pair.get("auto_buy_campaign_id"),
        )
        auto_sell_trade_count, auto_sell_amount, auto_sell_received = wallet_pair_trade_totals(
            pair["wallet_id"],
            pair_id,
            "sell",
            pair.get("auto_sell_campaign_id"),
        )
    else:
        auto_buy_trade_count, auto_buy_amount, auto_buy_spent = (
            0, Decimal(0), Decimal(0)
        )
        auto_sell_trade_count, auto_sell_amount, auto_sell_received = (
            0, Decimal(0), Decimal(0)
        )
    return {
        "pair_id": pair_id,
        "token_symbol": pair.get("token_symbol") or "TOKEN",
        "token_name": pair.get("token_name"),
        "network": pair["network"],
        "active_slug": gt_data.get("active_network_slug") or pair.get("active_slug") or pair["network"],
        "token_address": pair["token_address"],
        "quote_token_address": pair["quote_token_address"],
        "quote_symbol": pair.get("quote_symbol") or "ETH",
        "trade_amount": float(pair.get("trade_amount") or 0.0),
        "entry_price": entry_price,
        "amount_held": amount_held,
        "holding_value_usd": holding_value_usd,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_percent": unrealized_pnl_percent,
        "logo_url": gt_data.get("logo_url") or pair.get("logo_url"),
        "website": gt_data.get("website") or pair.get("website"),
        "twitter": gt_data.get("twitter") or pair.get("twitter"),
        "pool_address": gt_data.get("pool_address") or pair.get("pool_address"),
        "dex_pairs_list": gt_data.get("dex_pairs_list") or pair.get("dex_pairs_list", []),
        "change_24h": float(gt_data.get("change_24h") or pair.get("change_24h") or 0.0),
        "current_price": current_price,
        "target_buy_price": pair.get("target_buy_price"),
        "target_sell_price": pair.get("target_sell_price"),
        "auto_trade_enabled": pair.get("auto_trade_enabled", False),
        "wallet_id": pair.get("wallet_id"),
        "max_slippage_percent": pair.get("max_slippage_percent", 1.0),
        "auto_buy_mode": pair.get("auto_buy_mode", "single"),
        "auto_buy_target_quantity": pair.get("auto_buy_target_quantity"),
        "auto_buy_total_budget": pair.get("auto_buy_total_budget"),
        "auto_buy_max_transactions": pair.get("auto_buy_max_transactions", 10),
        "auto_buy_duration_minutes": pair.get("auto_buy_duration_minutes", 60),
        "auto_buy_quantity_duration_minutes": pair.get("auto_buy_quantity_duration_minutes"),
        "auto_buy_schedule": pair.get("auto_buy_schedule", []),
        "auto_buy_next_price": pair.get("auto_buy_next_price"),
        "auto_buy_next_trade_at": pair.get("auto_buy_next_trade_at"),
        "auto_sell_enabled": pair.get("auto_sell_enabled", False),
        "auto_sell_mode": pair.get("auto_sell_mode", "target_quantity"),
        "auto_sell_target_quantity": pair.get("auto_sell_target_quantity"),
        "auto_sell_max_transactions": pair.get("auto_sell_max_transactions", 10),
        "auto_sell_duration_minutes": pair.get("auto_sell_duration_minutes", 60),
        "auto_sell_quantity_duration_minutes": pair.get("auto_sell_quantity_duration_minutes"),
        "auto_sell_schedule": pair.get("auto_sell_schedule", []),
        "schedule_timezone": pair.get("schedule_timezone", "UTC"),
        "auto_sell_next_price": pair.get("auto_sell_next_price"),
        "auto_sell_next_trade_at": pair.get("auto_sell_next_trade_at"),
        "auto_buy_trade_count": auto_buy_trade_count,
        "auto_buy_quantity_bought": format(auto_buy_amount, "f"),
        "auto_buy_budget_spent": format(auto_buy_spent, "f"),
        "auto_sell_enabled": pair.get("auto_sell_enabled", False),
        "auto_sell_mode": pair.get("auto_sell_mode", "target_quantity"),
        "auto_sell_target_quantity": pair.get("auto_sell_target_quantity"),
        "auto_sell_max_transactions": pair.get("auto_sell_max_transactions", 10),
        "auto_sell_trade_count": auto_sell_trade_count,
        "auto_sell_quantity_sold": format(auto_sell_amount, "f"),
        "auto_sell_quote_received": format(auto_sell_received, "f"),
        "status": "PRICE_UNAVAILABLE" if not gt_data.get("price_usd") else "MONITORING"
    }


# --- GEKOTERMINAL API INTEGRATOR ---
def fetch_geckoterminal_details(network: str, token_address: str, custom_pair_contract: Optional[str] = None) -> dict:
    network_key = network.lower().strip()
    clean_addr = token_address.lower().strip()
    custom_pool = custom_pair_contract.strip() if custom_pair_contract else None
    cache_key = (network_key, clean_addr, custom_pool.lower() if custom_pool else None)
    with wallet_store_lock:
        cached = market_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 25:
            return copy.deepcopy(cached[1])

    config = NETWORK_CONFIG.get(network_key, {})
    slugs = config.get("slugs", [network_key])
    result = {
        "price_usd": None,
        "change_24h": 0.0,
        "logo_url": None,
        "website": None,
        "twitter": None,
        "pool_address": custom_pool,
        "dex_pairs_list": [],
        "token_symbol": None,
        "token_name": None,
        "active_network_slug": slugs[0],
    }
    headers = {"Accept": "application/json;version=20230302", "User-Agent": "AlDashboard/1.0"}

    def request_json(url: str, source: str) -> Optional[dict]:
        try:
            response = requests.get(url, headers=headers, timeout=6)
            if response.status_code != 200:
                if response.status_code == 429:
                    logging.warning("%s ogranicza zapytania dla %s", source, network_key)
                elif response.status_code >= 400:
                    logging.info("%s zwrócił HTTP %s dla %s", source, response.status_code, network_key)
                return None
            return response.json()
        except (requests.RequestException, ValueError) as error:
            logging.warning("Błąd zapytania %s dla %s/%s: %s", source, network_key, clean_addr, error)
            return None

    for slug in slugs:
        pools_url = (
            f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/{clean_addr}"
            "/pools?include=base_token,quote_token&page=1"
        )
        pools_data = request_json(
            pools_url,
            "GeckoTerminal",
        )
        pools = pools_data.get("data", []) if pools_data else []
        next_page = (pools_data or {}).get("links", {}).get("next")
        seen_pages = set()
        if next_page:
            next_page = urljoin(pools_url, next_page)
        while next_page and next_page not in seen_pages:
            current_page = next_page
            seen_pages.add(current_page)
            next_data = request_json(current_page, "GeckoTerminal")
            if not next_data:
                break
            pools.extend(next_data.get("data", []))
            next_page = next_data.get("links", {}).get("next")
            if next_page:
                next_page = urljoin(current_page, next_page)
        if pools:
            result["active_network_slug"] = slug
            for pool in pools:
                attrs = pool.get("attributes", {})
                address = pool.get("id", "").rsplit("_", 1)[-1]
                relationships = pool.get("relationships", {})
                base_id = (relationships.get("base_token", {}).get("data") or {}).get("id", "")
                quote_id = (relationships.get("quote_token", {}).get("data") or {}).get("id", "")
                try:
                    price = float(attrs.get("token_price_usd") or attrs.get("base_token_price_usd") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                try:
                    fee_percent = float(attrs.get("pool_fee_percentage") or 0) or None
                except (TypeError, ValueError):
                    fee_percent = None
                dex_id = str(attrs.get("dex_id") or "").lower()
                dex_version = next(
                    (version for version in ("v2", "v3", "v4") if version in dex_id),
                    "v4" if len(address) == 66 else "unknown",
                )
                result["dex_pairs_list"].append({
                    "dex_name": str(attrs.get("dex_id") or "DEX").replace("_", " ").title(),
                    "pair_name": attrs.get("name", "Pair"),
                    "pool_address": address,
                    "price_usd": price,
                    "liquidity_usd": float(attrs.get("reserve_in_usd") or 0),
                    "dex_version": dex_version,
                    "fee_percent": fee_percent,
                    "created_at": attrs.get("pool_created_at"),
                    "base_token_address": base_id.rsplit("_", 1)[-1] if base_id else None,
                    "quote_token_address": quote_id.rsplit("_", 1)[-1] if quote_id else None,
                    "base_token_symbol": None,
                    "quote_token_symbol": None,
                })

            selected_pool = next(
                (pool for pool in result["dex_pairs_list"]
                 if custom_pool and pool["pool_address"].lower() == custom_pool.lower()),
                None,
            )
            if not selected_pool and not custom_pool:
                selected_pool = max(result["dex_pairs_list"], key=lambda pool: pool["liquidity_usd"])
            if selected_pool and not custom_pool:
                result["pool_address"] = selected_pool["pool_address"]
            if selected_pool and selected_pool["price_usd"]:
                result["price_usd"] = selected_pool["price_usd"]
            if selected_pool and selected_pool["pool_address"]:
                pool_detail = request_json(
                    f"https://api.geckoterminal.com/api/v2/networks/{slug}/pools/{selected_pool['pool_address']}",
                    "GeckoTerminal",
                )
                attrs = (pool_detail or {}).get("data", {}).get("attributes", {})
                try:
                    result["price_usd"] = float(
                        attrs.get("token_price_usd") or attrs.get("price_in_usd") or result["price_usd"] or 0
                    ) or result["price_usd"]
                except (TypeError, ValueError):
                    pass
                changes = attrs.get("price_change_percentage", {})
                if isinstance(changes, dict):
                    try:
                        result["change_24h"] = float(changes.get("h24") or 0)
                    except (TypeError, ValueError):
                        pass

        token_data = request_json(
            f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/{clean_addr}",
            "GeckoTerminal",
        )
        token_attrs = (token_data or {}).get("data", {}).get("attributes", {})
        result["token_symbol"] = token_attrs.get("symbol") or result["token_symbol"]
        result["token_name"] = token_attrs.get("name") or result["token_name"]
        result["logo_url"] = token_attrs.get("image_url") or result["logo_url"]
        if not result["price_usd"]:
            try:
                result["price_usd"] = float(token_attrs.get("price_usd") or 0) or None
            except (TypeError, ValueError):
                pass
        info_data = request_json(
            f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/{clean_addr}/info",
            "GeckoTerminal",
        )
        info_attrs = (info_data or {}).get("data", {}).get("attributes", {})
        websites = info_attrs.get("websites") or []
        result["website"] = websites[0] if websites else result["website"]
        twitter = info_attrs.get("twitter_handle")
        result["twitter"] = f"https://x.com/{twitter}" if twitter else result["twitter"]
        result["logo_url"] = info_attrs.get("image_url") or result["logo_url"]

    dex_chain = config.get("dex_chain_id")
    if dex_chain:
        dex_url = (
            f"https://api.dexscreener.com/latest/dex/pairs/{dex_chain}/{custom_pool}"
            if custom_pool
            else f"https://api.dexscreener.com/latest/dex/tokens/{clean_addr}"
        )
        dex_data = request_json(dex_url, "DexScreener")
        dex_pairs = (dex_data or {}).get("pairs") or []
        dex_pairs = [
            pair for pair in dex_pairs
            if str(pair.get("chainId", "")).lower() == dex_chain.lower()
            and (
                not custom_pool
                or str(pair.get("pairAddress", "")).lower() == custom_pool.lower()
            )
        ]
        dex_pairs.sort(
            key=lambda pair: float((pair.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        for pair in dex_pairs:
            base = pair.get("baseToken") or {}
            quote = pair.get("quoteToken") or {}
            base_price = float(pair.get("priceUsd") or 0)
            price = base_price
            if base.get("address", "").lower() != clean_addr and quote.get("address", "").lower() == clean_addr:
                native_ratio = float(pair.get("priceNative") or 0)
                price = base_price / native_ratio if base_price and native_ratio else 0
            pair_address = str(pair.get("pairAddress") or "")
            labels = [str(label).lower() for label in pair.get("labels", [])]
            version = next((label for label in labels if label in {"v2", "v3", "v4"}), None)
            if not version:
                version = "v4" if len(pair_address) == 66 else "unknown"
            result["dex_pairs_list"].append({
                "dex_name": str(pair.get("dexId") or "DEX").capitalize(),
                "pair_name": f"{base.get('symbol', 'TOKEN')}/{quote.get('symbol', 'TOKEN')}",
                "pool_address": pair_address,
                "price_usd": price,
                "liquidity_usd": float((pair.get("liquidity") or {}).get("usd") or 0),
                "dex_version": version,
                "fee_percent": None,
                "created_at": (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pair["pairCreatedAt"] / 1000))
                    if pair.get("pairCreatedAt")
                    else None
                ),
                "base_token_address": base.get("address"),
                "quote_token_address": quote.get("address"),
                "base_token_symbol": base.get("symbol"),
                "quote_token_symbol": quote.get("symbol"),
            })
            if not result["logo_url"]:
                result["logo_url"] = (pair.get("info") or {}).get("imageUrl")

        if dex_pairs:
            selected_pair = next(
                (pair for pair in dex_pairs
                 if str(pair.get("pairAddress", "")).lower() == str(custom_pool or result["pool_address"] or "").lower()),
                dex_pairs[0],
            )
            base = selected_pair.get("baseToken") or {}
            quote = selected_pair.get("quoteToken") or {}
            if not result["token_symbol"]:
                target = base if base.get("address", "").lower() == clean_addr else quote
                result["token_symbol"] = target.get("symbol")
            if not result["token_name"]:
                target = base if base.get("address", "").lower() == clean_addr else quote
                result["token_name"] = target.get("name")
            base_price = float(selected_pair.get("priceUsd") or 0)
            dex_price = base_price
            if base.get("address", "").lower() != clean_addr and quote.get("address", "").lower() == clean_addr:
                native_ratio = float(selected_pair.get("priceNative") or 0)
                dex_price = base_price / native_ratio if base_price and native_ratio else 0
            if dex_price:
                result["price_usd"] = dex_price
            result["pool_address"] = str(selected_pair.get("pairAddress") or result["pool_address"] or "")
            result["change_24h"] = float(selected_pair.get("priceChange", {}).get("h24") or result["change_24h"] or 0)

    if result["pool_address"] and not any(
        item["pool_address"].lower() == result["pool_address"].lower()
        for item in result["dex_pairs_list"]
    ):
        result["dex_pairs_list"].insert(0, {
            "dex_name": "Ręcznie dodana",
            "pair_name": result["pool_address"],
            "pool_address": result["pool_address"],
            "price_usd": result["price_usd"] or 0.0,
            "liquidity_usd": 0.0,
        })
    unique_pairs = {}
    for pair in result["dex_pairs_list"]:
        key = pair["pool_address"].lower()
        previous = unique_pairs.get(key)
        if previous is None:
            unique_pairs[key] = pair
            continue
        preferred, fallback = (
            (pair, previous)
            if pair["liquidity_usd"] > previous["liquidity_usd"]
            or (not previous["price_usd"] and pair["price_usd"])
            else (previous, pair)
        )
        merged = {**fallback, **preferred}
        for field in (
            "dex_version", "fee_percent", "created_at", "base_token_address",
            "quote_token_address", "base_token_symbol", "quote_token_symbol",
        ):
            if not merged.get(field) or merged[field] == "unknown":
                merged[field] = fallback.get(field) or merged.get(field)
        if not merged.get("price_usd"):
            merged["price_usd"] = fallback.get("price_usd") or 0.0
        merged["liquidity_usd"] = max(
            float(previous.get("liquidity_usd") or 0),
            float(pair.get("liquidity_usd") or 0),
        )
        unique_pairs[key] = merged
    result["dex_pairs_list"] = list(unique_pairs.values())
    if result["token_symbol"]:
        result["token_symbol"] = result["token_symbol"].upper()

    with wallet_store_lock:
        market_cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
    return result

# --- REFRESHER DLA TICKERA CENOWEGO (BOBER, ETH, BNB, BTC) ---
def update_market_ticker():
    # 1. BOBER na Robinhood Chain
    bober_data = fetch_geckoterminal_details("robinhood", BOBER_CONTRACT)
    if bober_data["price_usd"]:
        ticker_prices["BOBER"] = {"price": bober_data["price_usd"], "change_24h": bober_data["change_24h"]}

    # 2. ETH na Ethereum
    eth_data = fetch_geckoterminal_details("eth", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")
    if eth_data["price_usd"]:
        ticker_prices["ETH"] = {"price": eth_data["price_usd"], "change_24h": eth_data["change_24h"]}

    # 3. BNB na BSC
    bnb_data = fetch_geckoterminal_details("bsc", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c")
    if bnb_data["price_usd"]:
        ticker_prices["BNB"] = {"price": bnb_data["price_usd"], "change_24h": bnb_data["change_24h"]}

    # 4. BTC (WBTC na Ethereum)
    btc_data = fetch_geckoterminal_details("eth", "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599")
    if btc_data["price_usd"]:
        ticker_prices["BTC"] = {"price": btc_data["price_usd"], "change_24h": btc_data["change_24h"]}


def auto_swap_failure(stage: str, error: Exception, pair: dict) -> RuntimeError:
    raw_error = str(error).strip() or error.__class__.__name__
    lower_error = raw_error.lower()
    empty_revert = (
        "execution reverted" in lower_error
        and any(marker in lower_error for marker in ("0x", "b''", 'b""'))
    )
    if stage in {"quote", "v3_quote"}:
        router_label = "V3" if stage == "v3_quote" else "V2"
        discovered_versions = sorted({
            str(item.get("dex_version", "")).lower()
            for item in pair.get("dex_pairs_list", [])
            if item.get("dex_version")
        })
        version_note = (
            f"Wykryte typy pul DEX: {', '.join(discovered_versions)}. "
            if discovered_versions else ""
        )
        if empty_revert:
            route_note = (
                "Możliwy brak płynnej puli V3 dla wybranej pary. "
                if stage == "v3_quote"
                else "Możliwy brak płynnej puli V2 dla wybranej pary. "
            )
            explanation = (
                f"Router {router_label} odrzucił wycenę bez podania powodu. "
                f"{version_note}{route_note}"
                "automatyczny swap nie przełącza się na inny router po nieudanej wycenie."
            )
        else:
            explanation = f"Nie udało się pobrać wyceny z routera {router_label}."
    elif stage in {"simulation", "v3_simulation"}:
        if empty_revert:
            explanation = (
                "RPC odrzuciło symulację swapu przed wysłaniem transakcji i nie podało "
                "powodu revertu. Możliwe przyczyny: ograniczenia transferu tokena, "
                "niewystarczające saldo, zmiana płynności lub limit poślizgu."
            )
        else:
            explanation = "RPC odrzuciło symulację swapu przed wysłaniem transakcji."
    else:
        explanation = f"Nie powiódł się etap {stage} automatycznego swapu."
    return RuntimeError(f"{explanation} Szczegóły RPC: {raw_error[:320]}")


def select_auto_swap_route(
    pair: dict, token_in: str, token_out: str, network: dict, web3: Optional[Web3] = None
) -> dict:
    wrapped_native = str(network.get("wrapped_native") or "").lower()
    normalized_in = (
        wrapped_native
        if token_in.lower() == NATIVE_TOKEN_ADDRESS.lower()
        else token_in.lower()
    )
    normalized_out = (
        wrapped_native
        if token_out.lower() == NATIVE_TOKEN_ADDRESS.lower()
        else token_out.lower()
    )
    matching = []
    for pool in pair.get("dex_pairs_list", []):
        base = str(pool.get("base_token_address") or "").lower()
        quote = str(pool.get("quote_token_address") or "").lower()
        if not base or not quote or {
            base, quote
        } != {normalized_in, normalized_out}:
            continue
        version = str(pool.get("dex_version") or "").lower()
        matching.append((pool, version))

    metadata_v3_pools = [
        pool for pool, version in matching
        if version == "v3" and Web3.is_address(str(pool.get("pool_address") or ""))
    ]
    metadata_liquidity = {
        str(pool["pool_address"]).lower(): float(pool.get("liquidity_usd") or 0)
        for pool in metadata_v3_pools
    }

    discovered_v3_pools = []
    if web3 and network.get("v3_factory") and network.get("v3_quoter"):
        try:
            factory = web3.eth.contract(
                address=Web3.to_checksum_address(network["v3_factory"]),
                abi=V3_FACTORY_ABI,
            )
            requested_fees = {100, 500, 2500, 3000, 10000}
            for pool in metadata_v3_pools:
                try:
                    pool_contract = web3.eth.contract(
                        address=Web3.to_checksum_address(pool["pool_address"]),
                        abi=V3_POOL_FEE_ABI,
                    )
                    requested_fees.add(int(pool_contract.functions.fee().call()))
                except Exception:
                    logging.warning(
                        "Nie udało się odczytać fee puli DEX %s",
                        pool.get("pool_address"),
                        exc_info=True,
                    )
            for fee in sorted(requested_fees):
                pool_address = factory.functions.getPool(
                    Web3.to_checksum_address(normalized_in),
                    Web3.to_checksum_address(normalized_out),
                    fee,
                ).call()
                if pool_address.lower() == NATIVE_TOKEN_ADDRESS.lower():
                    continue
                pool_address = Web3.to_checksum_address(pool_address)
                if not web3.eth.get_code(pool_address):
                    continue
                pool_contract = web3.eth.contract(
                    address=pool_address,
                    abi=V3_POOL_FEE_ABI,
                )
                actual_fee = int(pool_contract.functions.fee().call())
                liquidity = int(pool_contract.functions.liquidity().call())
                discovered_v3_pools.append({
                    "pool_address": pool_address,
                    "dex_version": "v3",
                    "fee": actual_fee,
                    "liquidity_usd": metadata_liquidity.get(pool_address.lower(), 0),
                    "onchain_liquidity": liquidity,
                })
        except Exception:
            logging.warning(
                "Nie udało się wykryć puli V3 przez fabrykę; używam metadanych DEX",
                exc_info=True,
            )

    if discovered_v3_pools:
        with_metadata = [
            pool for pool in discovered_v3_pools
            if pool["liquidity_usd"] > 0
        ]
        return {
            "version": "v3",
            "pool": max(
                with_metadata or discovered_v3_pools,
                key=lambda item: (
                    item["liquidity_usd"]
                    if with_metadata
                    else item["onchain_liquidity"]
                ),
            ),
        }

    if metadata_v3_pools:
        return {
            "version": "v3",
            "pool": max(
                metadata_v3_pools,
                key=lambda item: float(item.get("liquidity_usd") or 0),
            ),
        }
    v2_pools = [pool for pool, version in matching if version == "v2"]
    if v2_pools:
        return {
            "version": "v2",
            "pool": max(
                v2_pools,
                key=lambda item: float(item.get("liquidity_usd") or 0),
            ),
        }
    if any(version == "v4" for _pool, version in matching):
        raise RuntimeError(
            "Dla tej pary znaleziono wyłącznie pulę DEX V4. Automatyczny portfel "
            "nie obsługuje jeszcze wykonywania swapów V4; transakcja nie została wysłana."
        )
    return {"version": "v2", "pool": None}


def quote_auto_v3(
    web3: Web3,
    network: dict,
    route: dict,
    pair: dict,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> tuple:
    if not network.get("v3_factory") or not network.get("v3_quoter"):
        raise RuntimeError("Brak konfiguracji fabryki lub quoter-a Uniswap V3.")
    pool_address = Web3.to_checksum_address(route["pool"]["pool_address"])
    pool = web3.eth.contract(address=pool_address, abi=V3_POOL_FEE_ABI)
    try:
        fee = int(pool.functions.fee().call())
        factory = web3.eth.contract(
            address=Web3.to_checksum_address(network["v3_factory"]),
            abi=V3_FACTORY_ABI,
        )
        canonical_pool = factory.functions.getPool(
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
        ).call()
        if canonical_pool.lower() != pool_address.lower():
            raise RuntimeError(
                "Wybrana pula V3 nie pasuje do tokenów, opłaty i fabryki tej sieci."
            )
        quoter = web3.eth.contract(
            address=Web3.to_checksum_address(network["v3_quoter"]),
            abi=V3_QUOTER_ABI,
        )
        result = quoter.functions.quoteExactInputSingle((
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_in,
            fee,
            0,
        )).call()
    except Exception as error:
        raise auto_swap_failure("v3_quote", error, pair) from error
    amount_out = int(result[0])
    if amount_out <= 0:
        raise RuntimeError("Quoter V3 zwrócił zerową kwotę wyjściową.")
    return amount_out, fee


def send_auto_v3_swap(
    web3: Web3,
    network: dict,
    account,
    private_key: str,
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    fee: int,
    is_native_input: bool,
    is_native_output: bool,
    pair: dict,
) -> tuple:
    router_address = Web3.to_checksum_address(network["v3_router"])
    if not is_native_input:
        ensure_managed_wallet_input_allowance(
            web3,
            network,
            account,
            private_key,
            (token_in, amount_in, router_address),
        )
    router = web3.eth.contract(
        address=router_address,
        abi=V3_ROUTER_AUTO_ABI,
    )
    recipient = router_address if is_native_output else account.address
    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        fee,
        recipient,
        amount_in,
        amount_out_min,
        0,
    )
    swap_function = router.functions.exactInputSingle(params)
    tx_value = amount_in if is_native_input else 0
    if is_native_output:
        swap_data = bytes.fromhex(swap_function._encode_transaction_data()[2:])
        unwrap_data = bytes.fromhex(
            router.functions.unwrapWETH9(
                amount_out_min, account.address
            )._encode_transaction_data()[2:]
        )
        transaction_call = router.functions.multicall([swap_data, unwrap_data])
        tx_value = 0
    else:
        transaction_call = swap_function

    try:
        gas_estimate = transaction_call.estimate_gas({
            "from": account.address,
            "value": tx_value,
        })
    except Exception as error:
        raise auto_swap_failure(
            "v3_simulation", error, pair
        ) from error
    transaction = transaction_call.build_transaction({
        "from": account.address,
        "nonce": web3.eth.get_transaction_count(account.address),
        "chainId": network["chain_id"],
        "gas": int(gas_estimate * 1.2),
        **transaction_fee_fields(web3),
        "value": tx_value,
    })
    signed_transaction = Account.sign_transaction(transaction, private_key)
    tx_hash = web3.eth.send_raw_transaction(signed_transaction.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"Transakcja {tx_hash.hex()} została odrzucona przez sieć.")
    return tx_hash, receipt


def execute_auto_buy(pair: dict, wallet_id: str) -> dict:
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise RuntimeError("Portfel nie istnieje albo jest zablokowany.")
    if wallet["network"] != pair["network"]:
        raise RuntimeError("Sieć portfela nie pasuje do monitorowanej pary.")
    network = NETWORK_CONFIG.get(pair["network"])
    if not network or not network.get("router"):
        raise RuntimeError("Brak zweryfikowanego routera dla tej sieci.")
    if not network.get("rpc"):
        raise RuntimeError(
            "Brak RPC Robinhood Chain. Ustaw ROBINHOOD_RPC_URL albo ALCHEMY_API_KEY."
        )

    web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 12}))
    if not web3.is_connected():
        raise RuntimeError("Nie można połączyć się z RPC tej sieci.")
    if web3.eth.chain_id != network["chain_id"]:
        raise RuntimeError("RPC zwróciło inny chain ID; transakcja została wstrzymana.")

    account = Account.from_key(private_key)
    router = web3.eth.contract(
        address=Web3.to_checksum_address(network["router"]),
        abi=V2_ROUTER_ABI,
    )
    token_address = Web3.to_checksum_address(pair["token_address"])
    token_contract = web3.eth.contract(address=token_address, abi=ERC20_TRADE_ABI)
    token_decimals = token_contract.functions.decimals().call()
    token_balance_before = token_contract.functions.balanceOf(account.address).call()
    quote_address = (pair.get("quote_token_address") or NATIVE_TOKEN_ADDRESS).lower()
    is_native_input = quote_address == NATIVE_TOKEN_ADDRESS.lower()
    input_token = None
    if is_native_input:
        input_address = router.functions.WETH().call()
        input_decimals = 18
    else:
        input_address = Web3.to_checksum_address(quote_address)
        input_token = web3.eth.contract(address=input_address, abi=ERC20_TRADE_ABI)
        input_decimals = input_token.functions.decimals().call()

    try:
        amount_decimal = Decimal(str(pair["trade_amount"]))
    except (InvalidOperation, KeyError) as error:
        raise RuntimeError("Nieprawidłowa kwota zakupu.") from error
    scale = 10 ** input_decimals
    scaled_amount = amount_decimal * scale
    if not amount_decimal.is_finite() or amount_decimal <= 0:
        raise RuntimeError("Kwota zakupu ma zbyt wiele miejsc dziesiętnych.")
    if scaled_amount != scaled_amount.to_integral_value():
        if pair.get("auto_buy_mode") != "target_quantity":
            raise RuntimeError("Kwota zakupu ma zbyt wiele miejsc dziesiętnych.")
        amount_in = int(scaled_amount)
    else:
        amount_in = int(scaled_amount)
    if amount_in <= 0:
        raise RuntimeError("Kwota transakcji jest mniejsza od najmniejszej jednostki waluty płatności.")
    quote_balance_before = (
        None if is_native_input else input_token.functions.balanceOf(account.address).call()
    )
    path = [Web3.to_checksum_address(input_address), token_address]
    route = select_auto_swap_route(pair, input_address, token_address, network, web3)
    v3_fee = None
    if route["version"] == "v3":
        amount_out, v3_fee = quote_auto_v3(
            web3, network, route, pair, input_address, token_address, amount_in
        )
    else:
        try:
            amounts = router.functions.getAmountsOut(amount_in, path).call()
        except Exception as error:
            raise auto_swap_failure("quote", error, pair) from error
        amount_out = amounts[-1]
    remaining_quantity_value = pair.get("auto_buy_remaining_quantity")
    exact_schedule_quantity = bool(
        pair.get("auto_buy_exact_schedule_quantity")
        and remaining_quantity_value is not None
    )
    if remaining_quantity_value is not None:
        remaining_raw = int(
            Decimal(str(remaining_quantity_value)) * (10 ** token_decimals)
        )
        if remaining_raw <= 0:
            raise RuntimeError("Docelowa ilość tokenów została już osiągnięta.")

        def quote_input(candidate_amount: int) -> tuple:
            if route["version"] == "v3":
                return quote_auto_v3(
                    web3, network, route, pair, input_address, token_address,
                    candidate_amount
                )
            try:
                candidate_amounts = router.functions.getAmountsOut(
                    candidate_amount, path
                ).call()
            except Exception as error:
                raise auto_swap_failure("quote", error, pair) from error
            return candidate_amounts[-1], None

        if exact_schedule_quantity:
            max_amount_in = amount_in
            for _ in range(8):
                if amount_out <= 0 or amount_out == remaining_raw:
                    break
                if amount_out < remaining_raw:
                    estimated_input = (
                        amount_in * remaining_raw + amount_out - 1
                    ) // amount_out
                    next_amount_in = min(
                        max_amount_in, max(amount_in + 1, estimated_input)
                    )
                else:
                    next_amount_in = amount_in * remaining_raw // amount_out
                    if next_amount_in >= amount_in:
                        next_amount_in = amount_in - 1
                if next_amount_in <= amount_in:
                    if amount_out > remaining_raw and next_amount_in > 0:
                        amount_in = next_amount_in
                        amount_out, v3_fee = quote_input(amount_in)
                    break
                amount_in = next_amount_in
                amount_out, v3_fee = quote_input(amount_in)
        elif amount_out > remaining_raw:
            amount_in = amount_in * remaining_raw // amount_out
            if amount_in <= 0:
                raise RuntimeError("Pozostała ilość tokenów jest zbyt mała dla transakcji.")
            amount_out, v3_fee = quote_input(amount_in)
        if exact_schedule_quantity and amount_out < remaining_raw:
            raise RuntimeError(
                "Limit waluty płatności nie wystarcza na zaplanowaną ilość tokenów; "
                "transakcja nie została wysłana."
            )
    slippage = Decimal(str(pair.get("max_slippage_percent", 1.0)))
    amount_out_min = (
        remaining_raw
        if exact_schedule_quantity
        else int(Decimal(amount_out) * (Decimal(100) - slippage) / Decimal(100))
    )
    if amount_out_min <= 0:
        raise RuntimeError("Minimalna ilość wyjściowa wynosi zero; zakup przerwany.")
    deadline = int(time.time()) + 300
    transaction = {}
    if route["version"] == "v3":
        tx_hash, receipt = send_auto_v3_swap(
            web3, network, account, private_key,
            input_address, token_address, amount_in, amount_out_min,
            v3_fee, is_native_input, False, pair,
        )
    else:
        if is_native_input:
            swap = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                amount_out_min, path, account.address, deadline
            )
            tx_value = amount_in
        else:
            allowance = input_token.functions.allowance(account.address, network["router"]).call()
            if allowance < amount_in:
                approval = input_token.functions.approve(network["router"], amount_in)
                approval_gas = approval.estimate_gas({"from": account.address})
                approval_tx = approval.build_transaction({
                    "from": account.address,
                    "nonce": web3.eth.get_transaction_count(account.address),
                    "chainId": network["chain_id"],
                    "gas": int(approval_gas * 1.2),
                    **transaction_fee_fields(web3),
                })
                signed_approval = Account.sign_transaction(approval_tx, private_key)
                approval_hash = web3.eth.send_raw_transaction(signed_approval.raw_transaction)
                approval_receipt = web3.eth.wait_for_transaction_receipt(approval_hash, timeout=180)
                if approval_receipt.status != 1:
                    raise RuntimeError(f"Autoryzacja tokena nie powiodła się: {approval_hash.hex()}")
            swap = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in, amount_out_min, path, account.address, deadline
            )
            tx_value = 0

        try:
            gas_estimate = swap.estimate_gas({"from": account.address, "value": tx_value})
        except Exception as error:
            raise auto_swap_failure("simulation", error, pair) from error
        transaction = swap.build_transaction({
            "from": account.address,
            "nonce": web3.eth.get_transaction_count(account.address),
            "chainId": network["chain_id"],
            "gas": int(gas_estimate * 1.2),
            **transaction_fee_fields(web3),
            "value": tx_value,
        })
        signed_transaction = Account.sign_transaction(transaction, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed_transaction.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"Transakcja {tx_hash.hex()} została odrzucona przez sieć.")
    try:
        token_balance_after = token_contract.functions.balanceOf(account.address).call()
        token_received = max(token_balance_after - token_balance_before, 0)
    except Exception:
        logging.exception("Zakup wykonany, ale nie udało się odczytać ilości otrzymanych tokenów.")
        token_received = None
    if is_native_input:
        quote_spent = amount_in
    else:
        try:
            quote_balance_after = input_token.functions.balanceOf(account.address).call()
            quote_spent = max(quote_balance_before - quote_balance_after, 0)
        except Exception:
            logging.exception("Zakup wykonany, ale nie udało się odczytać wydanej kwoty.")
            quote_spent = amount_in
    return {
        "type": "buy",
        "network": pair["network"],
        "pair_id": pair.get("id"),
        "campaign_id": pair.get("auto_buy_campaign_id"),
        "token_address": token_address,
        "token_symbol": pair.get("token_symbol") or "TOKEN",
        "token_amount": (
            format_raw_token_amount(token_received, token_decimals)
            if token_received is not None else None
        ),
        "quote_symbol": pair.get("quote_symbol") or network.get("native_symbol", "ETH"),
        "quote_token_address": pair.get("quote_token_address") or NATIVE_TOKEN_ADDRESS,
        "quote_amount": (
            format_raw_token_amount(quote_spent, input_decimals)
            if quote_spent is not None else None
        ),
        "transaction_cost": transaction_cost_details(
            {"value": amount_in if is_native_input else 0, **transaction},
            receipt,
            network.get("native_symbol") or ("BNB" if pair["network"] == "bsc" else "ETH"),
        ),
        "tx_hash": tx_hash.hex(),
        "timestamp": int(time.time()),
    }


def execute_auto_sell(pair: dict, wallet_id: str) -> dict:
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise RuntimeError("Portfel nie istnieje albo jest zablokowany.")
    if wallet["network"] != pair["network"]:
        raise RuntimeError("Sieć portfela nie pasuje do monitorowanej pary.")
    network = NETWORK_CONFIG.get(pair["network"])
    if not network or not network.get("router"):
        raise RuntimeError("Brak zweryfikowanego routera dla tej sieci.")
    if not network.get("rpc"):
        raise RuntimeError("Brak RPC dla wybranej sieci.")

    web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 12}))
    if not web3.is_connected():
        raise RuntimeError("Nie można połączyć się z RPC tej sieci.")
    if web3.eth.chain_id != network["chain_id"]:
        raise RuntimeError("RPC zwróciło inny chain ID; transakcja została wstrzymana.")

    account = Account.from_key(private_key)
    router = web3.eth.contract(
        address=Web3.to_checksum_address(network["router"]),
        abi=V2_ROUTER_ABI,
    )
    token_address = Web3.to_checksum_address(pair["token_address"])
    token_contract = web3.eth.contract(address=token_address, abi=ERC20_TRADE_ABI)
    token_decimals = token_contract.functions.decimals().call()
    try:
        amount_decimal = Decimal(str(pair["auto_sell_amount"]))
    except (InvalidOperation, KeyError) as error:
        raise RuntimeError("Nieprawidłowa ilość tokenów do sprzedaży.") from error
    scaled_amount = amount_decimal * (10 ** token_decimals)
    if not amount_decimal.is_finite() or amount_decimal <= 0 or scaled_amount < 1:
        raise RuntimeError("Ilość tokenów do sprzedaży jest mniejsza od najmniejszej jednostki.")
    amount_in = int(scaled_amount)
    token_balance_before = token_contract.functions.balanceOf(account.address).call()
    if token_balance_before < amount_in:
        available = format_raw_token_amount(token_balance_before, token_decimals)
        raise RuntimeError(
            f"Niewystarczające saldo tokena: dostępne {available}, wymagane {amount_decimal}."
        )

    quote_address = (pair.get("quote_token_address") or NATIVE_TOKEN_ADDRESS).lower()
    is_native_output = quote_address == NATIVE_TOKEN_ADDRESS.lower()
    output_address = (
        router.functions.WETH().call()
        if is_native_output
        else Web3.to_checksum_address(quote_address)
    )
    output_token = None
    quote_decimals = 18
    if not is_native_output:
        output_token = web3.eth.contract(address=output_address, abi=ERC20_TRADE_ABI)
        quote_decimals = output_token.functions.decimals().call()

    path = [token_address, Web3.to_checksum_address(output_address)]
    route = select_auto_swap_route(pair, token_address, output_address, network, web3)
    v3_fee = None
    if route["version"] == "v3":
        amount_out, v3_fee = quote_auto_v3(
            web3, network, route, pair, token_address, output_address, amount_in
        )
    else:
        try:
            amounts = router.functions.getAmountsOut(amount_in, path).call()
        except Exception as error:
            raise auto_swap_failure("quote", error, pair) from error
        amount_out = amounts[-1]
    slippage = Decimal(str(pair.get("max_slippage_percent", 1.0)))
    amount_out_min = int(Decimal(amount_out) * (Decimal(100) - slippage) / Decimal(100))
    if amount_out_min <= 0:
        raise RuntimeError("Minimalna ilość wyjściowa wynosi zero; sprzedaż przerwana.")

    deadline = int(time.time()) + 300
    transaction = {}
    if route["version"] == "v3":
        tx_hash, receipt = send_auto_v3_swap(
            web3, network, account, private_key,
            token_address, output_address, amount_in, amount_out_min,
            v3_fee, False, is_native_output, pair,
        )
    else:
        allowance = token_contract.functions.allowance(
            account.address, network["router"]
        ).call()
        if allowance < amount_in:
            approval = token_contract.functions.approve(network["router"], amount_in)
            approval_gas = approval.estimate_gas({"from": account.address})
            approval_tx = approval.build_transaction({
                "from": account.address,
                "nonce": web3.eth.get_transaction_count(account.address),
                "chainId": network["chain_id"],
                "gas": int(approval_gas * 1.2),
                **transaction_fee_fields(web3),
            })
            signed_approval = Account.sign_transaction(approval_tx, private_key)
            approval_hash = web3.eth.send_raw_transaction(signed_approval.raw_transaction)
            approval_receipt = web3.eth.wait_for_transaction_receipt(approval_hash, timeout=180)
            if approval_receipt.status != 1:
                raise RuntimeError(f"Autoryzacja tokena nie powiodła się: {approval_hash.hex()}")
        if is_native_output:
            swap = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                amount_in, amount_out_min, path, account.address, deadline
            )
        else:
            swap = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in, amount_out_min, path, account.address, deadline
            )
        try:
            gas_estimate = swap.estimate_gas({"from": account.address})
        except Exception as error:
            raise auto_swap_failure("simulation", error, pair) from error
        transaction = swap.build_transaction({
            "from": account.address,
            "nonce": web3.eth.get_transaction_count(account.address),
            "chainId": network["chain_id"],
            "gas": int(gas_estimate * 1.2),
            **transaction_fee_fields(web3),
            "value": 0,
        })
    quote_balance_before = (
        None if is_native_output
        else output_token.functions.balanceOf(account.address).call()
    )
    native_balance_before = (
        web3.eth.get_balance(account.address) if is_native_output else None
    )
    if route["version"] == "v2":
        signed_transaction = Account.sign_transaction(transaction, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed_transaction.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"Transakcja {tx_hash.hex()} została odrzucona przez sieć.")

    try:
        token_balance_after = token_contract.functions.balanceOf(account.address).call()
        token_sold = max(token_balance_before - token_balance_after, 0)
    except Exception:
        logging.exception("Sprzedaż wykonana, ale nie udało się odczytać sprzedanej ilości.")
        token_sold = amount_in
    if is_native_output:
        try:
            native_balance_after = web3.eth.get_balance(account.address)
            gas_price = receipt.get(
                "effectiveGasPrice",
                transaction.get("gasPrice", transaction.get("maxFeePerGas", 0)),
            )
            gas_cost = receipt["gasUsed"] * gas_price
            quote_received = max(native_balance_after - native_balance_before + gas_cost, 0)
        except Exception:
            logging.exception("Sprzedaż wykonana, ale nie udało się odczytać wpływu native.")
            quote_received = amount_out
        quote_decimals = 18
    else:
        try:
            quote_balance_after = output_token.functions.balanceOf(account.address).call()
            quote_received = max(quote_balance_after - quote_balance_before, 0)
            quote_decimals = output_token.functions.decimals().call()
        except Exception:
            logging.exception("Sprzedaż wykonana, ale nie udało się odczytać wpływu tokena.")
            quote_received = amount_out
    return {
        "type": "sell",
        "network": pair["network"],
        "pair_id": pair.get("id"),
        "campaign_id": pair.get("auto_sell_campaign_id"),
        "token_address": token_address,
        "token_symbol": pair.get("token_symbol") or "TOKEN",
        "token_amount": format_raw_token_amount(token_sold, token_decimals),
        "quote_symbol": pair.get("quote_symbol") or network.get("native_symbol", "ETH"),
        "quote_token_address": pair.get("quote_token_address") or NATIVE_TOKEN_ADDRESS,
        "quote_amount": format_raw_token_amount(quote_received, quote_decimals),
        "transaction_cost": transaction_cost_details(
            transaction,
            receipt,
            network.get("native_symbol") or ("BNB" if pair["network"] == "bsc" else "ETH"),
        ),
        "tx_hash": tx_hash.hex(),
        "timestamp": int(time.time()),
    }


def validate_managed_wallet_transaction(
    web3: Web3,
    network: dict,
    account_address: str,
    transaction: ManagedWalletTransaction,
) -> dict:
    to_address = Web3.to_checksum_address(transaction.to)
    if not transaction.data.startswith("0x") or len(transaction.data) > 100_000:
        raise ValueError("Nieprawidłowe dane transakcji.")
    try:
        data = bytes.fromhex(transaction.data[2:])
        value = int(transaction.value, 0) if transaction.value.startswith("0x") else int(transaction.value)
    except ValueError as error:
        raise ValueError("Nieprawidłowa wartość lub dane transakcji.") from error
    if value < 0:
        raise ValueError("Wartość transakcji nie może być ujemna.")
    if len(data) < 4:
        raise ValueError("Brak danych wywołania swapu.")

    routers = {
        address.lower()
        for address in (
            network.get("router"),
            network.get("v3_router"),
            network.get("universal_router"),
        )
        if address
    }
    selector = data[:4]
    signatures = {
        "v2_buy": "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
        "v2_sell_native": "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        "v2_swap": "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        "v3_single": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
        "v3_multicall": "multicall(bytes[])",
        "v3_unwrap": "unwrapWETH9(uint256,address)",
        "v4_execute": "execute(bytes,bytes[],uint256)",
        "erc20_approve": "approve(address,uint256)",
        "permit2_approve": "approve(address,address,uint160,uint48)",
    }
    selectors = {
        name: Web3.keccak(text=signature)[:4]
        for name, signature in signatures.items()
    }

    def decode(types: list) -> tuple:
        try:
            return web3.codec.decode(types, data[4:])
        except Exception as error:
            raise ValueError("Nieprawidłowe argumenty wywołania kontraktu.") from error

    def validate_v2_swap(call_data: bytes) -> None:
        call_selector = call_data[:4]
        if call_selector == selectors["v2_buy"]:
            amount_out_min, path, recipient, deadline = web3.codec.decode(
                ["uint256", "address[]", "address", "uint256"], call_data[4:]
            )
            native_input = True
            amount_in = value
        elif call_selector == selectors["v2_sell_native"]:
            amount_in, amount_out_min, path, recipient, deadline = web3.codec.decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                call_data[4:],
            )
            native_input = False
        elif call_selector == selectors["v2_swap"]:
            amount_in, amount_out_min, path, recipient, deadline = web3.codec.decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                call_data[4:],
            )
            native_input = False
        else:
            raise ValueError("Router V2: metoda swap nie jest dozwolona.")
        if recipient.lower() != account_address.lower():
            raise ValueError("Adres odbiorcy swapu nie pasuje do wybranego portfela.")
        if len(path) not in (2, 3) or any(
            not Web3.is_address(token) or token.lower() == NATIVE_TOKEN_ADDRESS.lower()
            for token in path
        ):
            raise ValueError("Nieprawidłowa ścieżka tokenów swapu.")
        if int(amount_out_min) <= 0 or (not native_input and int(amount_in) <= 0):
            raise ValueError("Kwota swapu musi być większa od zera.")
        if native_input != (value > 0):
            raise ValueError("Natywna kwota swapu nie zgadza się z wartością transakcji.")
        if int(deadline) < int(time.time()) or int(deadline) > int(time.time()) + 600:
            raise ValueError("Termin transakcji jest nieprawidłowy lub wygasł.")

    if to_address.lower() in routers:
        if to_address.lower() == str(network.get("router", "")).lower() and selector in {
            selectors["v2_buy"], selectors["v2_sell_native"], selectors["v2_swap"]
        }:
            validate_v2_swap(data)
        elif to_address.lower() == str(network.get("v3_router", "")).lower():
            if selector == selectors["v3_single"]:
                (params,) = decode([
                    "(address,address,uint24,address,uint256,uint256,uint160)"
                ])
                token_in, token_out, _fee, recipient, amount_in, amount_out_min, _limit = params
                if recipient.lower() != account_address.lower():
                    raise ValueError("Adres odbiorcy swapu V3 jest nieprawidłowy.")
                if int(amount_in) <= 0 or int(amount_out_min) <= 0:
                    raise ValueError("Kwota swapu V3 musi być większa od zera.")
                if value > 0 and (
                    value != int(amount_in)
                    or token_in.lower() != str(network.get("wrapped_native", "")).lower()
                ):
                    raise ValueError("Natywna kwota swapu V3 nie zgadza się z transakcją.")
                if token_in.lower() == NATIVE_TOKEN_ADDRESS.lower():
                    raise ValueError("Brak natywnej kwoty wejściowej dla swapu V3.")
                if token_in.lower() == token_out.lower():
                    raise ValueError("Tokeny wejściowy i wyjściowy muszą być różne.")
            elif selector == selectors["v3_multicall"]:
                (calls,) = decode(["bytes[]"])
                if len(calls) != 2 or calls[0][:4] != selectors["v3_single"] or calls[1][:4] != selectors["v3_unwrap"]:
                    raise ValueError("Dozwolony jest wyłącznie swap V3 z wypłatą natywną.")
                if value != 0:
                    raise ValueError("Swap V3 z wypłatą natywną nie powinien wysyłać wartości wejściowej.")
                validate_v3_single_without_value(
                    web3, calls[0], to_address,
                    network.get("wrapped_native"),
                )
                unwrap_amount, recipient = web3.codec.decode(
                    ["uint256", "address"], calls[1][4:]
                )
                if int(unwrap_amount) <= 0 or recipient.lower() != account_address.lower():
                    raise ValueError("Nieprawidłowa wypłata natywna swapu V3.")
            else:
                raise ValueError("Router V3: metoda nie jest dozwolona.")
        elif to_address.lower() == str(network.get("universal_router", "")).lower():
            if selector != selectors["v4_execute"]:
                raise ValueError("Universal Router: dozwolony jest wyłącznie swap V4.")
            commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"])
            if commands != b"\x10" or len(inputs) != 1 or int(deadline) < int(time.time()) or int(deadline) > int(time.time()) + 600:
                raise ValueError("Nieprawidłowe polecenie lub termin swapu V4.")
            actions, action_inputs = web3.codec.decode(
                ["bytes", "bytes[]"], inputs[0]
            )
            if actions != b"\x06\x0c\x0f" or len(action_inputs) != 3:
                raise ValueError("Niedozwolony zestaw akcji Uniswap V4.")
            (swap_params,) = web3.codec.decode(
                ["((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)"],
                action_inputs[0],
            )
            pool_key, zero_for_one, amount_in, amount_out_min, _price_limit, _hook_data = swap_params
            currency0, currency1, _fee, _spacing, _hooks = pool_key
            settle_currency, settle_amount = web3.codec.decode(
                ["address", "uint256"], action_inputs[1]
            )
            take_currency, take_amount = web3.codec.decode(
                ["address", "uint256"], action_inputs[2]
            )
            expected_input = currency0 if zero_for_one else currency1
            expected_output = currency1 if zero_for_one else currency0
            if (
                int(amount_in) <= 0 or int(amount_out_min) <= 0
                or int(settle_amount) != int(amount_in)
                or int(take_amount) != int(amount_out_min)
                or settle_currency.lower() != expected_input.lower()
                or take_currency.lower() != expected_output.lower()
            ):
                raise ValueError("Kwoty i tokeny swapu V4 nie są spójne.")
            native_input = expected_input.lower() == NATIVE_TOKEN_ADDRESS.lower()
            if native_input != (value > 0) or (native_input and value != int(amount_in)):
                raise ValueError("Wartość natywna swapu V4 nie zgadza się z transakcją.")
        else:
            raise ValueError("Niedozwolony kontrakt routera.")
    elif (
        to_address.lower() == str(network.get("permit2", "")).lower()
        and selector == selectors["permit2_approve"]
    ):
        token, spender, amount, expiration = decode(
            ["address", "address", "uint160", "uint48"]
        )
        if (
            not Web3.is_address(token)
            or spender.lower() != str(network.get("universal_router", "")).lower()
            or int(amount) <= 0
            or int(expiration) < int(time.time())
            or int(expiration) > int(time.time()) + 86400
            or value != 0
        ):
            raise ValueError("Nieprawidłowa, nadmierna lub nieobsługiwana autoryzacja Permit2.")
    elif selector == selectors["erc20_approve"]:
        spender, amount = decode(["address", "uint256"])
        allowed_spenders = routers | {str(network.get("permit2", "")).lower()}
        if (
            spender.lower() not in allowed_spenders
            or int(amount) <= 0
            or int(amount) > 2**160 - 1
            or value != 0
        ):
            raise ValueError("Nieprawidłowa lub nadmierna autoryzacja tokena.")
    else:
        raise ValueError("Dozwolone są wyłącznie swap DEX i ograniczone autoryzacje tokenów.")

    return {
        "to": to_address,
        "data": transaction.data,
        "value": value,
    }


def managed_wallet_input_requirement(
    web3: Web3,
    network: dict,
    validated_transaction: dict,
) -> Optional[tuple]:
    data = bytes.fromhex(validated_transaction["data"][2:])
    selector = data[:4]
    signatures = {
        "v2_sell_native": "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        "v2_swap": "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        "v3_single": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
        "v3_multicall": "multicall(bytes[])",
        "v4_execute": "execute(bytes,bytes[],uint256)",
    }
    selectors = {
        name: Web3.keccak(text=signature)[:4]
        for name, signature in signatures.items()
    }
    target = validated_transaction["to"].lower()
    value = validated_transaction["value"]
    if target == str(network.get("router", "")).lower() and selector in {
        selectors["v2_sell_native"], selectors["v2_swap"]
    }:
        amount_in, _minimum, _path, _recipient, _deadline = web3.codec.decode(
            ["uint256", "uint256", "address[]", "address", "uint256"],
            data[4:],
        )
        return target, int(amount_in), validated_transaction["to"]
    if target == str(network.get("v3_router", "")).lower():
        if selector == selectors["v3_single"]:
            (params,) = web3.codec.decode(
                ["(address,address,uint24,address,uint256,uint256,uint160)"],
                data[4:],
            )
        elif selector == selectors["v3_multicall"]:
            (calls,) = web3.codec.decode(["bytes[]"], data[4:])
            (params,) = web3.codec.decode(
                ["(address,address,uint24,address,uint256,uint256,uint160)"],
                calls[0][4:],
            )
        else:
            return None
        token_in, _token_out, _fee, _recipient, amount_in, _minimum, _limit = params
        if value:
            return None
        return token_in.lower(), int(amount_in), validated_transaction["to"]
    if target == str(network.get("universal_router", "")).lower() and selector == selectors["v4_execute"]:
        _commands, inputs, _deadline = web3.codec.decode(
            ["bytes", "bytes[]", "uint256"], data[4:]
        )
        _actions, action_inputs = web3.codec.decode(
            ["bytes", "bytes[]"], inputs[0]
        )
        (swap_params,) = web3.codec.decode(
            ["((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)"],
            action_inputs[0],
        )
        pool_key, zero_for_one, amount_in, _minimum, _limit, _hook_data = swap_params
        currency0, currency1, _fee, _spacing, _hooks = pool_key
        token_in = currency0 if zero_for_one else currency1
        if token_in.lower() == NATIVE_TOKEN_ADDRESS.lower():
            return None
        return token_in.lower(), int(amount_in), validated_transaction["to"]
    return None


def _send_managed_approval(web3: Web3, account, private_key: str, approval) -> None:
    gas_estimate = approval.estimate_gas({"from": account.address})
    transaction = approval.build_transaction({
        "from": account.address,
        "nonce": web3.eth.get_transaction_count(account.address),
        "chainId": web3.eth.chain_id,
        "gas": int(gas_estimate * 1.2),
        **transaction_fee_fields(web3),
    })
    signed = Account.sign_transaction(transaction, private_key)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"Autoryzacja tokena nie powiodła się: {tx_hash.hex()}")


def ensure_managed_wallet_input_allowance(
    web3: Web3,
    network: dict,
    account,
    private_key: str,
    requirement: Optional[tuple],
) -> None:
    if requirement is None:
        return
    token_address, amount_in, spender = requirement
    if amount_in <= 0 or amount_in > 2**160 - 1:
        raise RuntimeError("Kwota wejściowa przekracza bezpieczny limit autoryzacji tokena.")
    token_address = Web3.to_checksum_address(token_address)
    token = web3.eth.contract(address=token_address, abi=ERC20_TRADE_ABI)
    balance = token.functions.balanceOf(account.address).call()
    if balance < amount_in:
        decimals = token.functions.decimals().call()
        raise RuntimeError(
            "Niewystarczające saldo tokena wejściowego: "
            f"dostępne {format_raw_token_amount(balance, decimals)}, "
            f"wymagane {format_raw_token_amount(amount_in, decimals)}."
        )

    permit2_address = network.get("permit2")
    is_v4 = spender.lower() == str(network.get("universal_router", "")).lower()
    if is_v4 and not permit2_address:
        raise RuntimeError("Brak skonfigurowanego adresu Permit2 dla swapu Uniswap V4.")
    erc20_spender = permit2_address if is_v4 else spender
    allowance = token.functions.allowance(
        account.address, erc20_spender
    ).call()
    if allowance < amount_in:
        if allowance:
            _send_managed_approval(
                web3, account, private_key, token.functions.approve(erc20_spender, 0)
            )
        _send_managed_approval(
            web3,
            account,
            private_key,
            token.functions.approve(erc20_spender, amount_in),
        )
    if token.functions.allowance(account.address, erc20_spender).call() < amount_in:
        raise RuntimeError("Autoryzacja tokena wejściowego nadal jest niewystarczająca.")

    if not is_v4:
        return
    permit2_abi = [
        {
            "inputs": [
                {"type": "address"}, {"type": "address"}, {"type": "address"}
            ],
            "name": "allowance",
            "outputs": [
                {"type": "uint160"}, {"type": "uint48"}, {"type": "uint48"}
            ],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [
                {"type": "address"}, {"type": "address"},
                {"type": "uint160"}, {"type": "uint48"}
            ],
            "name": "approve",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]
    permit2 = web3.eth.contract(
        address=Web3.to_checksum_address(permit2_address),
        abi=permit2_abi,
    )
    permit_allowance = permit2.functions.allowance(
        account.address, token_address, spender
    ).call()
    expiration = int(permit_allowance[1])
    if int(permit_allowance[0]) < amount_in or expiration < int(time.time()) + 60:
        deadline = int(time.time()) + 3600
        _send_managed_approval(
            web3,
            account,
            private_key,
            permit2.functions.approve(token_address, spender, amount_in, deadline),
        )
    permit_allowance = permit2.functions.allowance(
        account.address, token_address, spender
    ).call()
    if (
        int(permit_allowance[0]) < amount_in
        or int(permit_allowance[1]) < int(time.time()) + 60
    ):
        raise RuntimeError("Autoryzacja Permit2 dla routera Uniswap V4 jest niewystarczająca.")


def validate_v3_single_without_value(
    web3: Web3,
    call_data: bytes,
    router_address: str,
    wrapped_native: Optional[str],
) -> None:
    (params,) = web3.codec.decode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        call_data[4:],
    )
    token_in, token_out, _fee, recipient, amount_in, amount_out_min, _limit = params
    if (
        recipient.lower() != router_address.lower()
        or not wrapped_native
        or token_out.lower() != wrapped_native.lower()
        or token_in.lower() == wrapped_native.lower()
        or token_in.lower() == token_out.lower()
        or int(amount_in) <= 0
        or int(amount_out_min) <= 0
    ):
        raise ValueError("Nieprawidłowa transakcja Uniswap V3.")


# --- STARTUP Domyślne załadowanie BOBER ---
def init_default_bober():
    pair_id = f"robinhood_{BOBER_CONTRACT[:8]}_eth".lower()
    if pair_id not in active_pairs:
        active_pairs[pair_id] = {
            "id": pair_id,
            "network": "robinhood",
            "token_address": BOBER_CONTRACT,
            "token_symbol": "BOBER",
            "quote_token_address": "0x0000000000000000000000000000000000000000", # Native ETH
            "quote_symbol": "ETH",
            "trade_amount": 0.05,
            "target_buy_price": None,
            "target_sell_price": None,
            "buy_percent": -5.0,
            "sell_percent": 10.0,
            "entry_price": None,
            "amount_held": 1000000.0,
            "pair_contract_address": None,
            "auto_trade_enabled": False,
            "auto_buy_mode": "single",
            "auto_buy_max_transactions": 10,
            "auto_buy_duration_minutes": 60,
            "auto_sell_enabled": False,
            "auto_sell_mode": "target_quantity",
            "auto_sell_max_transactions": 10,
            "auto_sell_duration_minutes": 60,
            "wallet_id": None,
            "max_slippage_percent": 1.0
        }


async def run_due_daily_schedule(pair_id: str, pair: dict, now: float) -> None:
    wallet_id = pair.get("wallet_id")
    for direction, enabled, schedule_key in (
        ("buy", pair.get("auto_trade_enabled"), "auto_buy_schedule"),
        ("sell", pair.get("auto_sell_enabled"), "auto_sell_schedule"),
    ):
        if not enabled:
            continue
        for entry in pair.get(schedule_key, []):
            due_at = float(entry.get("next_run_at") or 0)
            if due_at <= 0 or now < due_at:
                continue

            entry["next_run_at"] = next_daily_schedule_at(
                entry["time"], pair.get("schedule_timezone", "UTC"), now
            )
            if now - due_at > 60:
                trade_logs.append(
                    f"⏭️ HARMONOGRAM {direction.upper()} POMINIĘTY: "
                    f"{pair.get('token_symbol', 'TOKEN')} — minęło ponad 60 s "
                    "od zaplanowanej godziny; kolejna próba jutro."
                )
                continue

            with wallet_store_lock:
                wallet = wallet_store.get(wallet_id or "")
                unlocked = wallet_id in unlocked_wallet_keys
            if not wallet or not unlocked:
                trade_logs.append(
                    f"🔒 HARMONOGRAM {direction.upper()} POMINIĘTY: "
                    f"{pair.get('token_symbol', 'TOKEN')} — portfel zablokowany; "
                    "kolejna próba jutro."
                )
                continue

            trade_pair = pair.copy()
            try:
                if direction == "buy":
                    trade_pair["trade_amount"] = str(entry["max_quote"])
                    trade_pair["auto_buy_remaining_quantity"] = str(entry["quantity"])
                    trade_pair["auto_buy_exact_schedule_quantity"] = True
                    trade = await asyncio.to_thread(
                        execute_auto_buy, trade_pair, wallet_id
                    )
                else:
                    trade_pair["auto_sell_amount"] = str(entry["quantity"])
                    trade = await asyncio.to_thread(
                        execute_auto_sell, trade_pair, wallet_id
                    )
                record_wallet_trade(wallet_id, trade)
                pair[f"auto_{direction}_last_schedule_at"] = now
                trade_logs.append(
                    f"✅ HARMONOGRAM {direction.upper()} POTWIERDZONY: "
                    f"{trade['token_amount']} {trade['token_symbol']} — "
                    f"{trade['tx_hash']} · {format_trade_cost_log(trade)}"
                    f"{transaction_explorer_link(pair.get('network', ''), trade['tx_hash'])}"
                )
            except Exception as error:
                logging.exception(
                    "Transakcja z harmonogramu %s nie powiodła się dla %s",
                    direction,
                    pair_id,
                )
                trade_logs.append(
                    f"❌ HARMONOGRAM {direction.upper()} NIEUDANY: "
                    f"{pair.get('token_symbol', 'TOKEN')} — {error}. "
                    "Nie ponawiam; kolejna próba zgodnie z harmonogramem."
                    f"{transaction_explorer_link(pair.get('network', ''), transaction_hash_from_error(error) or '')}"
                )


# --- PĘTLA MONITORUJĄCA BOTA ---
async def trading_bot_loop():
    init_default_bober()
    
    while True:
        await asyncio.to_thread(update_market_ticker)
        updates = []
        total_value_usd = 0.0
        total_pnl_usd = 0.0

        for pair_id, pair in list(active_pairs.items()):
            await run_due_daily_schedule(pair_id, pair, time.time())
            gt_data = await asyncio.to_thread(
                fetch_geckoterminal_details,
                pair["network"],
                pair["token_address"],
                pair.get("pair_contract_address")
            )
            current_price = gt_data["price_usd"]
            if current_price is None or current_price == 0:
                pair["dex_pairs_list"] = gt_data["dex_pairs_list"] or pair.get("dex_pairs_list", [])
                pair["pool_address"] = gt_data["pool_address"] or pair.get("pool_address")
                pair["active_slug"] = gt_data["active_network_slug"]
                pair["logo_url"] = gt_data["logo_url"] or pair.get("logo_url")
                pair["token_name"] = gt_data.get("token_name") or pair.get("token_name")
                pair["token_symbol"] = gt_data.get("token_symbol") or pair.get("token_symbol")
                pair["status"] = "PRICE_UNAVAILABLE"
                last_price = pair.get("last_price")
                if last_price:
                    amount_held = float(pair.get("amount_held") or 0.0)
                    entry_price = float(pair.get("entry_price") or 0.0)
                    total_value_usd += amount_held * last_price
                    total_pnl_usd += amount_held * (last_price - entry_price)
                updates.append(pair_snapshot(pair_id, pair))
                continue

            pair["last_price"] = current_price
            pair["status"] = "MONITORING"
            pair["logo_url"] = gt_data["logo_url"] or pair.get("logo_url")
            pair["website"] = gt_data["website"] or pair.get("website")
            pair["twitter"] = gt_data["twitter"] or pair.get("twitter")
            pair["token_name"] = gt_data.get("token_name") or pair.get("token_name")
            pair["pool_address"] = gt_data["pool_address"] or pair.get("pool_address")
            pair["dex_pairs_list"] = gt_data["dex_pairs_list"] or pair.get("dex_pairs_list", [])
            pair["change_24h"] = gt_data["change_24h"]
            pair["active_slug"] = gt_data["active_network_slug"]
            
            if gt_data["token_symbol"]:
                pair["token_symbol"] = gt_data["token_symbol"]

            if pair.get("entry_price") is None or pair["entry_price"] == 0:
                pair["entry_price"] = current_price

            calc_buy_target = pair.get("target_buy_price")
            calc_sell_target = pair.get("target_sell_price")

            auto_buy_mode = pair.get("auto_buy_mode", "single")
            auto_sell_mode = pair.get("auto_sell_mode", "target_quantity")
            if auto_buy_mode != "target_price" and pair.get("buy_percent") is not None and pair["entry_price"] > 0:
                calc_buy_target = pair["entry_price"] * (1 + pair["buy_percent"] / 100.0)

            if auto_sell_mode != "target_price" and pair.get("sell_percent") is not None and pair["entry_price"] > 0:
                calc_sell_target = pair["entry_price"] * (1 + pair["sell_percent"] / 100.0)

            amount_held = pair.get("amount_held", 0.0)
            holding_value_usd = amount_held * current_price
            cost_basis_usd = amount_held * pair["entry_price"]
            unrealized_pnl_usd = holding_value_usd - cost_basis_usd
            unrealized_pnl_percent = ((current_price - pair["entry_price"]) / pair["entry_price"] * 100) if pair["entry_price"] > 0 else 0.0

            total_value_usd += holding_value_usd
            total_pnl_usd += unrealized_pnl_usd

            status = "MONITORING"

            buy_price_trigger = bool(
                auto_buy_mode not in {"target_price", "schedule"}
                and calc_buy_target
                and current_price <= calc_buy_target
            )
            buy_quantity_campaign = bool(
                pair.get("auto_trade_enabled")
                and auto_buy_mode == "target_quantity"
                and pair.get("auto_buy_quantity_duration_minutes") is not None
            )
            buy_trigger = buy_price_trigger or buy_quantity_campaign
            buy_ladder_ready = False
            buy_ladder_complete = False
            buy_count, bought_amount, spent_amount = (0, Decimal(0), Decimal(0))
            if pair.get("auto_trade_enabled") and auto_buy_mode == "target_price":
                buy_count, bought_amount, spent_amount = wallet_pair_buy_totals(
                    pair.get("wallet_id") or "", pair_id, pair.get("auto_buy_campaign_id")
                )
                if not pair.get("auto_buy_start_price"):
                    pair["auto_buy_start_price"] = current_price
                if not pair.get("auto_buy_started_at"):
                    pair["auto_buy_started_at"] = time.time()
                max_transactions = int(pair.get("auto_buy_max_transactions") or 1)
                duration_minutes = int(pair.get("auto_buy_duration_minutes") or 60)
                try:
                    next_level, price_level_reached, buy_ladder_complete = price_ladder_step(
                        pair["auto_buy_start_price"],
                        pair.get("target_buy_price") or 0,
                        current_price,
                        buy_count,
                        max_transactions,
                    )
                    started_at = float(pair.get("auto_buy_started_at") or time.time())
                    last_trade_at = pair.get("auto_buy_last_success_at")
                    last_trade_at = float(last_trade_at) if last_trade_at is not None else None
                    buy_ladder_ready = price_level_reached and price_ladder_trade_due(
                        started_at, time.time(), buy_count, max_transactions,
                        duration_minutes, last_trade_at
                    )
                    pair["auto_buy_next_trade_at"] = price_ladder_next_trade_at(
                        started_at, buy_count, max_transactions, duration_minutes,
                        last_trade_at
                    )
                    if buy_ladder_complete and not pair.get("auto_buy_target_logged"):
                        status = "AUTO_BUY_COMPLETE"
                        trade_logs.append(
                            f"✅ DRABINKA AUTO-KUPNA ZAKOŃCZONA: {pair['token_symbol']} "
                            f"— wykonano {buy_count}/{max_transactions} transakcji."
                        )
                        pair["auto_buy_target_logged"] = True
                    elif buy_ladder_complete:
                        status = "AUTO_BUY_COMPLETE"
                    pair["auto_buy_next_price"] = next_level
                    if buy_ladder_ready:
                        buy_trigger = True
                        status = "TRIGGER_BUY"
                    elif price_level_reached and not buy_ladder_complete:
                        status = "AUTO_BUY_WAITING_SCHEDULE"
                except (InvalidOperation, ValueError) as error:
                    logging.error("Nieprawidłowa drabinka kupna dla %s: %s", pair_id, error)
                    if not pair.get("auto_buy_target_logged"):
                        trade_logs.append(f"❌ DRABINKA AUTO-KUPNA WSTRZYMANA: {error}")
                        pair["auto_buy_target_logged"] = True

            if buy_price_trigger:
                status = "TRIGGER_BUY"
                log_msg = (
                    f"🟢 KUPNO [{pair['network'].upper()}]: {pair['token_symbol']} "
                    f"osiągnął cenę ${current_price:.8f} "
                    f"(Cel: ${calc_buy_target or pair.get('target_buy_price') or 0:.8f})"
                )
                if not pair.get("buy_signal_active"):
                    trade_logs.append(log_msg)
                    pair["buy_signal_active"] = True
            else:
                pair["buy_signal_active"] = False

            target_changed = pair.get("last_auto_buy_target") != calc_buy_target
            if target_changed:
                pair["last_auto_buy_target"] = calc_buy_target
                pair["auto_buy_trigger_consumed"] = False

            wallet_id = pair.get("wallet_id")
            buy_mode_is_campaign = auto_buy_mode in {"target_quantity", "target_price"}
            buy_eligible = bool(
                pair.get("auto_trade_enabled")
                and buy_trigger
                and (buy_mode_is_campaign or not pair.get("auto_buy_trigger_consumed"))
                and not buy_ladder_complete
            )
            if buy_eligible:
                wallet = wallet_store.get(wallet_id) if wallet_id else None
                if not wallet or wallet_id not in unlocked_wallet_keys:
                    status = "TRIGGER_BUY_LOCKED"
                    if not pair.get("auto_buy_waiting_logged"):
                        trade_logs.append(
                            f"🔒 AUTO-KUPNO WSTRZYMANE: {pair['token_symbol']} — "
                            "dodaj/odblokuj portfel i sprawdź ustawienia."
                        )
                        pair["auto_buy_waiting_logged"] = True
                else:
                    pair["auto_buy_waiting_logged"] = False
                    trade_pair = pair.copy()
                    now = time.time()
                    if auto_buy_mode == "target_quantity":
                        buy_count, bought_amount, spent_amount = wallet_pair_buy_totals(
                            wallet_id, pair_id, pair.get("auto_buy_campaign_id")
                        )
                        max_transactions = int(pair.get("auto_buy_max_transactions") or 1)
                        target_quantity = Decimal(str(pair.get("auto_buy_target_quantity") or 0))
                        total_budget = Decimal(str(pair.get("auto_buy_total_budget") or 0))
                        remaining_budget = total_budget - spent_amount
                        remaining_quantity = target_quantity - bought_amount
                        if buy_count >= max_transactions or remaining_budget <= 0 or remaining_quantity <= 0:
                            status = "AUTO_BUY_COMPLETE"
                            pair["auto_buy_next_trade_at"] = None
                            buy_eligible = False
                            if not pair.get("auto_buy_target_logged"):
                                trade_logs.append(
                                    f"✅ AUTO-KUPNO ZAKOŃCZONE: {pair['token_symbol']} — "
                                    f"kupiono {bought_amount} z {target_quantity} "
                                    f"{pair['token_symbol']} za {spent_amount} "
                                    f"{pair.get('quote_symbol') or 'QUOTE'} w {buy_count} transakcjach."
                                )
                                pair["auto_buy_target_logged"] = True
                        else:
                            quantity_duration = pair.get("auto_buy_quantity_duration_minutes")
                            if quantity_duration is not None:
                                started_at = float(
                                    pair.setdefault("auto_buy_quantity_started_at", now)
                                )
                                last_success_at = pair.get("auto_buy_quantity_last_success_at")
                                last_success_at = (
                                    float(last_success_at) if last_success_at is not None else None
                                )
                                schedule_ready = price_ladder_trade_due(
                                    started_at, now, buy_count, max_transactions,
                                    int(quantity_duration), last_success_at
                                )
                                pair["auto_buy_next_trade_at"] = price_ladder_next_trade_at(
                                    started_at, buy_count, max_transactions,
                                    int(quantity_duration), last_success_at
                                )
                                last_attempt_at = float(pair.get("auto_buy_last_attempt_at") or 0)
                                retry_pending = last_attempt_at > (last_success_at or 0)
                                if not schedule_ready or (
                                    retry_pending and now - last_attempt_at < 60
                                ):
                                    buy_eligible = False
                                    status = "AUTO_BUY_WAITING_SCHEDULE"
                                else:
                                    status = "TRIGGER_BUY"
                            elif (
                                now - float(pair.get("auto_buy_last_attempt_at") or 0) < 60
                                or now - float(pair.get("auto_trade_last_attempt_at") or 0) < 60
                            ):
                                buy_eligible = False
                            if buy_eligible:
                                tranche = total_budget / max_transactions
                                trade_pair["trade_amount"] = str(min(tranche, remaining_budget))
                                trade_pair["auto_buy_remaining_quantity"] = str(
                                    auto_buy_quantity_tranche(
                                        remaining_quantity, buy_count, max_transactions
                                    )
                                )
                    elif auto_buy_mode == "target_price":
                        max_transactions = int(pair.get("auto_buy_max_transactions") or 1)
                        if buy_count >= max_transactions:
                            status = "AUTO_BUY_COMPLETE"
                            buy_eligible = False
                        elif now < float(pair.get("auto_buy_retry_after") or 0):
                            buy_eligible = False
                    elif now - float(pair.get("auto_trade_last_attempt_at") or 0) < 60:
                        buy_eligible = False

                    if buy_eligible:
                        if auto_buy_mode == "single":
                            pair["auto_buy_trigger_consumed"] = True
                        pair["auto_buy_last_attempt_at"] = now
                        pair["auto_trade_last_attempt_at"] = now
                        try:
                            trade = await asyncio.to_thread(execute_auto_buy, trade_pair, wallet_id)
                            record_wallet_trade(wallet_id, trade)
                            pair["last_auto_buy_tx"] = trade["tx_hash"]
                            if auto_buy_mode == "target_price":
                                pair["auto_buy_last_success_at"] = float(
                                    trade.get("timestamp") or time.time()
                                )
                            elif pair.get("auto_buy_quantity_duration_minutes") is not None:
                                pair["auto_buy_quantity_last_success_at"] = float(
                                    trade.get("timestamp") or time.time()
                                )
                            pair["auto_buy_retry_after"] = 0
                            trade_logs.append(
                                f"✅ AUTO-KUPNO POTWIERDZONE: "
                                f"{trade['token_amount'] or 'ilość nieodczytana'} {trade['token_symbol']} "
                                f"za {trade['quote_amount'] or 'kwota nieodczytana'} "
                                f"{trade['quote_symbol']} — {trade['tx_hash']} · "
                                f"{format_trade_cost_log(trade)}"
                                f"{transaction_explorer_link(pair['network'], trade['tx_hash'])}"
                            )
                        except Exception as error:
                            logging.exception("Auto-kupno nie powiodło się dla %s", pair_id)
                            if auto_buy_mode == "target_price":
                                pair["auto_buy_retry_after"] = now + 60
                            trade_logs.append(
                                f"❌ AUTO-KUPNO NIEUDANE: {pair['token_symbol']} — {error}"
                                f"{transaction_explorer_link(pair['network'], transaction_hash_from_error(error) or '')}"
                            )

            if not buy_trigger and calc_buy_target:
                pair["last_auto_buy_target"] = calc_buy_target
                pair["auto_buy_trigger_consumed"] = False

            sell_trigger = bool(
                auto_sell_mode not in {"target_price", "schedule"}
                and calc_sell_target
                and current_price >= calc_sell_target
            )
            sell_ladder_ready = False
            sell_ladder_complete = False
            sell_count, sold_amount, received_amount = (0, Decimal(0), Decimal(0))
            if pair.get("auto_sell_enabled") and auto_sell_mode == "target_price":
                sell_count, sold_amount, received_amount = wallet_pair_trade_totals(
                    wallet_id or "", pair_id, "sell", pair.get("auto_sell_campaign_id")
                )
                if not pair.get("auto_sell_start_price"):
                    pair["auto_sell_start_price"] = current_price
                if not pair.get("auto_sell_started_at"):
                    pair["auto_sell_started_at"] = time.time()
                max_sell_transactions = int(pair.get("auto_sell_max_transactions") or 1)
                sell_duration_minutes = int(pair.get("auto_sell_duration_minutes") or 60)
                try:
                    next_sell_level, sell_price_level_reached, sell_ladder_complete = price_ladder_step(
                        pair["auto_sell_start_price"],
                        pair.get("target_sell_price") or 0,
                        current_price,
                        sell_count,
                        max_sell_transactions,
                    )
                    sell_started_at = float(pair.get("auto_sell_started_at") or time.time())
                    last_sell_trade_at = pair.get("auto_sell_last_success_at")
                    last_sell_trade_at = (
                        float(last_sell_trade_at)
                        if last_sell_trade_at is not None
                        else None
                    )
                    sell_ladder_ready = sell_price_level_reached and price_ladder_trade_due(
                        sell_started_at, time.time(), sell_count,
                        max_sell_transactions, sell_duration_minutes,
                        last_sell_trade_at
                    )
                    pair["auto_sell_next_trade_at"] = price_ladder_next_trade_at(
                        sell_started_at, sell_count, max_sell_transactions,
                        sell_duration_minutes, last_sell_trade_at
                    )
                    if sell_ladder_complete and not pair.get("auto_sell_target_logged"):
                        status = "AUTO_SELL_COMPLETE"
                        trade_logs.append(
                            f"✅ DRABINKA AUTO-SPRZEDAŻY ZAKOŃCZONA: {pair['token_symbol']} "
                            f"— wykonano {sell_count}/{max_sell_transactions} transakcji."
                        )
                        pair["auto_sell_target_logged"] = True
                    elif sell_ladder_complete:
                        status = "AUTO_SELL_COMPLETE"
                    pair["auto_sell_next_price"] = next_sell_level
                    if sell_ladder_ready:
                        sell_trigger = True
                        status = "TRIGGER_SELL"
                    elif sell_price_level_reached and not sell_ladder_complete:
                        status = "AUTO_SELL_WAITING_SCHEDULE"
                except (InvalidOperation, ValueError) as error:
                    logging.error("Nieprawidłowa drabinka sprzedaży dla %s: %s", pair_id, error)
                    if not pair.get("auto_sell_target_logged"):
                        trade_logs.append(f"❌ DRABINKA AUTO-SPRZEDAŻY WSTRZYMANA: {error}")
                        pair["auto_sell_target_logged"] = True

            if sell_trigger:
                status = "TRIGGER_SELL"
                if not pair.get("sell_signal_active"):
                    trade_logs.append(
                        f"🔴 SPRZEDAŻ [{pair['network'].upper()}]: {pair['token_symbol']} "
                        f"osiągnął cenę ${current_price:.8f} "
                        f"(Cel: ${calc_sell_target or pair.get('target_sell_price') or 0:.8f})"
                    )
                pair["sell_signal_active"] = True
            else:
                pair["sell_signal_active"] = False

            sell_eligible = bool(
                pair.get("auto_sell_enabled")
                and sell_trigger
                and not sell_ladder_complete
            )
            if sell_eligible:
                wallet = wallet_store.get(wallet_id) if wallet_id else None
                if not wallet or wallet_id not in unlocked_wallet_keys:
                    status = "TRIGGER_SELL_LOCKED"
                    if not pair.get("auto_sell_waiting_logged"):
                        trade_logs.append(
                            f"🔒 AUTO-SPRZEDAŻ WSTRZYMANA: {pair['token_symbol']} — "
                            "dodaj/odblokuj portfel i sprawdź ustawienia."
                        )
                        pair["auto_sell_waiting_logged"] = True
                else:
                    pair["auto_sell_waiting_logged"] = False
                    sell_count, sold_amount, received_amount = wallet_pair_trade_totals(
                        wallet_id, pair_id, "sell", pair.get("auto_sell_campaign_id")
                    )
                    quantity_duration = pair.get("auto_sell_quantity_duration_minutes")
                    quantity_schedule_waiting = False
                    now = time.time()
                    if auto_sell_mode == "target_quantity" and quantity_duration is not None:
                        started_at = float(
                            pair.setdefault("auto_sell_quantity_started_at", now)
                        )
                        last_success_at = pair.get("auto_sell_quantity_last_success_at")
                        last_success_at = (
                            float(last_success_at) if last_success_at is not None else None
                        )
                        schedule_ready = price_ladder_trade_due(
                            started_at, now, sell_count,
                            int(pair.get("auto_sell_max_transactions") or 1),
                            int(quantity_duration), last_success_at
                        )
                        pair["auto_sell_next_trade_at"] = price_ladder_next_trade_at(
                            started_at, sell_count,
                            int(pair.get("auto_sell_max_transactions") or 1),
                            int(quantity_duration), last_success_at
                        )
                        last_attempt_at = float(pair.get("auto_sell_last_attempt_at") or 0)
                        retry_pending = last_attempt_at > (last_success_at or 0)
                        quantity_schedule_waiting = not schedule_ready or (
                            retry_pending and now - last_attempt_at < 60
                        )
                    if (
                        auto_sell_mode == "target_price"
                        and time.time() < float(pair.get("auto_sell_retry_after") or 0)
                    ) or (
                        auto_sell_mode == "target_quantity"
                        and quantity_duration is None
                        and time.time() - float(pair.get("auto_trade_last_attempt_at") or 0) < 60
                    ) or quantity_schedule_waiting:
                        sell_eligible = False
                    else:
                        sell_count, sold_amount, received_amount = wallet_pair_trade_totals(
                            wallet_id, pair_id, "sell", pair.get("auto_sell_campaign_id")
                        )
                        max_sell_transactions = int(pair.get("auto_sell_max_transactions") or 1)
                        target_sell_quantity = Decimal(str(pair.get("auto_sell_target_quantity") or 0))
                        remaining_sell_quantity = target_sell_quantity - sold_amount
                        if (
                            sell_count >= max_sell_transactions
                            or remaining_sell_quantity <= 0
                        ):
                            status = "AUTO_SELL_COMPLETE"
                            sell_eligible = False
                            if not pair.get("auto_sell_target_logged"):
                                trade_logs.append(
                                    f"✅ AUTO-SPRZEDAŻ ZAKOŃCZONA: {pair['token_symbol']} — "
                                    f"sprzedano {sold_amount} z {target_sell_quantity} "
                                    f"{pair['token_symbol']} w {sell_count} transakcjach."
                                )
                                pair["auto_sell_target_logged"] = True
                        else:
                            tranche = target_sell_quantity / max_sell_transactions
                            trade_pair = pair.copy()
                            trade_pair["auto_sell_amount"] = str(
                                min(tranche, remaining_sell_quantity)
                            )
                            trade_pair["auto_sell_campaign_id"] = pair.get("auto_sell_campaign_id")

                    if sell_eligible:
                        now = time.time()
                        pair["auto_sell_last_attempt_at"] = now
                        pair["auto_trade_last_attempt_at"] = now
                        try:
                            trade = await asyncio.to_thread(
                                execute_auto_sell, trade_pair, wallet_id
                            )
                            record_wallet_trade(wallet_id, trade)
                            pair["last_auto_sell_tx"] = trade["tx_hash"]
                            if auto_sell_mode == "target_price":
                                pair["auto_sell_last_success_at"] = float(
                                    trade.get("timestamp") or time.time()
                                )
                            elif pair.get("auto_sell_quantity_duration_minutes") is not None:
                                pair["auto_sell_quantity_last_success_at"] = float(
                                    trade.get("timestamp") or time.time()
                                )
                            pair["auto_sell_retry_after"] = 0
                            trade_logs.append(
                                f"✅ AUTO-SPRZEDAŻ POTWIERDZONA: "
                                f"{trade['token_amount']} {trade['token_symbol']} "
                                f"za {trade['quote_amount']} {trade['quote_symbol']} "
                                f"— {trade['tx_hash']} · {format_trade_cost_log(trade)}"
                                f"{transaction_explorer_link(pair['network'], trade['tx_hash'])}"
                            )
                        except Exception as error:
                            logging.exception("Auto-sprzedaż nie powiodła się dla %s", pair_id)
                            if auto_sell_mode == "target_price":
                                pair["auto_sell_retry_after"] = now + 60
                            trade_logs.append(
                                f"❌ AUTO-SPRZEDAŻ NIEUDANA: {pair['token_symbol']} — {error}"
                                f"{transaction_explorer_link(pair['network'], transaction_hash_from_error(error) or '')}"
                            )

            auto_buy_trade_count, auto_buy_quantity_bought, auto_buy_budget_spent = (
                wallet_pair_buy_totals(wallet_id, pair_id, pair.get("auto_buy_campaign_id"))
                if wallet_id else (0, Decimal(0), Decimal(0))
            )
            auto_sell_trade_count, auto_sell_quantity_sold, auto_sell_quote_received = (
                wallet_pair_trade_totals(
                    wallet_id, pair_id, "sell", pair.get("auto_sell_campaign_id")
                )
                if wallet_id else (0, Decimal(0), Decimal(0))
            )
            updates.append({
                "pair_id": pair_id,
                "token_symbol": pair["token_symbol"],
                "token_name": pair.get("token_name"),
                "network": pair["network"],
                "active_slug": pair.get("active_slug", "robinhood"),
                "token_address": pair["token_address"],
                "quote_token_address": pair["quote_token_address"],
                "quote_symbol": pair["quote_symbol"],
                "trade_amount": pair["trade_amount"],
                "entry_price": pair["entry_price"],
                "amount_held": amount_held,
                "holding_value_usd": holding_value_usd,
                "unrealized_pnl_usd": unrealized_pnl_usd,
                "unrealized_pnl_percent": unrealized_pnl_percent,
                "logo_url": pair.get("logo_url"),
                "website": pair.get("website"),
                "twitter": pair.get("twitter"),
                "pool_address": pair.get("pool_address"),
                "dex_pairs_list": pair.get("dex_pairs_list", []),
                "change_24h": pair.get("change_24h", 0.0),
                "current_price": current_price,
                "target_buy_price": calc_buy_target,
                "target_sell_price": calc_sell_target,
                "auto_trade_enabled": pair.get("auto_trade_enabled", False),
                "wallet_id": pair.get("wallet_id"),
                "auto_buy_mode": pair.get("auto_buy_mode", "single"),
                "auto_buy_target_quantity": pair.get("auto_buy_target_quantity"),
                "auto_buy_total_budget": pair.get("auto_buy_total_budget"),
                "auto_buy_max_transactions": pair.get("auto_buy_max_transactions", 10),
                "auto_buy_duration_minutes": pair.get("auto_buy_duration_minutes", 60),
                "auto_buy_quantity_duration_minutes": pair.get("auto_buy_quantity_duration_minutes"),
                "auto_buy_schedule": pair.get("auto_buy_schedule", []),
                "auto_buy_trade_count": auto_buy_trade_count,
                "auto_buy_quantity_bought": format(auto_buy_quantity_bought, "f"),
                "auto_buy_budget_spent": format(auto_buy_budget_spent, "f"),
                "auto_buy_next_price": pair.get("auto_buy_next_price"),
                "auto_buy_next_trade_at": pair.get("auto_buy_next_trade_at"),
                "auto_sell_enabled": pair.get("auto_sell_enabled", False),
                "auto_sell_mode": auto_sell_mode,
                "auto_sell_target_quantity": pair.get("auto_sell_target_quantity"),
                "auto_sell_max_transactions": pair.get("auto_sell_max_transactions", 10),
                "auto_sell_duration_minutes": pair.get("auto_sell_duration_minutes", 60),
                "auto_sell_quantity_duration_minutes": pair.get("auto_sell_quantity_duration_minutes"),
                "auto_sell_schedule": pair.get("auto_sell_schedule", []),
                "schedule_timezone": pair.get("schedule_timezone", "UTC"),
                "auto_sell_trade_count": auto_sell_trade_count,
                "auto_sell_quantity_sold": format(auto_sell_quantity_sold, "f"),
                "auto_sell_quote_received": format(auto_sell_quote_received, "f"),
                "auto_sell_next_price": pair.get("auto_sell_next_price"),
                "auto_sell_next_trade_at": pair.get("auto_sell_next_trade_at"),
                "status": status
            })

        bot_stats = {
            "total_invested": total_value_usd,
            "total_pnl_usd": total_pnl_usd,
            "total_pnl_percent": (total_pnl_usd / total_value_usd * 100) if total_value_usd > 0 else 0.0
        }
        latest_bot_stats.update(bot_stats)

        await manager.broadcast({
            "type": "PRICE_UPDATE", 
            "ticker": ticker_prices,
            "data": updates, 
            "stats": bot_stats,
            "logs": trade_logs[-15:]
        })
        await asyncio.sleep(4)

@app.on_event("startup")
async def startup_event():
    load_wallet_store()
    asyncio.create_task(trading_bot_loop())


def holder_distribution_preflight(
    wallet_id: str,
    token_address: str,
    amount_min: Decimal,
    recipient_count: int,
    amount_max: Optional[Decimal] = None,
) -> dict:
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise RuntimeError("Wybrany portfel nie istnieje albo jest zablokowany.")
    network = NETWORK_CONFIG["robinhood"]
    web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 20}))
    if not web3.is_connected() or web3.eth.chain_id != network["chain_id"]:
        raise RuntimeError("Nie można zweryfikować połączenia z Robinhood Chain.")
    token_address = Web3.to_checksum_address(token_address)
    if not web3.eth.get_code(token_address):
        raise ValueError("Podany adres nie jest kontraktem w Robinhood Chain.")
    contract = web3.eth.contract(
        address=token_address,
        abi=ERC20_TRADE_ABI,
    )
    try:
        actual_decimals = int(contract.functions.decimals().call())
        symbol = str(contract.functions.symbol().call()) or "TOKEN"
    except Exception as error:
        raise ValueError("Podany kontrakt nie udostępnia prawidłowych metadanych ERC-20.") from error
    if not 0 <= actual_decimals <= 255:
        raise ValueError("Kontrakt zwrócił nieprawidłową precyzję tokena.")
    amount_max = amount_min if amount_max is None else amount_max
    scale = 10 ** actual_decimals
    scaled_min = amount_min * scale
    scaled_max = amount_max * scale
    if (
        scaled_min != scaled_min.to_integral_value()
        or scaled_max != scaled_max.to_integral_value()
    ):
        raise ValueError(
            f"Ilość przekracza precyzję tokena ({actual_decimals} miejsc dziesiętnych)."
        )
    amount_min_raw = int(scaled_min)
    amount_max_raw = int(scaled_max)
    if amount_min_raw <= 0 or amount_max_raw <= 0:
        raise ValueError("Ilość jest mniejsza od najmniejszej jednostki tokena.")
    if amount_max_raw < amount_min_raw:
        raise ValueError("Maksymalna ilość musi być większa lub równa minimalnej.")
    amount_raws = [
        amount_min_raw + secrets.randbelow(amount_max_raw - amount_min_raw + 1)
        for _ in range(recipient_count)
    ]
    account = Account.from_key(private_key)
    total_required = sum(amount_raws)
    if total_required > 2**256 - 1:
        raise ValueError("Łączna ilość przekracza limit kontraktu ERC-20.")
    balance = contract.functions.balanceOf(account.address).call()
    if balance < total_required:
        raise ValueError(
            "Niewystarczające saldo tokena: "
            f"dostępne {format_raw_token_amount(balance, actual_decimals)}, "
            f"potrzebne łącznie {format_raw_token_amount(total_required, actual_decimals)} "
            f"({recipient_count} odbiorców, po "
            f"{format_raw_token_amount(amount_min_raw, actual_decimals)}–"
            f"{format_raw_token_amount(amount_max_raw, actual_decimals)})."
        )
    return {
        "decimals": actual_decimals,
        "symbol": symbol,
        "amount_raw": amount_min_raw,
        "amount_raws": amount_raws,
    }


async def run_holder_distribution(job_id: str) -> None:
    with holder_distribution_lock:
        job = holder_distribution_jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
    for index, recipient in enumerate(job["recipients"], start=1):
        amount_raw = job["amount_raws"][index - 1]
        try:
            tx_hash = await asyncio.to_thread(
                send_holder_token_transfer,
                job["wallet_id"],
                job["token_address"],
                recipient,
                amount_raw,
            )
        except Exception as error:
            logging.exception(
                "Rozsyłanie tokenów zatrzymano przy odbiorcy %s/%s",
                index,
                len(job["recipients"]),
            )
            with holder_distribution_lock:
                job["status"] = "failed" if job["sent"] == 0 else "partial_failed"
                job["failed_recipient"] = recipient
                job["error"] = str(error)
                job["completed_at"] = int(time.time())
            return
        with holder_distribution_lock:
            job["sent"] = index
            job["transactions"].append({
                "recipient": recipient,
                "amount": format_raw_token_amount(amount_raw, job["decimals"]),
                "tx_hash": tx_hash,
            })
    with holder_distribution_lock:
        job["status"] = "completed"
        job["completed_at"] = int(time.time())


def public_holder_distribution(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "token_address": job["token_address"],
        "token_symbol": job["token_symbol"],
        "wallet_address": job["wallet_address"],
        "recipient_count": len(job["recipients"]),
        "sent": job["sent"],
        "amount_per_recipient": job["amount_per_recipient"],
        "amount_min": job["amount_min"],
        "amount_max": job["amount_max"],
        "total_amount": job["total_amount"],
        "transactions": list(job["transactions"]),
        "failed_recipient": job.get("failed_recipient"),
        "error": job.get("error"),
        "created_at": job["created_at"],
        "completed_at": job.get("completed_at"),
    }


@app.get("/api/wallets")
async def list_wallets():
    with wallet_store_lock:
        wallets = [public_wallet(wallet_id, wallet) for wallet_id, wallet in wallet_store.items()]
    return {"wallets": wallets}


@app.get("/api/robinhood/tokens/{token_address}/holders")
async def list_robinhood_token_holders(
    token_address: str,
    limit: int = 500,
    cursor: Optional[str] = None,
):
    if not Web3.is_address(token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena.")
    limit = max(1, min(limit, 500))
    cursor_param = None
    cursor_key = "{}"
    raw_cursor = cursor
    if raw_cursor:
        try:
            cursor_param = json.loads(raw_cursor)
        except (TypeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail="Nieprawidłowy kursor paginacji.") from error
        if (
            not isinstance(cursor_param, dict)
            or len(raw_cursor) > 4096
            or any(
                not isinstance(key, str)
                or (value is not None and not isinstance(value, (str, int, float, bool)))
                for key, value in cursor_param.items()
            )
        ):
            raise HTTPException(status_code=400, detail="Nieprawidłowy kursor paginacji.")
        cursor_key = json.dumps(cursor_param, sort_keys=True, separators=(",", ":"))
    token_key = Web3.to_checksum_address(token_address).lower()
    cache_key = f"{token_key}:{cursor_key}"
    with wallet_store_lock:
        cached = robinhood_holder_cache.get(cache_key)
        if cached and time.time() - cached["fetched_at"] < 60:
            return {**cached, "holders": cached["holders"][:limit]}
    try:
        return await asyncio.to_thread(
            fetch_robinhood_token_holders, token_address, limit, cursor_param
        )
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/robinhood/tokens/{token_address}/metadata")
async def get_robinhood_token_metadata(token_address: str):
    if not Web3.is_address(token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena.")
    try:
        return await asyncio.to_thread(robinhood_token_metadata, token_address)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/wallets/{wallet_id}/holder-distributions")
async def start_holder_distribution(
    wallet_id: str,
    distribution: HolderDistributionStart,
    request: Request,
):
    require_secure_wallet_request(request)
    if not distribution.confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź rozsyłanie tokenów w oknie potwierdzenia.")
    if not Web3.is_address(distribution.token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena.")
    token_address = Web3.to_checksum_address(distribution.token_address)
    if len(distribution.recipients) < 1:
        raise HTTPException(status_code=400, detail="Wybierz co najmniej jednego odbiorcę.")
    if len({recipient.lower() for recipient in distribution.recipients}) != len(distribution.recipients):
        raise HTTPException(status_code=400, detail="Lista odbiorców zawiera powtarzające się adresy.")
    if any(not Web3.is_address(recipient) for recipient in distribution.recipients):
        raise HTTPException(status_code=400, detail="Lista zawiera nieprawidłowy adres EVM.")
    if distribution.amount_per_recipient is not None:
        if distribution.amount_min is not None or distribution.amount_max is not None:
            raise HTTPException(status_code=400, detail="Podaj stałą ilość albo zakres losowania, nie oba.")
        raw_min = raw_max = distribution.amount_per_recipient
    elif distribution.amount_min is not None and distribution.amount_max is not None:
        raw_min, raw_max = distribution.amount_min, distribution.amount_max
    else:
        raise HTTPException(status_code=400, detail="Podaj minimalną i maksymalną ilość tokenów.")
    try:
        amount_min = Decimal(raw_min)
        amount_max = Decimal(raw_max)
    except (InvalidOperation, TypeError) as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowy zakres ilości tokenów.") from error
    if (
        not amount_min.is_finite()
        or not amount_max.is_finite()
        or amount_min <= 0
        or amount_max < amount_min
    ):
        raise HTTPException(
            status_code=400,
            detail="Ilości muszą być dodatnie, a maksimum nie może być mniejsze od minimum.",
        )

    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise HTTPException(status_code=403, detail="Wybrany portfel nie istnieje albo jest zablokowany.")
    if wallet.get("network") != "robinhood":
        raise HTTPException(status_code=400, detail="Portfel nadawcy musi być w Robinhood Chain.")
    recipients = [Web3.to_checksum_address(recipient) for recipient in distribution.recipients]
    if any(recipient.lower() == wallet["address"].lower() for recipient in recipients):
        raise HTTPException(status_code=400, detail="Nie można wybrać własnego portfela jako odbiorcy.")

    try:
        token_info = await asyncio.to_thread(
            holder_distribution_preflight,
            wallet_id,
            token_address,
            amount_min,
            len(recipients),
            amount_max,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logging.exception("Nie powiodła się walidacja rozsyłania tokena")
        raise HTTPException(status_code=502, detail=str(error)) from error

    amount_raws = token_info.get(
        "amount_raws", [token_info["amount_raw"]] * len(recipients)
    )
    with holder_distribution_lock:
        existing_job = next(
            (
                job for job in holder_distribution_jobs.values()
                if job["wallet_id"] == wallet_id
                and job["status"] in {"queued", "running"}
            ),
            None,
        )
        if existing_job:
            raise HTTPException(
                status_code=409,
                detail="Ten portfel ma już aktywne rozsyłanie tokenów.",
            )
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "wallet_id": wallet_id,
            "wallet_address": wallet["address"],
            "token_address": token_address,
            "token_symbol": token_info["symbol"],
            "decimals": token_info["decimals"],
            "recipients": recipients,
            "amount_per_recipient": (
                format(amount_min, "f") if amount_min == amount_max else None
            ),
            "amount_min": format(amount_min, "f"),
            "amount_max": format(amount_max, "f"),
            "amount_raw": token_info["amount_raw"],
            "amount_raws": amount_raws,
            "total_amount": format_raw_token_amount(
                sum(amount_raws), token_info["decimals"]
            ),
            "sent": 0,
            "transactions": [],
            "status": "queued",
            "created_at": int(time.time()),
        }
        holder_distribution_jobs[job_id] = job
    asyncio.create_task(run_holder_distribution(job_id))
    return public_holder_distribution(job)


@app.get("/api/wallets/{wallet_id}/holder-distributions/{job_id}")
async def get_holder_distribution(wallet_id: str, job_id: str):
    with holder_distribution_lock:
        job = holder_distribution_jobs.get(job_id)
        if not job or job["wallet_id"] != wallet_id:
            raise HTTPException(status_code=404, detail="Nie znaleziono zadania rozsyłania.")
        return public_holder_distribution(job)


@app.get("/api/networks")
async def list_networks():
    return {
        name: {
            "chain_id": network["chain_id"],
            "name": {
                "robinhood": "Robinhood Chain",
                "arbitrum": "Arbitrum One",
                "eth": "Ethereum Mainnet",
                "bsc": "BNB Smart Chain",
            }[name],
            "rpc_url": network.get("rpc"),
            "native_symbol": "BNB" if name == "bsc" else "ETH",
        }
        for name, network in NETWORK_CONFIG.items()
    }


@app.get("/api/walletconnect-config")
async def walletconnect_config():
    return {"project_id": os.getenv("WALLETCONNECT_PROJECT_ID", "").strip()}


@app.post("/api/wallets")
def add_wallet(wallet_request: WalletCreate, request: Request):
    require_secure_wallet_request(request)
    name = wallet_request.name.strip()
    password = wallet_request.vault_password.get_secret_value()
    network = wallet_request.network.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Podaj nazwę portfela.")
    if len(name) > 48:
        raise HTTPException(status_code=400, detail="Nazwa portfela może mieć maksymalnie 48 znaków.")
    if len(password) < 12:
        raise HTTPException(status_code=400, detail="Hasło skarbca musi mieć co najmniej 12 znaków.")
    if network not in NETWORK_CONFIG:
        raise HTTPException(status_code=400, detail="Nieobsługiwana sieć portfela.")
    try:
        account = Account.from_key(wallet_request.private_key.get_secret_value().strip())
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowy klucz prywatny EVM.") from error

    with wallet_store_lock:
        if wallet_store:
            try:
                decrypt_wallet_key(next(iter(wallet_store)), password)
            except Exception as error:
                raise HTTPException(
                    status_code=401,
                    detail="Hasło skarbca jest nieprawidłowe.",
                ) from error
        if any(
            item["address"].lower() == account.address.lower() and item["network"] == network
            for item in wallet_store.values()
        ):
            raise HTTPException(status_code=409, detail="Ten portfel jest już dodany do tej sieci.")
        wallet_id = uuid.uuid4().hex
        wallet_store[wallet_id] = {
            "name": name,
            "address": account.address,
            "network": network,
            "keystore": Account.encrypt(account.key, password),
        }
        try:
            save_wallet_store()
        except OSError as error:
            wallet_store.pop(wallet_id, None)
            raise HTTPException(status_code=500, detail="Nie można zapisać zaszyfrowanego portfela.") from error
    return {"wallet": public_wallet(wallet_id, wallet_store[wallet_id])}


@app.post("/api/wallets/hd")
def add_hd_wallets(hd_request: HDWalletCreate, request: Request):
    """Derives sub-wallets m/44'/60'/0'/0/i (BIP32/39/44) from one seed phrase."""
    require_secure_wallet_request(request)
    name = hd_request.name.strip()
    password = hd_request.vault_password.get_secret_value()
    if not name or len(name) > 40:
        raise HTTPException(status_code=400, detail="Nazwa portfela: 1-40 znaków.")
    if len(password) < 12:
        raise HTTPException(status_code=400, detail="Hasło skarbca musi mieć co najmniej 12 znaków.")
    if hd_request.words not in (12, 24):
        raise HTTPException(status_code=400, detail="Fraza seed musi mieć 12 lub 24 słowa.")
    network = "robinhood"
    Account.enable_unaudited_hdwallet_features()
    generated = None
    try:
        if hd_request.mnemonic and hd_request.mnemonic.get_secret_value().strip():
            mnemonic = " ".join(hd_request.mnemonic.get_secret_value().split()).lower()
            Account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")
        else:
            _, mnemonic = Account.create_with_mnemonic(num_words=hd_request.words)
            generated = mnemonic
        accounts = [
            (index, Account.from_mnemonic(mnemonic, account_path=f"m/44'/60'/0'/0/{index}"))
            for index in range(hd_request.start_index, hd_request.start_index + hd_request.count)
        ]
    except Exception as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowa fraza seed (BIP39).") from error

    created_ids = []
    with wallet_store_lock:
        if wallet_store:
            try:
                decrypt_wallet_key(next(iter(wallet_store)), password)
            except Exception as error:
                raise HTTPException(status_code=401, detail="Hasło skarbca jest nieprawidłowe.") from error
        existing = {
            item["address"].lower() for item in wallet_store.values() if item["network"] == network
        }
        for index, account in accounts:
            if account.address.lower() in existing:
                continue
            wallet_id = uuid.uuid4().hex
            wallet_store[wallet_id] = {
                "name": f"{name} #{index}",
                "address": account.address,
                "network": network,
                "derivation_path": f"m/44'/60'/0'/0/{index}",
                "keystore": Account.encrypt(
                    account.key, password, kdf="scrypt", iterations=HD_SCRYPT_N
                ),
            }
            created_ids.append(wallet_id)
        try:
            save_wallet_store()
        except OSError as error:
            for wallet_id in created_ids:
                wallet_store.pop(wallet_id, None)
            raise HTTPException(status_code=500, detail="Nie można zapisać zaszyfrowanych portfeli.") from error
        wallets = [public_wallet(wid, wallet_store[wid]) for wid in created_ids]
    return {"wallets": wallets, "mnemonic": generated}


DISPERSE_ARTIFACT = json.loads(
    (Path(__file__).parent / "contracts" / "BatchDisperse.json").read_text(encoding="utf-8")
)
DISPERSE_BATCH_SIZE = 100
DISPERSE_PATH = WALLET_STORE_PATH.with_name("disperse.json")


def load_disperse_address() -> Optional[str]:
    try:
        return json.loads(DISPERSE_PATH.read_text(encoding="utf-8")).get("robinhood")
    except (OSError, ValueError):
        return None


def robinhood_web3_for_wallet(wallet_id: str):
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise HTTPException(status_code=403, detail="Portfel jest zablokowany lub nie istnieje.")
    if wallet["network"] != "robinhood":
        raise HTTPException(status_code=400, detail="Portfel musi być w Robinhood Chain.")
    network = NETWORK_CONFIG["robinhood"]
    if not network.get("rpc"):
        raise HTTPException(status_code=400, detail="Brak RPC Robinhood Chain.")
    web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 20}))
    if not web3.is_connected() or web3.eth.chain_id != network["chain_id"]:
        raise HTTPException(status_code=502, detail="Nie można zweryfikować RPC Robinhood Chain.")
    return web3, network, Account.from_key(private_key), private_key


def send_signed(web3: Web3, network: dict, account, private_key: str, tx: dict):
    tx.update({
        "from": account.address,
        "chainId": network["chain_id"],
        "nonce": web3.eth.get_transaction_count(account.address, "pending"),
        **transaction_fee_fields(web3),
    })
    tx["gas"] = int(web3.eth.estimate_gas(tx) * 1.2)
    signed = Account.sign_transaction(tx, private_key)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"Transakcja {Web3.to_hex(tx_hash)} została odrzucona przez sieć.")
    return receipt


@app.get("/api/disperse")
def get_disperse():
    return {"address": load_disperse_address()}


@app.post("/api/disperse/deploy")
def deploy_disperse(body: DisperseDeploy, request: Request):
    require_secure_wallet_request(request)
    web3, network, account, private_key = robinhood_web3_for_wallet(body.wallet_id)
    try:
        contract = web3.eth.contract(abi=DISPERSE_ARTIFACT["abi"], bytecode=DISPERSE_ARTIFACT["bytecode"])
        tx = contract.constructor().build_transaction({"from": account.address})
        tx.pop("gas", None)
        receipt = send_signed(web3, network, account, private_key, tx)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Wdrożenie nie powiodło się: {error}") from error
    address = receipt.contractAddress
    DISPERSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISPERSE_PATH.write_text(json.dumps({"robinhood": address}), encoding="utf-8")
    return {"address": address, "tx_hash": Web3.to_hex(receipt.transactionHash)}


@app.post("/api/disperse/fund")
def fund_subwallets(body: DisperseFund, request: Request):
    require_secure_wallet_request(request)
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź zasilenie podportfeli.")
    address = load_disperse_address()
    if not address:
        raise HTTPException(status_code=400, detail="Najpierw wdróż kontrakt Batch/Disperse.")
    try:
        amount = Decimal(body.amount_each)
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
        amount_wei = int(amount * Decimal(10) ** 18)
        if amount_wei <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError) as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowa kwota ETH na portfel.") from error
    with wallet_store_lock:
        recipients = []
        for rid in dict.fromkeys(body.recipient_wallet_ids):
            wallet = wallet_store.get(rid)
            if not wallet or wallet["network"] != "robinhood":
                raise HTTPException(status_code=404, detail="Nie znaleziono portfela odbiorcy.")
            recipients.append(Web3.to_checksum_address(wallet["address"]))
    web3, network, account, private_key = robinhood_web3_for_wallet(body.wallet_id)
    total = amount_wei * len(recipients)
    sent, gas_used, tx_hashes = 0, 0, []
    try:
        if not web3.eth.get_code(Web3.to_checksum_address(address)):
            raise HTTPException(status_code=400, detail="Brak kontraktu pod zapisanym adresem; wdróż ponownie.")
        if web3.eth.get_balance(account.address) < total:
            raise HTTPException(status_code=400, detail="Saldo ETH portfela nadawcy jest za małe.")
        contract = web3.eth.contract(address=Web3.to_checksum_address(address), abi=DISPERSE_ARTIFACT["abi"])
        for start in range(0, len(recipients), DISPERSE_BATCH_SIZE):
            chunk = recipients[start:start + DISPERSE_BATCH_SIZE]
            tx = contract.functions.disperseEth(chunk, [amount_wei] * len(chunk)).build_transaction(
                {"from": account.address, "value": amount_wei * len(chunk)}
            )
            tx.pop("gas", None)
            receipt = send_signed(web3, network, account, private_key, tx)
            sent += len(chunk)
            gas_used += receipt.gasUsed
            tx_hashes.append(Web3.to_hex(receipt.transactionHash))
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Zasilenie przerwane po {sent}/{len(recipients)} portfelach: {error}",
        ) from error
    return {"tx_hash": tx_hashes[-1], "tx_hashes": tx_hashes, "recipients": sent, "gas_used": gas_used}


@app.post("/api/disperse/fund-token")
def fund_subwallets_token(body: DisperseTokenFund, request: Request):
    require_secure_wallet_request(request)
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź wysyłkę tokenów.")
    address = load_disperse_address()
    if not address:
        raise HTTPException(status_code=400, detail="Najpierw wdróż kontrakt Batch/Disperse.")
    if not Web3.is_address(body.token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena.")
    try:
        amount = Decimal(body.amount_each)
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError) as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowa kwota tokena na portfel.") from error
    with wallet_store_lock:
        recipients = []
        for rid in dict.fromkeys(body.recipient_wallet_ids):
            wallet = wallet_store.get(rid)
            if not wallet or wallet["network"] != "robinhood":
                raise HTTPException(status_code=404, detail="Nie znaleziono portfela odbiorcy.")
            recipients.append(Web3.to_checksum_address(wallet["address"]))
    web3, network, account, private_key = robinhood_web3_for_wallet(body.wallet_id)
    disperse_address = Web3.to_checksum_address(address)
    token_address = Web3.to_checksum_address(body.token_address)
    try:
        if not web3.eth.get_code(disperse_address):
            raise HTTPException(status_code=400, detail="Brak kontraktu pod zapisanym adresem; wdróż ponownie.")
        if not web3.eth.get_code(token_address):
            raise HTTPException(status_code=400, detail="Podany adres nie jest kontraktem tokena.")
        token = web3.eth.contract(address=token_address, abi=ERC20_TRADE_ABI)
        decimals = int(token.functions.decimals().call())
        amount_units = int(amount * Decimal(10) ** decimals)
        if amount_units <= 0:
            raise HTTPException(status_code=400, detail="Kwota jest zbyt mała dla tego tokena.")
        total = amount_units * len(recipients)
        sent, gas_used, tx_hashes = 0, 0, []
        if token.functions.balanceOf(account.address).call() < total:
            raise HTTPException(status_code=400, detail="Saldo tokena portfela nadawcy jest za małe.")
        if token.functions.allowance(account.address, disperse_address).call() < total:
            tx = token.functions.approve(disperse_address, total).build_transaction({"from": account.address})
            tx.pop("gas", None)
            send_signed(web3, network, account, private_key, tx)
        contract = web3.eth.contract(address=disperse_address, abi=DISPERSE_ARTIFACT["abi"])
        for start in range(0, len(recipients), DISPERSE_BATCH_SIZE):
            chunk = recipients[start:start + DISPERSE_BATCH_SIZE]
            tx = contract.functions.disperseToken(
                token_address, chunk, [amount_units] * len(chunk)
            ).build_transaction({"from": account.address})
            tx.pop("gas", None)
            receipt = send_signed(web3, network, account, private_key, tx)
            sent += len(chunk)
            gas_used += receipt.gasUsed
            tx_hashes.append(Web3.to_hex(receipt.transactionHash))
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Wysyłka przerwana po {sent}/{len(recipients)} portfelach: {error}",
        ) from error
    return {"tx_hash": tx_hashes[-1], "tx_hashes": tx_hashes, "recipients": sent, "gas_used": gas_used}


HD_SCRYPT_N = 2 ** 14
hd_unlock_state = {"generation": 0, "pending": 0}
hd_unlock_lock = threading.Lock()


def finish_hd_unlock(generation: int, hd_ids: list, password: str) -> None:
    """Decrypts HD sub-wallets in the background and migrates slow keystores to HD_SCRYPT_N."""
    from concurrent.futures import ThreadPoolExecutor

    def work(wallet_id: str):
        with wallet_store_lock:
            wallet = wallet_store.get(wallet_id)
            keystore = wallet["keystore"] if wallet else None
        if keystore is None:
            return None
        key_bytes = Account.decrypt(keystore, password)
        upgraded = None
        kdfparams = keystore.get("crypto", {}).get("kdfparams", {})
        if kdfparams.get("n", 0) > HD_SCRYPT_N:
            upgraded = Account.encrypt(key_bytes, password, kdf="scrypt", iterations=HD_SCRYPT_N)
        return wallet_id, key_bytes.hex(), upgraded

    try:
        with ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as pool:
            upgraded_any = False
            for result in pool.map(work, hd_ids):
                if not result or hd_unlock_state["generation"] != generation:
                    continue
                wallet_id, key_hex, upgraded = result
                with wallet_store_lock:
                    if wallet_id in wallet_store:
                        unlocked_wallet_keys[wallet_id] = key_hex
                        if upgraded:
                            wallet_store[wallet_id]["keystore"] = upgraded
                            upgraded_any = True
                with hd_unlock_lock:
                    hd_unlock_state["pending"] = max(0, hd_unlock_state["pending"] - 1)
        if upgraded_any:
            with wallet_store_lock:
                save_wallet_store()
    except Exception:
        logging.exception("Odblokowanie portfeli HD w tle nie powiodło się")
    finally:
        with hd_unlock_lock:
            if hd_unlock_state["generation"] == generation:
                hd_unlock_state["pending"] = 0


def require_hd_unlock_finished() -> None:
    if hd_unlock_state["pending"] > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Trwa odblokowywanie portfeli HD (zostało {hd_unlock_state['pending']}). Spróbuj za chwilę.",
        )


hd_trade_jobs: Dict[str, dict] = {}
hd_trade_stop_events: Dict[str, threading.Event] = {}
hd_trade_lock = threading.Lock()
HD_TRADE_MAX_STEPS = 2000
HD_TRADE_MAX_CONSECUTIVE_FAILURES = 5


def hd_wallet_ids_by_number() -> Dict[int, str]:
    """Number n (1..500) is the sub-wallet with derivation path m/44'/60'/0'/0/(n-1)."""
    mapping: Dict[int, str] = {}
    with wallet_store_lock:
        for wallet_id, wallet in wallet_store.items():
            match = re.fullmatch(r"m/44'/60'/0'/0/(\d+)", wallet.get("derivation_path") or "")
            if match and wallet["network"] == "robinhood":
                mapping.setdefault(int(match.group(1)) + 1, wallet_id)
    return mapping


def hd_trade_log(job: dict, message: str) -> None:
    job["logs"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
    del job["logs"][:-60]


def run_hd_trade_job(
    job_id: str, trade_pair: dict, side: str, selected: list, rounds: int, shuffle: bool,
    base_delay: float, jitter: float, schedule: Optional[dict],
) -> None:
    job = hd_trade_jobs[job_id]
    stop = hd_trade_stop_events[job_id]
    rng = secrets.SystemRandom()
    failures_in_row = 0
    try:
        while True:
            if schedule:
                run_at = schedule["run_at"]
                job.update({"status": "scheduled", "scheduled_at": run_at, "next_at": None})
                hd_trade_log(
                    job,
                    "⏰ Start zaplanowany na "
                    + datetime.fromtimestamp(run_at, ZoneInfo(schedule["timezone"])).strftime("%Y-%m-%d %H:%M")
                    + f" ({schedule['timezone']})",
                )
                if stop.wait(max(0.0, run_at - time.time())):
                    break
                job["status"] = "running"
                job["scheduled_at"] = None
            plan = []
            for _ in range(rounds):
                order = list(selected)
                if shuffle:
                    rng.shuffle(order)
                plan.extend(order)
            job.update({"total": len(plan), "done": 0, "ok": 0, "failed": 0})
            for step, (number, wallet_id) in enumerate(plan):
                if side == "mixed":
                    step_side = rng.choice(("buy", "sell"))
                elif side == "alternate":
                    step_side = "buy" if step % 2 == 0 else "sell"
                else:
                    step_side = side
                if step:
                    delay = base_delay * (1 + rng.uniform(-jitter, jitter))
                    job["next_at"] = time.time() + max(delay, 0.5)
                    if stop.wait(max(delay, 0.5)):
                        break
                elif stop.is_set():
                    break
                job["next_at"] = None
                with wallet_store_lock:
                    unlocked = wallet_id in unlocked_wallet_keys
                if not unlocked:
                    job["failed"] += 1
                    hd_trade_log(job, f"🔒 #{number}: portfel zablokowany — pominięto")
                else:
                    try:
                        executor = execute_auto_buy if step_side == "buy" else execute_auto_sell
                        trade = executor(dict(trade_pair), wallet_id)
                        record_wallet_trade(wallet_id, trade)
                        job["ok"] += 1
                        failures_in_row = 0
                        hd_trade_log(job, f"✅ #{number}: {step_side} {trade.get('token_amount')} — {trade['tx_hash']}")
                    except Exception as error:
                        job["failed"] += 1
                        failures_in_row += 1
                        hd_trade_log(job, f"❌ #{number} ({step_side}): {error}")
                job["done"] = step + 1
                if failures_in_row >= HD_TRADE_MAX_CONSECUTIVE_FAILURES:
                    hd_trade_log(job, "⛔ Zatrzymano po 5 kolejnych błędach.")
                    job["status"] = "failed"
                    return
            if stop.is_set() or not (schedule and schedule["daily"]):
                break
            schedule["run_at"] = next_daily_schedule_at(
                schedule["time"], schedule["timezone"], max(time.time(), schedule["run_at"])
            )
            hd_trade_log(job, f"Dzień zakończony: ok {job['ok']}, błędy {job['failed']}")
        job["status"] = "stopped" if stop.is_set() else "finished"
    except Exception as error:
        logging.exception("Zadanie handlu HD zakończone błędem")
        hd_trade_log(job, f"⛔ {error}")
        job["status"] = "failed"
    finally:
        job["next_at"] = None
        job["scheduled_at"] = None


@app.post("/api/hd-trade")
def start_hd_trade(body: HDTradeJob, request: Request):
    require_secure_wallet_request(request)
    require_hd_unlock_finished()
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź uruchomienie handlu.")
    side = body.side.strip().lower()
    if side not in {"buy", "sell", "mixed", "alternate"}:
        raise HTTPException(status_code=400, detail="Kierunek: buy, sell, mixed albo alternate.")
    if body.interval_unit not in {"seconds", "minutes"}:
        raise HTTPException(status_code=400, detail="Jednostka odstępu: sekundy albo minuty.")
    if not Web3.is_address(body.token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres kontraktu tokena.")
    if body.wallet_from > body.wallet_to:
        raise HTTPException(status_code=400, detail="Zakres portfeli: 'od' nie może być większe niż 'do'.")
    try:
        quantity = Decimal(body.quantity)
        max_quote = Decimal(body.max_quote) if body.max_quote else None
        if not quantity.is_finite() or quantity <= 0:
            raise InvalidOperation
        if side != "sell" and (max_quote is None or not max_quote.is_finite() or max_quote <= 0):
            raise InvalidOperation
    except (InvalidOperation, TypeError) as error:
        raise HTTPException(
            status_code=400,
            detail="Podaj dodatnią ilość tokenów (przy kupnie także maksymalny wydatek ETH).",
        ) from error
    token_address = Web3.to_checksum_address(body.token_address)
    numbers = hd_wallet_ids_by_number()
    selected = [(n, numbers[n]) for n in range(body.wallet_from, body.wallet_to + 1) if n in numbers]
    if not selected:
        raise HTTPException(status_code=404, detail="W podanym zakresie nie ma portfeli HD. Najpierw je utwórz.")
    total_steps = len(selected) * body.rounds
    if total_steps > HD_TRADE_MAX_STEPS:
        raise HTTPException(status_code=400, detail=f"Maksymalnie {HD_TRADE_MAX_STEPS} transakcji w jednym zadaniu.")
    schedule = None
    if body.start_time:
        try:
            run_at = next_daily_schedule_at(body.start_time, body.timezone, time.time())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        schedule = {"run_at": run_at, "time": body.start_time, "timezone": body.timezone, "daily": body.daily}
    elif body.daily:
        raise HTTPException(status_code=400, detail="Powtarzanie codzienne wymaga godziny startu.")

    trade_pair = {
        "id": f"hd_{token_address[:10].lower()}",
        "network": "robinhood",
        "token_address": token_address,
        "token_symbol": "TOKEN",
        "quote_token_address": NATIVE_TOKEN_ADDRESS,
        "quote_symbol": NETWORK_CONFIG["robinhood"].get("native_symbol", "ETH"),
        "dex_pairs_list": [],
        "max_slippage_percent": 1.0,
    }
    if side != "sell":
        trade_pair.update({
            "trade_amount": str(max_quote),
            "auto_buy_remaining_quantity": str(quantity),
            "auto_buy_exact_schedule_quantity": True,
        })
    if side != "buy":
        trade_pair["auto_sell_amount"] = str(quantity)
    base_delay = body.interval_value * (60 if body.interval_unit == "minutes" else 1)

    with hd_trade_lock:
        if any(job["status"] in {"running", "scheduled"} for job in hd_trade_jobs.values()):
            raise HTTPException(status_code=409, detail="Zadanie handlu HD już działa lub czeka na start. Zatrzymaj je najpierw.")
        job_id = uuid.uuid4().hex
        hd_trade_jobs[job_id] = {
            "id": job_id, "status": "running", "side": side, "token_address": token_address,
            "total": total_steps, "done": 0, "ok": 0, "failed": 0, "next_at": None,
            "scheduled_at": None, "daily": bool(schedule and schedule["daily"]),
            "logs": [], "started_at": time.time(),
        }
        hd_trade_stop_events[job_id] = threading.Event()
        for old_id in list(hd_trade_jobs)[:-5]:
            if hd_trade_jobs[old_id]["status"] not in {"running", "scheduled"}:
                hd_trade_jobs.pop(old_id, None)
                hd_trade_stop_events.pop(old_id, None)
    threading.Thread(
        target=run_hd_trade_job,
        args=(job_id, trade_pair, side, selected, body.rounds, body.shuffle,
              base_delay, body.jitter_percent / 100, schedule),
        daemon=True,
    ).start()
    return {
        "job_id": job_id,
        "total": total_steps,
        "scheduled_at": schedule["run_at"] if schedule else None,
    }


@app.get("/api/hd-trade")
def get_hd_trade():
    with hd_trade_lock:
        jobs = list(hd_trade_jobs.values())
    return {"job": jobs[-1] if jobs else None}


@app.post("/api/hd-trade/stop")
def stop_hd_trade():
    with hd_trade_lock:
        for job_id, job in hd_trade_jobs.items():
            if job["status"] in {"running", "scheduled"}:
                hd_trade_stop_events[job_id].set()
    return {"stopping": True}


disperse_jobs: Dict[str, dict] = {}
disperse_stop_events: Dict[str, threading.Event] = {}
disperse_job_lock = threading.Lock()


def run_disperse_job(job_id: str, params: dict, batches: list) -> None:
    job = disperse_jobs[job_id]
    stop = disperse_stop_events[job_id]
    rng = secrets.SystemRandom()

    def log(message: str) -> None:
        job["logs"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
        del job["logs"][:-60]

    try:
        run_at = params.get("run_at")
        if run_at:
            job.update({"status": "scheduled", "scheduled_at": run_at})
            log("⏰ Start zaplanowany na "
                + datetime.fromtimestamp(run_at, ZoneInfo(params["timezone"])).strftime("%Y-%m-%d %H:%M")
                + f" ({params['timezone']})")
            if stop.wait(max(0.0, run_at - time.time())):
                job["status"] = "stopped"
                return
            job.update({"status": "running", "scheduled_at": None})
        with wallet_store_lock:
            if params["wallet_id"] not in unlocked_wallet_keys:
                raise RuntimeError("portfel nadawcy jest zablokowany")
        web3, network, account, private_key = robinhood_web3_for_wallet(params["wallet_id"])
        disperse_address = Web3.to_checksum_address(load_disperse_address())
        contract = web3.eth.contract(address=disperse_address, abi=DISPERSE_ARTIFACT["abi"])
        token = None
        if params["asset"] == "token":
            token_address = Web3.to_checksum_address(params["token_address"])
            token = web3.eth.contract(address=token_address, abi=ERC20_TRADE_ABI)
            unit = 10 ** int(token.functions.decimals().call())
        else:
            unit = 10 ** 18
        base_units = int(params["amount_each"] * unit)
        for step, chunk in enumerate(batches):
            if step:
                delay = params["base_delay"] * (1 + rng.uniform(-params["jitter"], params["jitter"]))
                job["next_at"] = time.time() + delay
                if delay > 0 and stop.wait(delay):
                    break
            if stop.is_set():
                break
            job["next_at"] = None
            first, last = chunk[0][0], chunk[-1][0]
            try:
                addresses = [address for _, address in chunk]
                amounts = [
                    max(1, int(base_units * (1 + rng.uniform(-params["amount_jitter"], params["amount_jitter"]))))
                    for _ in chunk
                ]
                total = sum(amounts)
                if token is None:
                    if web3.eth.get_balance(account.address) < total:
                        raise RuntimeError("saldo ETH nadawcy jest za małe")
                    tx = contract.functions.disperseEth(addresses, amounts).build_transaction(
                        {"from": account.address, "value": total}
                    )
                else:
                    if token.functions.balanceOf(account.address).call() < total:
                        raise RuntimeError("saldo tokena nadawcy jest za małe")
                    if token.functions.allowance(account.address, disperse_address).call() < total:
                        approve_tx = token.functions.approve(disperse_address, total).build_transaction(
                            {"from": account.address}
                        )
                        approve_tx.pop("gas", None)
                        send_signed(web3, network, account, private_key, approve_tx)
                    tx = contract.functions.disperseToken(token_address, addresses, amounts).build_transaction(
                        {"from": account.address}
                    )
                tx.pop("gas", None)
                receipt = send_signed(web3, network, account, private_key, tx)
                job["ok"] += len(chunk)
                job["failures_in_row"] = 0
                log(f"✅ portfele #{first}–#{last} ({len(chunk)}): {Web3.to_hex(receipt.transactionHash)}")
            except Exception as error:
                job["failed"] += len(chunk)
                job["failures_in_row"] += 1
                log(f"❌ portfele #{first}–#{last}: {error}")
            job["done"] = step + 1
            if job["failures_in_row"] >= 3:
                log("⛔ Zatrzymano po 3 kolejnych błędach.")
                job["status"] = "failed"
                return
        job["status"] = "stopped" if stop.is_set() else "finished"
    except Exception as error:
        logging.exception("Zadanie zasilania zakończone błędem")
        log(f"⛔ {error}")
        job["status"] = "failed"
    finally:
        job["next_at"] = None
        job["scheduled_at"] = None


@app.post("/api/disperse/job")
def start_disperse_job(body: DisperseJob, request: Request):
    require_secure_wallet_request(request)
    require_hd_unlock_finished()
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź zasilanie podportfeli.")
    if body.asset not in {"eth", "token"}:
        raise HTTPException(status_code=400, detail="Aktywo: eth albo token.")
    if body.interval_unit not in {"seconds", "minutes"}:
        raise HTTPException(status_code=400, detail="Jednostka odstępu: sekundy albo minuty.")
    if body.wallet_from > body.wallet_to:
        raise HTTPException(status_code=400, detail="Zakres portfeli: 'od' nie może być większe niż 'do'.")
    if not load_disperse_address():
        raise HTTPException(status_code=400, detail="Najpierw wdróż kontrakt Batch/Disperse.")
    if body.asset == "token" and not (body.token_address and Web3.is_address(body.token_address)):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena.")
    try:
        amount = Decimal(body.amount_each)
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError) as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowa kwota na portfel.") from error
    if body.wallet_id not in unlocked_wallet_keys:
        raise HTTPException(status_code=403, detail="Portfel nadawcy jest zablokowany lub nie istnieje.")
    numbers = hd_wallet_ids_by_number()
    recipients = []
    with wallet_store_lock:
        for number in range(body.wallet_from, body.wallet_to + 1):
            wallet_id = numbers.get(number)
            if wallet_id and wallet_id != body.wallet_id:
                recipients.append((number, Web3.to_checksum_address(wallet_store[wallet_id]["address"])))
    if not recipients:
        raise HTTPException(status_code=404, detail="W podanym zakresie nie ma portfeli HD. Najpierw je utwórz.")
    if body.shuffle:
        secrets.SystemRandom().shuffle(recipients)
    batches = [recipients[i:i + body.batch_size] for i in range(0, len(recipients), body.batch_size)]
    params = {
        "wallet_id": body.wallet_id,
        "asset": body.asset,
        "token_address": body.token_address,
        "amount_each": amount,
        "base_delay": body.interval_value * (60 if body.interval_unit == "minutes" else 1),
        "jitter": body.jitter_percent / 100,
        "amount_jitter": body.amount_jitter_percent / 100,
        "run_at": None,
        "timezone": body.timezone,
    }
    if body.start_time:
        try:
            params["run_at"] = next_daily_schedule_at(body.start_time, body.timezone, time.time())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    with disperse_job_lock:
        if any(job["status"] in {"running", "scheduled"} for job in disperse_jobs.values()):
            raise HTTPException(status_code=409, detail="Zasilanie już trwa lub czeka na start. Zatrzymaj je najpierw.")
        job_id = uuid.uuid4().hex
        disperse_jobs[job_id] = {
            "id": job_id, "status": "scheduled" if params["run_at"] else "running",
            "scheduled_at": params["run_at"], "asset": body.asset, "total": len(batches),
            "recipients": len(recipients), "done": 0, "ok": 0, "failed": 0,
            "failures_in_row": 0, "next_at": None, "logs": [], "started_at": time.time(),
        }
        disperse_stop_events[job_id] = threading.Event()
        for old_id in list(disperse_jobs)[:-5]:
            if disperse_jobs[old_id]["status"] not in {"running", "scheduled"}:
                disperse_jobs.pop(old_id, None)
                disperse_stop_events.pop(old_id, None)
    threading.Thread(target=run_disperse_job, args=(job_id, params, batches), daemon=True).start()
    return {"job_id": job_id, "recipients": len(recipients), "batches": len(batches), "scheduled_at": params["run_at"]}


@app.get("/api/disperse/job")
def get_disperse_job():
    with disperse_job_lock:
        jobs = list(disperse_jobs.values())
    return {"job": jobs[-1] if jobs else None}


@app.post("/api/disperse/job/stop")
def stop_disperse_job():
    with disperse_job_lock:
        for job_id, job in disperse_jobs.items():
            if job["status"] in {"running", "scheduled"}:
                disperse_stop_events[job_id].set()
    return {"stopping": True}


@app.post("/api/wallets/unlock")
def unlock_wallets(wallet_request: WalletUnlock, request: Request):
    require_secure_wallet_request(request)
    password = wallet_request.vault_password.get_secret_value()
    with wallet_store_lock:
        unlocked_wallet_keys.clear()
        with hd_unlock_lock:
            hd_unlock_state["generation"] += 1
            generation = hd_unlock_state["generation"]
            hd_unlock_state["pending"] = 0
        if not wallet_store:
            raise HTTPException(status_code=404, detail="Najpierw dodaj portfel do skarbca.")
        regular_ids = [wid for wid, w in wallet_store.items() if not w.get("derivation_path")]
        hd_ids = [wid for wid, w in wallet_store.items() if w.get("derivation_path")]
        eager = regular_ids + hd_ids[:1]
        try:
            unlocked = {wid: decrypt_wallet_key(wid, password) for wid in eager}
        except Exception as error:
            raise HTTPException(status_code=401, detail="Nieprawidłowe hasło skarbca.") from error
        unlocked_wallet_keys.update(unlocked)
        rest = hd_ids[1:]
        with hd_unlock_lock:
            hd_unlock_state["pending"] = len(rest)
        if rest:
            threading.Thread(
                target=finish_hd_unlock, args=(generation, rest, password), daemon=True
            ).start()
        return {
            "unlocked": True,
            "hd_pending": len(rest),
            "wallets": [public_wallet(wallet_id, wallet) for wallet_id, wallet in wallet_store.items()],
        }


@app.post("/api/wallets/lock")
def lock_wallets():
    with wallet_store_lock:
        with hd_unlock_lock:
            hd_unlock_state["generation"] += 1
            hd_unlock_state["pending"] = 0
        unlocked_wallet_keys.clear()
    return {"unlocked": False}


@app.post("/api/wallets/{wallet_id}/lock")
def lock_wallet(wallet_id: str):
    with wallet_store_lock:
        if wallet_id not in wallet_store:
            raise HTTPException(status_code=404, detail="Nie znaleziono portfela.")
        unlocked_wallet_keys.pop(wallet_id, None)
        return {"unlocked": False, "wallet": public_wallet(wallet_id, wallet_store[wallet_id])}


@app.post("/api/wallets/{wallet_id}/transaction")
def send_managed_wallet_transaction(
    wallet_id: str,
    transaction: ManagedWalletTransaction,
    request: Request,
):
    require_secure_wallet_request(request)
    if not transaction.confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź swap w oknie transakcji.")
    with wallet_store_lock:
        wallet = wallet_store.get(wallet_id)
        private_key = unlocked_wallet_keys.get(wallet_id)
    if not wallet or not private_key:
        raise HTTPException(status_code=403, detail="Wybrany portfel jest zablokowany lub nie istnieje.")
    network_key = transaction.network.strip().lower()
    if network_key != wallet["network"]:
        raise HTTPException(status_code=400, detail="Sieć transakcji nie pasuje do wybranego portfela.")
    network = NETWORK_CONFIG.get(network_key)
    if not network or not network.get("rpc"):
        raise HTTPException(status_code=400, detail="Brak RPC dla sieci wybranego portfela.")
    trade_record = None
    if transaction.trade:
        trade = transaction.trade
        if trade.trade_type not in {"buy", "sell"}:
            raise HTTPException(status_code=400, detail="Nieprawidłowy kierunek swapu dla historii.")
        if not Web3.is_address(trade.token_address) or not Web3.is_address(trade.quote_token_address):
            raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena w historii swapu.")
        pair = active_pairs.get(trade.pair_id)
        if (
            not pair
            or pair.get("network") != network_key
            or pair.get("token_address", "").lower() != trade.token_address.lower()
        ):
            raise HTTPException(status_code=400, detail="Token historii nie pasuje do monitorowanej pozycji.")
        try:
            token_amount = Decimal(trade.token_amount)
            quote_amount = Decimal(trade.quote_amount)
        except (InvalidOperation, TypeError) as error:
            raise HTTPException(status_code=400, detail="Nieprawidłowe kwoty do historii swapu.") from error
        if (
            not token_amount.is_finite() or token_amount <= 0
            or not quote_amount.is_finite() or quote_amount <= 0
        ):
            raise HTTPException(status_code=400, detail="Kwoty do historii swapu muszą być dodatnie.")
        trade_record = {
            "type": trade.trade_type,
            "network": network_key,
            "pair_id": trade.pair_id,
            "campaign_id": None,
            "token_address": Web3.to_checksum_address(trade.token_address),
            "token_symbol": trade.token_symbol[:32],
            "token_amount": format(token_amount, "f"),
            "quote_token_address": Web3.to_checksum_address(trade.quote_token_address),
            "quote_symbol": trade.quote_symbol[:16],
            "quote_amount": format(quote_amount, "f"),
            "source": "manual_swap",
        }
    try:
        account = Account.from_key(private_key)
        web3 = Web3(Web3.HTTPProvider(network["rpc"], request_kwargs={"timeout": 20}))
        if not web3.is_connected():
            raise RuntimeError("Nie można połączyć się z RPC tej sieci.")
        if web3.eth.chain_id != network["chain_id"]:
            raise RuntimeError("RPC zwróciło inny chain ID; transakcja została wstrzymana.")
        tx = validate_managed_wallet_transaction(
            web3, network, account.address, transaction
        )
        input_requirement = managed_wallet_input_requirement(
            web3, network, tx
        )
        ensure_managed_wallet_input_allowance(
            web3, network, account, private_key, input_requirement
        )
        tx["from"] = account.address
        web3.eth.call(tx)
        gas_estimate = web3.eth.estimate_gas(tx)
        tx.update({
            "chainId": network["chain_id"],
            "nonce": web3.eth.get_transaction_count(account.address),
            "gas": int(gas_estimate * 1.2),
            **transaction_fee_fields(web3),
        })
        signed_transaction = Account.sign_transaction(tx, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed_transaction.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"Transakcja {Web3.to_hex(tx_hash)} została odrzucona przez sieć.")
        normalized_hash = Web3.to_hex(receipt.get("transactionHash") or tx_hash)
        if trade_record:
            record_wallet_trade(wallet_id, {
                **trade_record,
                "tx_hash": normalized_hash,
                "timestamp": int(time.time()),
            })
        return {"tx_hash": normalized_hash, "hash": normalized_hash, "status": "confirmed"}
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logging.exception("Swap podpisywany przez skarbiec nie powiódł się dla %s", wallet_id)
        raise HTTPException(
            status_code=502,
            detail=f"Swap nie został potwierdzony: {error}",
        ) from error


@app.delete("/api/wallets/{wallet_id}")
def remove_wallet(wallet_id: str, wallet_request: WalletRemove, request: Request):
    require_secure_wallet_request(request)
    with wallet_store_lock:
        if wallet_id not in wallet_store:
            raise HTTPException(status_code=404, detail="Nie znaleziono portfela.")
        try:
            decrypt_wallet_key(wallet_id, wallet_request.vault_password.get_secret_value())
        except Exception as error:
            raise HTTPException(status_code=401, detail="Nieprawidłowe hasło skarbca.") from error
        affected_pairs = [
            pair for pair in active_pairs.values()
            if pair.get("wallet_id") == wallet_id and pair.get("auto_trade_enabled")
        ]
        previous_unlocked_key = unlocked_wallet_keys.get(wallet_id)
        removed_wallet = wallet_store[wallet_id]
        del wallet_store[wallet_id]
        unlocked_wallet_keys.pop(wallet_id, None)
        for pair in affected_pairs:
            pair["auto_trade_enabled"] = False
        try:
            save_wallet_store()
        except OSError as error:
            wallet_store[wallet_id] = removed_wallet
            if previous_unlocked_key:
                unlocked_wallet_keys[wallet_id] = previous_unlocked_key
            for pair in affected_pairs:
                pair["auto_trade_enabled"] = True
            logging.exception("Nie można usunąć portfela z magazynu")
            raise HTTPException(status_code=500, detail="Nie można zaktualizować magazynu portfeli.") from error
    return {"status": "removed"}


@app.post("/api/pairs")
async def add_pair(pair: TradingPair):
    pair.network = pair.network.strip().lower()
    if pair.network not in NETWORK_CONFIG:
        raise HTTPException(status_code=400, detail="Nieobsługiwana sieć tokena.")
    try:
        trade_amount = Decimal(str(pair.trade_amount))
    except InvalidOperation as error:
        raise HTTPException(status_code=400, detail="Nieprawidłowa kwota transakcji.") from error
    if not trade_amount.is_finite() or trade_amount <= 0:
        raise HTTPException(status_code=400, detail="Kwota transakcji musi być większa od zera.")
    for field_name, field_value in (
        ("target_buy_price", pair.target_buy_price),
        ("target_sell_price", pair.target_sell_price),
    ):
        if field_value is not None:
            value = Decimal(str(field_value))
            if not value.is_finite() or value <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Docelowe ceny DEX muszą być większe od zera.",
                )
    for field_name, field_value in (
        ("buy_percent", pair.buy_percent),
        ("sell_percent", pair.sell_percent),
    ):
        if field_value is not None:
            value = Decimal(str(field_value))
            if not value.is_finite() or value <= -100:
                raise HTTPException(
                    status_code=400,
                    detail="Procentowy próg kupna i sprzedaży musi być większy niż -100%.",
                )
    if pair.auto_buy_mode not in {"single", "target_quantity", "target_price", "schedule"}:
        raise HTTPException(status_code=400, detail="Nieobsługiwana strategia automatycznego kupna.")
    if pair.auto_trade_enabled and pair.auto_buy_mode == "target_quantity":
        try:
            target_quantity = Decimal(str(pair.auto_buy_target_quantity))
            total_budget = Decimal(str(pair.auto_buy_total_budget))
        except InvalidOperation as error:
            raise HTTPException(
                status_code=400,
                detail="Podaj docelową ilość tokenów i łączny budżet automatycznego kupna.",
            ) from error
        if (
            not target_quantity.is_finite() or target_quantity <= 0
            or not total_budget.is_finite() or total_budget <= 0
        ):
            raise HTTPException(
                status_code=400,
                detail="Docelowa ilość tokenów i łączny budżet muszą być większe od zera.",
            )
    if pair.auto_trade_enabled and pair.auto_buy_mode == "target_price":
        try:
            target_buy_price = Decimal(str(pair.target_buy_price))
        except InvalidOperation as error:
            raise HTTPException(
                status_code=400,
                detail="Podaj dodatnią docelową cenę DEX dla drabinki auto-kupna.",
            ) from error
        if not target_buy_price.is_finite() or target_buy_price <= 0:
            raise HTTPException(
                status_code=400,
                detail="Podaj dodatnią docelową cenę DEX dla drabinki auto-kupna.",
            )
    elif pair.auto_trade_enabled and pair.auto_buy_mode == "schedule":
        if not pair.auto_buy_schedule:
            raise HTTPException(
                status_code=400,
                detail="Dodaj co najmniej jeden wpis do harmonogramu auto-kupna.",
            )
        if len({entry.time for entry in pair.auto_buy_schedule}) != len(pair.auto_buy_schedule):
            raise HTTPException(
                status_code=400,
                detail="Godziny auto-kupna w harmonogramie muszą być unikalne.",
            )
    elif (
        pair.auto_trade_enabled
        and pair.target_buy_price is None
        and pair.buy_percent is None
        and not (
            pair.auto_buy_mode == "target_quantity"
            and pair.auto_buy_quantity_duration_minutes is not None
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="Ustaw cenę kupna albo procentowy próg kupna dla automatycznego handlu.",
        )
    if pair.auto_sell_mode not in {"target_quantity", "target_price", "schedule"}:
        raise HTTPException(status_code=400, detail="Nieobsługiwana strategia automatycznej sprzedaży.")
    if pair.auto_sell_enabled:
        if pair.auto_sell_mode == "schedule":
            if not pair.auto_sell_schedule:
                raise HTTPException(
                    status_code=400,
                    detail="Dodaj co najmniej jeden wpis do harmonogramu auto-sprzedaży.",
                )
            if len({entry.time for entry in pair.auto_sell_schedule}) != len(pair.auto_sell_schedule):
                raise HTTPException(
                    status_code=400,
                    detail="Godziny auto-sprzedaży w harmonogramie muszą być unikalne.",
                )
        else:
            try:
                target_sell_quantity = Decimal(str(pair.auto_sell_target_quantity))
            except InvalidOperation as error:
                raise HTTPException(
                    status_code=400,
                    detail="Podaj łączną ilość tokenów do automatycznej sprzedaży.",
                ) from error
            if not target_sell_quantity.is_finite() or target_sell_quantity <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Ilość tokenów do automatycznej sprzedaży musi być większa od zera.",
                )
        if pair.auto_sell_mode == "target_price":
            try:
                target_sell_price = Decimal(str(pair.target_sell_price))
            except InvalidOperation as error:
                raise HTTPException(
                    status_code=400,
                    detail="Podaj dodatnią docelową cenę DEX dla drabinki auto-sprzedaży.",
                ) from error
            if not target_sell_price.is_finite() or target_sell_price <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Podaj dodatnią docelową cenę DEX dla drabinki auto-sprzedaży.",
                )
        elif pair.auto_sell_mode == "target_quantity" and pair.target_sell_price is None and pair.sell_percent is None:
            raise HTTPException(
                status_code=400,
                detail="Ustaw cenę sprzedaży albo procentowy próg sprzedaży.",
            )
    if pair.auto_buy_mode == "schedule" or pair.auto_sell_mode == "schedule":
        try:
            ZoneInfo(pair.schedule_timezone)
        except ZoneInfoNotFoundError as error:
            raise HTTPException(
                status_code=400,
                detail="Nieprawidłowa strefa czasowa harmonogramu.",
            ) from error
        if (
            pair.auto_trade_enabled
            and pair.auto_sell_enabled
            and pair.auto_buy_mode == "schedule"
            and pair.auto_sell_mode == "schedule"
            and {entry.time for entry in pair.auto_buy_schedule}
            & {entry.time for entry in pair.auto_sell_schedule}
        ):
            raise HTTPException(
                status_code=400,
                detail="Nie ustawiaj kupna i sprzedaży tokena na tę samą godzinę.",
            )
    if not Web3.is_address(pair.token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena.")
    if not Web3.is_address(pair.quote_token_address):
        raise HTTPException(status_code=400, detail="Nieprawidłowy adres tokena płatności.")
    if pair.logo_url and urlparse(pair.logo_url).scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Adres logo musi używać HTTP albo HTTPS.")
    if pair.auto_trade_enabled or pair.auto_sell_enabled:
        with wallet_store_lock:
            wallet = wallet_store.get(pair.wallet_id or "")
            wallet_unlocked = pair.wallet_id in unlocked_wallet_keys
        if not wallet:
            raise HTTPException(status_code=400, detail="Wybierz zapisany portfel dla automatycznego handlu.")
        if wallet["network"] != pair.network:
            raise HTTPException(status_code=400, detail="Portfel i token muszą być w tej samej sieci.")
        if not wallet_unlocked:
            raise HTTPException(
                status_code=409,
                detail="Wybrany portfel jest zablokowany w pamięci serwera. Odśwież listę portfeli i odblokuj go ponownie przed włączeniem auto-handlu.",
            )
    pair_id = f"{pair.network}_{pair.token_address}_{pair.quote_token_address}".lower()
    pair_data = pair.dict()
    pair_data["id"] = pair_id
    started_at = time.time()
    pair_data["auto_buy_schedule"] = (
        initialize_daily_schedule(
            pair_data["auto_buy_schedule"], pair.schedule_timezone, started_at
        )
        if pair.auto_trade_enabled and pair.auto_buy_mode == "schedule"
        else []
    )
    pair_data["auto_sell_schedule"] = (
        initialize_daily_schedule(
            pair_data["auto_sell_schedule"], pair.schedule_timezone, started_at
        )
        if pair.auto_sell_enabled and pair.auto_sell_mode == "schedule"
        else []
    )
    pair_data["auto_buy_campaign_id"] = uuid.uuid4().hex
    pair_data["auto_sell_campaign_id"] = uuid.uuid4().hex
    pair_data.setdefault("auto_buy_last_attempt_at", 0)
    pair_data.setdefault("auto_sell_last_attempt_at", 0)
    pair_data["auto_buy_start_price"] = None
    pair_data["auto_sell_start_price"] = None
    pair_data["auto_buy_started_at"] = None
    pair_data["auto_sell_started_at"] = None
    pair_data["auto_buy_last_success_at"] = None
    pair_data["auto_sell_last_success_at"] = None
    pair_data["auto_buy_quantity_started_at"] = (
        time.time()
        if (
            pair.auto_trade_enabled
            and pair.auto_buy_mode == "target_quantity"
            and pair.auto_buy_quantity_duration_minutes is not None
        )
        else None
    )
    pair_data["auto_buy_next_trade_at"] = pair_data["auto_buy_quantity_started_at"]
    pair_data["auto_sell_next_trade_at"] = None
    pair_data["auto_buy_target_logged"] = False
    pair_data["auto_sell_target_logged"] = False
    active_pairs[pair_id] = pair_data
    await manager.broadcast({
        "type": "PRICE_UPDATE",
        "ticker": ticker_prices,
        "data": [
            pair_snapshot(existing_id, existing_pair)
            for existing_id, existing_pair in active_pairs.items()
        ],
        "stats": latest_bot_stats,
        "logs": trade_logs[-15:]
    })
    return {"status": "success", "pair_id": pair_id}

@app.delete("/api/pairs/{pair_id}")
async def remove_pair(pair_id: str):
    if pair_id in active_pairs:
        del active_pairs[pair_id]
        return {"status": "removed"}
    return {"status": "not_found"}

@app.get("/")
async def get_dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({
            "type": "PRICE_UPDATE",
            "ticker": ticker_prices,
            "data": [
                pair_snapshot(pair_id, pair)
                for pair_id, pair in active_pairs.items()
            ],
            "stats": latest_bot_stats,
            "logs": trade_logs[-15:]
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)