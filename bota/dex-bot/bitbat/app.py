import streamlit as st
import plotly.graph_objects as go
import time
from bot_engine import get_token_details, EVMTrader

# Konfiguracja Strony
st.set_page_config(page_title="Crypto Bot Trader (Python)", layout="wide", page_icon="📈")

st.title("🤖 Crypto Trading Bot (EVM / Solana / Robinhood)")

# Panel Boczny: Wybór Sieci i Portfela
st.sidebar.header("⚙️ Konfiguracja Połączenia")

chain = st.sidebar.selectbox(
    "Wybierz Sieć",
    ["Robinhood (Arbitrum)", "Ethereum", "BNB Chain", "Solana"]
)

network_key_map = {
    "Robinhood (Arbitrum)": "robinhood",
    "Ethereum": "ethereum",
    "BNB Chain": "bnb",
    "Solana": "solana"
}

rpc_url = st.sidebar.text_input(
    "Adres RPC Node",
    value="https://arb1.arbitrum.io/rpc" if "Robinhood" in chain else "https://eth.llamarpc.com"
)

private_key = st.sidebar.text_input("Klucz Prywatny Portfela (Opcjonalnie)", type="password")
wallet_address = st.sidebar.text_input("Adres Portfela (do podglądu bilansu)")

# Wyświetlanie Bilansu
if wallet_address and "Solana" not in chain:
    try:
        trader = EVMTrader(rpc_url)
        bal = trader.get_balance(wallet_address)
        st.sidebar.metric(label="Bilans Portfela (Native Token)", value=f"{bal:.4f}")
    except Exception as e:
        st.sidebar.error(f"Błąd pobierania bilansu: {e}")

st.divider()

# Sekcja Wprowadzania Kontraktu
col_input, col_btn = st.columns([4, 1])
with col_input:
    contract_address = st.text_input("Wklej adres kontraktu tokena:", placeholder="0x... lub adres Solana")
with col_btn:
    st.write(" ")
    st.write(" ")
    fetch_clicked = st.button("Pobierz Token", use_container_width=True)

if fetch_clicked and contract_address:
    try:
        with st.spinner("Pobieranie danych z GeckoTerminal..."):
            token_info = get_token_details(network_key_map[chain], contract_address)
            st.session_state["token_info"] = token_info
            st.success("Pomyślnie pobrano dane tokena!")
    except Exception as e:
        st.error(f"Błąd: {e}")

# Wyświetlanie Panelu Głównego po pobraniu danych
if "token_info" in st.session_state:
    info = st.session_state["token_info"]

    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.subheader("📌 Metadane Tokena")
        if info["logo_url"]:
            st.image(info["logo_url"], width=80)
        st.markdown(f"**Nazwa:** {info['name']} (`{info['symbol']}`)")
        st.markdown(f"**Aktualna Cena:** `${info['price_usd']:.6f}`")
        st.markdown(f"**Para DEX:** `{info['dex_pair']}`")
        if info["twitter"]:
            st.markdown(f"[Twitter / X]({info['twitter']})")

        st.divider()

        # Ustawienia Bota Handlowego
        st.subheader("⚡ Automatyczne Zlecenie (Limit Order)")
        order_type = st.radio("Typ Zlecenia", ["KUPNO (BUY)", "SPRZEDAŻ (SELL)"])
        target_price = st.number_input("Cena Docelowa USD", value=info["price_usd"] * 0.95, format="%.6f")
        trade_amount = st.number_input("Kwota Transakcji (USD / ETH / SOL)", value=0.01)

        if st.button("🚀 Uruchom Bota Monitoringowego", type="primary"):
            st.warning("Bot został aktywowany. Sprawdzanie ceny w pętli...")
            
            # Pętla monitorująca cenę
            placeholder = st.empty()
            for _ in range(10): # Przykładowo 10 cykli sprawdzania
                current_info = get_token_details(network_key_map[chain], contract_address)
                curr_price = current_info["price_usd"]
                
                placeholder.info(f"Aktualna cena: **${curr_price:.6f}** \vert{} Cel: **${target_price:.6f}**")
                
                if "KUPNO" in order_type and curr_price <= target_price:
                    st.success("🎯 Osiągnięto cenę zakupu! Wykonywanie transakcji...")
                    break
                elif "SPRZEDAŻ" in order_type and curr_price >= target_price:
                    st.success("🎯 Osiągnięto cenę sprzedaży! Wykonywanie transakcji...")
                    break
                    
                time.sleep(3)

    with col_right:
        st.subheader("📊 Wykres Świecowy 24h (GeckoTerminal)")
        df = info["chart_df"]

        # Wykres Świecowy w Plotly
        fig = go.Figure(data=[go.Candlestick(
            x=df['timestamp'],
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name="Cena USD"
        )])

        fig.update_layout(
            template="plotly_dark",
            xaxis_rangeslider_visible=False,
            margin=dict(l=20, r=20, t=20, b=20),
            height=500
        )

        st.plotly_chart(fig, use_container_width=True)