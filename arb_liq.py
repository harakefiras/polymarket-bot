import os, time, logging, requests, json, asyncio, math, threading
from datetime import date, datetime, timezone

# ============================================================
# BOT ARB LIQUIDATION CRYPTO v1 - POLYMARKET + COINGECKO
# Principe :
# - Scanne les marchés Polymarket crypto expirant dans < 2h
# - Compare le résultat CONNU (prix réel CoinGecko) vs prix Polymarket
# - Si le côté gagnant est encore < 0.97, achète et attend le règlement
# Exemple : BTC clôture > 100k, marché à 0.93 → achète Yes à 0.93 → règlement à 1.00
# ============================================================

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET      = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

TRADE_USDC     = float(os.getenv("LIQ_TRADE_USDC",     "10"))
MAX_CONCURRENT = int(os.getenv("LIQ_MAX_CONCURRENT",   "3"))
STOP_LOSS_USDC = float(os.getenv("LIQ_STOP_LOSS_USDC", "15"))
SCAN_INTERVAL  = float(os.getenv("LIQ_SCAN_INTERVAL",  "60"))  # 1 min

# Fenêtre d'expiration : marchés expirant dans X minutes max
MAX_EXPIRY_MIN = float(os.getenv("LIQ_MAX_EXPIRY_MIN", "120"))  # 2h
MIN_EXPIRY_MIN = float(os.getenv("LIQ_MIN_EXPIRY_MIN", "5"))    # pas trop tard

# Prix minimum pour entrer (gain minimum garanti)
MIN_ENTRY_PRICE = float(os.getenv("LIQ_MIN_ENTRY", "0.90"))  # achète si < 0.97, gain > 3%
MAX_ENTRY_PRICE = float(os.getenv("LIQ_MAX_ENTRY", "0.97"))  # pas la peine si déjà à 0.99

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
GECKO_API   = "https://api.coingecko.com/api/v3"
PNL_FILE    = "/app/liq_daily_pnl.txt"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "arb-liq-bot/1.0"})

import sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("arb_liq")

# ── MAPPING CRYPTO ─────────────────────────────────────────────────────────────
# Mots-clés Polymarket → ID CoinGecko
CRYPTO_MAP = {
    "bitcoin": "bitcoin",  "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana",    "sol": "solana",
    "bnb": "binancecoin",
    "xrp": "ripple",
    "doge": "dogecoin",
    "cardano": "cardano",  "ada": "cardano",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "chainlink": "chainlink",   "link": "chainlink",
}

# Cache prix CoinGecko (refresh toutes les 30s)
price_cache     = {}
price_cache_ts  = 0
PRICE_CACHE_TTL = 30  # secondes

# ── PNL ────────────────────────────────────────────────────────────────────────

def load_daily_pnl():
    try:
        if os.path.exists(PNL_FILE):
            with open(PNL_FILE, "r") as f:
                lines = f.read().strip().split("\n")
                if len(lines) == 2 and lines[0] == str(date.today()):
                    return float(lines[1])
        return 0.0
    except:
        return 0.0

def save_daily_pnl(pnl):
    try:
        with open(PNL_FILE, "w") as f:
            f.write(str(date.today()) + "\n" + str(round(pnl, 4)))
    except:
        pass

daily_pnl   = load_daily_pnl()
pnl_date    = date.today()
open_trades = []
trades_lock = threading.Lock()
done_slugs  = set()

balance_lock     = threading.Lock()
reserved_balance = 0.0

# ── COINGECKO ──────────────────────────────────────────────────────────────────

def get_all_prices():
    """Récupère tous les prix crypto en une seule requête CoinGecko."""
    global price_cache, price_cache_ts
    now = time.time()
    if now - price_cache_ts < PRICE_CACHE_TTL and price_cache:
        return price_cache
    try:
        ids = ",".join(set(CRYPTO_MAP.values()))
        r = SESSION.get(GECKO_API + "/simple/price",
                        params={"ids": ids, "vs_currencies": "usd"},
                        timeout=10)
        if r.ok:
            data = r.json()
            price_cache    = {k: v["usd"] for k, v in data.items()}
            price_cache_ts = now
            return price_cache
    except Exception as e:
        log.warning("CoinGecko KO: " + str(e))
    return price_cache  # retourne le cache même périmé en cas d'erreur

def get_crypto_price(coin_id):
    """Prix USD d'un coin depuis le cache."""
    prices = get_all_prices()
    return prices.get(coin_id)

# ── ANALYSE DU MARCHE ──────────────────────────────────────────────────────────

