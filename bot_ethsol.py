import os, time, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

# ============================================================
# BOT POLYMARKET - ETH + SOL - 15 MINUTES
# Même stratégie que BTC 5min, adaptée pour ETH et SOL 15min
# Source prix : Coinbase (fiable, non bloquée)
# Fenêtre : 15 minutes (900 secondes)
# Gap ETH : 8$ | Gap SOL : 3$
# ============================================================

PRIVATE_KEY  = os.environ.get("PRIVATE_KEY", "")
WALLET       = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

# Stop loss / take profit journaliers (communs aux deux marchés)
STOP_LOSS_USDC    = float(os.getenv("ETHSOL_STOP_LOSS_USDC",    "15"))
DAILY_TAKE_PROFIT = float(os.getenv("ETHSOL_DAILY_TAKE_PROFIT", "60"))
MAX_OPEN_USDC     = float(os.getenv("ETHSOL_MAX_OPEN_USDC",     "20"))

# Seuils de sortie par position
STOP_LOSS_PCT     = float(os.getenv("ETHSOL_STOP_LOSS_PCT",     "0.30"))  # -30% du prix d'entree
TAKE_PROFIT_GAIN  = float(os.getenv("ETHSOL_TAKE_PROFIT_GAIN", "0.40"))
TAKE_PROFIT_PCT   = float(os.getenv("ETHSOL_TAKE_PROFIT_PCT",  "0.30"))  # +30% valeur
STOP_LOSS_VAL_PCT = float(os.getenv("ETHSOL_STOP_LOSS_VAL_PCT","0.25"))  # -25% valeur
EXIT_REVERSAL     = float(os.getenv("ETHSOL_EXIT_REVERSAL",     "0.10"))

# Surveillance
MONITOR_INTERVAL  = float(os.getenv("ETHSOL_MONITOR_INTERVAL",  "10"))

# Mise FIXE unique (5 ou 10, pas de variation)
BET_SIZE = float(os.getenv("ETHSOL_BET_SIZE", "5"))

