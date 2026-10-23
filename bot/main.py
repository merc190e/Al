import asyncio
import hmac
import logging
import os
import requests
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Literal, Optional
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from web3.exceptions import Web3Exception

from bot.swap_executor import execute_swap

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# --- MODELE DANYCH ---
class TradingPair(BaseModel):
    id: Optional[str] = None
    network: str                     # eth, bsc, solana, robinhood, arbitrum
    token_address: str               # Adres kontraktu tokena docelowego (Base Token)
    token_symbol: Optional[str] = "TOKEN"
    quote_token_address: str         # Adres kontraktu tokena płatności (Quote Token, np. USDT/WETH/Custom)
    quote_symbol: str = "USDT"       # Symbol waluty płatności (ETH, USDT, USDC, etc.)
    trade_amount: float = 100.0      # Za ile kupujemy / ile sprzedajemy
    target_buy_price: Optional[float] = None
    target_sell_price: Optional[float] = None
    buy_percent: Optional[float] = None       # np. -5.0%
    sell_percent: Optional[float] = None      # np. +10.0%
    entry_price: Optional[float] = None       # Średnia cena wejścia ($)
    amount_held: float = 0.0                  # Ilość posiadanych tokenów
    pair_contract_address: Optional[str] = None # Ręcznie podany kontrakt pary/puli DEX

class PoolSelection(BaseModel):
    pool_address: str

class TradeOrder(BaseModel):
    side: Literal["buy", "sell"]
    buy_amount: Optional[float] = Field(default=None, gt=0)
    sell_amount_percent: float = Field(default=100.0, gt=0, le=100)