def detect_crypto_and_threshold(question):
    """
    Analyse la question du marché pour détecter :
    - Quel crypto (BTC, ETH...)
    - Le seuil de prix (ex: 100000)
    - La direction (above/below, will/won't)
    Retourne (coin_id, threshold, direction) ou None si non détectable.
    """
    q = question.lower()

    # Détecte le crypto
    coin_id = None
    for kw, cid in CRYPTO_MAP.items():
        if kw in q:
            coin_id = cid
            break
    if not coin_id:
        return None

    # Détecte le seuil de prix
    # Cherche des patterns comme "$100,000", "100k", "100000"
    import re
    threshold = None

    # Pattern $X,XXX,XXX ou $X,XXX
    m = re.search(r'\$?([\d,]+)k?\b', q.replace(",", ""))
    if m:
        val = float(m.group(1).replace(",", ""))
        if "k" in q[m.start():m.end()+1]:
            val *= 1000
        if val > 100:  # filtre les petits nombres (pourcentages etc)
            threshold = val

    if threshold is None:
        return None

    # Détecte la direction
    above_keywords = ["above", "over", "exceed", "higher", "more than",
                      "surpass", "hit", "reach", "top", "break"]
    below_keywords = ["below", "under", "fall", "lower", "less than",
                      "drop", "beneath"]

    direction = None
    for kw in above_keywords:
        if kw in q:
            direction = "above"
            break
    if direction is None:
        for kw in below_keywords:
            if kw in q:
                direction = "below"
                break

    # Par défaut on suppose "above" pour les marchés type "will X reach Y"
    if direction is None and any(w in q for w in ["will", "reach", "hit"]):
        direction = "above"

    if direction is None:
        return None

    return coin_id, threshold, direction

def determine_winning_outcome(coin_id, threshold, direction, outcomes, token_ids):
    """
    Détermine quel token acheter selon le prix réel.
    Retourne (token_id, outcome_name, current_price, confidence) ou None.
    """
    price = get_crypto_price(coin_id)
    if price is None:
        return None

    if direction == "above":
        result_is_yes = price > threshold
    else:
        result_is_yes = price < threshold

    # Cherche le token Yes ou No selon le résultat
    for i, outcome in enumerate(outcomes):
        o = outcome.lower()
        is_yes_token = o in ["yes", "true", "1", "up", "higher", "above"]
        is_no_token  = o in ["no", "false", "0", "down", "lower", "below"]

        if result_is_yes and is_yes_token:
            return token_ids[i], outcome, price, True
        if not result_is_yes and is_no_token:
            return token_ids[i], outcome, price, True

    # Si on ne trouve pas Yes/No explicite, prend le premier token si résultat Yes
    if result_is_yes and len(token_ids) > 0:
        return token_ids[0], outcomes[0], price, False
    if not result_is_yes and len(token_ids) > 1:
        return token_ids[1], outcomes[1], price, False

    return None

# ── CLOB ───────────────────────────────────────────────────────────────────────

def get_best_ask(token_id):
    try:
        r = SESSION.get(CLOB_API + "/book",
                        params={"token_id": token_id}, timeout=8)
        if r.ok:
            asks = r.json().get("asks", [])
            if asks:
                best = min(asks, key=lambda a: float(a["price"]))
                return float(best["price"]), float(best["size"])
        return None, 0
    except:
        return None, 0

def get_usdc_balance_clob():
    try:
        r = SESSION.get(CLOB_API + "/balance-allowance",
                        params={"asset_type": "USDC", "signature_type": "EOA"},
                        timeout=8)
        if r.ok:
            data = r.json()
            bal = data.get("balance") or data.get("allowance")
            if bal is not None:
                return float(bal) / 1_000_000
        return None
    except Exception as e:
        log.warning("Solde CLOB KO: " + str(e))
        return None

def get_usdc_balance():
    bal = get_usdc_balance_clob()
    if bal is not None:
        return bal
    try:
        r = SESSION.get("https://data-api.polymarket.com/value",
                        params={"user": WALLET}, timeout=8)
        if r.ok:
            data = r.json()
            if isinstance(data, list) and data:
                return float(data[0].get("value", 0))
            if isinstance(data, dict):
                return float(data.get("value", data.get("balance", 0)))
    except:
        pass
    return None

# ── EXECUTION ──────────────────────────────────────────────────────────────────

