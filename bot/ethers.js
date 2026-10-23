<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DEX Trading Bot + MetaMask & Robinhood</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/ethers/5.7.2/ethers.umd.min.js"></script>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen p-6">
    <div class="max-w-7xl mx-auto space-y-6">
        
        <!-- NAGŁÓWEK ORAZ INTEGRACJA PORTFELA -->
        <header class="flex flex-col sm:flex-row justify-between items-center bg-slate-800 p-4 rounded-xl border border-slate-700 gap-4">
            <div>
                <h1 class="text-2xl font-bold text-emerald-400 flex items-center gap-2">
                    🤖 DEX Trading Bot
                </h1>
                <p class="text-xs text-slate-400">Obsługa MetaMask, Robinhood Chain & DexScreener Logo</p>
            </div>
            
            <div class="flex items-center gap-4">
                <div class="flex items-center space-x-2 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-700">
                    <span id="status-indicator" class="w-2.5 h-2.5 rounded-full bg-red-500"></span>
                    <span id="status-text" class="text-xs text-slate-400">Rozłączono z botem</span>
                </div>

                <!-- PRZYCISK WEBMASK / WEB3 -->
                <button id="connect-wallet-btn" onclick="connectWallet()" class="bg-amber-500 hover:bg-amber-600 text-slate-950 font-semibold text-xs px-4 py-2 rounded-lg transition flex items-center gap-2">
                    <img src="https://upload.wikimedia.org/wikipedia/commons/3/36/MetaMask_Fox.svg" class="w-4 h-4" alt="MetaMask">
                    <span id="wallet-btn-text">Połącz Portfel</span>
                </button>
            </div>
        </header>

        <!-- PANEL INFORMACJI O PORTFELU -->
        <div id="wallet-info-box" class="hidden bg-slate-800/60 p-4 rounded-xl border border-amber-500/30 flex justify-between items-center text-xs">
            <div>
                <span class="text-slate-400">Podłączony portfel:</span>
                <span id="connected-address" class="font-mono text-amber-400 font-bold ml-2">0x...</span>
            </div>
            <div>
                <span class="text-slate-400">Saldo:</span>
                <span id="connected-balance" class="font-mono text-emerald-400 font-bold ml-2">0.00 ETH</span>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            
            <!-- FORMULARZ DODAWANIA PARY -->
            <div class="bg-slate-800 p-5 rounded-xl border border-slate-700 space-y-4">
                <h2 class="text-lg font-semibold text-slate-200">Dodaj token do bota</h2>
                
                <div>
                    <label class="block text-xs text-slate-400 mb-1">Sieć Blockchain</label>
                    <select id="chain" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                        <option value="robinhood">Robinhood Chain / Wallet Network</option>
                        <option value="ethereum">Ethereum (ETH)</option>
                        <option value="arbitrum">Arbitrum One</option>
                        <option value="bsc">Binance Smart Chain (BSC)</option>
                        <option value="polygon">Polygon (MATIC)</option>
                    </select>
                </div>

                <div>
                    <label class="block text-xs text-slate-400 mb-1">Adres Kontraktu Tokena</label>
                    <input type="text" id="token_address" placeholder="0x..." class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm font-mono">
                </div>

                <div class="grid grid-cols-2 gap-2">
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">Symbol Tokena</label>
                        <input type="text" id="token_symbol" placeholder="np. HOOD" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">Waluta Bazowa</label>
                        <select id="quote_symbol" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                            <option value="USDT">USDT</option>
                            <option value="ETH">ETH / WETH</option>
                            <option value="USDC">USDC</option>
                        </select>
                    </div>
                </div>

                <div class="border-t border-slate-700 pt-3 space-y-2">
                    <span class="text-xs font-semibold text-emerald-400">Progi Kwotowe ($)</span>
                    <div class="grid grid-cols-2 gap-2">
                        <input type="number" step="any" id="target_buy_price" placeholder="Kup przy cenie $" class="bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                        <input type="number" step="any" id="target_sell_price" placeholder="Sprzedaj przy cenie $" class="bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                    </div>
                </div>

                <div class="border-t border-slate-700 pt-3 space-y-2">
                    <span class="text-xs font-semibold text-cyan-400">Progi Procentowe (%)</span>
                    <div class="grid grid-cols-2 gap-2">
                        <input type="number" step="any" id="buy_percent" placeholder="Kup zmiana % (np. -5)" class="bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                        <input type="number" step="any" id="sell_percent" placeholder="Sprzedaj zmiana % (np. 10)" class="bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                    </div>
                </div>

                <button onclick="addPair()" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 rounded transition text-sm">
                    + Dodaj parę do Bota
                </button>
            </div>

            <!-- TABELA MONITOROWANYCH PAR -->
            <div class="lg:col-span-2 bg-slate-800 p-5 rounded-xl border border-slate-700 space-y-4">
                <h2 class="text-lg font-semibold text-slate-200">Aktywne Pary i Ceny DEX z Logo</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm text-slate-300">
                        <thead class="bg-slate-900 text-slate-400 uppercase text-xs">
                            <tr>
                                <th class="p-3">Token</th>
                                <th class="p-3">Cena DEX ($)</th>
                                <th class="p-3">Cel Kupna</th>
                                <th class="p-3">Cel Sprzedaży</th>
                                <th class="p-3">Status</th>
                                <th class="p-3 text-right">Akcja</th>
                            </tr>
                        </thead>
                        <tbody id="pairs-table-body">
                            <tr>
                                <td colspan="6" class="p-4 text-center text-slate-500">Brak aktywnych par. Dodaj token po lewej stronie.</td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <!-- LOGI TRANSAKCJI -->
                <div class="pt-4 border-t border-slate-700">
                    <h3 class="text-xs font-semibold text-slate-400 mb-2">Logi i powiadomienia na żywo</h3>
                    <div id="logs-container" class="bg-slate-900 p-3 rounded h-32 overflow-y-auto font-mono text-xs text-slate-400 space-y-1">
                        <div>[System] Połączenie inicjalizowane...</div>
                    </div>
                </div>
            </div>

        </div>
    </div>

    <script>
        let ws;
        let provider, signer, userAddress;

        // --- INTEGRACJA PORTFELA METAMASK / WEB3 ---
        async function connectWallet() {
            if (window.ethereum) {
                try {
                    provider = new ethers.providers.Web3Provider(window.ethereum);
                    await provider.send("eth_requestAccounts", []);
                    signer = provider.getSigner();
                    userAddress = await signer.getAddress();
                    
                    const balance = await provider.getBalance(userAddress);
                    const formattedBalance = parseFloat(ethers.utils.formatEther(balance)).toFixed(4);

                    document.getElementById('connected-address').innerText = userAddress.substring(0, 6) + "..." + userAddress.substring(38);
                    document.getElementById('connected-balance').innerText = `${formattedBalance} ETH`;
                    document.getElementById('wallet-btn-text').innerText = "Połączono";
                    document.getElementById('wallet-info-box').classList.remove('hidden');

                } catch (error) {
                    alert("Błąd połączenia z portfelem: " + error.message);
                }
            } else {
                alert("Nie wykryto MetaMask ani Web3 Wallet w przeglądarce!");
            }
        }

        // --- WEBSOCKET I ODŚWIEŻANIE CEN ---
        function connectWebSocket() {
            ws = new WebSocket(`ws://${location.host}/ws`);

            ws.onopen = () => {
                document.getElementById('status-indicator').className = 'w-2.5 h-2.5 rounded-full bg-emerald-500';
                document.getElementById('status-text').innerText = 'Połączono z botem';
            };

            ws.onmessage = (event) => {
                const message = JSON.parse(event.data);
                if (message.type === 'PRICE_UPDATE') {
                    renderPairs(message.data);
                    renderLogs(message.logs);
                }
            };

            ws.onclose = () => {
                document.getElementById('status-indicator').className = 'w-2.5 h-2.5 rounded-full bg-red-500';
                document.getElementById('status-text').innerText = 'Rozłączono (ponowne łączenie...)';
                setTimeout(connectWebSocket, 3000);
            };
        }

        function renderPairs(pairs) {
            const tbody = document.getElementById('pairs-table-body');
            if (!pairs || pairs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="p-4 text-center text-slate-500">Brak aktywnych par. Dodaj token po lewej stronie.</td></tr>';
                return;
            }

            tbody.innerHTML = pairs.map(p => {
                let statusBadge = '<span class="bg-slate-700 text-slate-300 px-2 py-1 rounded text-xs">Monitoring</span>';
                if (p.status === 'TRIGGER_BUY') statusBadge = '<span class="bg-emerald-900 text-emerald-300 px-2 py-1 rounded text-xs font-bold animate-pulse">KUPNO!</span>';
                if (p.status === 'TRIGGER_SELL') statusBadge = '<span class="bg-red-900 text-red-300 px-2 py-1 rounded text-xs font-bold animate-pulse">SPRZEDAŻ!</span>';

                // Domyślny avatar, jeśli brak logo z DexScreenera
                const defaultLogo = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ethereum/info/logo.png";
                const tokenLogo = p.logo_url || defaultLogo;

                return `
                    <tr class="border-b border-slate-700/50 hover:bg-slate-750">
                        <td class="p-3">
                            <div class="flex items-center gap-3">
                                <img src="${tokenLogo}" onerror="this.src='${defaultLogo}'" class="w-7 h-7 rounded-full bg-slate-700 p-0.5 object-cover" alt="Logo">
                                <div>
                                    <div class="font-bold text-white">${p.token_symbol} / ${p.quote_symbol}</div>
                                    <div class="text-[10px] text-slate-500">${p.chain}</div>
                                </div>
                            </div>
                        </td>
                        <td class="p-3 font-mono text-emerald-400 font-bold">$${p.current_price ? p.current_price.toFixed(6) : '---'}</td>
                        <td class="p-3 font-mono text-slate-400">${p.target_buy_price ? '$' + p.target_buy_price.toFixed(6) : '---'}</td>
                        <td class="p-3 font-mono text-slate-400">${p.target_sell_price ? '$' + p.target_sell_price.toFixed(6) : '---'}</td>
                        <td class="p-3">${statusBadge}</td>
                        <td class="p-3 text-right">
                            <button onclick="removePair('${p.pair_id}')" class="bg-red-600/20 hover:bg-red-600 text-red-400 hover:text-white px-2 py-1 rounded text-xs transition">
                                Usuń
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        function renderLogs(logs) {
            const container = document.getElementById('logs-container');
            if (logs && logs.length > 0) {
                container.innerHTML = logs.map(l => `<div>${l}</div>`).join('');
                container.scrollTop = container.scrollHeight;
            }
        }

        async function addPair() {
            const payload = {
                chain: document.getElementById('chain').value,
                token_address: document.getElementById('token_address').value.trim(),
                token_symbol: document.getElementById('token_symbol').value.trim() || "TOKEN",
                quote_symbol: document.getElementById('quote_symbol').value,
                target_buy_price: parseFloat(document.getElementById('target_buy_price').value) || None,
                target_sell_price: parseFloat(document.getElementById('target_sell_price').value) || None,
                buy_percent: parseFloat(document.getElementById('buy_percent').value) || None,
                sell_percent: parseFloat(document.getElementById('sell_percent').value) || None
            };

            if (!payload.token_address) {
                alert("Podaj adres kontraktu tokena!");
                return;
            }

            await fetch('/api/pairs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            document.getElementById('token_address').value = '';
            document.getElementById('target_buy_price').value = '';
            document.getElementById('target_sell_price').value = '';
            document.getElementById('buy_percent').value = '';
            document.getElementById('sell_percent').value = '';
        }

        async function removePair(pairId) {
            await fetch(`/api/pairs/${pairId}`, { method: 'DELETE' });
        }

        connectWebSocket();
    </script>
</body>
</html>