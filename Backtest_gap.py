"""
BACKTEST GAP OPTIMAL - Dataset Hugging Face
==========================================
Utilise le dataset obadiaha/polymarket-crypto-5m-15m
Telecharge les fichiers Parquet directement via HTTPS
Aucune cle API requise.

Lancer sur Railway : python backtest_gap.py
"""

import requests, io, math, time
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "--break-system-packages", "-q"])
    import pandas as pd

# Dataset Hugging Face (fichiers Parquet publics)
HF_BASE = "https://huggingface.co/datasets/obadiaha/polymarket-crypto-5m-15m/resolve/main"

# Gaps a tester (en % du prix crypto)
GAPS_PCT = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]

# Fourchette d'entree token
ENTRY_MIN = 0.52
ENTRY_MAX = 0.68

# Fenetre d'entree depuis le debut (secondes)
ENTRY_WINDOW = {"BTC": 60, "ETH": 180, "SOL": 180}

# Configs par crypto
MARKETS = {
    "BTC": {"dir": "trades",      "timeframe": "5m",  "window": 300},
    "ETH": {"dir": "trades",      "timeframe": "15m", "window": 900},
    "SOL": {"dir": "trades",      "timeframe": "15m", "window": 900},
}

def download_parquet(url):
    """Telecharge un fichier Parquet depuis Hugging Face."""
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok:
            return pd.read_parquet(io.BytesIO(r.content))
        print("  Erreur download:", r.status_code, url[:80])
        return None
    except Exception as e:
        print("  Exception download:", str(e)[:100])
        return None

def list_parquet_files(crypto, timeframe):
    """Liste les fichiers disponibles pour une crypto/timeframe."""
    # Format du dataset : data/trades/BTC/5m/part-0.parquet etc.
    urls = []
    for part in range(5):  # essaie les 5 premieres partitions
        url = f"{HF_BASE}/data/trades/{crypto}/{timeframe}/part-{part}.parquet"
        urls.append(url)
    return urls

