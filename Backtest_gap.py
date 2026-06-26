"""
BACKTEST GAP OPTIMAL - Dataset Hugging Face
Dataset: BrockMisner/polymarket-crypto-5m-15m
"""

import requests, io, time, sys, subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "pyarrow", "-q"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import pandas as pd
print("pandas OK")

HF_BASE   = "https://huggingface.co/datasets/BrockMisner/polymarket-crypto-5m-15m/resolve/main"
GAPS_PCT  = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]
ENTRY_MIN = 0.52
ENTRY_MAX = 0.68
ENTRY_SEC = {"BTC": 60,  "ETH": 180, "SOL": 180}
WIN_SEC   = {"BTC": 300, "ETH": 900, "SOL": 900}

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

def list_files(subset):
    r = requests.get(
        f"https://huggingface.co/api/datasets/BrockMisner/polymarket-crypto-5m-15m/tree/main/{subset}",
        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    if r.ok:
        return [f["path"] for f in r.json()]
    return []

def load_subset(subset):
    files = list_files(subset)
    print(f"  {subset}: {len(files)} fichiers")
    dfs = []
    for path in files:
        df = fetch(f"{HF_BASE}/{path}")
        if df is not None:
            dfs.append(df)
        time.sleep(0.2)
    return pd.concat(dfs, ignore_index=True) if dfs else None

def to_ts(series):
    try:
        return pd.to_datetime(series, utc=True).astype("int64") // 10**9
    except:
        return series.astype("int64")

print("BACKTEST GAP OPTIMAL - Polymarket BTC/ETH/SOL")
print()

# Charge les donnees
print("=== resolutions ===")
res = load_subset("resolutions")
print(f"  {len(res)} lignes | {list(res.columns)}")

print("=== price_history ===")
ph = load_subset("price_history")
print(f"  {len(ph)} lignes | {list(ph.columns)}")

print("=== crypto_prices ===")
cp = load_subset("crypto_prices")
print(f"  {len(cp)} lignes | {list(cp.columns)}")

# Normalise timestamps
res["ts"] = to_ts(res["resolved_at"])
ph["ts"]  = to_ts(ph["timestamp"])
cp["ts"]  = to_ts(cp["timestamp"])
cp["asset"] = cp["asset"].str.upper()
ph["asset"] = ph["asset"].str.upper()
res["asset"]= res["asset"].str.upper()

print("\n" + "="*50)
final = {}

for crypto in ["BTC", "ETH", "SOL"]:
    print(f"\n--- {crypto} ---")
    res_c = res[res["asset"] == crypto].copy()
    ph_c  = ph[ph["asset"]  == crypto].copy()
    cp_c  = cp[cp["asset"]  == crypto].copy()
    print(f"  Marches: {len(res_c)} | Tokens: {len(ph_c)} | Crypto: {len(cp_c)}")

    if len(res_c) == 0 or len(cp_c) == 0:
        print("  Donnees insuffisantes")
        final[crypto] = (None, 0)
        continue

    results = {g: {"wins": 0, "losses": 0, "skipped": 0} for g in GAPS_PCT}
    processed = 0

    for _, row in res_c.iterrows():
        try:
            market_id = row["market_id"]
            outcome   = str(row["outcome"]).strip()
            t_res     = int(row["ts"])
            t_start   = t_res - WIN_SEC[crypto]
            t_entry   = t_start + ENTRY_SEC[crypto]

            # Prix crypto au strike
            cp_s = cp_c[(cp_c["ts"] >= t_start - 120) & (cp_c["ts"] <= t_start + 120)]
            if cp_s.empty:
                continue
            strike = float(cp_s.iloc[0]["open"] if "open" in cp_s.columns else cp_s.iloc[0]["close"])
            if strike <= 0:
                continue

            # Prix crypto a l'entree
            cp_e = cp_c[(cp_c["ts"] >= t_entry - 120) & (cp_c["ts"] <= t_entry + 120)]
            if cp_e.empty:
                continue
            price_e = float(cp_e.iloc[0]["open"] if "open" in cp_e.columns else cp_e.iloc[0]["close"])
            if price_e <= 0:
                continue

            gap       = price_e - strike
            gap_pct   = abs(gap) / strike * 100
            direction = "Up" if gap > 0 else "Down"
            we_win    = (direction == outcome)

            # Prix token a l'entree
            ph_m = ph_c[ph_c["market_id"] == market_id]
            ph_e = ph_m[(ph_m["ts"] >= t_entry - 120) & (ph_m["ts"] <= t_entry + 120)]
            tok  = float(ph_e["price"].iloc[0]) if not ph_e.empty else None

            for g in GAPS_PCT:
                if gap_pct < g:
                    results[g]["skipped"] += 1
                    continue
                if tok is not None and not (ENTRY_MIN <= tok <= ENTRY_MAX):
                    results[g]["skipped"] += 1
                    continue
                results[g]["wins" if we_win else "losses"] += 1

            processed += 1
            if processed % 100 == 0:
                print(f"  {processed} marches traites...")

        except:
            continue

    print(f"\nResultats {crypto} ({processed} marches):")
    print(f"{'Gap':>7} | {'Trades':>7} | {'Wins':>6} | {'Losses':>7} | {'Taux':>7}")
    print("-" * 45)
    best_gap, best_rate = None, 0
    for g in GAPS_PCT:
        r     = results[g]
        total = r["wins"] + r["losses"]
        rate  = (r["wins"] / total * 100) if total > 0 else 0
        print(f"{g:>6}% | {total:>7} | {r['wins']:>6} | {r['losses']:>7} | {rate:>6.1f}%")
        if total >= 10 and rate > best_rate:
            best_rate, best_gap = rate, g
    if best_gap:
        print(f"\n  *** OPTIMAL {crypto}: {best_gap}% ({best_rate:.1f}%) ***")
    final[crypto] = (best_gap, best_rate)

print("\n" + "="*50)
print("RESUME FINAL")
print("="*50)
for c, (g, r) in final.items():
    if g:
        print(f"  {c}: GAP_PCT = {g}  ({r:.1f}% reussite)")
    else:
        print(f"  {c}: donnees insuffisantes")