active_pairs: Dict[str, dict] = {}
trade_logs: List[str] = []
bot_stats = {
    "total_invested": 0.0,
    "total_pnl_usd": 0.0,
    "total_pnl_percent": 0.0
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

def _token_price_from_pool(attributes: dict, relationships: dict, token_address: str) -> Optional[float]:
    base_id = relationships.get("base_token", {}).get("data", {}).get("id", "")
    quote_id = relationships.get("quote_token", {}).get("data", {}).get("id", "")
    base_address = base_id.split("_")[-1]
    quote_address = quote_id.split("_")[-1]

    if base_address.lower() == token_address.lower():
        value = attributes.get("base_token_price_usd")
    elif quote_address.lower() == token_address.lower():
        value = attributes.get("quote_token_price_usd")
    else:
        value = attributes.get("token_price_usd")

    try:
        price = float(value)
        return price if price > 0 else None
    except (TypeError, ValueError):
        return None

# --- POBIERANIE PARY DEX, DANYCH I STRONY Z GECKOTERMINAL ---
def fetch_geckoterminal_details(network: str, token_address: str, custom_pair_contract: Optional[str] = None) -> dict:
    headers = {
        "Accept": "application/json;version=20230203",
        "User-Agent": "Mozilla/5.0"
    }
    
    clean_network = network.lower().strip()
    clean_token = token_address.strip()
    if not clean_network or not clean_token or any(char in clean_network + clean_token for char in "/?#"):
        raise ValueError("Invalid network or token address.")
    
    result = {
        "price_usd": None,
        "change_24h": 0.0,
        "logo_url": None,
        "website": None,
        "twitter": None,
        "pool_address": custom_pair_contract.strip() if custom_pair_contract else None,
        "dex_pairs_list": [],
        "token_symbol": None,
        "token_name": None,
    }
    
    try:
        network_path = quote(clean_network, safe="")
        token_path = quote(clean_token, safe="")
        # 1. Pobieranie listy dostępnych par DEX dla danego tokena
        pools_url = f"https://api.geckoterminal.com/api/v2/networks/{network_path}/tokens/{token_path}/pools"
        res_pools = requests.get(pools_url, headers=headers, timeout=5)
        if res_pools.status_code == 200:
            pools_data = res_pools.json().get("data", [])
            for pool in pools_data[:6]:  # Zachowujemy top 6 par DEX
                attrs = pool.get("attributes", {})
                p_addr = pool.get("id", "").split("_")[-1]
                result["dex_pairs_list"].append({
                    "dex_name": attrs.get("dex_id", "DEX").capitalize(),
                    "pair_name": attrs.get("name", "Unknown Pair"),
                    "pool_address": p_addr,
                    "price_usd": _token_price_from_pool(
                        attrs,
                        pool.get("relationships", {}),
                        clean_token,
                    )
                })

            # Jeśli użytkownik nie podał ręcznego kontraktu pary, wybieramy pierwszą z brzegów
            if not result["pool_address"] and len(result["dex_pairs_list"]) > 0:
                result["pool_address"] = result["dex_pairs_list"][0]["pool_address"]

        # 2. Pobieranie dokładnej ceny z konkretnego kontraktu pary DEX
        if result["pool_address"]:
            pool_path = quote(result["pool_address"], safe="")
            pool_url = f"https://api.geckoterminal.com/api/v2/networks/{network_path}/pools/{pool_path}"
            res_pool = requests.get(pool_url, headers=headers, timeout=5)
            if res_pool.status_code == 200:
                pool_data = res_pool.json().get("data", {})
                pool_attrs = pool_data.get("attributes", {})
                result["price_usd"] = _token_price_from_pool(
                    pool_attrs,
                    pool_data.get("relationships", {}),
                    clean_token,
                )
                price_change = pool_attrs.get("price_change_percentage", {})
                if isinstance(price_change, dict):
                    result["change_24h"] = float(price_change.get("h24") or 0.0)

        # 3. Pobieranie symbolu tokena i loga
        token_url = f"https://api.geckoterminal.com/api/v2/networks/{network_path}/tokens/{token_path}"
        res_token = requests.get(token_url, headers=headers, timeout=5)
        if res_token.status_code == 200:
            t_data = res_token.json().get("data", {}).get("attributes", {})
            if t_data.get("name"):
                result["token_name"] = t_data["name"]
            if not result["price_usd"] and t_data.get("price_usd"):
                result["price_usd"] = float(t_data["price_usd"])
            if t_data.get("symbol"):
                result["token_symbol"] = t_data["symbol"].upper()
            if t_data.get("image_url"):
                result["logo_url"] = t_data["image_url"]

        # 4. Pobieranie Socials (Website / Twitter)
        info_url = f"https://api.geckoterminal.com/api/v2/networks/{network_path}/tokens/{token_path}/info"
        res_info = requests.get(info_url, headers=headers, timeout=5)
        if res_info.status_code == 200:
            info_attrs = res_info.json().get("data", {}).get("attributes", {})
            websites = info_attrs.get("websites", [])
            if websites:
                website = websites[0]
                if isinstance(website, dict):
                    website = website.get("url")
                if isinstance(website, str) and urlparse(website).scheme in {"http", "https"}:
                    result["website"] = website
            twitter_handle = info_attrs.get("twitter_handle")
            if twitter_handle:
                handle = str(twitter_handle).strip().lstrip("@")
                if handle and all(char.isalnum() or char == "_" for char in handle):
                    result["twitter"] = f"https://x.com/{handle}"
            if not result["logo_url"] and info_attrs.get("image_url"):
                result["logo_url"] = info_attrs["image_url"]

    except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError) as e:
        logging.error("Błąd GeckoTerminal API dla %s: %s", clean_token, e)
        
    return result


def fetch_geckoterminal_ohlcv(network: str, pool_address: str) -> list[dict]:
    clean_network = network.lower().strip()
    clean_pool = pool_address.strip()
    if not clean_network or not clean_pool or any(char in clean_network + clean_pool for char in "/?#"):
        raise ValueError("Invalid network or pool address.")

    url = (
        f"https://api.geckoterminal.com/api/v2/networks/{quote(clean_network, safe='')}"
        f"/pools/{quote(clean_pool, safe='')}/ohlcv/minute?aggregate=15&limit=96"
    )
    response = requests.get(
        url,
        headers={"Accept": "application/json;version=20230203"},
        timeout=10,
    )
    response.raise_for_status()
    candles = response.json()["data"]["attributes"]["ohlcv_list"]
    parsed_candles = []
    for candle in candles:
        if not isinstance(candle, list) or len(candle) != 6:
            raise ValueError("GeckoTerminal returned an invalid OHLCV candle.")
        timestamp, *values = candle
        parsed_candles.append({
            "timestamp": int(timestamp),
            "open": float(values[0]),
            "high": float(values[1]),
            "low": float(values[2]),
            "close": float(values[3]),
            "volume": float(values[4]),
        })
    return sorted(parsed_candles, key=lambda candle: candle["timestamp"])

