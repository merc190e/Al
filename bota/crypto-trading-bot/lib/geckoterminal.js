import axios from 'axios';

const NETWORK_MAP = {
  robinhood: 'arbitrum',
  ethereum: 'eth',
  bnb: 'bsc',
  solana: 'solana'
};

export async function getTokenDetails(chain, contractAddress) {
  const network = NETWORK_MAP[chain.toLowerCase()] || 'arbitrum';
  
  try {
    const tokenRes = await axios.get(
      `https://api.geckoterminal.com/api/v2/networks/${network}/tokens/${contractAddress}`
    );
    const attr = tokenRes.data.data.attributes;

    const poolRes = await axios.get(
      `https://api.geckoterminal.com/api/v2/networks/${network}/tokens/${contractAddress}/top_pools`
    );
    const topPool = poolRes.data.data[0];

    const ohlcvRes = await axios.get(
      `https://api.geckoterminal.com/api/v2/networks/${network}/pools/${topPool.id}/ohlcv/minute?aggregate=15&limit=96`
    );

    return {
      name: attr.name,
      symbol: attr.symbol,
      priceUsd: parseFloat(attr.price_usd),
      logoUrl: attr.image_url,
      websites: attr.websites || [],
      twitter: attr.twitter_handle ? `https://twitter.com/${attr.twitter_handle}` : null,
      dexPair: topPool.attributes.name,
      chart24h: ohlcvRes.data.data.attributes.ohlcv_list.map(c => ({
        timestamp: c[0] * 1000,
        open: c[1], high: c[2], low: c[3], close: c[4], volume: c[5]
      }))
    };
  } catch (err) {
    console.error('Błąd pobierania danych z GeckoTerminal:', err);
    throw err;
  }
}