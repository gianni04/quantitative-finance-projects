"""amm.py — Constant-product market-microstructure model for the HLD/ETH pool."""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np


@dataclass
class Pool:
    x: float = 0.003          # ETH reserve, from data/pool_state.json (block #49023454)
    y: float = 65_992_981.0   # HLD reserve
    fee: float = 0.01         # 1%, verified on-chain (lpFee = 10000 pips)

    @property
    def k(self) -> float:
        return self.x * self.y

    @property
    def spot_hld_per_eth(self) -> float:
        return self.y / self.x

    @property
    def spot_eth_per_hld(self) -> float:
        return self.x / self.y

    def buy_hld(self, dx_eth: float) -> dict:
        """Returns execution metrics for sending dx_eth ETH into the pool."""
        dx = dx_eth * (1 - self.fee)
        x_new = self.x + dx
        y_new = self.k / x_new
        hld_out = self.y - y_new
        eff_eth_per_hld = dx_eth / hld_out
        slippage = eff_eth_per_hld / self.spot_eth_per_hld - 1
        return {
            "eth_in": dx_eth,
            "hld_out": hld_out,
            "effective_eth_per_hld": eff_eth_per_hld,
            "slippage": slippage,
            "new_price_hld_per_eth": y_new / x_new,
        }

    def eth_to_move_price(self, pct_up: float) -> float:
        """Returns the gross ETH input required to push HLD price up by pct_up."""
        r = 1 + pct_up
        x_new = np.sqrt(self.k * (self.x / self.y) * r)
        return (x_new - self.x) / (1 - self.fee)


def impermanent_loss(price_ratio):
    """Returns IL vs HODL for a 50/50 CPMM position (price_ratio = P_final / P_initial)."""
    r = np.asarray(price_ratio, dtype=float)
    return 2 * np.sqrt(r) / (1 + r) - 1


if __name__ == "__main__":
    p = Pool()
    print("spot: 1 ETH =", f"{p.spot_hld_per_eth:,.0f}", "HLD")
    for eur in (1, 5, 10):
        m = p.buy_hld(eur / 1645.88)
        print(f"buy {eur:>2}EUR -> slippage {m['slippage']*100:6.1f}%  "
              f"HLD_out {m['hld_out']:,.0f}")
    print("IL at r=2:", f"{impermanent_loss(2)*100:.2f}%")
