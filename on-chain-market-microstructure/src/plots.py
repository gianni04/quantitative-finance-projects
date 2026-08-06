"""plots.py — Visualisations de la micro-structure du pool HLD/ETH (Uniswap V4).

Les données de pool (sqrtPriceX96, tick, lpFee, liquidité) sont réelles, lues
via extract_live.py. Les courbes de coût d'exécution sont des sorties de
modèle constant-product (x*y=k) appliqué à ces réserves, pas des transactions
observées.
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "pool_state.json"
OUT_DIR = ROOT / "figures"
OUT_DIR.mkdir(exist_ok=True)


def load_pool_state():
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def compute_virtual_reserves(L, sqrt_price):
    """Retourne les réserves virtuelles x (ETH) et y (HLD) en raw token amounts."""
    x = L / sqrt_price
    y = L * sqrt_price
    return x, y


def constant_product_slippage(x, y, trade_hld_amount, is_buy):
    """Retourne (prix_moyen_hld_per_eth, cost_bps) pour un trade modélisé en constant-product."""
    k = x * y
    if is_buy:
        dy = trade_hld_amount
        new_y = y - dy
        if new_y <= 0:
            return float("inf"), float("inf")
        new_x = k / new_y
        dx = new_x - x
        avg_price = dy / dx
        spot = y / x
    else:
        dy = trade_hld_amount
        new_y = y + dy
        new_x = k / new_y
        dx = x - new_x
        avg_price = dy / dx if dx > 0 else float("inf")
        spot = y / x

    cost_bps = abs(avg_price - spot) / spot * 10000 if spot > 0 and avg_price != float("inf") else float("inf")
    return avg_price, cost_bps


def plot_slippage_curve(state):
    """Courbe de coût d'exécution modélisé en fonction de la taille du trade."""
    L = state["liquidity"]
    Q96 = 2 ** 96
    sqrt_p = state["sqrtPriceX96"] / Q96

    x, y = compute_virtual_reserves(L, sqrt_p)
    spot_hld_per_eth = y / x

    max_trade = y * 0.9
    sizes_hld = np.logspace(1, np.log10(max_trade), 500)
    costs_bps = []
    for size in sizes_hld:
        _, cost = constant_product_slippage(x, y, size, is_buy=True)
        costs_bps.append(min(cost, 10000))

    sizes_m_hld = sizes_hld / 1e6

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(sizes_m_hld, costs_bps, linewidth=2, color="#2172E5")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Taille du trade (millions de HLD)")
    ax.set_ylabel("Coût d'exécution modélisé (bps)")
    ax.set_title("Coût d'exécution modélisé — Pool HLD/ETH (Uniswap V4, Base)")
    ax.grid(True, alpha=0.3, which="both")

    ax.axhline(100, color="orange", linestyle="--", alpha=0.5, label="1 % (lpFee)")
    ax.axhline(10000, color="red", linestyle="--", alpha=0.3, label="100 %")
    ax.legend()
    fig.tight_layout()
    out = OUT_DIR / "slippage_curve.png"
    fig.savefig(out, dpi=150)
    print(f"✓ Figure → {out}")
    plt.close(fig)


def plot_depth(state):
    """Profil de liquidité autour du prix spot."""
    L = state["liquidity"]
    Q96 = 2 ** 96
    sqrt_p_spot = state["sqrtPriceX96"] / Q96
    spot_hld_per_eth = sqrt_p_spot ** 2

    prices = np.linspace(spot_hld_per_eth * 0.80, spot_hld_per_eth * 1.20, 200)
    eth_amounts = []
    for p in prices:
        sp = np.sqrt(p)
        x_virt = L / sp
        eth_amounts.append(x_virt)

    eth_eur = state.get("eth_eur", 1645.88)
    eth_usd_per_eth = eth_eur / 0.92

    fig, ax1 = plt.subplots(figsize=(10, 5))
    color = "#2172E5"
    ax1.plot(prices / 1e9, np.array(eth_amounts) * eth_usd_per_eth, linewidth=2, color=color)
    ax1.axvline(spot_hld_per_eth / 1e9, color="red", linestyle="--", alpha=0.5,
                label=f"Spot = {spot_hld_per_eth/1e9:,.2f} Mrd HLD/ETH")
    ax1.set_xlabel("Prix (milliards de HLD par ETH)")
    ax1.set_ylabel("ETH virtuels (équivalent USD)", color=color)
    ax1.tick_params(axis="y", labelcolor=color)
    ax1.set_title("Profil de liquidité — Pool HLD/ETH (Uniswap V4, Base)")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    fig.tight_layout()
    out = OUT_DIR / "depth_profile.png"
    fig.savefig(out, dpi=150)
    print(f"✓ Figure → {out}")
    plt.close(fig)