async def execute_buy_async(token_id, px, shares, market_key, question):
    """Achète le côté gagnant et attend le règlement."""
    global daily_pnl, reserved_balance
    try:
        from polymarket import AsyncSecureClient

        cout_total     = px * shares
        cout_avec_marge = cout_total * 1.05

        # Vérification solde avec lock
        with balance_lock:
            solde = get_usdc_balance()
            if solde is None:
                log.warning("[" + market_key + "] Solde indisponible — ABANDON")
                return False
            solde_dispo = solde - reserved_balance
            if solde_dispo < cout_avec_marge:
                log.warning("[" + market_key + "] Solde insuffisant ("
                            + str(round(solde_dispo, 2)) + "$ dispo, "
                            + str(round(cout_avec_marge, 2)) + "$ requis) — ABANDON")
                return False
            reserved_balance += cout_total
            log.info("[" + market_key + "] Solde OK: " + str(round(solde, 2))
                     + "$ | achat " + str(round(cout_total, 2)) + "$")

        try:
            async with await AsyncSecureClient.create(
                    private_key=PRIVATE_KEY, wallet=WALLET) as client:
                resp = await client.place_limit_order(
                    token_id=token_id, side="BUY",
                    price=str(round(px, 4)), size=str(shares))
                if resp.ok:
                    gain_prevu = (1.0 - px) * shares
                    log.info("[" + market_key + "] ✅ ACHAT | " + question[:50]
                             + " | px " + str(round(px, 4))
                             + " | gain prévu +" + str(round(gain_prevu, 2)) + "$")
                    return True
                else:
                    log.error("[" + market_key + "] Achat échoué")
                    return False
        finally:
            with balance_lock:
                reserved_balance = max(0.0, reserved_balance - cout_total)

    except Exception as e:
        log.error("Exception achat: " + str(e))
        with balance_lock:
            reserved_balance = max(0.0, reserved_balance - (px * shares))
        return False

def execute_buy(token_id, px, shares, market_key, question):
    return asyncio.run(execute_buy_async(token_id, px, shares, market_key, question))

# ── SETTLE ─────────────────────────────────────────────────────────────────────

def settle_loop():
    global daily_pnl
    log.info("Thread règlement démarré")
    while True:
        try:
            now = int(time.time())
            with trades_lock:
                trades_copy = list(open_trades)
            for t in trades_copy:
                if now >= t["expiry"] + 60:
                    gain = (1.0 - t["px"]) * t["shares"]
                    daily_pnl += gain
                    save_daily_pnl(daily_pnl)
                    log.info("[" + t["market"] + "] 💰 RÉGLÉ | +"
                             + str(round(gain, 2)) + "$ | PnL: "
                             + str(round(daily_pnl, 2)) + "$")
                    with trades_lock:
                        if t in open_trades:
                            open_trades.remove(t)
        except Exception as e:
            log.error("Erreur règlement: " + str(e))
        time.sleep(15)

# ── SCAN ───────────────────────────────────────────────────────────────────────

