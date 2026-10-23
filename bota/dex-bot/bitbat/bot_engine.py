import requests
import pandas as pd
from datetime import datetime
from web3 import Web3

# Mapowanie sieci dla GeckoTerminal API
NETWORK_MAP = {
    "robinhood": "arbitrum",
    "ethereum": "eth",
    "bnb": "bsc",
    "solana": "solana"
}

# Standardowe ABI Uniswap/PancakeSwap Router (swapExactETHForTokens / swapExactTokensForETH)
ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

def get_token_details(chain: str, contract_address: str):
    """Pobiera informacje o tokenie, cenie oraz dane do wykresu 24h z GeckoTerminal."""
    network = NETWORK_MAP.get(chain.lower(), "arbitrum")
    
    # 1. Pobranie metadanych tokena
    token_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{contract_address}"
    res = requests.get(token_url)
    if res.status_code != 200:
        raise Exception("Nie znaleziono tokena na wybranej sieci.")
    
    attr = res.json()["data"]["attributes"]
    
    # 2. Pobranie najpopularniejszej puli płynności (Pool)
    pools_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{contract_address}/top_pools"
    pool_res = requests.get(pools_url).json()
    top_pool = pool_res["data"][0]
    pool_id = top_pool["id"].split("_")[-1]

    # 3. Pobranie świec 24h (OHLCV - świece 15 min)
    ohlcv_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{top_pool['id']}/ohlcv/minute?aggregate=15&limit=96"
    ohlcv_data = requests.get(ohlcv_url).json()["data"]["attributes"]["ohlcv_list"]

    # Konwersja do Pandas DataFrame dla Plotly
    df = pd.DataFrame(ohlcv_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")

    return {
        "name": attr.get("name"),
        "symbol": attr.get("symbol"),
        "price_usd": float(attr.get("price_usd", 0)),
        "logo_url": attr.get("image_url"),
        "websites": attr.get("websites", []),
        "twitter": f"https://twitter.com/{attr.get('twitter_handle')}" if attr.get("twitter_handle") else None,
        "dex_pair": top_pool["attributes"]["name"],
        "chart_df": df
    }

class EVMTrader:
    def __init__(self, rpc_url: str, private_key: str = None):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = self.w3.eth.account.from_key(private_key) if private_key else None

    def get_balance(self, address: str) -> float:
        balance_wei = self.w3.eth.get_balance(address)
        return float(self.w3.from_wei(balance_wei, "ether"))

    def execute_buy(self, router_address: str, token_address: str, amount_eth: float):
        """Wykonywanie swapu ETH -> Token na wskazanym routerze."""
        if not self.account:
            raise Exception("Brak skonfigurowanego klucza prywatnego!")

        router = self.w3.eth.contract(address=Web3.to_checksum_address(router_address), abi=ROUTER_ABI)
        weth_address = router.functions.WETH().call() if hasattr(router.functions, 'WETH') else token_address

        path = [Web3.to_checksum_address(weth_address), Web3.to_checksum_address(token_address)]
        deadline = self.w3.eth.get_block('latest')['timestamp'] + 600

        tx = router.functions.swapExactETHForTokens(
            0, # Minimalny Otrzymany Kwant (Slippage)
            path,
            self.account.address,
            deadline
        ).build_transaction({
            'from': self.account.address,
            'value': self.w3.to_wei(amount_eth, 'ether'),
            'gas': 250000,
            'gasPrice': self.w3.eth.gas_price,
            'nonce': self.w3.eth.get_transaction_count(self.account.address)
        })

        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        return self.w3.to_hex(tx_hash)