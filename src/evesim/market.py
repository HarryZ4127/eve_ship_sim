"""Item prices for budget constraints.

Uses ESI's public ``/markets/prices/`` (CCP's average price, no login needed),
cached for a day. When ESI is unreachable the static ``basePrice`` is used,
which is only a rough NPC reference.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from evesim.data.store import StaticData

PRICES_URL = os.environ.get("EVESIM_ESI_BASE", "https://esi.evetech.net/latest") + "/markets/prices/"
MAX_AGE_S = 24 * 3600


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "evesim" / "prices.json"


class Prices:
    def __init__(self, data: StaticData, prices: dict[int, float] | None = None, source: str = "basePrice"):
        self.data = data
        self.prices = prices or {}
        self.source = source

    def __call__(self, type_id: int) -> float:
        p = self.prices.get(type_id)
        if p:
            return p
        t = self.data.types.get(type_id)
        return t.base_price if t else 0.0

    @classmethod
    def load(cls, data: StaticData, refresh: bool = False, offline: bool = False) -> "Prices":
        p = cache_path()
        if p.exists() and not refresh:
            d = json.loads(p.read_text())
            if offline or time.time() - d.get("fetched", 0) < MAX_AGE_S:
                return cls(data, {int(k): v for k, v in d["prices"].items()}, "esi-cache")
        if offline:
            return cls(data)
        try:
            req = urllib.request.Request(PRICES_URL, headers={"User-Agent": "evesim"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                rows = json.load(resp)
        except Exception:
            if p.exists():
                d = json.loads(p.read_text())
                return cls(data, {int(k): v for k, v in d["prices"].items()}, "esi-cache-stale")
            return cls(data)
        prices = {int(r["type_id"]): float(r.get("average_price") or r.get("adjusted_price") or 0) for r in rows}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"fetched": time.time(), "prices": prices}))
        return cls(data, prices, "esi")

    def fit_price(self, fit) -> float:
        total = self(fit.ship.id)
        for m in fit.modules:
            total += self(m.type.id)
        for d in fit.drones:
            total += self(d.type.id) * d.count
        return total
