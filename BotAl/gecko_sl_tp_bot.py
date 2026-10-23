import math
from typing import Optional
from urllib.parse import quote

import requests


class GeckoSLTPBot:
    def __init__(
        self,
        network: str,
        pool_address: str,
        entry_price: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        token_address: Optional[str] = None,
    ):
        self.network = network.strip().lower()
        self.pool_address = pool_address.strip()
        self.token_address = token_address.strip().lower() if token_address else None
        if not self.network or not self.pool_address or any(
            char in self.network + self.pool_address for char in "/?#"
        ):
            raise ValueError("Nieprawidłowa sieć lub adres puli.")
        if self.token_address and any(char in self.token_address for char in "/?#"):
            raise ValueError("Nieprawidłowy adres tokena.")
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("Cena wejścia musi być dodatnia.")
        if not math.isfinite(stop_loss_pct) or not 0 < stop_loss_pct < 100:
            raise ValueError("Stop-Loss musi być większy od 0% i mniejszy od 100%.")
        if not math.isfinite(take_profit_pct) or take_profit_pct <= 0:
            raise ValueError("Take-Profit musi być większy od 0%.")

        self.entry_price = entry_price
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.stop_loss_price = entry_price * (1.0 - stop_loss_pct / 100.0)
        self.take_profit_price = entry_price * (1.0 + take_profit_pct / 100.0)
        self.api_url = (
            "https://api.geckoterminal.com/api/v2/networks/"
            f"{quote(self.network, safe='')}/pools/{quote(self.pool_address, safe='')}"
        )

    def get_current_price(self) -> Optional[float]:
        """Fetch the position token's USD price from its GeckoTerminal pool."""
        response = requests.get(
            self.api_url,
            headers={"Accept": "application/json;version=20230203"},
            timeout=10,
        )
        response.raise_for_status()
        pool = response.json()["data"]
        attributes = pool["attributes"]

        price_key = "base_token_price_usd"
        if self.token_address:
            relationships = pool.get("relationships", {})
            base_id = relationships.get("base_token", {}).get("data", {}).get("id", "")
            quote_id = relationships.get("quote_token", {}).get("data", {}).get("id", "")
            base_address = base_id.split("_")[-1].lower()
            quote_address = quote_id.split("_")[-1].lower()
            if quote_address == self.token_address:
                price_key = "quote_token_price_usd"
            elif base_address != self.token_address:
                raise ValueError("Wybrany token nie należy do wskazanej puli.")

        price_value = attributes.get(price_key)
        if price_value is None:
            return None
        try:
            price = float(price_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("GeckoTerminal zwrócił nieprawidłową cenę tokena.") from exc
        return price if math.isfinite(price) and price > 0 else None

    def get_trigger_reason(self, current_price: float) -> Optional[str]:
        if not math.isfinite(current_price) or current_price <= 0:
            raise ValueError("Aktualna cena musi być dodatnia.")
        pnl_pct = (current_price - self.entry_price) / self.entry_price * 100.0
        if pnl_pct <= -self.stop_loss_pct:
            return "STOP-LOSS"
        if pnl_pct >= self.take_profit_pct:
            return "TAKE-PROFIT"
        return None