# Circuit breaker
MAX_CONSECUTIVE_LOSSES = int(os.getenv("ETHSOL_MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE  = int(os.getenv("ETHSOL_CIRCUIT_BREAKER_PAUSE",  "7200"))

# Fenêtre 15 minutes
WINDOW_SIZE      = 900   # secondes
ENTRY_WINDOW_MAX = 180   # 3 premières minutes pour entrer
FORCE_EXIT_SEC   = 720   # surveillance forcee : 3 dernieres minutes

# Plage horaire 7h-22h UTC
ACTIVE_HOURS = list(range(7, 22))

# Configuration des deux marchés
MARKETS = {
    "ETH": {
        "slug":     "eth-updown-15m-",
        "coinbase": "https://api.coinbase.com/v2/prices/ETH-USD/spot",
        "gap":      float(os.getenv("ETH_GAP_MIN", "8")),
        "gap_pct":  float(os.getenv("ETH_GAP_PCT", "0.09")),
        "dyn_factor": float(os.getenv("DYN_VOL_FACTOR", "0.5")),
        "dyn_min":    float(os.getenv("ETH_DYN_GAP_MIN", "3")),
        "dyn_max":    float(os.getenv("ETH_DYN_GAP_MAX", "50")),
        "deviation": float(os.getenv("ETH_DEVIATION", "50")),
        "entry_min": float(os.getenv("ETH_ENTRY_PRICE_MIN", "0.59")),
        "entry_max": float(os.getenv("ETH_ENTRY_PRICE_MAX", "0.73")),
    },
    "SOL": {
        "slug":     "sol-updown-15m-",
        "coinbase": "https://api.coinbase.com/v2/prices/SOL-USD/spot",
        "gap":      float(os.getenv("SOL_GAP_MIN", "3")),
        "gap_pct":  float(os.getenv("SOL_GAP_PCT", "0.09")),
        "dyn_factor": float(os.getenv("DYN_VOL_FACTOR", "0.5")),
        "dyn_min":    float(os.getenv("SOL_DYN_GAP_MIN", "0.3")),
        "dyn_max":    float(os.getenv("SOL_DYN_GAP_MAX", "5")),
        "deviation": float(os.getenv("SOL_DEVIATION", "10")),
        "entry_min": float(os.getenv("SOL_ENTRY_PRICE_MIN", "0.59")),
        "entry_max": float(os.getenv("SOL_ENTRY_PRICE_MAX", "0.73")),
    }
}

# APIs Polymarket
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# Fichiers mémoire persistante (un jeu par marché)
FILES = {
    "ETH": {
        "windows": "/app/eth_traded_windows.txt",
        "pnl":     "/app/eth_daily_pnl.txt",
        "strikes": "/app/eth_strikes.txt",
    },
    "SOL": {
        "windows": "/app/sol_traded_windows.txt",
        "pnl":     "/app/sol_daily_pnl.txt",
        "strikes": "/app/sol_strikes.txt",
    }
}

import sys
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("bot_ethsol")

# ============================================================
# MEMOIRE PERSISTANTE (par marché)
# ============================================================

def load_daily_pnl(market):
    try:
        f = FILES[market]["pnl"]
        if os.path.exists(f):
            with open(f, "r") as fp:
                lines = fp.read().strip().split("\n")
                if len(lines) == 2 and lines[0] == str(date.today()):
                    return float(lines[1])
        return 0.0
    except:
        return 0.0


PNL_HISTORY_FILES = {
    "ETH": "/app/eth_pnl_history.txt",
    "SOL": "/app/sol_pnl_history.txt",
}

def append_pnl_history(market, day, pnl):
    try:
        fp = PNL_HISTORY_FILES.get(market, "/app/" + market + "_pnl_history.txt")
        lines = []
        if os.path.exists(fp):
            with open(fp, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
        lines = [l for l in lines if not l.startswith(str(day))]
        lines.append(str(day) + " | " + str(round(pnl, 4)) + "$")
        lines = lines[-14:]
        with open(fp, "w") as f:
            f.write("\n".join(lines) + "\n")
        total = sum(float(l.split("|")[1].replace("$","").strip()) for l in lines)
        log.info("[" + market + "] PnL cumule 14j: " + str(round(total, 2)) + "$")
    except Exception as e:
        log.warning("[" + market + "] Historique PnL: " + str(e))

def save_daily_pnl(market, pnl):
    try:
        with open(FILES[market]["pnl"], "w") as fp:
            fp.write(str(date.today()) + "\n" + str(round(pnl, 4)))
    except:
        pass

def load_traded_windows(market):
    try:
        f = FILES[market]["windows"]
        if os.path.exists(f):
            with open(f, "r") as fp:
                lines = fp.read().strip().split("\n")
                today = str(date.today())
                w = set()
                for line in lines:
                    if line.strip() and line.startswith(today):
                        parts = line.strip().split(",")
                        if len(parts) == 2:
                            w.add(int(parts[1]))
                return w
        return set()
    except:
        return set()

def save_traded_window(market, window_ts):
    try:
        with open(FILES[market]["windows"], "a") as fp:
            fp.write(str(date.today()) + "," + str(window_ts) + "\n")
    except:
        pass

def load_strikes(market):
    s = {}
    try:
        f = FILES[market]["strikes"]
        if os.path.exists(f):
            with open(f, "r") as fp:
                for line in fp:
                    parts = line.strip().split(",")
                    if len(parts) == 2:
                        s[int(parts[0])] = float(parts[1])
    except:
        pass
    return s


def get_dynamic_gap_seuil_market(market, price_now):
    """Calcule le seuil dynamique pour ETH ou SOL."""
    cfg = MARKETS[market]
    hist = state[market]["gap_history"]
    if len(hist) < 3:
        return price_now * cfg.get("gap_pct", 0.09) / 100
    vol_moy = sum(hist) / len(hist)
    seuil   = vol_moy * cfg["dyn_factor"]
    seuil   = max(cfg["dyn_min"], min(cfg["dyn_max"], seuil))
    return seuil

def save_strike(market, window_ts, price):
    try:
        with open(FILES[market]["strikes"], "a") as fp:
            fp.write(str(window_ts) + "," + str(price) + "\n")
    except:
        pass

# ============================================================
# ETAT GLOBAL PAR MARCHE
# ============================================================

state = {
    m: {
        "daily_pnl":          load_daily_pnl(m),
        "pnl_date":           date.today(),
        "traded_windows":     load_traded_windows(m),
        "strikes":            load_strikes(m),
                "gap_history":        [],   # historique volatilite reelle
        "open_positions":     [],
        "consecutive_losses": 0,
        "circuit_breaker_until": 0,
    }
    for m in MARKETS
}

positions_lock = threading.Lock()

def open_val(market):
    with positions_lock:
        return sum(p["size"] for p in state[market]["open_positions"])

# ============================================================
# DONNEES MARCHE
# ============================================================

def get_price(market):
    try:
        r = requests.get(MARKETS[market]["coinbase"], timeout=10)
        if r.ok:
            return float(r.json()["data"]["amount"])
        return 0
    except:
        return 0

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price",
                         params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0


def get_best_ask(token_id):
    """Vrai prix d'ACHAT disponible (carnet d'ordres)."""
    try:
        r = requests.get(CLOB_API + "/book",
                         params={"token_id": token_id}, timeout=8)
        if r.ok:
            asks = r.json().get("asks", [])
            if asks:
                return float(min(asks, key=lambda a: float(a["price"]))["price"])
        return None
    except:
        return None

def get_best_bid(token_id):
    """Vrai prix de VENTE disponible (carnet d'ordres)."""
    try:
        r = requests.get(CLOB_API + "/book",
                         params={"token_id": token_id}, timeout=8)
        if r.ok:
            bids = r.json().get("bids", [])
            if bids:
                return float(max(bids, key=lambda b: float(b["price"]))["price"])
        return None
    except:
        return None

def get_market(market, window_ts):
    try:
        slug = MARKETS[market]["slug"] + str(window_ts)
        r = requests.get(GAMMA_API + "/markets",
                         params={"slug": slug}, timeout=10)
        if r.ok:
            data = r.json()
            m = data[0] if isinstance(data, list) and len(data) > 0 else None
            if m and m.get("slug") == slug:
                outcomes  = json.loads(m.get("outcomes", "[]")) \
                    if isinstance(m.get("outcomes"), str) \
                    else m.get("outcomes", [])
                token_ids = json.loads(m.get("clobTokenIds", "[]")) \
                    if isinstance(m.get("clobTokenIds"), str) \
                    else m.get("clobTokenIds", [])
                m["tokens"] = [
                    {"outcome": outcomes[i], "token_id": token_ids[i]}
                    for i in range(len(outcomes))
                ]
                return m
        return None
    except:
        return None

# ============================================================
# CALCUL DE LA MISE
# ============================================================

def calculate_bet_size(price):
    return 3.0  # mise fixe 3$ validee

# ============================================================
# ORDRES POLYMARKET
# ============================================================

async def sell_order_async(token_id, shares, reason, price):
    try:
        from polymarket import AsyncSecureClient
        # Arrondi conservateur pour eviter l'erreur "not enough balance"
        shares_safe = max(0.01, round(shares - 0.01, 2))
        # CORRECTION : vente agressive 0.04 SOUS le prix affiche
        # pour maximiser la chance d'execution immediate
        sell_px = max(0.01, round(price - 0.04, 4))
        async with await AsyncSecureClient.create(
                private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="SELL",
                price=str(sell_px), size=str(shares_safe))
            if response.ok:
                log.info("VENTE " + reason + " @ " + str(round(price, 2)))
                return True
            log.error("Erreur vente: " + str(response.message))
            return False
    except Exception as e:
        log.error("Exception vente: " + str(e))
        return False

def sell_order(token_id, shares, reason, price):
    return asyncio.run(sell_order_async(token_id, shares, reason, price))

async def place_order_async(token_id, outcome, price,
                             bet_size, crypto_entry, market):
    try:
        from polymarket import AsyncSecureClient
        # Achat agressif : +0.02 au-dessus du prix affiche pour
        # maximiser l'execution immediate. entry enregistre = prix paye.
        buy_px = min(0.99, round(price + 0.02, 4))
        shares = math.floor(bet_size / buy_px * 100) / 100
        async with await AsyncSecureClient.create(
                private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="BUY",
                price=str(buy_px), size=str(shares))
            if not response.ok:
                log.error("[" + market + "] Erreur ordre: "
                          + str(response.code) + " " + str(response.message))
                return False

            order_id = getattr(response, "order_id", None) \
                or getattr(response, "id", None)

            # VERIFICATION D'EXECUTION : attendre 12s puis verifier
            await asyncio.sleep(12)
            executed = True
            try:
                open_orders = None
                for meth in ("get_orders", "get_open_orders"):
                    fn = getattr(client, meth, None)
                    if fn:
                        open_orders = await fn()
                        break
                if open_orders is not None and order_id:
                    ids = []
                    items = getattr(open_orders, "orders", open_orders)
                    if isinstance(items, list):
                        for o in items:
                            oid = o.get("id") if isinstance(o, dict) \
                                else getattr(o, "id", None)
                            ids.append(oid)
                    if order_id in ids:
                        executed = False
                        try:
                            cancel = getattr(client, "cancel_order", None)
                            if cancel:
                                await cancel(order_id=order_id)
                                log.warning("[" + market + "] ACHAT NON EXECUTE"
                                            + " - annule, pas de position")
                        except Exception as ce:
                            log.warning("[" + market + "] Annulation: " + str(ce))
            except Exception as ve:
                log.warning("[" + market + "] Verif achat impossible ("
                            + str(ve) + ") - position supposee executee")

            if not executed:
                return False

            log.info("[" + market + "] TRADE " + outcome
                     + " " + str(bet_size) + " USDC @ "
                     + str(buy_px)
                     + " | " + market + ": " + str(round(crypto_entry, 2)))
            with positions_lock:
                state[market]["open_positions"].append({
                    "token_id":      token_id,
                    "entry_price":   buy_px,
                    "shares":        shares,
                    "size":          bet_size,
                    "outcome":       outcome,
                    "crypto_entry":  crypto_entry,
                    "side":          outcome,
                    "peak_price":    buy_px,
                    "window_ts":     (int(time.time())
                                      - int(time.time()) % WINDOW_SIZE),
                    "zero_count":    0,
                    "market":        market,
                })
            return True
    except Exception as e:
        log.error("[" + market + "] Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, bet_size, crypto_entry, market):
    return asyncio.run(place_order_async(token_id, outcome, price,
                                          bet_size, crypto_entry, market))

# ============================================================
# CIRCUIT BREAKER
# ============================================================

def record_loss(market):
    s = state[market]
    s["consecutive_losses"] += 1
    log.info("[" + market + "] Pertes consecutives: "
             + str(s["consecutive_losses"])
             + "/" + str(MAX_CONSECUTIVE_LOSSES))
    if s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        s["circuit_breaker_until"] = time.time() + CIRCUIT_BREAKER_PAUSE
        log.warning("[" + market + "] CIRCUIT BREAKER! Pause 2h")
        s["consecutive_losses"] = 0

# ============================================================
# THREAD DE SURVEILLANCE DES POSITIONS
# ============================================================

def monitor_loop():
    log.info("Thread surveillance ETH+SOL demarre")
    while True:
        try:
            for market in MARKETS:
                s = state[market]
                with positions_lock:
                    positions_copy = [p for p in s["open_positions"] if not p.get("sold")]
                if not positions_copy:
                    continue

                crypto_current = get_price(market)
                to_remove      = []

                for pos in positions_copy:
                    token_id = pos["token_id"]
                    # Prix vendable REEL (best bid), fallback dernier prix
                    current  = get_best_bid(token_id)
                    if current is None:
                        current = get_token_price(token_id)
                    side     = pos.get("side", "Up")
                    entry    = pos["entry_price"]
                    shares   = pos["shares"]
                    peak     = pos.get("peak_price", entry)

                    if current <= 0:
                        pos["zero_count"] = pos.get("zero_count", 0) + 1
                        if pos["zero_count"] >= 3:
                            # CORRECTION CRITIQUE : marche expire sans vente
                            # = PERTE TOTALE comptee dans le PnL
                            pnl = (0 - entry) * shares
                            s["daily_pnl"] += pnl
                            save_daily_pnl(market, s["daily_pnl"])
                            record_loss(market)
                            log.warning("[" + market + "] EXPIRATION SANS VENTE"
                                        + " - PERTE TOTALE " + str(round(pnl, 2))
                                        + " | Total: "
                                        + str(round(s["daily_pnl"], 2)))
                            to_remove.append((market, pos))
                            pos["sold"] = True  # evite double vente
                        continue
                    pos["zero_count"] = 0

                    if current > peak:
                        pos["peak_price"] = current
                        peak = current

                    log.info("[" + market + "] " + side
                             + " | Token: " + str(round(current, 3))
                             + " | Peak: "  + str(round(peak, 3))
                             + " | PnL: "   + str(round(s["daily_pnl"], 2)))

                    # 1. Marché expiré en perte
                    if current <= 0.02:
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        s["daily_pnl"] += pnl
                        save_daily_pnl(market, s["daily_pnl"])
                        record_loss(market)
                        log.info("[" + market + "] Expire PERTE "
                                 + str(round(pnl, 2)))
                        to_remove.append((market, pos))
                        pos["sold"] = True  # evite double vente

                    # 2. Marché expiré en gain
                    elif current >= 0.98:
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        s["daily_pnl"] += pnl
                        save_daily_pnl(market, s["daily_pnl"])
                        s["consecutive_losses"] = 0
                        log.info("[" + market + "] Expire GAIN +"
                                 + str(round(pnl, 2))
                                 + " | Total: " + str(round(s["daily_pnl"], 2)))
                        to_remove.append((market, pos))
                        pos["sold"] = True  # evite double vente

                    # 3. TAKE PROFIT : valeur position >= mise * (1 + 30%)
                    elif current * shares >= pos["size"] * (1 + TAKE_PROFIT_PCT):
                        val = round(current * shares, 2)
                        if sell_order(token_id, shares, "TP", current):
                            pnl = (max(0.01, current - 0.04) - entry) * shares
                            s["daily_pnl"] += pnl
                            save_daily_pnl(market, s["daily_pnl"])
                            s["consecutive_losses"] = 0
                            log.info("[" + market + "] TP +30% valeur " + str(val)
                                     + "$ | +" + str(round(pnl, 2))
                                     + " | Total: " + str(round(s["daily_pnl"], 2)))
                            to_remove.append((market, pos))
                            pos["sold"] = True

                    # 4. Stop loss : valeur position <= mise * (1 - 25%)
                    elif current * shares <= pos["size"] * (1 - STOP_LOSS_VAL_PCT):
                        val = round(current * shares, 2)
                        log.info("[" + market + "] STOP LOSS -25%! valeur "
                                 + str(val) + "$")
                        if sell_order(token_id, shares, "SL", current):
                            pnl = (max(0.01, current - 0.04) - entry) * shares
                            s["daily_pnl"] += pnl
                            save_daily_pnl(market, s["daily_pnl"])
                            record_loss(market)
                            to_remove.append((market, pos))
                            pos["sold"] = True  # evite double vente

                    # 6. 3 DERNIERES MINUTES : si en perte -> vente forcee SYSTEMATIQUE
                    elif (pos.get("window_ts", 0) > 0
                          and (int(time.time()) - pos["window_ts"]) >= FORCE_EXIT_SEC
                          and current < entry):
                        log.info("[" + market + "] VENTE FORCEE fin de fenetre"
                                 + " (en perte) @ " + str(round(current, 3)))
                        if sell_order(token_id, shares, "FORCE_FIN", current):
                            pnl = (max(0.01, current - 0.04) - entry) * shares
                            s["daily_pnl"] += pnl
                            save_daily_pnl(market, s["daily_pnl"])
                            record_loss(market)
                            log.info("[" + market + "] FORCE_FIN "
                                     + str(round(pnl, 2)))
                            to_remove.append((market, pos))
                            pos["sold"] = True  # evite double vente

                # Nettoyage des positions soldées
                if to_remove:
                    with positions_lock:
                        for (m, pos) in to_remove:
                            if pos in state[m]["open_positions"]:
                                state[m]["open_positions"].remove(pos)

        except Exception as e:
            log.error("Erreur monitor: " + str(e))

        time.sleep(MONITOR_INTERVAL)

# ============================================================
# BOUCLE PRINCIPALE
# ============================================================

def run():
    log.info("Bot Polymarket ETH+SOL 15min demarre")
    for market, cfg in MARKETS.items():
        log.info("[" + market + "] Gap: " + str(cfg["gap"])
                 + "$ | Entree: " + str(cfg["entry_min"])
                 + "-" + str(cfg["entry_max"])
                 + " | PnL: " + str(round(state[market]["daily_pnl"], 2)))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("POLYMARKET_WALLET_ADDRESS manquante!")
        return

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    while True:
        try:
            now               = int(time.time())
            seconds_in_window = now % WINDOW_SIZE
            window_ts         = now - seconds_in_window
            hour_utc          = datetime.now(timezone.utc).hour

            for market in MARKETS:
                s   = state[market]
                cfg = MARKETS[market]

                # Reset journalier
                today = date.today()
                if today != s["pnl_date"]:
                    log.info("[" + market + "] Nouveau jour - PnL hier: "
                             + str(round(s["daily_pnl"], 2)))
                    s["daily_pnl"]          = 0.0
                    s["pnl_date"]           = today
                    s["traded_windows"]     = set()
                    s["consecutive_losses"] = 0
                    save_daily_pnl(market, 0.0)

                # Vérifications globales
                if s["daily_pnl"] <= -STOP_LOSS_USDC:
                    log.warning("[" + market + "] Stop-loss journalier!")
                    continue
                if s["daily_pnl"] >= DAILY_TAKE_PROFIT:
                    log.info("[" + market + "] Take profit journalier!")
                    continue
                if time.time() < s["circuit_breaker_until"]:
                    reste = int(s["circuit_breaker_until"] - time.time())
                    log.warning("[" + market + "] Circuit breaker - "
                                + str(reste // 60) + " min")
                    continue
                if hour_utc not in ACTIVE_HOURS:
                    continue

                # Capture du strike dans les 5 premières secondes
                if window_ts not in s["strikes"] and seconds_in_window <= 5:
                    price_strike = get_price(market)
                    if price_strike > 0:
                        s["strikes"][window_ts] = price_strike
                        save_strike(market, window_ts, price_strike)
                        log.info("[" + market + "] Strike capture: "
                                 + str(round(price_strike, 2)))

                # Logique d'entrée
                if (window_ts not in s["traded_windows"]
                        and window_ts in s["strikes"]
                        and seconds_in_window <= ENTRY_WINDOW_MAX
                        and len(s["open_positions"]) == 0):

                    strike      = s["strikes"][window_ts]
                    price_now   = get_price(market)
                    gap         = price_now - strike
                    gap_abs     = abs(gap)
                    # Enregistre dans l'historique de volatilite
                    s["gap_history"].append(gap_abs)
                    s["gap_history"] = s["gap_history"][-10:]
                    gap_seuil   = get_dynamic_gap_seuil_market(market, price_now)
                    if gap_abs >= gap_seuil:
                        target_outcome = "Up" if gap > 0 else "Down"
                        log.info("[" + market + "] Gap "
                                 + str(round(gap, 2)) + "$ | "
                                 + target_outcome + " | "
                                 + market + ": " + str(round(price_now, 2))
                                 + " vs strike " + str(round(strike, 2)))

                        mkt = get_market(market, window_ts)
                        if mkt:
                            tokens = mkt.get("tokens", [])
                            target = next(
                                (t for t in tokens
                                 if t["outcome"] == target_outcome), None)
                            if target:
                                # Vrai prix d'achat (best ask), fallback
                                token_price = get_best_ask(target["token_id"])
                                if token_price is None:
                                    token_price = get_token_price(target["token_id"])
                                log.info("[" + market + "] "
                                         + target_outcome + " token @ "
                                         + str(round(token_price, 3)))

                                if cfg["entry_min"] <= token_price <= cfg["entry_max"]:
                                    bet_size = calculate_bet_size(token_price)
                                    log.info("[" + market + "] ENTREE VALIDEE | "
                                             + target_outcome + " @ "
                                             + str(round(token_price, 3))
                                             + " | Mise: " + str(bet_size) + " USDC")
                                    if place_order(target["token_id"],
                                                   target_outcome,
                                                   token_price, bet_size,
                                                   price_now, market):
                                        s["traded_windows"].add(window_ts)
                                        save_traded_window(market, window_ts)

                                elif token_price > cfg["entry_max"]:
                                    log.info("[" + market + "] Token trop cher ("
                                             + str(round(token_price, 3)) + ") - skip")
                                else:
                                    log.info("[" + market + "] Token trop bas ("
                                             + str(round(token_price, 3)) + ") - skip")
                    else:
                        log.info("[" + market + "] Gap insuffisant ("
                                 + str(round(abs(gap), 2)) + "$ < "
                                 + str(round(gap_seuil, 2)) + "$)")

                # Nettoyage mémoire strikes
                if len(s["strikes"]) > 50:
                    for k in sorted(s["strikes"].keys())[:-50]:
                        del s["strikes"][k]

        except Exception as e:
            log.error("Erreur boucle: " + str(e))

        time.sleep(5)

if __name__ == "__main__":
    run()
