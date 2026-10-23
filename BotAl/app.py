import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from web3 import Web3
from web3.exceptions import Web3Exception

from dex_seller import sell_erc20_token
from gecko_sl_tp_bot import GeckoSLTPBot

# ==========================================
# 1. KONFIGURACJA STRONY I STYLIZACJA
# ==========================================
st.set_page_config(
    page_title="Gecko DEX Trading Bot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Mapowanie sieci na identyfikatory GeckoTerminal i RPC
NETWORKS = {
    "Ethereum": {"gecko": "eth", "chain_id": 1, "currency": "ETH", "rpc": "https://eth.llamarpc.com"},
    "BNB Chain": {"gecko": "bsc", "chain_id": 56, "currency": "BNB", "rpc": "https://bsc-dataseed.binance.org"},
    "Solana": {"gecko": "solana", "chain_id": 0, "currency": "SOL", "rpc": "https://api.mainnet-beta.solana.com"},
    "Robinhood Chain": {"gecko": "arbitrum", "chain_id": 42161, "currency": "ETH", "rpc": "https://arb1.arbitrum.io/rpc"}
}
SELL_NETWORKS = {
    "Ethereum": "ethereum",
    "BNB Chain": "bsc",
    "Robinhood Chain": "arbitrum",
}


@st.fragment(run_every="5s")
def render_sltp_monitor():
    position = st.session_state.get("sltp_monitor")
    if not position:
        return

    status = position["status"]
    if status not in {"monitoring", "executing"}:
        if status == "sold":
            st.success(
                f"{position['reason']} osiągnięty. Sprzedaż zakończona: "
                f"[transakcja](https://{position['explorer']}/tx/{position['tx_hash']})."
            )
        elif status == "error":
            st.error(f"Automatyczna sprzedaż nie powiodła się: {position['error']}")
        elif status == "stopped":
            st.info("Monitoring SL/TP został zatrzymany.")
        return

    st.info(
        f"Monitoring aktywny dla {position['token_symbol']}: "
        f"SL ${position['entry_price'] * (1 - position['stop_loss_pct'] / 100):.8f} · "
        f"TP ${position['entry_price'] * (1 + position['take_profit_pct'] / 100):.8f}"
    )
    if status == "monitoring" and st.button(
        "⏹️ Zatrzymaj monitoring", key="stop_sltp_monitor", use_container_width=True
    ):
        position["status"] = "stopped"
        position.pop("private_key", None)
        st.session_state.sltp_monitor = position
        st.info("Monitoring SL/TP został zatrzymany.")
        return

    if status == "executing":
        st.warning("Próg osiągnięty — trwa wysyłanie transakcji sprzedaży.")
        return

    try:
        monitor = GeckoSLTPBot(
            network=position["network"],
            pool_address=position["pool_address"],
            token_address=position["token_address"],
            entry_price=position["entry_price"],
            stop_loss_pct=position["stop_loss_pct"],
            take_profit_pct=position["take_profit_pct"],
        )
        current_price = monitor.get_current_price()
    except requests.RequestException as exc:
        st.warning(f"Nie udało się pobrać ceny z GeckoTerminal: {exc}")
        return
    except (ValueError, KeyError, TypeError) as exc:
        position["status"] = "error"
        position["error"] = str(exc)
        st.session_state.sltp_monitor = position
        st.error(f"Monitoring SL/TP został zatrzymany: {exc}")
        return

    if current_price is None:
        st.warning("GeckoTerminal nie zwrócił aktualnej ceny; monitoring trwa dalej.")
        return

    st.metric(
        "Cena monitorowanej pozycji",
        f"${current_price:.8f}",
        f"{((current_price - position['entry_price']) / position['entry_price'] * 100):+.2f}%",
    )

    reason = monitor.get_trigger_reason(current_price)
    if reason is None:
        return

    position["status"] = "executing"
    position["reason"] = reason
    st.session_state.sltp_monitor = position
    try:
        tx_hash = sell_erc20_token(
            rpc_url=position["rpc_url"],
            private_key=position["private_key"],
            token_address=position["token_address"],
            network_name=position["sell_network"],
            amount_to_sell_pct=100.0,
        )
    except (
        ConnectionError,
        TimeoutError,
        RuntimeError,
        ValueError,
        KeyError,
        Web3Exception,
        requests.RequestException,
    ) as exc:
        position["status"] = "error"
        position["error"] = str(exc)
        position.pop("private_key", None)
        st.session_state.sltp_monitor = position
        st.error(f"{reason} osiągnięty, ale sprzedaż nie powiodła się: {exc}")
        return

    position["status"] = "sold"
    position["tx_hash"] = tx_hash
    position.pop("private_key", None)
    st.session_state.sltp_monitor = position
    st.session_state.history.insert(0, {
        "Czas": datetime.now().strftime("%H:%M:%S"),
        "Typ": "SELL",
        "Symbol": position["token_symbol"],
        "Ilość": "100% salda",
        "Cena USD": f"${current_price:.8f}",
        "Wartość USD": "Transakcja on-chain",
        "Pula/Para": position["pool_address"][:10] + "...",
        "Powód": reason,
        "Tx Hash": tx_hash,
    })
    st.success(f"{reason} osiągnięty. Sprzedaż zakończona: {tx_hash}")

# Inicjalizacja historii portfela w sesji
if "history" not in st.session_state:
    st.session_state.history = []
if "portfolio_value" not in st.session_state:
    st.session_state.portfolio_value = 1000.0  # Początkowa wartość portfela w USD

# ==========================================
# 2. INTEGRACJA Z GECKOTERMINAL API
# ==========================================
@st.cache_data(ttl=15)
def get_token_metadata(network_key: str, token_address: str):
    """Pobiera metadane tokena: cenę, logo, social media oraz najpłynniejszą pulę."""
    gecko_net = NETWORKS[network_key]["gecko"]
    url = f"https://api.geckoterminal.com/api/v2/networks/{gecko_net}/tokens/{token_address}"
    headers = {"Accept": "application/json;version=20230203"}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        data = res.json()["data"]["attributes"]
        
        # Pobranie pul płynności dla tokena
        pools_url = f"https://api.geckoterminal.com/api/v2/networks/{gecko_net}/tokens/{token_address}/pools"
        pools_res = requests.get(pools_url, headers=headers, timeout=10).json()
        
        top_pool = pools_res["data"][0]["attributes"]["address"] if pools_res.get("data") else ""
        pool_list = [
            {
                "address": p["attributes"]["address"],
                "name": p["attributes"]["name"],
                "reserve_usd": p["attributes"].get("reserve_in_usd", "0")
            }
            for p in pools_res.get("data", [])
        ]
        
        return {
            "name": data.get("name", "Unknown"),
            "symbol": data.get("symbol", "TOKEN"),
            "price_usd": float(data.get("price_usd", 0) or 0),
            "logo": data.get("image_url", ""),
            "website": (data.get("websites") or [None])[0],
            "twitter": f"https://x.com/{data.get('twitter_handle')}" if data.get("twitter_handle") else None,
            "top_pool": top_pool,
            "pools": pool_list
        }
    except Exception as e:
        st.error(f"Błąd podczas pobierania danych z GeckoTerminal: {e}")
        return None

@st.cache_data(ttl=60)
def get_ohlcv_chart(network_key: str, pool_address: str):
    """Pobiera dane świecowe (OHLCV) dla podanej puli."""
    if not pool_address:
        return None
    gecko_net = NETWORKS[network_key]["gecko"]
    url = f"https://api.geckoterminal.com/api/v2/networks/{gecko_net}/pools/{pool_address}/ohlcv/day"
    headers = {"Accept": "application/json;version=20230203"}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        ohlcv_list = res.json()["data"]["attributes"]["ohlcv_list"]
        
        df = pd.DataFrame(ohlcv_list, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
        return df.sort_values("datetime")
    except Exception:
        return None

# ==========================================
# 3. PANEL BOCZNY (PORTFEL I USTAWIENIA)
# ==========================================
with st.sidebar:
    st.title("🤖 Trading Bot Settings")
    
    # Wybór Sieci Blockchain
    selected_network = st.selectbox("Wybierz Sieć (Chain):", list(NETWORKS.keys()))
    net_info = NETWORKS[selected_network]
    
    st.divider()
    
    # Podłączenie Portfela
    st.subheader("🔌 Połączenie z Portfelem")
    wallet_type = st.radio("Metoda połączenia:", ["MetaMask / Web3", "QR WalletConnect", "Klucz Prywatny (Automatyczny)"])
    
    wallet_address = ""
    private_key = ""
    if wallet_type == "Klucz Prywatny (Automatyczny)":
        private_key = st.text_input(
            "Klucz Prywatny:",
            type="password",
            help="Klucz jest używany lokalnie przez aplikację do automatycznej egzekucji zleceń.",
            key="private_key",
        )
        if private_key:
            try:
                w3 = Web3(Web3.HTTPProvider(net_info["rpc"]))
                account = w3.eth.account.from_key(private_key)
                wallet_address = account.address
                st.success(f"Połączono: {wallet_address[:6]}...{wallet_address[-4:]}")
            except Exception:
                st.error("Nieprawidłowy klucz prywatny")
    else:
        wallet_address = st.text_input("Adres Portfela:", value="0x71C...9B23")
        st.info("Zeskanuj kod QR lub zatwierdź połączenie w portfelu.")

    st.divider()
    
    # Podsumowanie Portfela
    st.subheader("💼 Saldo i Wartość")
    st.metric("Wartość Portfela", f"${st.session_state.portfolio_value:,.2f}")

# ==========================================
# 4. GŁÓWNY INTERFEJS APULIKACJI
# ==========================================
st.title("📊 GeckoTerminal DEX Trading Dashboard")

# Wyszukiwarka Tokena
token_contract = st.text_input(
    f"Podaj adres kontraktu coina ({selected_network}):",
    value="0x6b175474e89094c44da98b954eedeac495271d0f",
    help="Wklej adres tokena ERC20 / SPL"
)

token_info = get_token_metadata(selected_network, token_contract)

if token_info:
    # Nagłówek Coina z Logo i Linkami Social Media
    col_logo, col_details, col_socials = st.columns([1, 3, 2])
    
    with col_logo:
        if token_info["logo"]:
            st.image(token_info["logo"], width=75)
            
    with col_details:
        st.subheader(f"{token_info['name']} ({token_info['symbol']})")
        st.markdown(f"### **Cena:** `${token_info['price_usd']:.8f}`")
        
    with col_socials:
        st.write("**Odnośniki:**")
        links = []
        if token_info["website"]:
            links.append(f"[🌐 Strona WWW]({token_info['website']})")
        if token_info["twitter"]:
            links.append(f"[🐦 Twitter / X]({token_info['twitter']})")
        st.markdown(" | ".join(links) if links else "Brak linków")

    st.divider()

    # Wybór Pary / Puli Handlowej
    col_pair1, col_pair2 = st.columns(2)
    with col_pair1:
        pair_mode = st.radio("Wybór Pary Handlowej:", ["Automatyczny (GeckoTerminal)", "Ręczny Adres Pary"], horizontal=True)
    
    active_pool_address = token_info["top_pool"]
    
    with col_pair2:
        if pair_mode == "Automatyczny (GeckoTerminal)":
            pool_options = {p["address"]: f"{p['name']} (Płynność: ${float(p['reserve_usd']):,.0f})" for p in token_info["pools"]}
            if pool_options:
                active_pool_address = st.selectbox("Wybierz Pulę:", list(pool_options.keys()), format_func=lambda x: pool_options[x])
        else:
            active_pool_address = st.text_input("Wpisz Adres Pary (Pair Contract):", value=token_info["top_pool"])

    # Wykres Świecowy (Plotly)
    df_chart = get_ohlcv_chart(selected_network, active_pool_address)
    if df_chart is not None and not df_chart.empty:
        fig = go.Figure(data=[go.Candlestick(
            x=df_chart['datetime'],
            open=df_chart['open'],
            high=df_chart['high'],
            low=df_chart['low'],
            close=df_chart['close'],
            increasing_line_color='#00c853',
            decreasing_line_color='#ff1744'
        )])
        fig.update_layout(
            title=f"Wykres Ceny {token_info['symbol']} (GeckoTerminal)",
            template="plotly_dark",
            xaxis_rangeslider_visible=False,
            height=400,
            margin=dict(l=20, r=20, t=40, b=20)
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Brak danych świecowych dla wybranej puli.")

    st.divider()

    # ==========================================
    # 5. AUTOMATYCZNA OCHRONA POZYCJI (SL / TP)
    # ==========================================
    st.subheader("🛡️ Automatyczny Stop-Loss / Take-Profit")
    st.caption(
        "Po osiągnięciu progu aplikacja sprzedaje 100% salda tokena. "
        "Monitoring odpytuje GeckoTerminal co 5 sekund. "
        "Sprzedaż używa istniejącego routera V2 (token → wrapped native, slippage 2%); "
        "wybrana pula służy do monitorowania ceny."
    )
    if selected_network not in SELL_NETWORKS:
        st.error(
            "Automatyczna sprzedaż nie jest dostępna dla Solany: obecny wykonawca "
            "obsługuje tylko Ethereum, BNB Chain i Arbitrum."
        )
    else:
        with st.form("sltp_form"):
            col_sl, col_tp, col_entry = st.columns(3)
            with col_entry:
                entry_price = st.number_input(
                    "Cena wejścia (USD)",
                    min_value=0.0,
                    value=float(token_info["price_usd"] or 0.0),
                    format="%.12f",
                )
            with col_sl:
                stop_loss_pct = st.number_input(
                    "Stop-Loss (%)", min_value=0.0, max_value=99.99, value=3.0, step=0.5
                )
            with col_tp:
                take_profit_pct = st.number_input(
                    "Take-Profit (%)", min_value=0.0, value=8.0, step=0.5
                )

            start_monitoring = st.form_submit_button(
                "▶️ Uruchom monitoring i automatyczną sprzedaż",
                use_container_width=True,
                disabled=(
                    st.session_state.get("sltp_monitor", {}).get("status")
                    in {"monitoring", "executing"}
                ),
            )
        if start_monitoring:
            if wallet_type != "Klucz Prywatny (Automatyczny)" or not private_key:
                st.error("Do automatycznej sprzedaży podłącz portfel za pomocą klucza prywatnego.")
            elif not active_pool_address:
                st.error("Wybierz lub podaj adres puli przed uruchomieniem monitoringu.")
            elif entry_price <= 0 or stop_loss_pct <= 0 or take_profit_pct <= 0:
                st.error("Cena wejścia oraz progi Stop-Loss i Take-Profit muszą być większe od 0.")
            else:
                st.session_state.sltp_monitor = {
                    "status": "monitoring",
                    "network": net_info["gecko"],
                    "sell_network": SELL_NETWORKS[selected_network],
                    "rpc_url": net_info["rpc"],
                    "pool_address": active_pool_address,
                    "token_address": token_contract,
                    "token_symbol": token_info["symbol"],
                    "entry_price": entry_price,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                    "private_key": private_key,
                    "explorer": {
                        "Ethereum": "etherscan.io",
                        "BNB Chain": "bscscan.com",
                        "Robinhood Chain": "arbiscan.io",
                    }[selected_network],
                }
                st.success("Monitoring SL/TP uruchomiony.")

    st.divider()

    # ==========================================
    # 6. PANEL TRADINGOWY (BUY / SELL ENGINE)
    # ==========================================
    st.subheader("⚡ Panel Handlowy Bota")
    
    col_trade_left, col_trade_right = st.columns(2)
    
    with col_trade_left:
        trade_type = st.radio("Typ Zlecenia:", ["KUPNO (BUY)", "SPRZEDAŻ (SELL)"], horizontal=True)
        
        # Wybór Waluty
        if trade_type == "KUPNO (BUY)":
            pay_currency = st.selectbox(
                "Płacę w walucie:",
                [f"Native ({net_info['currency']})", "USD (USDT/USDC)", "Ręczny Kontrakt Waluty"]
            )
        else:
            pay_currency = st.selectbox(
                "Otrzymuję w walucie:",
                [f"Native ({net_info['currency']})", "USD (USDT/USDC)", "Ręczny Kontrakt Waluty"]
            )
            
        custom_pay_contract = ""
        if pay_currency == "Ręczny Kontrakt Waluty":
            custom_pay_contract = st.text_input("Wpisz adres kontraktu waluty docelowej/płatniczej:")

    with col_trade_right:
        amount_type = st.radio("Sposób ustalania ilości:", ["Procentowo (%)", "Stała Kwota / Cena"], horizontal=True)
        
        trade_amount = 0.0
        if amount_type == "Procentowo (%)":
            percent = st.select_slider("Wybierz procent salda:", options=[25, 50, 75, 100], value=50)
            st.write(f"Wybrano: **{percent}%** wartości portfela")
            trade_amount = (st.session_state.portfolio_value * (percent / 100)) / (token_info["price_usd"] or 1)
        else:
            exact_val = st.number_input("Wpisz kwotę w USD:", min_value=1.0, value=100.0)
            trade_amount = exact_val / (token_info["price_usd"] or 1)

    # Przycisk wykonania transakcji
    if st.button(f"🚀 EXECUTE {trade_type}", use_container_width=True):
        if not wallet_address:
            st.error("Najpierw podłącz portfel w panelu bocznym!")
        else:
            action = "BUY" if "KUPNO" in trade_type else "SELL"
            total_usd = trade_amount * token_info["price_usd"]
            
            # Aktualizacja salda portfela w sesji
            if action == "BUY":
                st.session_state.portfolio_value -= total_usd
            else:
                st.session_state.portfolio_value += total_usd
                
            # Zapis transakcji do historii
            new_trade = {
                "Czas": datetime.now().strftime("%H:%M:%S"),
                "Typ": action,
                "Symbol": token_info["symbol"],
                "Ilość": f"{trade_amount:,.2f}",
                "Cena USD": f"${token_info['price_usd']:.8f}",
                "Wartość USD": f"${total_usd:,.2f}",
                "Pula/Para": active_pool_address[:10] + "..."
            }
            st.session_state.history.insert(0, new_trade)
            st.balloons()
            st.success(f"Pomyślnie wykonano zlecenie {action} dla {token_info['symbol']}!")

    st.divider()

    # ==========================================
    # 7. HISTORIA TRANSAKCJI I STATYSTYKI PORTFELA
    # ==========================================
    st.subheader("📈 Wykres Portfela i Statystyki Bota")
    
    if st.session_state.history:
        df_history = pd.DataFrame(st.session_state.history)
        
        col_stat1, col_stat2, col_stat3 = st.columns(3)
        total_buys = len([x for x in st.session_state.history if x["Typ"] == "BUY"])
        total_sells = len([x for x in st.session_state.history if x["Typ"] == "SELL"])
        
        col_stat1.metric("Wykonane Kupna", total_buys)
        col_stat2.metric("Wykonane Sprzedaże", total_sells)
        col_stat3.metric("Łączne Zlecenia", len(st.session_state.history))
        
        st.write("**Historia Zleceń:**")
        st.dataframe(df_history, use_container_width=True)
    else:
        st.info("Brak wykonanych transakcji w tej sesji.")

else:
    st.warning("Nie znaleziono tokena dla podanego adresu kontraktu w wybranej sieci. Sprawdź poprawność danych.")

render_sltp_monitor()