# --- SILNIK BOTA I KALKULACJA PnL ---
async def trading_bot_loop():
    while True:
        updates = []
        total_value_usd = 0.0
        total_pnl_usd = 0.0
        total_cost_basis_usd = 0.0

        for pair_id, pair in list(active_pairs.items()):
            gt_data = await asyncio.to_thread(
                fetch_geckoterminal_details,
                pair["network"],
                pair["token_address"],
                pair.get("pair_contract_address"),
            )
            
            current_price = gt_data["price_usd"]
            if current_price is None or current_price == 0:
                continue

            pair["logo_url"] = gt_data["logo_url"] or pair.get("logo_url")
            pair["website"] = gt_data["website"] or pair.get("website")
            pair["twitter"] = gt_data["twitter"] or pair.get("twitter")
            pair["pool_address"] = gt_data["pool_address"] or pair.get("pool_address")
            pair["dex_pairs_list"] = gt_data["dex_pairs_list"]
            pair["change_24h"] = gt_data["change_24h"]
            
            if gt_data["token_symbol"] and pair["token_symbol"] == "TOKEN":
                pair["token_symbol"] = gt_data["token_symbol"]

            if pair.get("entry_price") is None or pair["entry_price"] == 0:
                pair["entry_price"] = current_price

            calc_buy_target = pair.get("target_buy_price")
            calc_sell_target = pair.get("target_sell_price")

            if pair.get("buy_percent") is not None and pair["entry_price"] > 0:
                calc_buy_target = pair["entry_price"] * (1 + pair["buy_percent"] / 100.0)

            if pair.get("sell_percent") is not None and pair["entry_price"] > 0:
                calc_sell_target = pair["entry_price"] * (1 + pair["sell_percent"] / 100.0)

            # Kalkulacje zysku / straty (PnL)
            amount_held = pair.get("amount_held", 0.0)
            holding_value_usd = amount_held * current_price
            cost_basis_usd = amount_held * pair["entry_price"]
            unrealized_pnl_usd = holding_value_usd - cost_basis_usd
            unrealized_pnl_percent = ((current_price - pair["entry_price"]) / pair["entry_price"] * 100) if pair["entry_price"] > 0 else 0.0

            total_value_usd += holding_value_usd
            total_pnl_usd += unrealized_pnl_usd
            total_cost_basis_usd += cost_basis_usd

            status = "MONITORING"

            if calc_buy_target and current_price <= calc_buy_target:
                status = "TRIGGER_BUY"
                log_msg = f"🟢 KUPNO [{pair['network'].upper()}]: Kup za {pair['trade_amount']} USD (Kontrakt płatności: {pair['quote_token_address'][:8]}...) -> {pair['token_symbol']} przy ${current_price:.6f}"
                if len(trade_logs) == 0 or trade_logs[-1] != log_msg:
                    trade_logs.append(log_msg)

            elif calc_sell_target and current_price >= calc_sell_target:
                status = "TRIGGER_SELL"
                log_msg = f"🔴 SPRZEDAŻ [{pair['network'].upper()}]: Sprzedaj {pair['token_symbol']} do płatności ({pair['quote_symbol']}) przy ${current_price:.6f}"
                if len(trade_logs) == 0 or trade_logs[-1] != log_msg:
                    trade_logs.append(log_msg)

            updates.append({
                "pair_id": pair_id,
                "token_symbol": pair["token_symbol"],
                "network": pair["network"],
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
                "status": status
            })

        bot_stats["total_invested"] = total_value_usd
        bot_stats["total_pnl_usd"] = total_pnl_usd
        bot_stats["total_pnl_percent"] = (
            total_pnl_usd / total_cost_basis_usd * 100
            if total_cost_basis_usd > 0
            else 0.0
        )

        await manager.broadcast({
            "type": "PRICE_UPDATE", 
            "data": updates, 
            "stats": bot_stats,
            "logs": trade_logs[-15:]
        })
        await asyncio.sleep(4)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(trading_bot_loop())

@app.post("/api/pairs")
async def add_pair(pair: TradingPair):
    pair_id = f"{pair.network}_{pair.token_address[:8]}_{pair.quote_token_address[:8]}".lower()
    pair_data = pair.model_dump()
    pair_data["id"] = pair_id
    active_pairs[pair_id] = pair_data
    return {"status": "success", "pair_id": pair_id}