def plot_summary(state):
    """Génère le dashboard récapitulatif."""
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    fig.suptitle(f"HLD/ETH Pool — Bloc #{state['block']} ({state['timestamp_utc'][:10]})",
                 fontsize=14, fontweight="bold")

    L = state["liquidity"]
    Q96 = 2 ** 96
    sqrt_p = state["sqrtPriceX96"] / Q96
    spot = sqrt_p ** 2
    x, y = compute_virtual_reserves(L, sqrt_p)
    eth_eur = state.get("eth_eur", 1645.88)

    ax = axes[0, 0]
    ax.axis("off")
    hld_price_eur = state["spot_price"]["eth_per_hld"] * eth_eur
    text = (
        f"1 ETH = {spot:,.0f} HLD\n"
        f"1 HLD = {state['spot_price']['eth_per_hld']:.4e} ETH\n"
        f"1 M HLD ≈ {hld_price_eur * 1e6:.2f} €\n"
        f"ETH/EUR = {eth_eur:.2f} €"
    )
    ax.text(0.5, 0.5, text, transform=ax.transAxes, fontsize=12,
            verticalalignment="center", horizontalalignment="center",
            bbox=dict(boxstyle="round", facecolor="#f0f4ff", alpha=0.8))

    ax = axes[0, 1]
    ax.axis("off")
    pool_value_eth = 2 * x
    text = (
        f"Liquidité L = {L:,.0f}\n"
        f"ETH virtuels = {x:,.6f}\n"
        f"HLD virtuels = {y:,.0f}\n"
        f"Valeur pool ≈ {pool_value_eth * eth_eur:,.0f} €"
    )
    ax.text(0.5, 0.5, text, transform=ax.transAxes, fontsize=12,
            verticalalignment="center", horizontalalignment="center",
            bbox=dict(boxstyle="round", facecolor="#fff8f0", alpha=0.8))

    ax = axes[1, 0]
    ax.axis("off")
    text = (
        f"lpFee = {state['lpFee']} pips ({state['lpFee']/10000:.1f}%)\n"
        f"protocolFee = {state['protocolFee']}\n"
        f"tick = {state['tick']}\n"
        f"tickSpacing = 200"
    )
    ax.text(0.5, 0.5, text, transform=ax.transAxes, fontsize=12,
            verticalalignment="center", horizontalalignment="center",
            bbox=dict(boxstyle="round", facecolor="#f0fff0", alpha=0.8))

    ax = axes[1, 1]
    sizes_hld = np.logspace(1, np.log10(y * 0.5), 200)
    costs = []
    for s in sizes_hld:
        _, c = constant_product_slippage(x, y, s, is_buy=True)
        costs.append(min(c, 1000))
    ax.plot(sizes_hld / 1e6, costs, linewidth=2)
    ax.set_xscale("log")
    ax.set_xlabel("Trade (M HLD)")
    ax.set_ylabel("Coût modélisé (bps)")
    ax.set_title("Coût d'exécution modélisé (zoom <1000 bps)")
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    out = OUT_DIR / "summary_dashboard.png"
    fig.savefig(out, dpi=150)
    print(f"✓ Figure → {out}")
    plt.close(fig)


def main():
    state = load_pool_state()
    sp = state["spot_price"]
    eth_eur = state.get("eth_eur", 1645.88)
    print(f"Pool HLD/ETH — Bloc #{state['block']} ({state['timestamp_utc'][:10]})")
    print(f"Prix spot réel (on-chain) : 1 ETH = {sp['hld_per_eth']:,.0f} HLD")
    print(f"                         : 1 HLD = {sp['eth_per_hld']:.4e} ETH ≈ {sp['eth_per_hld'] * eth_eur * 1e6:.4f} € / M HLD")
    print(f"Liquidité L              : {state['liquidity']:,}")
    print(f"ETH/EUR                  : {eth_eur:.2f} €")
    print(f"Cohérence tick/px        : {'✓' if state['consistency_check']['consistent'] else '✗'}")
    print()
    plot_slippage_curve(state)
    plot_depth(state)
    plot_summary(state)
    print("\n⚠️  Rappel: les courbes de coût d'exécution sont des SORTIES DE MODÈLE (x*y=k),")
    print("    PAS des transactions observées. Toujours lire « coût d'exécution modélisé ».")


if __name__ == "__main__":
    main()
