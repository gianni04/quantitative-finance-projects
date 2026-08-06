#!/usr/bin/env python3
"""extract_live.py — Lecture directe de l'état du pool Uniswap V4 HLD/ETH sur Base.

Lit le storage du PoolManager via extsload(bytes32) et décode sqrtPriceX96,
tick, lpFee, protocolFee (Slot0) et la liquidité active L.
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from eth_abi import encode as abi_encode
from eth_hash.auto import keccak
from eth_utils import encode_hex, to_checksum_address
from web3 import Web3

POOL_MANAGER = to_checksum_address("0x498581fF718922c3f8e6A244956aF099B2652b2b")
TOKEN_HLD    = to_checksum_address("0xd5b6cd58d75544aAD829CaE5396eb1fD53969dBa")
CURRENCY_0   = "0x0000000000000000000000000000000000000000"
CURRENCY_1   = "0xd5b6cd58d75544aAD829CaE5396eb1fD53969dBa"
FEE          = 10000
TICK_SPACING = 200
HOOKS        = "0x0000000000000000000000000000000000000000"

EXTSLOAD_SELECTOR = "0x1e2eaeaf"
EXPECTED_POOL_ID  = "0xa4ca172912412f8ef645dfc18ed9e10400a58d6df1bfbcc927b02bff8c0234dd"
POOLS_SLOT = 6
Q96 = 2 ** 96

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR  = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
POOL_STATE_FILE = DATA_DIR / "pool_state.json"

RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base-pokt.nodies.app",
    "https://1rpc.io/base",
]


def connect_rpc():
    """Essaie chaque RPC jusqu'à en trouver un qui répond."""
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                print(f"✓ RPC connecté : {url}")
                return w3
        except Exception as e:
            print(f"✗ {url} → {e}")
    print("✗ Aucun RPC Base disponible.")
    sys.exit(1)


def compute_pool_id() -> bytes:
    """Calcule le PoolId = keccak256(abi.encode(PoolKey))."""
    pool_key = abi_encode(
        ["address", "address", "uint24", "int24", "address"],
        [
            to_checksum_address(CURRENCY_0),
            to_checksum_address(CURRENCY_1),
            FEE,
            TICK_SPACING,
            to_checksum_address(HOOKS),
        ],
    )
    pool_id = keccak(pool_key)
    return pool_id


def compute_state_slot(pool_id: bytes) -> bytes:
    """stateSlot = keccak256(abi.encode(poolId, uint256(POOLS_SLOT)))."""
    return keccak(abi_encode(["bytes32", "uint256"], [pool_id, POOLS_SLOT]))


def extsload(w3: Web3, slot: bytes) -> bytes:
    """Appelle extsload(bytes32) → bytes32."""
    payload = bytes.fromhex(EXTSLOAD_SELECTOR[2:]) + slot
    return w3.eth.call({"to": POOL_MANAGER, "data": "0x" + payload.hex()})


def unpack_slot0(raw: bytes):
    """Décode le Slot0 selon le layout Uniswap V4 (MSB→LSB)."""
    assert len(raw) == 32, f"Slot0 doit faire 32 bytes, reçu {len(raw)}"

    lp_fee    = int.from_bytes(raw[3:6], "big")
    proto_fee = int.from_bytes(raw[6:9], "big")
    tick_raw  = int.from_bytes(raw[9:12], "big")
    if tick_raw >= 2 ** 23:
        tick_raw -= 2 ** 24
    tick = tick_raw
    sqrt_price_x96 = int.from_bytes(raw[12:32], "big")

    return {
        "sqrtPriceX96": sqrt_price_x96,
        "tick": tick,
        "protocolFee": proto_fee,
        "lpFee": lp_fee,
    }


def compute_spot_price(sqrt_price_x96: int) -> tuple[float, float]:
    """Retourne (hld_per_eth, eth_per_hld)."""
    sqrt_p = sqrt_price_x96 / Q96
    price = sqrt_p * sqrt_p
    eth_per_hld = 1.0 / price if price > 0 else float("inf")
    return price, eth_per_hld


