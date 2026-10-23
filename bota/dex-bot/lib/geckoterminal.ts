const NETWORKS: Record<string, string> = {
  robinhood: "arbitrum",
  ethereum: "eth",
  bnb: "bsc",
  solana: "solana",
};

export type OHLCVCandle = {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type TokenDetails = {
  name: string;
  symbol: string;
  priceUsd: number;
  logoUrl: string | null;
  twitter: string | null;
  dexPair: string;
  poolAddress: string;
  pools: Array<{ address: string; name: string }>;
  chart24h: OHLCVCandle[];
};

function networkSlug(chain: string): string {
  const network = NETWORKS[chain.toLowerCase()];
  if (!network) throw new Error("Wybrana sieć nie jest obsługiwana.");
  return network;
}

async function geckoJson(url: string): Promise<Record<string, unknown>> {
  const response = await fetch(url, {
    headers: { Accept: "application/json;version=20230302" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) {
    throw new Error(`GeckoTerminal zwrócił HTTP ${response.status}.`);
  }
  return response.json() as Promise<Record<string, unknown>>;
}

function tokenPoolPrice(
  attributes: Record<string, unknown>,
  relationships: Record<string, unknown>,
  tokenAddress: string,
): number | null {
  const relatedAddress = (key: string) => {
    const relation = relationships[key] as
      | { data?: { id?: string } }
      | undefined;
    return relation?.data?.id?.split("_").at(-1)?.toLowerCase();
  };
  const normalizedAddress = tokenAddress.toLowerCase();
  const priceKey =
    relatedAddress("base_token") === normalizedAddress
      ? "base_token_price_usd"
      : relatedAddress("quote_token") === normalizedAddress
        ? "quote_token_price_usd"
        : "token_price_usd";
  const price = Number(attributes[priceKey]);
  return Number.isFinite(price) && price > 0 ? price : null;
}

export async function getTokenPrice(
  chain: string,
  contractAddress: string,
  poolAddress: string,
): Promise<number> {
  const network = networkSlug(chain);
  const url = new URL(
    `https://api.geckoterminal.com/api/v2/networks/${encodeURIComponent(network)}/pools/${encodeURIComponent(poolAddress)}`,
  );
  const payload = await geckoJson(url.toString());
  const data = payload.data as
    | {
        attributes?: Record<string, unknown>;
        relationships?: Record<string, unknown>;
      }
    | undefined;
  const price = tokenPoolPrice(
    data?.attributes ?? {},
    data?.relationships ?? {},
    contractAddress,
  );
  if (price === null) {
    throw new Error("Nie udało się odczytać ceny tokena z wybranej puli.");
  }
  return price;
}

export async function getTokenDetails(
  chain: string,
  contractAddress: string,
  selectedPoolAddress?: string,
): Promise<TokenDetails> {
  const network = networkSlug(chain);
  const token = encodeURIComponent(contractAddress.trim());
  const baseUrl = `https://api.geckoterminal.com/api/v2/networks/${encodeURIComponent(network)}`;
  const [tokenPayload, poolsPayload, infoPayload] = await Promise.all([
    geckoJson(`${baseUrl}/tokens/${token}`),
    geckoJson(`${baseUrl}/tokens/${token}/pools`),
    geckoJson(`${baseUrl}/tokens/${token}/info`),
  ]);
  const tokenAttributes = (
    tokenPayload.data as { attributes?: Record<string, unknown> } | undefined
  )?.attributes;
  const poolData = poolsPayload.data;
  if (!tokenAttributes || !Array.isArray(poolData) || poolData.length === 0) {
    throw new Error("Nie znaleziono tokena ani jego puli na wybranej sieci.");
  }

  const pools = poolData.flatMap((pool) => {
    if (!pool || typeof pool !== "object") return [];
    const item = pool as {
      id?: string;
      attributes?: Record<string, unknown>;
    };
    const address = item.id?.split("_").at(-1);
    if (!address) return [];
    return [{
      address,
      name: String(item.attributes?.name ?? "Nieznana para DEX"),
    }];
  });
  const selectedPool =
    pools.find((pool) => pool.address === selectedPoolAddress) ?? pools[0];
  if (!selectedPool) throw new Error("GeckoTerminal nie zwrócił adresu puli.");
  const selectedPoolData = poolData.find((pool) => {
    if (!pool || typeof pool !== "object") return false;
    return (
      (pool as { id?: string }).id?.split("_").at(-1) === selectedPool.address
    );
  }) as
    | {
        attributes?: Record<string, unknown>;
        relationships?: Record<string, unknown>;
      }
    | undefined;
  if (!selectedPoolData) {
    throw new Error("GeckoTerminal nie zwrócił danych wybranej puli.");
  }
  const poolAddress = selectedPool.address;

  const [poolPayload, chartPayload, info] = await Promise.all([
    geckoJson(`${baseUrl}/pools/${encodeURIComponent(poolAddress)}`),
    geckoJson(
      `${baseUrl}/pools/${encodeURIComponent(poolAddress)}/ohlcv/minute?aggregate=15&limit=96`,
    ),
    Promise.resolve(
      (infoPayload.data as { attributes?: Record<string, unknown> } | undefined)
        ?.attributes ?? {},
    ),
  ]);
  const pool = poolPayload.data as
    | { attributes?: Record<string, unknown>; relationships?: Record<string, unknown> }
    | undefined;
  const priceUsd = tokenPoolPrice(
    pool?.attributes ?? selectedPoolData.attributes ?? {},
    pool?.relationships ?? selectedPoolData.relationships ?? {},
    contractAddress,
  );
  if (priceUsd === null) throw new Error("Nie udało się odczytać ceny tokena.");

  const ohlcv = (
    chartPayload.data as {
      attributes?: { ohlcv_list?: unknown };
    } | undefined
  )?.attributes?.ohlcv_list;
  if (!Array.isArray(ohlcv)) throw new Error("GeckoTerminal nie zwrócił danych wykresu.");
  const chart24h = ohlcv.map((row): OHLCVCandle => {
    if (!Array.isArray(row) || row.length !== 6) {
      throw new Error("GeckoTerminal zwrócił nieprawidłowe dane świecy.");
    }
    const values = row.map(Number);
    if (values.some((value) => !Number.isFinite(value))) {
      throw new Error("GeckoTerminal zwrócił nienumeryczne dane świecy.");
    }
    const [timestamp, open, high, low, close, volume] = values;
    return { timestamp, open, high, low, close, volume };
  }).sort((left, right) => left.timestamp - right.timestamp);

  const twitterHandle = info.twitter_handle;
  const twitter =
    typeof twitterHandle === "string" && /^[a-zA-Z0-9_]+$/.test(twitterHandle.replace(/^@/, ""))
      ? `https://x.com/${twitterHandle.replace(/^@/, "")}`
      : null;
  return {
    name: String(tokenAttributes.name ?? "Nieznany token"),
    symbol: String(tokenAttributes.symbol ?? "TOKEN"),
    priceUsd,
    logoUrl: typeof tokenAttributes.image_url === "string" ? tokenAttributes.image_url : null,
    twitter,
    dexPair: String(pool?.attributes?.name ?? selectedPoolData.attributes?.name ?? selectedPool.name),
    poolAddress,
    pools,
    chart24h,
  };
}
