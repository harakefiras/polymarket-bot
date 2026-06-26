"""
BACKTEST GAP OPTIMAL - Dataset Hugging Face
Dataset: obadiaha/polymarket-crypto-5m-15m
Utilise resolutions + price_history + crypto_prices
"""

import requests, io, time, math
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "--break-system-packages", "-q"])
    import pandas as pd

# Dataset BrockMisner (miroir public de obadiaha, meme structure)
HF_BASE = "https://huggingface.co/datasets/BrockMisner/polymarket-crypto-5m-15m/resolve/main"

GAPS_PCT   = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]
ENTRY_MIN  = 0.52
ENTRY_MAX  = 0.68
ENTRY_SEC  = {"BTC": 60, "ETH": 180, "SOL": 180}
WINDOW_SEC = {"BTC": 300, "ETH": 900, "SOL": 900}

def fetch(url):
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok:
            return pd.read_parquet(io.BytesIO(r.content))
        print("  404:", url.split("/")[-1])
        return None
    except Exception as e:
        print("  Erreur:", str(e)[:80])
        return None

def load_subset(name):
    """Charge un subset du dataset (essaie part-0 a part-4)."""
    dfs = []
    for i in range(5):
        url = f"{HF_BASE}/{name}/part-{i}.parquet"
        df  = fetch(url)
        if df is not None:
            dfs.append(df)
            print(f"  {name}/part-{i}: {len(df)} lignes")
        time.sleep(0.3)
    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return None

print("BACKTEST GAP OPTIMAL - Polymarket BTC/ETH/SOL")
print("Dataset: BrockMisner/polymarket-crypto-5m-15m (Hugging Face)")
print()

# Charge les 3 subsets necessaires
print("=== Chargement resolutions ===")
res = load_subset("resolutions")
if res is None:
    print("ERREUR: resolutions introuvables")
    exit(1)
print(f"Resolutions: {len(res)} lignes | Colonnes: {list(res.columns)}")

print("\n=== Chargement price_history ===")
ph = load_subset("price_history")
if ph is None:
    print("ERREUR: price_history introuvable")
    exit(1)
print(f"Price history: {len(ph)} lignes | Colonnes: {list(ph.columns)}")

print("\n=== Chargement crypto_prices ===")
cp = load_subset("crypto_prices")
if cp is None:
    print("ERREUR: crypto_prices introuvable")
    exit(1)
print(f"Crypto prices: {len(cp)} lignes | Colonnes: {list(cp.columns)}")

# Normalise les timestamps en secondes
def to_ts(series):
    try:
        return pd.to_datetime(series, utc=True).astype("int64") // 10**9
    except:
        return series.astype("int64")

res["ts"]    = to_ts(res["resolved_at"] if "resolved_at" in res.columns else res.iloc[:,0])
ph["ts"]     = to_ts(ph["timestamp"])
cp["ts"]     = to_ts(cp["timestamp"])
cp["asset"]  = cp["asset"].str.upper()
ph["asset"]  = ph["asset"].str.upper()
res["asset"] = res["asset"].str.upper()

print("\n" + "="*50)
print("BACKTEST PAR CRYPTO")
print("="*50)

final_results = {}

for crypto in ["BTC", "ETH", "SOL"]:
    print(f"\n--- {crypto} ---")
    window_size  = WINDOW_SEC[crypto]
    entry_window = ENTRY_SEC[crypto]

    # Filtre par crypto
    res_c = res[res["asset"] == crypto].copy()
    ph_c  = ph[ph["asset"]  == crypto].copy()
    cp_c  = cp[cp["asset"]  == crypto].copy()

    print(f"  Marches resolus: {len(res_c)} | Prix tokens: {len(ph_c)} | Prix crypto: {len(cp_c)}")

    if len(res_c) == 0:
        print(f"  Aucun marche resolu pour {crypto}")
        continue

    results   = {g: {"wins": 0, "losses": 0, "skipped": 0} for g in GAPS_PCT}
    processed = 0

    for _, row in res_c.iterrows():
        try:
            market_id = row["market_id"]
            outcome   = str(row["outcome"]).strip()  # "Up" ou "Down"
            t_res     = int(row["ts"])
            t_start   = t_res - window_size
            t_entry   = t_start + entry_window

            # Prix crypto au strike (debut fenetre)
            cp_window = cp_c[(cp_c["ts"] >= t_start - 120) & (cp_c["ts"] <= t_start + 120)]
            if cp_window.empty:
                continue
            strike = float(cp_window.iloc[0]["open"] if "open" in cp_window.columns else cp_window.iloc[0]["close"])
            if strike <= 0:
                continue

            # Prix crypto a l'entree
            cp_entry = cp_c[(cp_c["ts"] >= t_entry - 120) & (cp_c["ts"] <= t_entry + 120)]
            if cp_entry.empty:
                continue
            price_at_entry = float(cp_entry.iloc[0]["open"] if "open" in cp_entry.columns else cp_entry.iloc[0]["close"])
            if price_at_entry <= 0:
                continue

            gap       = price_at_entry - strike
            gap_pct   = abs(gap) / strike * 100
            direction = "Up" if gap > 0 else "Down"
            we_win    = (direction == outcome)

            # Prix du token a l'entree (depuis price_history)
            ph_market = ph_c[ph_c["market_id"] == market_id]
            ph_entry  = ph_market[(ph_market["ts"] >= t_entry - 120) & (ph_market["ts"] <= t_entry + 120)]
            tok_price = float(ph_entry["price"].iloc[0]) if not ph_entry.empty else None

            # Teste chaque gap
            for g in GAPS_PCT:
                if gap_pct < g:
                    results[g]["skipped"] += 1
                    continue
                if tok_price is not None and not (ENTRY_MIN <= tok_price <= ENTRY_MAX):
                    results[g]["skipped"] += 1
                    continue
                if we_win:
                    results[g]["wins"] += 1
                else:
                    results[g]["losses"] += 1

            processed += 1
            if processed % 200 == 0:
                print(f"  Traite {processed} marches...")

        except Exception:
            continue

    print(f"\nResultats {crypto} ({processed} marches traites):")
    print(f"{'Gap %':>8} | {'Trades':>7} | {'Wins':>6} | {'Losses':>7} | {'Taux':>7} | {'Skip':>6}")
    print("-" * 55)

    best_gap  = None
    best_rate = 0
    for g in GAPS_PCT:
        r     = results[g]
        total = r["wins"] + r["losses"]
        rate  = (r["wins"] / total * 100) if total > 0 else 0
        print(f"{g:>7}% | {total:>7} | {r['wins']:>6} | {r['losses']:>7} | {rate:>6.1f}% | {r['skipped']:>6}")
        if total >= 10 and rate > best_rate:
            best_rate = rate
            best_gap  = g

    if best_gap:
        print(f"\n  *** GAP OPTIMAL {crypto}: {best_gap}% ({best_rate:.1f}% reussite) ***")
    final_results[crypto] = (best_gap, best_rate)

print("\n" + "="*50)
print("RESUME FINAL")
print("="*50)
for crypto, (gap, rate) in final_results.items():
    if gap:
        print(f"  {crypto}: GAP_PCT = {gap}  ({rate:.1f}% de reussite)")
    else:
        print(f"  {crypto}: donnees insuffisantes")
print("\nMets a jour Railway avec ces valeurs de GAP_PCT.")