def extract(w3: Web3) -> dict:
    """Extraction complète de l'état du pool."""

    print("1. Vérification du sélecteur extsload(bytes32)…")
    code = w3.eth.get_code(POOL_MANAGER).hex()
    if EXTSLOAD_SELECTOR[2:] not in code:
        print(f"✗ Sélecteur {EXTSLOAD_SELECTOR} NON TROUVÉ dans le bytecode!")
        sys.exit(1)
    print(f"  ✓ Sélecteur {EXTSLOAD_SELECTOR} confirmé.")

    print("\n2. Calcul du PoolId…")
    pool_id = compute_pool_id()
    pool_id_hex = "0x" + pool_id.hex()
    print(f"  PoolKey encodé : {encode_hex(abi_encode(['address','address','uint24','int24','address'], [to_checksum_address(CURRENCY_0), to_checksum_address(CURRENCY_1), FEE, TICK_SPACING, to_checksum_address(HOOKS)]))}")
    print(f"  PoolId calculé  : {pool_id_hex}")
    if pool_id_hex != EXPECTED_POOL_ID:
        print(f"✗ PoolId attendu  : {EXPECTED_POOL_ID}")
        print("  → ERREUR: constantes du pool incorrectes.")
        sys.exit(1)
    print(f"  ✓ PoolId vérifié.")

    print("\n3. Lecture du Pool.State via extsload…")
    state_slot = compute_state_slot(pool_id)
    print(f"  stateSlot       : {encode_hex(state_slot)}")

    raw_slot0 = extsload(w3, state_slot)
    print(f"  Slot0 brut      : 0x{raw_slot0.hex()}")
    slot0 = unpack_slot0(raw_slot0)

    sqrt_price_x96 = slot0["sqrtPriceX96"]
    tick = slot0["tick"]
    lp_fee = slot0["lpFee"]
    proto_fee = slot0["protocolFee"]

    print(f"  sqrtPriceX96     : {sqrt_price_x96}")
    print(f"  tick             : {tick}")
    print(f"  lpFee            : {lp_fee} pips ({lp_fee / 1_000_000:.2%})")
    print(f"  protocolFee      : {proto_fee}")

    liq_slot_num = int.from_bytes(state_slot, "big") + 3
    liq_slot_bytes = liq_slot_num.to_bytes(32, "big")
    raw_liquidity = extsload(w3, liq_slot_bytes)
    liquidity = int.from_bytes(raw_liquidity[16:32], "big")
    print(f"  L (uint128)      : {liquidity}")

    print("\n4. Prix spot…")
    hld_per_eth, eth_per_hld = compute_spot_price(sqrt_price_x96)
    print(f"  1 ETH = {hld_per_eth:,.4f} HLD")
    print(f"  1 HLD = {eth_per_hld:.10f} ETH")

    price_from_tick = 1.0001 ** tick
    ratio = price_from_tick / hld_per_eth if hld_per_eth > 0 else float("inf")
    consistent = abs(ratio - 1.0) < 0.01
    print(f"  Prix tick (1.0001^{tick}) = {price_from_tick:,.4f}")
    print(f"  Ratio tick/sqrtPX96 = {ratio:.6f}")
    print(f"  ✓ Cohérence : {'OK' if consistent else 'ÉCART >1% — layout à vérifier'}")

    block = w3.eth.get_block("latest")
    block_number = block["number"]
    block_timestamp = block["timestamp"]
    block_datetime = datetime.fromtimestamp(block_timestamp, tz=timezone.utc)
    print(f"\n5. Bloc #{block_number} du {block_datetime.isoformat()}")

    sqrt_p = sqrt_price_x96 / Q96
    x_virt = liquidity / sqrt_p
    y_virt = liquidity * sqrt_p
    print(f"\n6. Réserves virtuelles (modèle constant-product):")
    print(f"  x_virt (ETH)  = {x_virt:,.6f}")
    print(f"  y_virt (HLD)  = {y_virt:,.0f}")

    return {
        "block": block_number,
        "timestamp_utc": block_datetime.isoformat(),
        "timestamp_unix": block_timestamp,
        "pool_id": pool_id_hex,
        "state_slot_hex": "0x" + state_slot.hex(),
        "slot0_raw_hex": "0x" + raw_slot0.hex(),
        "sqrtPriceX96": sqrt_price_x96,
        "tick": tick,
        "protocolFee": proto_fee,
        "lpFee": lp_fee,
        "liquidity": liquidity,
        "virtual_reserves": {
            "eth": x_virt,
            "hld": y_virt,
            "note": "Réserves VIRTUELLES (modèle x*y=k). Pas des soldes réels du contrat.",
        },
        "spot_price": {
            "hld_per_eth": hld_per_eth,
            "eth_per_hld": eth_per_hld,
            "description": "Donnée ON-CHAIN RÉELLE. 1 ETH = X HLD (currency0=ETH, currency1=HLD).",
        },
        "consistency_check": {
            "price_from_tick": price_from_tick,
            "ratio_tick_vs_sqrtpx96": ratio,
            "consistent": consistent,
        },
    }


def save_state(state: dict):
    """Écrit le fichier JSON."""
    with open(POOL_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"\n✓ État sauvegardé → {POOL_STATE_FILE}")


def main():
    print("=" * 72)
    print("UNISWAP V4 — Extraction live pool HLD/ETH (Base)")
    print("=" * 72 + "\n")

    w3 = connect_rpc()
    print(f"  Chain ID : {w3.eth.chain_id}")
    print(f"  Dernier bloc : {w3.eth.block_number}\n")

    state = extract(w3)
    save_state(state)

    print("\n" + "=" * 72)
    print("RÉCAPITULATIF")
    print("=" * 72)
    sp = state["spot_price"]
    print(f"  Bloc               : #{state['block']}")
    print(f"  Date               : {state['timestamp_utc']}")
    print(f"  sqrtPriceX96       : {state['sqrtPriceX96']}")
    print(f"  tick               : {state['tick']}")
    print(f"  lpFee              : {state['lpFee']} pips ({state['lpFee'] / 1_000_000:.2%})")
    print(f"  protocolFee        : {state['protocolFee']}")
    print(f"  Liquidité L        : {state['liquidity']}")
    print(f"  1 ETH              = {sp['hld_per_eth']:,.4f} HLD")
    print(f"  1 HLD              = {sp['eth_per_hld']:.10f} ETH")
    print(f"  Cohérence tick/px  : {'✓' if state['consistency_check']['consistent'] else '✗'}")
    print(f"\n  Vérifier sur BaseScan:")
    print(f"  → PoolManager : https://basescan.org/address/{POOL_MANAGER}#readContract")
    print(f"  → Token HLD   : https://basescan.org/token/{TOKEN_HLD}")
    print(f"  → extsload avec stateSlot = {state['state_slot_hex']}")
    print(f"  → Le tick ({state['tick']}) doit correspondre au prix spot affiché sur l'UI du pool.")
    print(f"  → Prix approximatif : 1 HLD ≈ {sp['eth_per_hld'] * 1e9:.4f} gwei")

    return state


if __name__ == "__main__":
    main()