def backtest_crypto(crypto, config):
    print(f"\n{'='*50}")
    print(f"BACKTEST {crypto} ({config['timeframe']} fenetre)")
    print(f"{'='*50}")

    window_size  = config["window"]
    timeframe    = config["timeframe"]
    entry_window = ENTRY_WINDOW[crypto]

    # Telecharge les donnees
    print("Telechargement des donnees Hugging Face...")
    dfs = []
    urls = list_parquet_files(crypto, timeframe)
    for url in urls:
        df = download_parquet(url)
        if df is not None:
            dfs.append(df)
            print(f"  Charge: {url.split('/')[-1]} ({len(df)} lignes)")
        time.sleep(0.5)

    if not dfs:
        # Essaie le format alternatif
        print("  Format alternatif...")
        for path in [
            f"{HF_BASE}/data/resolutions/{crypto}/{timeframe}/part-0.parquet",
            f"{HF_BASE}/trades/{crypto}_{timeframe}.parquet",
            f"{HF_BASE}/data/{crypto}_{timeframe}_trades.parquet",
        ]:
            df = download_parquet(path)
            if df is not None:
                dfs.append(df)
                print(f"  Charge: {path.split('/')[-1]} ({len(df)} lignes)")
                break
            time.sleep(0.3)

    if not dfs:
        print(f"  Aucune donnee disponible pour {crypto} {timeframe}")
        print(f"  Colonnes attendues: market_id, timestamp, price, outcome, resolved_to")
        return {}

    df = pd.concat(dfs, ignore_index=True)
    print(f"Total: {len(df)} lignes | Colonnes: {list(df.columns)}")

    # Compteurs par gap
    results = {g: {"wins": 0, "losses": 0, "skipped": 0} for g in GAPS_PCT}
    processed = 0

    # Groupe par marche (market_id ou window_ts)
    id_col = None
    for col in ["market_id", "window_ts", "market", "slug"]:
        if col in df.columns:
            id_col = col
            break

    if id_col is None:
        print("  Colonne market_id introuvable. Colonnes disponibles:", list(df.columns))
        return {}

    ts_col = next((c for c in ["timestamp", "ts", "time", "created_at"] if c in df.columns), None)
    price_col = next((c for c in ["price", "token_price", "p"] if c in df.columns), None)
    crypto_col = next((c for c in ["crypto_price", "btc_price", "eth_price", "spot_price"] if c in df.columns), None)
    outcome_col = next((c for c in ["resolved_to", "winner", "outcome", "resolution"] if c in df.columns), None)

    print(f"  Colonnes utilisees: id={id_col} ts={ts_col} price={price_col} crypto={crypto_col} outcome={outcome_col}")

    if not all([ts_col, price_col, outcome_col]):
        print("  Colonnes insuffisantes pour le backtest")
        return {}

    for market_id, group in df.groupby(id_col):
        try:
            group = group.sort_values(ts_col)
            t_start = group[ts_col].iloc[0]
            t_entry = t_start + entry_window
            t_end   = t_start + window_size

            # Prix crypto au strike et a l'entree
            if crypto_col:
                strike_row = group[group[ts_col] <= t_start + 10]
                entry_row  = group[(group[ts_col] >= t_entry - 30) & (group[ts_col] <= t_entry + 30)]
                if strike_row.empty or entry_row.empty:
                    continue
                strike         = float(strike_row[crypto_col].iloc[-1])
                price_at_entry = float(entry_row[crypto_col].iloc[0])
            else:
                # Pas de prix crypto → on ne peut pas calculer le gap
                continue

            if strike <= 0 or price_at_entry <= 0:
                continue

            gap     = price_at_entry - strike
            gap_pct = abs(gap) / strike * 100
            direction = "Up" if gap > 0 else "Down"

            # Resultat du marche
            outcome_val = group[outcome_col].iloc[-1]
            if pd.isna(outcome_val):
                continue
            we_win = str(outcome_val).lower() in [direction.lower(), "1", "true", "yes"]

            # Prix du token a l'entree
            entry_data = group[(group[ts_col] >= t_entry - 30) & (group[ts_col] <= t_entry + 30)]
            tok_price  = float(entry_data[price_col].iloc[0]) if not entry_data.empty else None

            # Teste chaque gap
            for gap_threshold in GAPS_PCT:
                if gap_pct < gap_threshold:
                    results[gap_threshold]["skipped"] += 1
                    continue
                if tok_price is not None and not (ENTRY_MIN <= tok_price <= ENTRY_MAX):
                    results[gap_threshold]["skipped"] += 1
                    continue
                if we_win:
                    results[gap_threshold]["wins"] += 1
                else:
                    results[gap_threshold]["losses"] += 1

            processed += 1
            if processed % 50 == 0:
                print(f"  Traite {processed} marches...")

        except Exception as e:
            continue

    # Affiche les resultats
    print(f"\nResultats {crypto} ({processed} marches traites) :")
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
        print(f"\n  *** GAP OPTIMAL {crypto} : {best_gap}% ({best_rate:.1f}% de reussite) ***")
    else:
        print(f"\n  Pas assez de donnees pour conclure")

    return results

def main():
    print("BACKTEST GAP OPTIMAL - Dataset Hugging Face")
    print("Dataset: obadiaha/polymarket-crypto-5m-15m")
    print(f"Fourchette token: {ENTRY_MIN} - {ENTRY_MAX}")
    print(f"Gaps testes: {GAPS_PCT}")

    all_results = {}
    for crypto, config in MARKETS.items():
        all_results[crypto] = backtest_crypto(crypto, config)
        time.sleep(2)

    print("\n" + "="*50)
    print("RESUME FINAL - GAPS OPTIMAUX RECOMMANDES")
    print("="*50)
    for crypto in MARKETS:
        r = all_results.get(crypto, {})
        best_gap  = None
        best_rate = 0
        for g, data in r.items():
            total = data["wins"] + data["losses"]
            if total >= 10:
                rate = data["wins"] / total * 100
                if rate > best_rate:
                    best_rate = rate
                    best_gap  = g
        if best_gap:
            print(f"  {crypto} : GAP_PCT = {best_gap}  ({best_rate:.1f}% de reussite)")
        else:
            print(f"  {crypto} : donnees insuffisantes")

    print("\nVariables Railway a mettre a jour selon les resultats.")

if __name__ == "__main__":
    main()
