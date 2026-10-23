'use client';

import { useState } from 'react';
import { getTokenDetails } from '@/lib/geckoterminal';
import DashboardCharts from '@/components/DashboardCharts';

export default function Home() {
  const [chain, setChain] = useState('robinhood');
  const [contract, setContract] = useState('');
  const [tokenData, setTokenData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [orderType, setOrderType] = useState('BUY');
  const [targetPrice, setTargetPrice] = useState('');
  const [amount, setAmount] = useState('');
  const [botStatus, setBotStatus] = useState('Bezczynek');

  const handleFetchToken = async () => {
    if (!contract) return;
    setLoading(true);
    try {
      const data = await getTokenDetails(chain, contract);
      setTokenData(data);
    } catch (e) {
      alert('Nie udało się pobrać tokena. Sprawdź adres kontraktu i wybraną sieć.');
    } finally {
      setLoading(false);
    }
  };

  const handleStartBot = () => {
    if (!targetPrice || !amount) {
      alert('Wpisz cenę docelową oraz kwotę do handlu!');
      return;
    }
    setBotStatus(`Aktywny [${orderType}]: Czekam na cenę $${targetPrice}`);
  };

  return (
    <main className="min-h-screen bg-black text-white p-6 max-w-7xl mx-auto space-y-6">
      <header className="flex justify-between items-center border-b border-gray-800 pb-4">
        <h1 className="text-2xl font-bold text-green-500">Crypto Trading Bot</h1>
        <w3m-button />
      </header>

      {/* Wybór sieci i wpisywanie kontraktu */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 bg-gray-900 p-4 rounded-xl border border-gray-800">
        <div>
          <label className="block text-sm text-gray-400 mb-1">Sieć (Domyślnie Robinhood)</label>
          <select 
            value={chain} 
            onChange={(e) => setChain(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 p-2 rounded text-white"
          >
            <option value="robinhood">Robinhood (Arbitrum Chain)</option>
            <option value="ethereum">Ethereum (ETH)</option>
            <option value="bnb">BNB Chain</option>
            <option value="solana">Solana</option>
          </select>
        </div>

        <div className="md:col-span-2">
          <label className="block text-sm text-gray-400 mb-1">Adres Kontraktu Tokena</label>
          <div className="flex gap-2">
            <input 
              type="text" 
              placeholder="0x... lub Adres Solana" 
              value={contract}
              onChange={(e) => setContract(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 p-2 rounded text-white"
            />
            <button 
              onClick={handleFetchToken}
              disabled={loading}
              className="bg-green-600 hover:bg-green-500 px-6 py-2 rounded font-bold"
            >
              {loading ? 'Pobieranie...' : 'Pobierz'}
            </button>
          </div>
        </div>
      </div>

      {/* Podgląd Tokena & Wykres */}
      {tokenData && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-gray-900 p-4 rounded-xl border border-gray-800 space-y-4">
            <div className="flex items-center gap-4">
              {tokenData.logoUrl && <img src={tokenData.logoUrl} alt="Logo" className="w-12 h-12 rounded-full" />}
              <div>
                <h2 className="text-xl font-bold">{tokenData.name} ({tokenData.symbol})</h2>
                <p className="text-green-400 text-lg font-mono">${tokenData.priceUsd}</p>
              </div>
            </div>
            
            <div className="text-sm text-gray-400 space-y-1">
              <p>Para DEX: <span className="text-white">{tokenData.dexPair}</span></p>
              {tokenData.twitter && (
                <p>Twitter: <a href={tokenData.twitter} target="_blank" className="text-blue-400 underline">Profil</a></p>
              )}
            </div>

            {/* Panel Wykonywania Zleceń */}
            <div className="border-t border-gray-800 pt-4 space-y-3">
              <h3 className="font-bold text-gray-200">Ustawienia Automatycznego Handlu</h3>
              <div className="flex gap-2">
                <button 
                  onClick={() => setOrderType('BUY')} 
                  className={`flex-1 py-1 rounded font-bold ${orderType === 'BUY' ? 'bg-green-600' : 'bg-gray-800'}`}
                >
                  Kupno
                </button>
                <button 
                  onClick={() => setOrderType('SELL')} 
                  className={`flex-1 py-1 rounded font-bold ${orderType === 'SELL' ? 'bg-red-600' : 'bg-gray-800'}`}
                >
                  Sprzedaż
                </button>
              </div>

              <input 
                type="number" 
                placeholder="Cena Docelowa (USD)" 
                value={targetPrice} 
                onChange={(e) => setTargetPrice(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 p-2 rounded text-sm text-white"
              />

              <input 
                type="number" 
                placeholder="Kwota (USD / ETH / SOL)" 
                value={amount} 
                onChange={(e) => setAmount(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 p-2 rounded text-sm text-white"
              />

              <button 
                onClick={handleStartBot}
                className="w-full bg-blue-600 hover:bg-blue-500 py-2 rounded font-bold"
              >
                Uruchom Bota
              </button>

              <p className="text-xs text-yellow-400 font-mono">Status: {botStatus}</p>
            </div>
          </div>

          <div className="md:col-span-2">
            <DashboardCharts chartData={tokenData.chart24h} />
          </div>
        </div>
      )}
    </main>
  );
}