"""extract_eth_swaps.py — Extraction des events Swap du pool ETH/USDC Uniswap V3
(Ethereum mainnet) et estimation de la microstructure.

Pool : 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640 (ETH/USDC 0.05%)
token0 = USDC (6 déc.), token1 = WETH (18 déc.) — vérifié on-chain.
Convention : flux signé positif = achat d'ETH par le taker = hausse de prix.

Côté ETH, données réelles observées, donc on estime (volatilité, lambda, spread).
Côté HLD, pas de transactions, donc on modélise (formule AMM).
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from eth_utils import to_checksum_address
from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
FIG_DIR = REPO_ROOT / "figures"
DATA_DIR.mkdir(exist_ok=True)
FIG_DIR.mkdir(exist_ok=True)

CSV_PATH = DATA_DIR / "eth_swaps.csv"
JSON_PATH = DATA_DIR / "eth_microstructure.json"
PLOT_PATH = FIG_DIR / "eth_price_impact.png"

sys.path.insert(0, str(REPO_ROOT / "src"))
from microstructure import (  # noqa: E402
    effective_spread,
    kyle_lambda,
    kyle_lambda_binned,
    realized_volatility,
)
from microstructure import Q96 as _Q96  # noqa: E402
Q96 = float(_Q96)

POOL_ADDRESS = to_checksum_address("0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640")

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

RPC_URLS = [
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://ethereum.publicnode.com",
    "https://1rpc.io/eth",
    "https://eth.drpc.org",
    "https://rpc.mevblocker.io",
    "https://eth-mainnet.public.blastapi.io",
    "https://virginia.rpc.blxrbdn.com",
]

LOOKBACK_HOURS = 36
BLOCKS_PER_CHUNK = 500
MAX_RETRIES = 2
RETRY_DELAY = 3.0

BLOCKS_PER_HOUR = 300


def connect_rpc(urls=None):
    """Essaie chaque RPC jusqu'à en trouver un qui répond."""
    if urls is None:
        urls = RPC_URLS
    for url in urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            if w3.is_connected():
                print(f"RPC connecte : {url}")
                return w3
        except Exception as e:
            print(f"  {url} -> {e}")
    print("Aucun RPC Ethereum disponible.")
    sys.exit(1)


