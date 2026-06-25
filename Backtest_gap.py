"""
BACKTEST GAP OPTIMAL - Polymarket BTC/ETH/SOL
==============================================
Ce script recupere l'historique des marches Polymarket fermes
et simule differents gaps pour trouver le meilleur taux de reussite.

Lancer sur le VPS : python3 backtest_gap.py
Resultat : tableau des gaps par crypto avec taux de reussite
"""

import requests, json, time, math
from datetime import datetime, timezone

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
COINBASE_API = "https://api.exchange.coinbase.com/products/{}-USD/candles"

# Gaps a tester (en % du prix crypto)
GAPS_PCT = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]

# Fourchette d'entree token (on n'entre qu'entre ces deux valeurs)
ENTRY_MIN = 0.52
ENTRY_MAX = 0.68

# Fenetre d'entree depuis le debut de la fenetre (secondes)
ENTRY_WINDOW = {"BTC": 60, "ETH": 180, "SOL": 180}

# Marches a analyser
MARKETS = {
    "BTC": {"slug": "btc-updown-5m-",  "window": 300,  "product": "BTC"},
    "ETH": {"slug": "eth-updown-15m-", "window": 900,  "product": "ETH"},
    "SOL": {"slug": "sol-updown-15m-", "window": 900,  "product": "SOL"},
}

# Nombre de marches historiques a recuperer par crypto
N_MARKETS = 200

headers = {"User-Agent": "Mozilla/5.0"}

def get_closed_markets(slug_prefix, n=200):
    """Recupere les N derniers marches fermes pour un slug."""
    markets = []
    offset  = 0
    limit   = 50
    while len(markets) < n:
        try:
            r = requests.get(GAMMA_API + "/markets", headers=headers, params={
                "slug_startswith": slug_prefix,
                "closed": "true",
                "limit":  limit,
                "offset": offset,
                "order":  "endDate",
                "ascending": "false"
            }, timeout=15)
            if not r.ok:
                print("  Erreur Gamma API:", r.status_code)
                break
            batch = r.json()
            if not batch:
                break
            markets.extend(batch)
            offset += limit
            time.sleep(0.3)
        except Exception as e:
            print("  Exception:", e)
            break
    return markets[:n]

def get_token_prices_history(token_id):
    """Recupere l'historique des prix d'un token."""
    try:
        r = requests.get(CLOB_API + "/prices-history", headers=headers, params={
            "market":     token_id,
            "interval":   "1m",
            "startTs":    0,
            "endTs":      int(time.time()),
            "fidelity":   1
        }, timeout=15)
        if r.ok:
            data = r.json()
            return data.get("history", [])
        return []
    except:
        return []

def get_crypto_candles(product, start_ts, end_ts):
    """Recupere les bougies 1min de Coinbase pour une crypto."""
    try:
        url = COINBASE_API.format(product)
        r = requests.get(url, headers=headers, params={
            "start":       datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
            "end":         datetime.fromtimestamp(end_ts,   tz=timezone.utc).isoformat(),
            "granularity": 60
        }, timeout=15)
        if r.ok:
            # Format : [time, low, high, open, close, volume]
            return r.json()
        return []
    except:
        return []

def price_at_ts(candles, target_ts):
    """Trouve le prix a un timestamp donne dans les bougies."""
    # Les bougies Coinbase sont ordonnees du plus recent au plus ancien
    best = None
    for c in candles:
        ts = c[0]
        if ts <= target_ts:
            if best is None or ts > best[0]:
                best = c
    if best:
        return best[4]  # close price
    return None

def token_price_at_ts(history, target_ts, window=30):
    """Trouve le prix du token a un timestamp donne (+-window secondes)."""
    best = None
    for point in history:
        ts = point.get("t", 0)
        p  = point.get("p", 0)
        if abs(ts - target_ts) <= window:
            if best is None or abs(ts - target_ts) < abs(best[0] - target_ts):
                best = (ts, p)
    return best[1] if best else None