def scan_expiring_markets():
    """
    Scanne les marchés crypto expirant dans MAX_EXPIRY_MIN minutes.
    Retourne les opportunités où le résultat est connu et le prix < MAX_ENTRY_PRICE.
    """
    opps = []
    now  = time.time()

    try:
        r = SESSION.get(GAMMA_API + "/markets",
                        params={"active": "true", "closed": "false",
                                "order": "endDate", "ascending": "true",
                                "limit": 200}, timeout=15)
        if not r.ok:
            return opps

        for m in r.json():
            try:
                question = m.get("question", "")
                slug     = m.get("slug", "")

                if slug in done_slugs:
                    continue

                # Filtre expiration
                end = m.get("endDate") or m.get("end_date_iso")
                if not end:
                    continue
                end_ts    = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                mins_left = (end_ts - now) / 60

                if mins_left > MAX_EXPIRY_MIN or mins_left < MIN_EXPIRY_MIN:
                    continue

                # Marchés binaires uniquement
                outcomes  = json.loads(m.get("outcomes", "[]")) \
                    if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                token_ids = json.loads(m.get("clobTokenIds", "[]")) \
                    if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
                if len(outcomes) != 2 or len(token_ids) != 2:
                    continue

                # Détecte crypto + seuil
                parsed = detect_crypto_and_threshold(question)
                if not parsed:
                    continue
                coin_id, threshold, direction = parsed

                # Détermine le côté gagnant
                winner = determine_winning_outcome(
                    coin_id, threshold, direction, outcomes, token_ids)
                if not winner:
                    continue

                win_token, win_outcome, current_price, high_confidence = winner

                # Lit le prix du côté gagnant
                ask, depth = get_best_ask(win_token)
                if ask is None:
                    continue

                # N'entre que si le prix est encore sous MAX_ENTRY_PRICE
                if ask < MIN_ENTRY_PRICE or ask > MAX_ENTRY_PRICE:
                    continue

                # Marge de sécurité : ignore si peu confiant et prix proche du seuil
                margin_pct = abs(current_price - threshold) / threshold * 100
                if not high_confidence and margin_pct < 2:
                    log.info("[" + slug[:20] + "] Trop proche du seuil ("
                             + str(round(margin_pct, 1)) + "%) — skip")
                    continue

                gain_prevu = (1.0 - ask) * (TRADE_USDC / ask)
                opps.append({
                    "question":      question[:70],
                    "slug":          slug,
                    "token_id":      win_token,
                    "outcome":       win_outcome,
                    "ask":           ask,
                    "depth":         depth,
                    "coin_id":       coin_id,
                    "threshold":     threshold,
                    "direction":     direction,
                    "current_price": current_price,
                    "margin_pct":    margin_pct,
                    "mins_left":     mins_left,
                    "expiry_ts":     int(end_ts),
                    "gain_prevu":    gain_prevu,
                    "high_confidence": high_confidence,
                })
                log.info("🎯 OPPORTUNITÉ LIQ | " + question[:60]
                         + " | " + coin_id + " @ $" + str(int(current_price))
                         + " vs seuil $" + str(int(threshold))
                         + " (" + str(round(margin_pct, 1)) + "% de marge)"
                         + " | côté gagnant: " + win_outcome
                         + " @ " + str(ask)
                         + " | gain prévu +" + str(round(gain_prevu, 2)) + "$"
                         + " | expire dans " + str(round(mins_left, 0)) + " min")

            except Exception as e:
                log.warning("Marché KO: " + str(e))
                continue

    except Exception as e:
        log.error("Scan KO: " + str(e))

    # Trie par gain prévu décroissant
    opps.sort(key=lambda x: x["gain_prevu"], reverse=True)
    return opps

# ── BOUCLE PRINCIPALE ──────────────────────────────────────────────────────────

def run():
    global daily_pnl, pnl_date, done_slugs

    log.info("Bot ARB LIQUIDATION CRYPTO v1 démarré")
    log.info("Fenêtre: marchés expirant dans " + str(int(MAX_EXPIRY_MIN)) + " min"
             + " | Entry: " + str(MIN_ENTRY_PRICE) + "-" + str(MAX_ENTRY_PRICE)
             + " | Trade: " + str(TRADE_USDC) + "$"
             + " | PnL: " + str(round(daily_pnl, 2)) + "$")

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("WALLET manquant!")
        return

    settle_thread = threading.Thread(target=settle_loop, daemon=True)
    settle_thread.start()

    last_heartbeat = 0

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl  = 0.0
                pnl_date   = today
                done_slugs = set()
                save_daily_pnl(0.0)

            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss atteint - pause 1h")
                time.sleep(3600)
                continue

            with trades_lock:
                n_open = len(open_trades)
            if n_open >= MAX_CONCURRENT:
                time.sleep(SCAN_INTERVAL)
                continue

            # Heartbeat
            if time.time() - last_heartbeat >= 300:
                prices = get_all_prices()
                price_str = " | ".join([
                    k.upper()[:3] + ": $" + str(int(v))
                    for k, v in list(prices.items())[:4]
                ])
                log.info("Scan actif | " + price_str
                         + " | Trades ouverts: " + str(n_open)
                         + " | PnL: " + str(round(daily_pnl, 2)) + "$")
                last_heartbeat = time.time()

            opps = scan_expiring_markets()

            for opp in opps:
                with trades_lock:
                    if len(open_trades) >= MAX_CONCURRENT:
                        break

                shares_cap   = math.floor(TRADE_USDC / opp["ask"] * 100) / 100
                shares_depth = math.floor(opp["depth"] * 100) / 100
                shares       = min(shares_cap, shares_depth)
                if shares < 1:
                    log.info("Profondeur insuffisante — skip")
                    continue

                ok = execute_buy(opp["token_id"], opp["ask"], shares,
                                 opp["slug"][:10], opp["question"])
                done_slugs.add(opp["slug"])

                if ok:
                    with trades_lock:
                        open_trades.append({
                            "market":   opp["slug"][:15],
                            "question": opp["question"],
                            "px":       opp["ask"],
                            "shares":   shares,
                            "expiry":   opp["expiry_ts"],
                        })

        except Exception as e:
            log.error("Erreur boucle: " + str(e))

        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
