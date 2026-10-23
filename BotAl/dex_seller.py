import time
from web3 import Web3

# Minimum ABI potrzebne do zatwierdzania tokena ERC-20
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function"
    }
]

# Minimum ABI Routera Uniswap V2 / PancakeSwap
ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"}
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Znane adresy routerów V2
ROUTERS = {
    "ethereum": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # Uniswap V2 Router
    "bsc": "0x10ED43335D1566572F56097017439275380020e6",       # PancakeSwap V2 Router
    "arbitrum": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"  # SushiSwap Arbitrum
}

# Adresy natywnych tokenów owiniętych (WETH / WBNB)
WRAPPED_NATIVE = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", # WETH
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",      # WBNB
    "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1" # WETH Arbitrum
}


def sell_erc20_token(
    rpc_url: str,
    private_key: str,
    token_address: str,
    network_name: str = "ethereum",
    slippage_pct: float = 2.0,
    amount_to_sell_pct: float = 100.0
) -> str:
    """
    Sprzedaje token ERC-20 na giełdzie DEX (Uniswap V2 / PancakeSwap) za natywną walutę (ETH/BNB).

    :param rpc_url: URL węzła RPC (np. Infura, Alchemy, LlamaNodes)
    :param private_key: Klucz prywatny portfela w formacie hex string
    :param token_address: Adres kontraktu sprzedawanego tokena
    :param network_name: Nazwa sieci ('ethereum', 'bsc', 'arbitrum')
    :param slippage_pct: Dopuszczalny poślizg cenowy (slippage) w % (domyślnie 2.0%)
    :param amount_to_sell_pct: Jaki procent posiadanego salda sprzedać (100.0 = całość)
    :return: Hash transakcji sprzedaży
    """
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError("Nie można połączyć się z węzłem RPC.")

    account = w3.eth.account.from_key(private_key)
    wallet_address = account.address

    router_address = Web3.to_checksum_address(ROUTERS[network_name])
    weth_address = Web3.to_checksum_address(WRAPPED_NATIVE[network_name])
    token_address = Web3.to_checksum_address(token_address)

    token_contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    router_contract = w3.eth.contract(address=router_address, abi=ROUTER_ABI)

    # 1. Pobranie salda tokena
    balance = token_contract.functions.balanceOf(wallet_address).call()
    if balance == 0:
        raise ValueError("Saldo tokena wynosi 0.")

    amount_in = int(balance * (amount_to_sell_pct / 100.0))
    print(f"💰 Przygotowywanie sprzedaży {amount_in} jednostek tokena ({amount_to_sell_pct}% salda)...")

    # 2. Sprawdzenie i ew. zatwierdzenie (Approve)
    current_allowance = token_contract.functions.allowance(wallet_address, router_address).call()
    if current_allowance < amount_in:
        print("🔓 Brak wystarczającej zgody (allowance). Wysyłanie transakcji approve...")
        
        # Maksymalny allowance (2^256 - 1)
        max_uint256 = 2**256 - 1
        approve_tx = token_contract.functions.approve(router_address, max_uint256).build_transaction({
            'from': wallet_address,
            'nonce': w3.eth.get_transaction_count(wallet_address),
            'gasPrice': int(w3.eth.gas_price * 1.2)
        })
        
        signed_approve = w3.eth.account.sign_transaction(approve_tx, private_key)
        approve_tx_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        print(f"⏳ Oczekiwanie na zatwierdzenie Approve: {approve_tx_hash.hex()}")
        approve_receipt = w3.eth.wait_for_transaction_receipt(approve_tx_hash)
        if approve_receipt.status != 1:
            raise RuntimeError(f"Transakcja approve nie powiodła się: {approve_tx_hash.hex()}")
        print("✅ Approve zatwierdzony!")

    # 3. Obliczenie minimalnej kwoty wyjściowej (Slippage protection)
    path = [token_address, weth_address]
    amounts_out = router_contract.functions.getAmountsOut(amount_in, path).call()
    if not amounts_out:
        raise ValueError("Router nie znalazł ścieżki wymiany dla tego tokena.")
    expected_out = amounts_out[-1]
    amount_out_min = int(expected_out * (1.0 - slippage_pct / 100.0))

    deadline = int(time.time()) + 300  # Ważne przez 5 minut

    # 4. Budowanie transakcji sprzedaży (Swap)
    print("⚡ Wysyłanie transakcji sprzedaży (Swap)...")
    swap_tx = router_contract.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
        amount_in,
        amount_out_min,
        path,
        wallet_address,
        deadline
    ).build_transaction({
        'from': wallet_address,
        'nonce': w3.eth.get_transaction_count(wallet_address),
        'gasPrice': int(w3.eth.gas_price * 1.25)  # Wyższy gas price dla szybkiej egzekucji SL
    })

    # 5. Podpisanie i wysłanie transakcji
    signed_swap = w3.eth.account.sign_transaction(swap_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
    
    print(f"🚀 Transakcja sprzedaży wysłana! Hash: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    
    if receipt.status == 1:
        print("✅ Sprzedaż zakończona sukcesem!")
    else:
        print("❌ Transakcja sprzedaży odrzucona na blockchainie.")
        raise RuntimeError(f"Transakcja sprzedaży nie powiodła się: {tx_hash.hex()}")

    return tx_hash.hex()