def backtest_crypto(crypto_name, config):
    print(f"\n{'='*50}")
    print(f"BACKTEST {crypto_name} ({config['window']}s fenetre)")
    print(f"{'='*50}")

    slug_prefix  = config["slug"]
    window_size  = config["window"]
    product      = config["product"]
    entry_window = ENTRY_WINDOW[crypto_name]

    # Recupere les marches historiques
    print(f"Recuperation des marches fermes...")
    markets = get_closed_markets(slug_prefix, N_MARKETS)
    print(f"  {len(markets)} marches recuperes")

    if not markets:
        print("  Aucun marche disponible - skip")
        return {}

    # Compteurs par gap
    results = {g: {"wins": 0, "losses": 0, "skipped": 0, "total_signal": 0} for g in GAPS_PCT}

    processed = 0
    for m in markets:
        try:
            # Parse du marche
            slug     = m.get("slug", "")
            winner   = m.get("winner", "")
            outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
            token_ids= json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])

            if not winner or len(token_ids) < 2 or len(outcomes) < 2:
                continue

            # Timestamp de la fenetre depuis le slug
            try:
                window_ts = int(slug.split("-")[-1])
            except:
                continue

            # Timestamps cles
            t_start  = window_ts
            t_entry  = window_ts + entry_window   # moment ou on regarde le gap
            t_end    = window_ts + window_size

            # Recupere les bougies crypto sur la fenetre
            candles = get_crypto_candles(product, t_start - 60, t_end + 60)
            if not candles:
                continue

            # Prix au debut de la fenetre (strike)
            strike = price_at_ts(candles, t_start)
            if not strike or strike <= 0:
                continue

            # Prix au moment d'entree possible
            price_at_entry = price_at_ts(candles, t_entry)
            if not price_at_entry or price_at_entry <= 0:
                continue

            # Gap observe
            gap        = price_at_entry - strike
            gap_pct    = abs(gap) / strike * 100
            direction  = "Up" if gap > 0 else "Down"

            # Index du token gagnant et perdant
            if winner in outcomes:
                winner_idx  = outcomes.index(winner)
                loser_idx   = 1 - winner_idx
            else:
                continue

            # Est-ce que notre direction correspond au gagnant ?
            our_idx    = outcomes.index(direction) if direction in outcomes else -1
            we_win     = (our_idx == winner_idx)

            # Recupere le prix du token a l'entree
            up_idx   = outcomes.index("Up") if "Up" in outcomes else 0
            our_token= token_ids[our_idx] if our_idx >= 0 else token_ids[0]

            history  = get_token_prices_history(our_token)
            tok_price= token_price_at_ts(history, t_entry) if history else None

            # Teste chaque gap
            for gap_threshold in GAPS_PCT:
                threshold_usd = strike * gap_threshold / 100

                if gap_pct < gap_threshold:
                    results[gap_threshold]["skipped"] += 1
                    continue

                results[gap_threshold]["total_signal"] += 1

                # Verifie si le token est dans la fourchette d'entree
                if tok_price is not None:
                    if not (ENTRY_MIN <= tok_price <= ENTRY_MAX):
                        results[gap_threshold]["skipped"] += 1
                        continue

                if we_win:
                    results[gap_threshold]["wins"] += 1
                else:
                    results[gap_threshold]["losses"] += 1

            processed += 1
            if processed % 20 == 0:
                print(f"  Traite {processed}/{len(markets)} marches...")

            time.sleep(0.2)  # Rate limiting

        except Exception as e:
            continue

    # Affiche les resultats
    print(f"\nResultats {crypto_name} ({processed} marches traites) :")
    print(f"{'Gap %':>8} | {'Trades':>7} | {'Wins':>6} | {'Losses':>7} | {'Taux':>7} | {'Skip':>6}")
    print("-" * 55)

    best_gap  = None
    best_rate = 0

    for g in GAPS_PCT:
        r  = results[g]
        total = r["wins"] + r["losses"]
        if total == 0:
            rate = 0
        else:
            rate = r["wins"] / total * 100
        skip = r["skipped"]
        print(f"{g:>7}% | {total:>7} | {r['wins']:>6} | {r['losses']:>7} | {rate:>6.1f}% | {skip:>6}")

        if total >= 10 and rate > best_rate:
            best_rate = rate
            best_gap  = g

    if best_gap:
        print(f"\n  *** GAP OPTIMAL {crypto_name} : {best_gap}% ({best_rate:.1f}% de reussite) ***")
    else:
        print(f"\n  Pas assez de donnees pour conclure")

    return results

def main():
    print("BACKTEST GAP OPTIMAL - Polymarket BTC/ETH/SOL")
    print("Periode : 200 derniers marches par crypto")
    print(f"Fourchette token : {ENTRY_MIN} - {ENTRY_MAX}")
    print(f"Gaps testes : {GAPS_PCT}")
    print()

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

    print("\nVariables Railway a mettre a jour selon les resultats ci-dessus.")

if __name__ == "__main__":
    main()
