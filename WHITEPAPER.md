# PumpChicken ($Chicken) — Whitepaper

**Version 1.0 · Robinhood Chain (chainId 4663)** · [Wersja polska](./WHITEPAPER.pl.md)

> Informational document only, not investment advice. Crypto assets are risky and you can lose your entire capital. The contract has not yet been audited by an independent firm — an audit is strongly recommended before mainnet deployment.

---

## 1. Vision

PumpChicken is the **Chicken** token plus a "mother" contract (launchpad). The mother contract has a built-in ETH/token DEX and lets users create further meme tokens that are born together with a liquidity pool. Everything runs on Robinhood Chain.

Goals:
- a fair, transparent token launch with rules written in code,
- one contract that handles trading, liquidity and creation of new tokens,
- buyer protection: no pause after launch, upgrades only after a delay.

## 2. The Chicken token

| Parameter | Value |
|---|---|
| Name / symbol | PumpChicken / **Chicken** |
| Standard | ERC-20 |
| Initial supply | **1,000,000 Chicken** (minted to the creator at deployment) |
| Network | Robinhood Chain, chainId 4663 |
| Tax | **1% on buys and 1% on sells** |
| Minting | Optional, capped (see section 5) |

Metadata (logo, website, Twitter, Telegram, description) is stored in the contract and exposed via API so aggregators and the explorer can read it right after deployment. The logo can also be hosted on IPFS.

## 3. Trading and `startTrading()`

1. **Pre-launch phase.** Trading is paused for the public. Only the owner, the mother contract and wallets on the early-access list (the creator's HD wallets **#1–#4000**) can move tokens.
2. **`startTrading()`.** A one-time owner function that opens trading to everyone.
3. **After launch.** There is no pause function and no way to lock trading again. The launch is **irreversible**.

Note for participants: before launch, listed wallets can trade and everyone else cannot. This rule is explicit and enforced in code.

## 4. DEX and tax

- The built-in DEX uses the constant-product formula **x · y = k**.
- Price = ETH in pool / tokens in pool.
- Every buy and sell carries a **1% tax**.
- **HD wallets #1–#4000 are tax-exempt.** The list is managed by the owner.

### Liquidity and starting price
The creator chooses the pool ratio (e.g. 80% tokens and a share in ETH) and the starting price. Example: 800,000 Chicken at 0.001 ETH requires 800 ETH; with 10 ETH in the pool, 10,000 Chicken go in and the rest stays with the creator.

## 5. Capped minting

- The owner sets a **supply cap** (`setMintCap`) and may top up supply to that cap (`mint`).
- `renounceMinting` **permanently disables** minting and freezes supply.
- Minting applies only to the main Chicken token.

## 6. Price booster

A DEX price only changes when pool reserves change. The booster is a set of panel-bot mechanisms:

1. **Buyback & burn** — periodically buying tokens from the pool and burning them (fewer tokens in the pool → higher price).
2. **One-sided ETH injection** — adding ETH to the pool without tokens.

The booster can be scheduled over time and in direction (raise or lower the price). It **does not guarantee a price increase** — results depend on the market and reserve size.

## 7. Mother contract for meme tokens

Users create their own tokens through the PumpChicken site. Each new token:
- is created with its own liquidity pool on the mother DEX,
- uses the same 1% tax model, except for exempt wallets,
- carries metadata (logo, links, description).

Early access and minting apply only to the main Chicken token.

## 8. Security and upgrades

- **Timelocked proxy:** the contract is upgradeable, but every upgrade must be proposed and wait **48 hours** (`proposeUpgrade` → `executeUpgrade`), with the option to cancel. Users have time to react.
- **`freezeUpgrades`:** permanently disables upgrades.
- **No pause:** after `startTrading()` trading cannot be stopped.
- **Multisig:** moving proxy administration (`changeProxyAdmin`) and ownership (`transferOwnership`) to a multisig wallet is recommended.
- **Verification:** the source code is verifiable on the Robinhood Chain explorer.
- **Audit:** get an independent audit before mainnet.

## 9. Admin panel

The `/chicken` panel covers: deployment (implementation + proxy), metadata and IPFS, liquidity, booster, minting, exempt-wallet list, contract upgrades, verification, Uniswap Assets export, HD wallets #1–#4000, swap series (buy / sell by % of balance or amount), and wallet connection via MetaMask or QR (WalletConnect).

## 10. Roadmap

| Stage | Scope |
|---|---|
| 1 | Contract deployment, verification, metadata |
| 2 | Liquidity, `startTrading()`, initial pool funding |
| 3 | User site for creating meme tokens |
| 4 | Listings and aggregators (CoinMarketCap, CoinGecko, Uniswap token list) |
| 5 | External audit, administration moved to multisig |

## 11. Risks

- Smart contract bugs, even after an audit.
- Low liquidity and high price volatility.
- Owner privileges (exempt list, minting up to the cap, upgrades after 48 h) require trust in the creator until limited by `renounceMinting`, `freezeUpgrades` and a multisig.
- Regulatory changes.

## 12. Contact and links

Fill in after deployment: website, Twitter/X, Telegram, launchpad address, token address, explorer link.