def verify_pool(w3):
    """Vérifie token0, token1 et leurs décimales on-chain."""
    print("\n--- Verification du pool ---")
    abi = [
        {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
        {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    ]
    pool = w3.eth.contract(address=POOL_ADDRESS, abi=abi)
    t0 = pool.functions.token0().call()
    t1 = pool.functions.token1().call()
    print(f"  token0 = {t0}")
    print(f"  token1 = {t1}")

    erc20_abi = [
        {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
        {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    ]
    c0 = w3.eth.contract(address=to_checksum_address(t0), abi=erc20_abi)
    c1 = w3.eth.contract(address=to_checksum_address(t1), abi=erc20_abi)
    dec0 = c0.functions.decimals().call()
    dec1 = c1.functions.decimals().call()
    sym0 = c0.functions.symbol().call()
    sym1 = c1.functions.symbol().call()
    print(f"  {sym0} : {dec0} decimales")
    print(f"  {sym1} : {dec1} decimales")

    is_eth = t1.lower() == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    print(f"  token1 = WETH ? {is_eth}")

    return {
        "token0": t0,
        "token1": t1,
        "symbol0": sym0,
        "symbol1": sym1,
        "decimals0": dec0,
        "decimals1": dec1,
        "token1_is_weth": is_eth,
    }


def _make_w3(url):
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))


def fetch_swaps_with_retry(urls, from_block, to_block):
    """Récupère les logs Swap en essayant chaque RPC."""
    for url in urls:
        try:
            w3 = _make_w3(url)
            logs = w3.eth.get_logs({
                "address": POOL_ADDRESS,
                "topics": [SWAP_TOPIC],
                "fromBlock": from_block,
                "toBlock": to_block,
            })
            return logs, w3
        except Exception:
            time.sleep(0.5)
            continue
    return None, None


def fetch_swaps(w3, from_block, to_block):
    """Récupère les events Swap entre from_block et to_block (inclus)."""
    pct = (to_block - from_block) / BLOCKS_PER_HOUR
    print(f"  blocs {from_block} -> {to_block} ({pct:.1f}h)...", end=" ", flush=True)

    logs, usable_w3 = fetch_swaps_with_retry(RPC_URLS, from_block, to_block)

    if logs is None:
        print("ECHEC (tous RPCs)")
        return [], w3

    block_cache = {}

    swaps = []
    for log in logs:
        raw_data = log["data"]
        if isinstance(raw_data, str):
            data_bytes = bytes.fromhex(raw_data[2:] if raw_data.startswith("0x") else raw_data)
        else:
            data_bytes = bytes(raw_data)

        amount0 = int.from_bytes(data_bytes[0:32], "big", signed=True)
        amount1 = int.from_bytes(data_bytes[32:64], "big", signed=True)
        sqrt_price_x96 = int.from_bytes(data_bytes[64:96], "big", signed=False)
        liquidity = int.from_bytes(data_bytes[96:128], "big", signed=False)
        tick_raw = int.from_bytes(data_bytes[128:160], "big", signed=True)

        block_num = log["blockNumber"]
        if block_num not in block_cache:
            try:
                block = usable_w3.eth.get_block(block_num)
                block_cache[block_num] = block["timestamp"]
            except Exception:
                block_cache[block_num] = 0
        timestamp = block_cache[block_num]

        swaps.append({
            "block": block_num,
            "timestamp": timestamp,
            "amount0": amount0,
            "amount1": amount1,
            "sqrtPriceX96": sqrt_price_x96,
            "liquidity": liquidity,
            "tick": tick_raw,
        })

    print(f"{len(swaps)} swaps")
    time.sleep(RETRY_DELAY)
    return swaps, w3


def compute_eth_price(sqrt_price_x96, dec0, dec1):
    """Retourne le prix ETH en USD depuis sqrtPriceX96."""
    sqrt_p = sqrt_price_x96 / Q96
    price_raw = sqrt_p * sqrt_p

    eth_usd = (1.0 / price_raw) * (10 ** dec1) / (10 ** dec0)
    return eth_usd


def check_coherence(df, pool_info, w3):
    """Vérifie que le prix ETH/USD reconstruit est cohérent avec CoinGecko."""
    print("\n--- Controle coherence ---")

    dec0 = pool_info["decimals0"]
    dec1 = pool_info["decimals1"]

    sqrt_px = df["sqrtPriceX96"].values
    recent = sqrt_px[-100:]
    eth_prices = np.array([compute_eth_price(p, dec0, dec1) for p in recent])
    eth_mean = np.mean(eth_prices)
    eth_std = np.std(eth_prices)

    print(f"  Prix ETH/USD (moyen 100 derniers swaps) : ${eth_mean:,.2f} +/- ${eth_std:,.2f}")

    try:
        import requests
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
            timeout=10,
        )
        if resp.status_code == 200:
            cg_price = resp.json()["ethereum"]["usd"]
            ecart_pct = abs(eth_mean - cg_price) / cg_price * 100
            print(f"  CoinGecko ETH/USD              : ${cg_price:,.2f}")
            print(f"  Ecart                          : {ecart_pct:.2f}%")
            if ecart_pct > 2:
                print(f"  Ecart > 2% — vérifier décimales ou pool.")
        else:
            cg_price = None
            print(f"  CoinGecko: HTTP {resp.status_code}")
    except Exception as e:
        cg_price = None
        print(f"  CoinGecko: indisponible ({e})")

    return eth_mean, cg_price


def plot_price_impact(df, pool_info, lambda_binned):
    """Génère le nuage de points flux net (ETH) vs ΔP avec droite de régression."""
    print("\n--- Generation figure ---")

    dec0 = pool_info["decimals0"]
    dec1 = pool_info["decimals1"]
    bin_sec = 300

    tstamp = np.asarray(df["timestamp"], dtype=np.float64)
    price_human = np.array([
        compute_eth_price(p, dec0, dec1)
        for p in np.asarray(df["sqrtPriceX96"], dtype=np.float64)
    ])
    q_signed = -np.asarray(df["amount1"], dtype=np.float64) / (10 ** dec1)

    t_start, t_end = tstamp[0], tstamp[-1]
    n_bins = max(1, int((t_end - t_start) / bin_sec))
    bin_edges = np.linspace(t_start, t_end, n_bins + 1)

    bin_flows, bin_dp, bin_counts = [], [], []
    for i in range(n_bins):
        mask = (tstamp >= bin_edges[i]) & (tstamp < bin_edges[i + 1])
        if np.sum(mask) >= 2:
            idx = np.where(mask)[0]
            dp = price_human[idx[-1]] - price_human[idx[0]]
            flow = np.sum(q_signed[mask])
            bin_flows.append(flow)
            bin_dp.append(dp)
            bin_counts.append(np.sum(mask))

    bin_flows = np.array(bin_flows)
    bin_dp = np.array(bin_dp)

    fig, ax = plt.subplots(figsize=(10, 6))
    sc = ax.scatter(bin_flows, bin_dp, c=np.log1p(bin_counts),
                    cmap="Blues", alpha=0.6, edgecolors="grey", linewidth=0.3)

    X = np.column_stack([np.ones(len(bin_flows)), bin_flows])
    beta, _, _, _ = np.linalg.lstsq(X, bin_dp, rcond=None)
    x_line = np.linspace(bin_flows.min(), bin_flows.max(), 100)
    ax.plot(x_line, beta[0] + beta[1] * x_line, "r--", linewidth=2,
            label=f"lambda = {beta[1]:.4f} USD/ETH")

    ax.axhline(0, color="grey", linewidth=0.5)
    ax.axvline(0, color="grey", linewidth=0.5)
    ax.set_xlabel("Flux net signe (ETH) — positif = achat taker")
    ax.set_ylabel("Variation de prix (USD) — intra-bin 5min")
    ax.set_title(f"Impact prix — ETH/USDC (Uniswap V3, {bin_sec}s bins)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    cbar = plt.colorbar(sc, ax=ax, label="log(1 + nb swaps/bin)")
    fig.tight_layout()
    fig.savefig(PLOT_PATH, dpi=150)
    print(f"  Figure -> {PLOT_PATH}")
    plt.close(fig)


def main():
    print("=" * 72)
    print("UNISWAP V3 — Extraction swaps ETH/USDC (Ethereum mainnet)")
    print("=" * 72)

    w3 = connect_rpc()
    chain_id = w3.eth.chain_id
    latest = w3.eth.block_number
    print(f"  Chain ID      : {chain_id}")
    print(f"  Dernier bloc  : {latest}")
    assert chain_id == 1, f"Mauvais chain ID: {chain_id}, attendu 1 (Ethereum mainnet)"

    pool_info = verify_pool(w3)

    blocks_back = LOOKBACK_HOURS * BLOCKS_PER_HOUR
    from_block = latest - blocks_back
    print(f"\n--- Extraction swaps ---")
    print(f"  Fenetre : {LOOKBACK_HOURS}h ({blocks_back} blocs)")
    print(f"  De bloc {from_block} a {latest}")

    all_swaps = []
    chunk_start = from_block
    while chunk_start <= latest:
        chunk_end = min(chunk_start + BLOCKS_PER_CHUNK - 1, latest)
        swaps, w3 = fetch_swaps(w3, chunk_start, chunk_end)
        all_swaps.extend(swaps)
        chunk_start += BLOCKS_PER_CHUNK

    print(f"\n  Total swaps extraits : {len(all_swaps)}")

    if len(all_swaps) < 100:
        print("  Pas assez de swaps, elargir LOOKBACK_HOURS.")
        sys.exit(1)

    df = pd.DataFrame(all_swaps)
    df = df.sort_values(["block", "timestamp"]).reset_index(drop=True)
    df.to_csv(CSV_PATH, index=False)
    print(f"  Sauvegarde -> {CSV_PATH} ({len(df)} lignes)")

    eth_mean, cg_price = check_coherence(df, pool_info, w3)

    dec0 = pool_info["decimals0"]
    dec1 = pool_info["decimals1"]

    print("\n--- Estimateurs microstructure ---")

    tstamp = np.asarray(df["timestamp"], dtype=np.float64)
    dt = np.diff(tstamp)
    avg_dt = np.mean(dt[dt > 0])
    periods_per_year = 365 * 24 * 3600 / avg_dt
    print(f"  dt moyen entre swaps : {avg_dt:.2f}s")
    print(f"  echantillons/an      : {periods_per_year:,.0f}")

    rv = realized_volatility(df, periods_per_year=365 * 24 * 3600)
    print(f"\n  Volatilite realisee annualisee : {rv:.4f} ({rv*100:.2f}%)")

    kt = kyle_lambda(df, dec0, dec1)
    print(f"\n  Kyle lambda (trade-by-trade):")
    print(f"    lambda  = {kt['lambda_']:.6f}")
    print(f"    std_err = {kt['std_err']:.6f}")
    print(f"    t-stat  = {kt['t_stat']:.2f}")
    print(f"    R2      = {kt['r_squared']:.4f}")
    print(f"    n_obs   = {kt['n_obs']}")

    kb60 = kyle_lambda_binned(df, 60, dec0, dec1)
    kb300 = kyle_lambda_binned(df, 300, dec0, dec1)
    print(f"\n  Kyle lambda (binned 60s):")
    print(f"    lambda  = {kb60['lambda_']:.6f}")
    print(f"    std_err = {kb60['std_err']:.6f}")
    print(f"    t-stat  = {kb60['t_stat']:.2f}")
    print(f"    R2      = {kb60['r_squared']:.4f}")
    print(f"    n_bins  = {kb60['n_bins']}")

    print(f"\n  Kyle lambda (binned 300s):")
    print(f"    lambda  = {kb300['lambda_']:.6f}")
    print(f"    std_err = {kb300['std_err']:.6f}")
    print(f"    t-stat  = {kb300['t_stat']:.2f}")
    print(f"    R2      = {kb300['r_squared']:.4f}")
    print(f"    n_bins  = {kb300['n_bins']}")

    es = effective_spread(df, dec0, dec1)
    print(f"\n  Spread effectif:")
    print(f"    median = {es['median_bps']:.2f} bps")
    print(f"    mean   = {es['mean_bps']:.2f} bps")
    print(f"    n      = {es['n_swaps']}")

    spot_weth_per_usdc = 1.0 / eth_mean if eth_mean > 0 else 0.000531
    rescale = 1.0 / (spot_weth_per_usdc ** 2)

    def _rescale_lambda(result_dict):
        """Convertit lambda en USD/ETH^2 et ajoute les champs rescaled."""
        lam_raw = result_dict.get("lambda_", np.nan)
        se_raw = result_dict.get("std_err", np.nan)
        if not np.isnan(lam_raw):
            result_dict["lambda_usd_per_eth2"] = abs(float(lam_raw)) * rescale
            if not np.isnan(se_raw):
                result_dict["std_err_usd_per_eth2"] = abs(float(se_raw)) * rescale
                result_dict["t_stat_usd"] = abs(float(lam_raw)) / abs(float(se_raw)) \
                    if abs(float(se_raw)) > 0 else np.nan
        else:
            result_dict["lambda_usd_per_eth2"] = np.nan
            result_dict["std_err_usd_per_eth2"] = np.nan
            result_dict["t_stat_usd"] = np.nan

    _rescale_lambda(kt)
    _rescale_lambda(kb60)
    _rescale_lambda(kb300)

    print(f"\n  Spot WETH/USDC = {spot_weth_per_usdc:.10f}")
    print(f"  Facteur rescaling lambda = {rescale:.4f}")
    print(f"  lambda (tbt)        = {kt.get('lambda_usd_per_eth2', np.nan):.6f} USD/ETH^2")
    print(f"  lambda (binned 60s)  = {kb60.get('lambda_usd_per_eth2', np.nan):.6f} USD/ETH^2")
    print(f"  lambda (binned 300s) = {kb300.get('lambda_usd_per_eth2', np.nan):.6f} USD/ETH^2")

    plot_price_impact(df, pool_info, kb300)

    best_lambda = kb300 if (kt["r_squared"] < 0.3 and not np.isnan(kb300["lambda_"])) else kt
    lambda_method = "binned_300s" if kt["r_squared"] < 0.3 else "trade_by_trade"

    results = {
        "pool": POOL_ADDRESS,
        "pool_name": "ETH/USDC V3 0.05%",
        "chain": "ethereum_mainnet",
        "token0": pool_info["token0"],
        "token1": pool_info["token1"],
        "symbol0": pool_info["symbol0"],
        "symbol1": pool_info["symbol1"],
        "decimals0": dec0,
        "decimals1": dec1,
        "sign_convention": (
            f"q_signed = -amount1 / 10^{dec1}  "
            "(positif = achat {pool_info['symbol1']} par taker = hausse de prix)"
        ),
        "extraction": {
            "block_start": int(df["block"].min()),
            "block_end": int(df["block"].max()),
            "timestamp_start_utc": datetime.fromtimestamp(
                int(df["timestamp"].min()), tz=timezone.utc
            ).isoformat(),
            "timestamp_end_utc": datetime.fromtimestamp(
                int(df["timestamp"].max()), tz=timezone.utc
            ).isoformat(),
            "lookback_hours": LOOKBACK_HOURS,
            "n_swaps": len(df),
            "avg_dt_sec": float(avg_dt),
        },
        "coherence": {
            "eth_usd_mean_100_last_swaps": float(eth_mean),
            "coingecko_eth_usd": cg_price,
            "ecart_pct": float(abs(eth_mean - cg_price) / cg_price * 100) if cg_price else None,
        },
        "estimates": {
            "note": "Estimations sur donnees reelles observees, pas des sorties de modele AMM.",
            "realized_volatility_annualized": float(rv),
            "kyle_lambda_trade_by_trade": {
                "lambda": kt["lambda_"] if not np.isnan(kt["lambda_"]) else None,
                "std_err": kt["std_err"] if not np.isnan(kt["std_err"]) else None,
                "t_stat": kt["t_stat"] if not np.isnan(kt["t_stat"]) else None,
                "r_squared": kt["r_squared"] if not np.isnan(kt["r_squared"]) else None,
                "n_obs": kt["n_obs"],
            },
            "kyle_lambda_binned_60s": {
                "lambda": kb60["lambda_"] if not np.isnan(kb60["lambda_"]) else None,
                "std_err": kb60["std_err"] if not np.isnan(kb60["std_err"]) else None,
                "t_stat": kb60["t_stat"] if not np.isnan(kb60["t_stat"]) else None,
                "r_squared": kb60["r_squared"] if not np.isnan(kb60["r_squared"]) else None,
                "n_bins": kb60["n_bins"],
                "bin_sec": 60,
            },
            "kyle_lambda_binned_300s": {
                "lambda": kb300["lambda_"] if not np.isnan(kb300["lambda_"]) else None,
                "std_err": kb300["std_err"] if not np.isnan(kb300["std_err"]) else None,
                "t_stat": kb300["t_stat"] if not np.isnan(kb300["t_stat"]) else None,
                "r_squared": kb300["r_squared"] if not np.isnan(kb300["r_squared"]) else None,
                "n_bins": kb300["n_bins"],
                "bin_sec": 300,
            },
            "best_lambda_method": lambda_method,
            "effective_spread": {
                "median_bps": es["median_bps"] if not np.isnan(es["median_bps"]) else None,
                "mean_bps": es["mean_bps"] if not np.isnan(es["mean_bps"]) else None,
                "n_swaps": es["n_swaps"],
            },
        },
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n  Resultats sauvegardes -> {JSON_PATH}")

    lam_usd_tbt = kt.get("lambda_usd_per_eth2", np.nan)
    lam_usd_60 = kb60.get("lambda_usd_per_eth2", np.nan)
    lam_usd_300 = kb300.get("lambda_usd_per_eth2", np.nan)

    print("\n" + "=" * 72)
    print("RECAPITULATIF — ETH/USDC (Uniswap V3, Ethereum mainnet)")
    print("=" * 72)
    print(f"""
| Mesure                              | ETH/USDC (reel, estime)                    |
|-------------------------------------|--------------------------------------------|
| Kyle lambda (trade-by-trade)        | {lam_usd_tbt:.6f} USD/ETH^2 (R2={kt['r_squared']:.3f}, n={kt['n_obs']}) |
| Kyle lambda (binned 60s)            | {lam_usd_60:.6f} USD/ETH^2 (R2={kb60['r_squared']:.3f}, n={kb60['n_bins']}) |
| Kyle lambda (binned 300s)           | {lam_usd_300:.6f} USD/ETH^2 (R2={kb300['r_squared']:.3f}, n={kb300['n_bins']}) |
| Volatilite realisee annualisee      | {rv:.4f} ({rv*100:.2f}%)                    |
| Spread effectif median (bps)        | {es['median_bps']:.2f}                      |
| Nombre de swaps / fenetre           | {len(df)} ({LOOKBACK_HOURS}h)               |
| Prix ETH/USD moyen (fenetre)        | ${eth_mean:,.2f}                           |""")

    if cg_price:
        print(f"| Reference CoinGecko               | ${cg_price:,.2f}                            |")
    print()
    print("Note : lambda en USD/ETH^2. lambda_raw en (WETH/USDC)/WETH dans le JSON.")
    print("Meilleur lambda retenu :", lambda_method)

    return results


if __name__ == "__main__":
    main()
