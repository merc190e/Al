"use client";

import { useEffect, useState } from "react";
import DashboardCharts from "@/components/DashboardCharts";
import {
  getTokenDetails,
  getTokenPrice,
  type TokenDetails,
} from "@/lib/geckoterminal";

const CHAINS = [
  { id: "robinhood", label: "Robinhood (Arbitrum)" },
  { id: "ethereum", label: "Ethereum" },
  { id: "bnb", label: "BNB Chain" },
  { id: "solana", label: "Solana" },
];

type MonitorOrder = {
  chain: string;
  contract: string;
  poolAddress: string;
  type: "BUY" | "SELL";
  targetPrice: number;
  amount: number;
};

type InjectedWallet = {
  request: (args: { method: string; params?: unknown[] }) => Promise<unknown>;
};

function formatPrice(price: number): string {
  return price.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumSignificantDigits: 8,
  });
}

export default function Home() {
  const [chain, setChain] = useState("robinhood");
  const [contract, setContract] = useState("");
  const [tokenData, setTokenData] = useState<TokenDetails | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [orderType, setOrderType] = useState<"BUY" | "SELL">("BUY");
  const [targetPrice, setTargetPrice] = useState("");
  const [amount, setAmount] = useState("");
  const [monitorOrder, setMonitorOrder] = useState<MonitorOrder | null>(null);
  const [botStatus, setBotStatus] = useState("Bezczynny");
  const [currentPrice, setCurrentPrice] = useState<number | null>(null);
  const [wallet, setWallet] = useState("");
  const [walletBalance, setWalletBalance] = useState("");

  const handleFetchToken = async () => {
    const normalizedContract = contract.trim();
    if (!normalizedContract) {
      setError("Wpisz adres kontraktu tokena.");
      return;
    }

    setLoading(true);
    setError("");
    setTokenData(null);
    setCurrentPrice(null);
    try {
      const details = await getTokenDetails(chain, normalizedContract);
      setTokenData(details);
      setCurrentPrice(details.priceUsd);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Nie udało się pobrać danych tokena.",
      );
    } finally {
      setLoading(false);
    }
  };

  const handleConnectWallet = async () => {
    const ethereum = (window as Window & { ethereum?: InjectedWallet }).ethereum;
    if (!ethereum) {
      setError("Nie znaleziono portfela EVM w przeglądarce.");
      return;
    }
    setError("");
    try {
      const accounts = (await ethereum.request({
        method: "eth_requestAccounts",
      })) as string[];
      const address = accounts[0];
      if (!address) throw new Error("Portfel nie zwrócił adresu konta.");
      const [balanceHex, chainId] = (await Promise.all([
        ethereum.request({
          method: "eth_getBalance",
          params: [address, "latest"],
        }),
        ethereum.request({ method: "eth_chainId" }),
      ])) as [string, string];
      const balance = Number(BigInt(balanceHex)) / 1e18;
      const nativeSymbol: Record<string, string> = {
        "0x1": "ETH",
        "0x38": "BNB",
        "0xa4b1": "ETH",
      };
      setWallet(address);
      setWalletBalance(
        `${balance.toFixed(4)} ${nativeSymbol[chainId.toLowerCase()] ?? "native"}`,
      );
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Nie udało się połączyć z portfelem.",
      );
    }
  };

  const handleStartBot = () => {
    const target = Number(targetPrice);
    const tradeAmount = Number(amount);
    if (!tokenData) {
      setError("Najpierw pobierz dane tokena.");
      return;
    }
    if (!Number.isFinite(target) || target <= 0) {
      setError("Cena docelowa musi być większa od zera.");
      return;
    }
    if (!Number.isFinite(tradeAmount) || tradeAmount <= 0) {
      setError("Kwota obserwowana musi być większa od zera.");
      return;
    }

    setError("");
    setMonitorOrder({
      chain,
      contract: contract.trim(),
      poolAddress: tokenData.poolAddress,
      type: orderType,
      targetPrice: target,
      amount: tradeAmount,
    });
    setBotStatus(`Aktywny [${orderType}]: oczekiwanie na $${formatPrice(target)}`);
  };

  useEffect(() => {
    if (!monitorOrder) return;

    let stopped = false;
    let checking = false;
    const checkPrice = async () => {
      if (checking || stopped) return;
      checking = true;
      try {
        const price = await getTokenPrice(
          monitorOrder.chain,
          monitorOrder.contract,
          monitorOrder.poolAddress,
        );
        if (stopped) return;
        setCurrentPrice(price);
        const triggered =
          monitorOrder.type === "BUY"
            ? price <= monitorOrder.targetPrice
            : price >= monitorOrder.targetPrice;
        if (triggered) {
          setBotStatus(
            `ALERT: ${monitorOrder.type} osiągnął $${formatPrice(price)}. ` +
              `Kwota ${monitorOrder.amount} — transakcja nie została wysłana.`,
          );
          setMonitorOrder(null);
          return;
        }
        setBotStatus(
          `Aktywny [${monitorOrder.type}]: ${formatPrice(price)} USD · ` +
            `cel $${formatPrice(monitorOrder.targetPrice)}`,
        );
      } catch (cause) {
        if (!stopped) {
          setBotStatus(
            `Błąd odczytu ceny; ponawiam za 5 s: ${
              cause instanceof Error ? cause.message : "nieznany błąd"
            }`,
          );
        }
      } finally {
        checking = false;
      }
    };

    void checkPrice();
    const timer = window.setInterval(() => void checkPrice(), 5_000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [monitorOrder]);

  return (
    <main className="mx-auto min-h-screen w-full max-w-7xl space-y-6 px-4 py-8 text-slate-100 sm:px-6 lg:px-8">
      <header className="flex flex-col justify-between gap-4 border-b border-white/10 pb-5 sm:flex-row sm:items-center">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.24em] text-emerald-400">
            GeckoTerminal · Multi-chain
          </p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-white">
            Crypto Trading Bot
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            Dane rynkowe, wykres świecowy i alerty cenowe
          </p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <button
            type="button"
            onClick={handleConnectWallet}
            className="rounded-lg border border-amber-400/30 bg-amber-400/10 px-4 py-2 text-sm font-semibold text-amber-200 transition hover:bg-amber-400/20"
          >
            {wallet ? `${wallet.slice(0, 6)}…${wallet.slice(-4)}` : "Połącz portfel EVM"}
          </button>
          {walletBalance && (
            <span className="text-xs font-mono text-emerald-300">
              {walletBalance}
            </span>
          )}
        </div>
      </header>

      <section className="grid gap-4 rounded-2xl border border-white/10 bg-slate-900/80 p-4 md:grid-cols-3">
        <label className="text-sm text-slate-300">
          Sieć
          <select
            value={chain}
            disabled={Boolean(monitorOrder)}
            onChange={(event) => {
              setChain(event.target.value);
              setTokenData(null);
              setCurrentPrice(null);
            }}
            className="mt-1.5 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-white outline-none focus:border-emerald-400 disabled:opacity-60"
          >
            {CHAINS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-300 md:col-span-2">
          Adres kontraktu tokena
          <span className="mt-1.5 flex gap-2">
            <input
              value={contract}
              disabled={Boolean(monitorOrder)}
              onChange={(event) => setContract(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void handleFetchToken();
              }}
              placeholder="0x… lub adres tokena Solana"
              className="min-w-0 flex-1 rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 font-mono text-sm text-white outline-none placeholder:text-slate-600 focus:border-emerald-400 disabled:opacity-60"
            />
            <button
              type="button"
              onClick={() => void handleFetchToken()}
              disabled={loading || Boolean(monitorOrder)}
              className="rounded-lg bg-emerald-500 px-4 py-2.5 text-sm font-bold text-slate-950 transition hover:bg-emerald-400 disabled:cursor-wait disabled:opacity-50"
            >
              {loading ? "Pobieranie…" : "Pobierz"}
            </button>
          </span>
        </label>
      </section>

      {error && (
        <div
          role="alert"
          className="rounded-xl border border-rose-400/20 bg-rose-400/10 px-4 py-3 text-sm text-rose-200"
        >
          {error}
        </div>
      )}

      {tokenData && (
        <section className="grid gap-6 lg:grid-cols-[minmax(280px,0.85fr)_minmax(0,2fr)]">
          <div className="space-y-5 rounded-2xl border border-white/10 bg-slate-900/80 p-5">
            <div className="flex items-center gap-4">
              {tokenData.logoUrl ? (
                // GeckoTerminal supplies token logos from external domains.
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={tokenData.logoUrl}
                  alt=""
                  className="h-12 w-12 rounded-full bg-slate-800 object-cover"
                />
              ) : (
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-400/10 font-bold text-emerald-300">
                  {tokenData.symbol.slice(0, 2)}
                </div>
              )}
              <div className="min-w-0">
                <h2 className="truncate text-xl font-bold text-white">
                  {tokenData.name}{" "}
                  <span className="text-slate-400">({tokenData.symbol})</span>
                </h2>
                <p className="font-mono text-lg text-emerald-300">
                  ${formatPrice(currentPrice ?? tokenData.priceUsd)}
                </p>
              </div>
            </div>

            <div className="space-y-2 border-t border-white/10 pt-4 text-sm">
              <p className="text-slate-400">
                Para DEX:{" "}
                <span className="text-slate-200">{tokenData.dexPair}</span>
              </p>
              <label className="block text-slate-400">
                Pula cenowa
                <select
                  value={tokenData.poolAddress}
                  disabled={Boolean(monitorOrder)}
                  onChange={async (event) => {
                    const selectedPool = tokenData.pools.find(
                      (pool) => pool.address === event.target.value,
                    );
                    if (!selectedPool) return;
                    setLoading(true);
                    setError("");
                    try {
                      const updated = await getTokenDetails(chain, contract.trim());
                      if (updated.poolAddress === selectedPool.address) {
                        setTokenData(updated);
                        setCurrentPrice(updated.priceUsd);
                        return;
                      }
                      const price = await getTokenPrice(
                        chain,
                        contract.trim(),
                        selectedPool.address,
                      );
                      setTokenData({
                        ...updated,
                        poolAddress: selectedPool.address,
                        dexPair: selectedPool.name,
                        priceUsd: price,
                      });
                      setCurrentPrice(price);
                    } catch (cause) {
                      setError(
                        cause instanceof Error
                          ? cause.message
                          : "Nie udało się pobrać danych wybranej puli.",
                      );
                    } finally {
                      setLoading(false);
                    }
                  }}
                  className="mt-1 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-sm text-white disabled:opacity-60"
                >
                  {tokenData.pools.map((pool) => (
                    <option key={pool.address} value={pool.address}>
                      {pool.name}
                    </option>
                  ))}
                </select>
              </label>
              {tokenData.twitter && (
                <a
                  href={tokenData.twitter}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-block text-sky-300 underline decoration-sky-300/30 underline-offset-4 hover:text-sky-200"
                >
                  Profil X / Twitter
                </a>
              )}
            </div>

            <div className="space-y-3 border-t border-white/10 pt-4">
              <h3 className="font-semibold text-white">Alert cenowy</h3>
              <p className="text-xs leading-5 text-amber-200/80">
                Monitoring sprawdza cenę co 5 sekund. Po osiągnięciu progu
                wyświetli alert — nie wysyła transakcji ani nie handluje
                automatycznie.
              </p>
              <div className="flex gap-2">
                {(["BUY", "SELL"] as const).map((type) => (
                  <button
                    key={type}
                    type="button"
                    disabled={Boolean(monitorOrder)}
                    onClick={() => setOrderType(type)}
                    className={`flex-1 rounded-lg py-2 text-sm font-bold transition disabled:opacity-50 ${
                      orderType === type
                        ? type === "BUY"
                          ? "bg-emerald-500 text-slate-950"
                          : "bg-rose-500 text-white"
                        : "bg-slate-800 text-slate-400 hover:bg-slate-700"
                    }`}
                  >
                    {type === "BUY" ? "Kupno" : "Sprzedaż"}
                  </button>
                ))}
              </div>
              <label className="block text-xs text-slate-400">
                Cena docelowa (USD)
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={targetPrice}
                  onChange={(event) => setTargetPrice(event.target.value)}
                  placeholder={formatPrice(tokenData.priceUsd)}
                  disabled={Boolean(monitorOrder)}
                  className="mt-1 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-sm text-white disabled:opacity-60"
                />
              </label>
              <label className="block text-xs text-slate-400">
                Obserwowana kwota (informacyjnie; bez transakcji)
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                  placeholder="np. 0.01"
                  disabled={Boolean(monitorOrder)}
                  className="mt-1 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-sm text-white disabled:opacity-60"
                />
              </label>
              {monitorOrder ? (
                <button
                  type="button"
                  onClick={() => {
                    setMonitorOrder(null);
                    setBotStatus("Monitoring zatrzymany");
                  }}
                  className="w-full rounded-lg bg-rose-600 px-4 py-2.5 text-sm font-bold text-white transition hover:bg-rose-500"
                >
                  Zatrzymaj monitoring
                </button>
              ) : (
                <button
                  type="button"
                  onClick={handleStartBot}
                  className="w-full rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-bold text-white transition hover:bg-blue-500"
                >
                  Uruchom monitoring
                </button>
              )}
              <p role="status" className="break-words text-xs text-amber-300">
                Status: {botStatus}
              </p>
            </div>
          </div>

          <DashboardCharts chartData={tokenData.chart24h} />
        </section>
      )}
    </main>
  );
}