@app.delete("/api/pairs/{pair_id}")
async def remove_pair(pair_id: str):
    if pair_id in active_pairs:
        del active_pairs[pair_id]
        return {"status": "removed"}
    return {"status": "not_found"}

@app.put("/api/pairs/{pair_id}/pool")
async def select_pair_pool(pair_id: str, selection: PoolSelection):
    pair = active_pairs.get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Para nie istnieje")

    pool_address = selection.pool_address.strip()
    available = {
        pool.get("pool_address")
        for pool in pair.get("dex_pairs_list", [])
    }
    if not pool_address or pool_address not in available:
        raise HTTPException(status_code=422, detail="Wybrana pula nie jest dostępna dla tego tokena")

    pair["pair_contract_address"] = pool_address
    pair["pool_address"] = pool_address
    return {"status": "selected", "pool_address": pool_address}

@app.get("/api/tokens/{network}/{token_address}")
async def get_token_details(
    network: str,
    token_address: str,
    pool_address: Optional[str] = None,
):
    try:
        details = await asyncio.to_thread(
            fetch_geckoterminal_details,
            network,
            token_address,
            pool_address,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    selected_pool = details.get("pool_address")
    if not selected_pool:
        raise HTTPException(status_code=404, detail="No GeckoTerminal pool found for this token.")
    try:
        candles = await asyncio.to_thread(
            fetch_geckoterminal_ohlcv,
            network,
            selected_pool,
        )
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        logging.exception("GeckoTerminal OHLCV request failed for %s", token_address)
        raise HTTPException(status_code=502, detail="Could not retrieve token OHLCV data.") from exc

    return {
        "name": details.get("token_name"),
        "symbol": details.get("token_symbol"),
        "price_usd": details.get("price_usd"),
        "logo_url": details.get("logo_url"),
        "website": details.get("website"),
        "twitter": details.get("twitter"),
        "pool_address": selected_pool,
        "dex_pairs": details.get("dex_pairs_list", []),
        "change_24h": details.get("change_24h"),
        "ohlcv_24h": candles,
    }

@app.post("/api/pairs/{pair_id}/trade")
async def submit_pair_trade(
    pair_id: str,
    order: TradeOrder,
    api_token: Optional[str] = Header(default=None, alias="X-Trading-API-Token"),
):
    configured_token = os.getenv("TRADING_API_TOKEN", "").strip()
    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="Trading API is disabled; configure TRADING_API_TOKEN first.",
        )
    if not api_token or not hmac.compare_digest(api_token, configured_token):
        raise HTTPException(status_code=401, detail="Invalid or missing trading API token.")

    pair = active_pairs.get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Trading pair not found.")

    chain_aliases = {
        "eth": "ethereum",
        "ethereum": "ethereum",
        "bsc": "bsc",
        "bnb": "bsc",
        "arbitrum": "arbitrum",
        "robinhood": "robinhood",
    }
    chain = chain_aliases.get(pair["network"].lower())
    if chain is None:
        raise HTTPException(status_code=422, detail="Live swaps are not supported on this network.")
    if order.side == "buy" and order.buy_amount is None:
        raise HTTPException(status_code=422, detail="Set a positive buy_amount in quote-token units.")

    executor_pair = SimpleNamespace(
        chain=chain,
        token_address=pair["token_address"],
        quote_address=(pair.get("quote_token_address") or "").strip() or None,
        buy_amount=order.buy_amount,
        sell_amount_percent=order.sell_amount_percent,
    )
    try:
        tx_hash = await asyncio.to_thread(execute_swap, executor_pair, order.side)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        logging.error("Swap configuration or RPC failed for pair %s: %s", pair_id, exc)
        raise HTTPException(status_code=503, detail="Swap could not be submitted; check RPC and signer configuration.") from exc
    except Web3Exception as exc:
        logging.exception("Swap submission failed for pair %s", pair_id)
        raise HTTPException(status_code=502, detail="The blockchain provider rejected the swap request.") from exc

    trade_logs.append(
        f"🧾 {order.side.upper()} submitted for {pair.get('token_symbol', 'TOKEN')} "
        f"on {chain}: {tx_hash}"
    )
    return {"status": "submitted", "tx_hash": tx_hash, "side": order.side}

@app.get("/")
async def get_dashboard():
    return HTMLResponse(
        content=Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    )

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)