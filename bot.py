import os, time, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

# ============================================================
# BOT POLYMARKET - STRATEGIE PRIX A BATTRE v9
# Source BTC : Coinbase (fiable depuis Railway/VPS)
# Logique : capture le prix BTC au debut de chaque fenetre 5min,
#           entre si le BTC s'ecarte suffisamment (gap), 
#           dans la 1ere minute, token entre 0.52 et 0.65 seulement.
# ============================================================

PRIVATE_KEY  = os.environ.get("PRIVATE_KEY", "")
WALLET       = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

# Stop loss / take profit journaliers
STOP_LOSS_USDC   = float(os.getenv("STOP_LOSS_USDC",   "15"))
DAILY_TAKE_PROFIT= float(os.getenv("DAILY_TAKE_PROFIT","60"))
MAX_OPEN_USDC    = float(os.getenv("MAX_OPEN_USDC",    "15"))

# Seuils de sortie par position
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",    "0.30"))  # -30% du prix d'entree
TAKE_PROFIT_GAIN = float(os.getenv("TAKE_PROFIT_GAIN","0.40"))
EXIT_REVERSAL    = float(os.getenv("EXIT_REVERSAL",    "0.10"))
BTC_DEVIATION    = float(os.getenv("BTC_DEVIATION",    "150"))

# Surveillance des positions
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "10"))

# Mise FIXE unique (5 ou 10, pas de variation)
BET_SIZE         = float(os.getenv("BET_SIZE", "5"))

# Circuit breaker
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE  = int(os.getenv("CIRCUIT_BREAKER_PAUSE",  "7200"))

# Strategie prix a battre
# Gap reduit a 50$ (au lieu de 30$) : 30$ c'est du bruit sur BTC,
# 50$ donne un signal plus fiable sur 5 minutes
# Gap DYNAMIQUE base sur la volatilite reelle des dernieres fenetres
# Nouvelles variables (n'entrent pas en conflit avec GAP_PCT/GAP_MIN existantes)
DYN_VOL_WINDOWS  = int(float(os.getenv("DYN_VOL_WINDOWS",  "10")))   # nb fenetres historiques
DYN_VOL_FACTOR   = float(os.getenv("DYN_VOL_FACTOR",   "0.5"))       # seuil = VOL_FACTOR * volatilite_moy
DYN_GAP_MIN      = float(os.getenv("DYN_GAP_MIN",      "20"))        # plancher absolu en $
DYN_GAP_MAX      = float(os.getenv("DYN_GAP_MAX",      "200"))       # plafond absolu en $
# Garde les anciennes variables pour compatibilite Railway (non utilisees si DYN actif)
GAP_MIN          = float(os.getenv("GAP_MIN",          "50"))
GAP_PCT          = float(os.getenv("GAP_PCT",          "0.09"))

# Fourchette d'entree resserree : on plafonne a 0.65 (au lieu de 0.75)
# Acheter a 0.75 = gain max limité, perte max totale → mauvais rapport
# Entre 0.52 et 0.65, le rapport risque/gain est plus equilibre
ENTRY_PRICE_MIN  = float(os.getenv("ENTRY_PRICE_MIN",  "0.52"))
ENTRY_PRICE_MAX  = float(os.getenv("ENTRY_PRICE_MAX",  "0.68"))

# Entree uniquement dans la 1ere minute (60s) -- garde
ENTRY_WINDOW_MAX = int(os.getenv("ENTRY_WINDOW_MAX",   "60"))

# Plage horaire : 7h-14h (UTC = heure Abidjan)
# On garde la plage eprouvee, pas 7h-23h qui dilue les signaux
ACTIVE_HOURS = list(range(7, 22))

# APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# Coinbase (fiable depuis Railway et VPS, pas bloquee comme Binance)
COINBASE_SPOT    = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

# Fichiers memoire persistante
WINDOWS_FILE = "/app/traded_windows.txt"
PNL_FILE     = "/app/daily_pnl.txt"
STRIKE_FILE  = "/app/strikes.txt"

import sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("bot")

# ============================================================
# MEMOIRE PERSISTANTE
# ============================================================

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


PNL_HISTORY_FILE = "/app/daily_pnl_history.txt"

def append_pnl_history(day, pnl):
    try:
        lines = []
        if os.path.exists(PNL_HISTORY_FILE):
            with open(PNL_HISTORY_FILE, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
        lines = [l for l in lines if not l.startswith(str(day))]
        lines.append(str(day) + " | " + str(round(pnl, 4)) + "$")
        lines = lines[-14:]
        with open(PNL_HISTORY_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
        # Affiche le total cumule
        total = sum(float(l.split("|")[1].replace("$","").strip()) for l in lines)
        log.info("PnL cumule 14j: " + str(round(total, 2)) + "$")
    except Exception as e:
        log.warning("Historique PnL: " + str(e))

def save_daily_pnl(pnl):
    try:
        with open(PNL_FILE, "w") as f:
            f.write(str(date.today()) + "\n" + str(round(pnl, 4)))
    except:
        pass

def load_traded_windows():
    try:
        if os.path.exists(WINDOWS_FILE):
            with open(WINDOWS_FILE, "r") as f:
                lines = f.read().strip().split("\n")
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

def save_traded_window(window_ts):
    try:
        with open(WINDOWS_FILE, "a") as f:
            f.write(str(date.today()) + "," + str(window_ts) + "\n")
    except:
        pass

strikes        = {}
gap_history    = []   # historique des gaps absolus des dernieres fenetres

def add_gap_history(gap_abs):
    """Ajoute un gap observe a l'historique et calcule le seuil dynamique."""
    global gap_history
    gap_history.append(gap_abs)
    gap_history = gap_history[-DYN_VOL_WINDOWS:]  # garde les N dernieres

def get_dynamic_gap_seuil(btc_now):
    """Calcule le seuil de gap dynamique base sur la volatilite recente."""
    if len(gap_history) < 3:
        # Pas assez d'historique : utilise 0.09% du prix
        return btc_now * 0.09 / 100
    vol_moy = sum(gap_history) / len(gap_history)
    seuil   = vol_moy * DYN_VOL_FACTOR
    seuil   = max(DYN_GAP_MIN, min(DYN_GAP_MAX, seuil))
    return seuil

def load_strikes():
    try:
        if os.path.exists(STRIKE_FILE):
            with open(STRIKE_FILE, "r") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) == 2:
                        strikes[int(parts[0])] = float(parts[1])
    except:
        pass

def save_strike(window_ts, price):
    try:
        with open(STRIKE_FILE, "a") as f:
            f.write(str(window_ts) + "," + str(price) + "\n")
    except:
        pass

# ============================================================
# ETAT GLOBAL
# ============================================================

daily_pnl          = load_daily_pnl()
pnl_date           = date.today()
traded_windows     = load_traded_windows()
open_positions     = []
positions_lock     = threading.Lock()
consecutive_losses = 0
circuit_breaker_until = 0

def check_stop_loss():
    return daily_pnl <= -STOP_LOSS_USDC

def check_take_profit():
    return daily_pnl >= DAILY_TAKE_PROFIT

def check_circuit_breaker():
    return time.time() < circuit_breaker_until

def open_val():
    with positions_lock:
        return sum(p["size"] for p in open_positions)

# ============================================================
# DONNEES MARCHE
# ============================================================

def get_btc_price():
    """Prix BTC en direct via Coinbase (fiable, non bloque)."""
    try:
        r = requests.get(COINBASE_SPOT, timeout=10)
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

def get_btc_market(window_ts):
    try:
        slug = "btc-updown-5m-" + str(window_ts)
        r = requests.get(GAMMA_API + "/markets",
                         params={"slug": slug}, timeout=10)
        if r.ok:
            data = r.json()
            market = data[0] if isinstance(data, list) and len(data) > 0 else None
            if market and market.get("slug") == slug:
                outcomes  = json.loads(market.get("outcomes", "[]")) \
                    if isinstance(market.get("outcomes"), str) \
                    else market.get("outcomes", [])
                token_ids = json.loads(market.get("clobTokenIds", "[]")) \
                    if isinstance(market.get("clobTokenIds"), str) \
                    else market.get("clobTokenIds", [])
                market["tokens"] = [
                    {"outcome": outcomes[i], "token_id": token_ids[i]}
                    for i in range(len(outcomes))
                ]
                return market
        return None
    except:
        return None

# ============================================================
# CALCUL DE LA MISE
# Paliers selon le prix du token :
#   token <= 0.58 → mise max (token pas cher = bon rapport)
#   token 0.58-0.65 → mise min (token deja cher)
# ============================================================

def calculate_bet_size(price):
    return 3.0  # mise fixe 3$ validee

# ============================================================
# ORDRES POLYMARKET
# ============================================================

async def sell_order_async(token_id, shares, reason, price):
    try:
        from polymarket import AsyncSecureClient
        # Arrondi conservateur : on retire 0.01 share pour eviter
        # l'erreur "not enough balance" due aux frais
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

async def place_order_async(token_id, outcome, price, bet_size, btc_entry):
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
                log.error("Erreur ordre: " + str(response.code)
                          + " " + str(response.message))
                return False

            order_id = getattr(response, "order_id", None) \
                or getattr(response, "id", None)

            # VERIFICATION D'EXECUTION : attendre 12s puis verifier
            await asyncio.sleep(12)
            executed = True   # par defaut on suppose execute
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
                        # Ordre toujours ouvert = NON execute -> annuler
                        executed = False
                        try:
                            cancel = getattr(client, "cancel_order", None)
                            if cancel:
                                await cancel(order_id=order_id)
                                log.warning("ACHAT NON EXECUTE - annule, pas de position")
                        except Exception as ce:
                            log.warning("Annulation achat: " + str(ce))
            except Exception as ve:
                log.warning("Verif achat impossible (" + str(ve)
                            + ") - position supposee executee")

            if not executed:
                return False

            log.info("TRADE " + outcome + " " + str(bet_size)
                     + " USDC @ " + str(buy_px)
                     + " | BTC: " + str(round(btc_entry)))
            with positions_lock:
                open_positions.append({
                    "token_id":   token_id,
                    "entry_price":buy_px,
                    "shares":     shares,
                    "size":       bet_size,
                    "outcome":    outcome,
                    "btc_entry":  btc_entry,
                    "side":       outcome,
                    "peak_price": buy_px,
                    "window_ts":  (int(time.time()) - int(time.time()) % 300),
                    "zero_count": 0,
                })
            return True
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, bet_size, btc_entry):
    return asyncio.run(place_order_async(token_id, outcome, price, bet_size, btc_entry))

# ============================================================
# CIRCUIT BREAKER
# ============================================================

def record_loss():
    global consecutive_losses, circuit_breaker_until
    consecutive_losses += 1
    log.info("Pertes consecutives: " + str(consecutive_losses)
             + "/" + str(MAX_CONSECUTIVE_LOSSES))
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        circuit_breaker_until = time.time() + CIRCUIT_BREAKER_PAUSE
        log.warning("CIRCUIT BREAKER! " + str(MAX_CONSECUTIVE_LOSSES)
                    + " pertes - pause 2h")
        consecutive_losses = 0

# ============================================================
# THREAD DE SURVEILLANCE DES POSITIONS
# ============================================================

def monitor_loop():
    global daily_pnl, open_positions, consecutive_losses  # BUG CORRIGE : consecutive_losses en global
    log.info("Thread surveillance demarre - toutes les "
             + str(int(MONITOR_INTERVAL)) + "s")
    while True:
        try:
            with positions_lock:
                positions_copy = list(open_positions)
            if not positions_copy:
                time.sleep(MONITOR_INTERVAL)
                continue

            btc_current = get_btc_price()
            to_remove   = []

            for pos in positions_copy:
                token_id = pos["token_id"]
                # Prix vendable REEL (best bid), fallback dernier prix echange
                current  = get_best_bid(token_id)
                if current is None:
                    current = get_token_price(token_id)
                side     = pos.get("side", "Up")
                entry    = pos["entry_price"]
                shares   = pos["shares"]
                peak     = pos.get("peak_price", entry)

                # Prix non disponible
                if current <= 0:
                    pos["zero_count"] = pos.get("zero_count", 0) + 1
                    if pos["zero_count"] >= 3:
                        # CORRECTION CRITIQUE : marche expire sans vente
                        # = PERTE TOTALE de la mise, comptee dans le PnL
                        pnl = (0 - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        record_loss()
                        log.warning("EXPIRATION SANS VENTE - PERTE TOTALE "
                                    + str(round(pnl, 2)) + " | Total: "
                                    + str(round(daily_pnl, 2)))
                        to_remove.append(pos)
                    continue
                pos["zero_count"] = 0

                # Mise a jour du pic
                if current > peak:
                    pos["peak_price"] = current
                    peak = current

                log.info("Monitor | " + side
                         + " | Token: " + str(round(current, 3))
                         + " | Peak: "  + str(round(peak, 3))
                         + " | PnL jour: " + str(round(daily_pnl, 2)))

                # 1. Marche expire en perte
                if current <= 0.02:
                    log.info("Marche expire - PERTE")
                    pnl = (max(0.01, current - 0.04) - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    record_loss()
                    to_remove.append(pos)

                # 2. Marche expire en gain
                elif current >= 0.98:
                    log.info("Marche expire - GAIN!")
                    pnl = (max(0.01, current - 0.04) - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    consecutive_losses = 0   # BUG CORRIGE : remet bien le compteur global a zero
                    log.info("PnL: +" + str(round(pnl, 2))
                             + " | Total: " + str(round(daily_pnl, 2)))
                    to_remove.append(pos)

                # 3. TAKE PROFIT : +40% de gain par rapport a l'entree
                elif current >= entry * (1 + TAKE_PROFIT_GAIN):
                    log.info("TAKE PROFIT +40%! @ " + str(round(current, 3)))
                    if sell_order(token_id, shares, "TP", current):
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        consecutive_losses = 0
                        log.info("PnL TP: +" + str(round(pnl, 2))
                                 + " | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)

                # 4. Stop loss : valeur position < mise * (1 - 30%)
                elif current * shares <= pos["size"] * (1 - STOP_LOSS_PCT):
                    val = round(current * shares, 2)
                    log.info("STOP LOSS -30%! valeur " + str(val)
                             + "$ < " + str(round(pos["size"] * (1 - STOP_LOSS_PCT), 2)) + "$")
                    if sell_order(token_id, shares, "SL-30%", current):
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        record_loss()
                        to_remove.append(pos)

                # 6. DERNIERE MINUTE (240s) : si en perte -> vente forcee SYSTEMATIQUE
                elif (pos.get("window_ts", 0) > 0
                      and (int(time.time()) - pos["window_ts"]) >= 240
                      and current < entry):
                    log.info("VENTE FORCEE derniere minute (en perte) @ "
                             + str(round(current, 3)))
                    if sell_order(token_id, shares, "FORCE_FIN", current):
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        record_loss()
                        log.info("PnL: " + str(round(pnl, 2))
                                 + " | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)

            # Nettoyage des positions soldees
            if to_remove:
                with positions_lock:
                    for pos in to_remove:
                        if pos in open_positions:
                            open_positions.remove(pos)

        except Exception as e:
            log.error("Erreur monitor: " + str(e))

        time.sleep(MONITOR_INTERVAL)

# ============================================================
# BOUCLE PRINCIPALE
# ============================================================

def run():
    global daily_pnl, pnl_date, traded_windows, consecutive_losses

    try:
        load_strikes()
    except:
        pass

    log.info("Bot Polymarket v9 - Prix a Battre - BTC via Coinbase")
    log.info("Gap dynamique: " + str(round(get_dynamic_gap_seuil(60000), 1)) + "$ (vol moy: " + str(round(sum(gap_history)/max(1,len(gap_history)),1)) + "$) | Entree: "
             + str(ENTRY_PRICE_MIN) + "-" + str(ENTRY_PRICE_MAX)
             + " | SL jour: " + str(STOP_LOSS_USDC)
             + "$ | TP jour: " + str(DAILY_TAKE_PROFIT) + "$")
    log.info("Plage: 7h-14h UTC | PnL: " + str(round(daily_pnl, 2))
             + " | Fenetres: " + str(len(traded_windows)))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante ou invalide!")
        return
    if not WALLET.startswith("0x"):
        log.error("POLYMARKET_WALLET_ADDRESS manquante ou invalide!")
        return

    # Lancement du thread de surveillance
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    while True:
        try:
            # Reset journalier
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                append_pnl_history(pnl_date, daily_pnl)
                daily_pnl = 0.0
                pnl_date  = today
                traded_windows.clear()
                consecutive_losses = 0
                save_daily_pnl(0.0)

            # Verifications globales
            if check_stop_loss():
                log.warning("Stop-loss journalier! Pause 1h.")
                time.sleep(3600)
                continue
            if check_take_profit():
                log.info("Take profit! +" + str(round(daily_pnl, 2))
                         + "$ - Pause 1h.")
                time.sleep(3600)
                continue
            if check_circuit_breaker():
                reste = int(circuit_breaker_until - time.time())
                log.warning("Circuit breaker actif - "
                            + str(reste // 60) + " min restantes")
                time.sleep(60)
                continue

            # Verif plage horaire (UTC = heure Abidjan)
            hour_utc = datetime.now(timezone.utc).hour
            if hour_utc not in ACTIVE_HOURS:
                time.sleep(60)
                continue

            now              = int(time.time())
            seconds_in_window= now % 300
            window_ts        = now - seconds_in_window

            # Capture du prix a battre (strike) dans les 5 premieres secondes
            if window_ts not in strikes and seconds_in_window <= 5:
                btc_strike = get_btc_price()
                if btc_strike > 0:
                    strikes[window_ts] = btc_strike
                    save_strike(window_ts, btc_strike)
                    log.info("Nouvelle fenetre | Strike capture: "
                             + str(round(btc_strike)))

            # Logique d'entree : 1ere minute, fenetre pas tradee, strike connu
            if (window_ts not in traded_windows
                    and window_ts in strikes
                    and seconds_in_window <= ENTRY_WINDOW_MAX
                    and len(open_positions) == 0):

                strike  = strikes[window_ts]
                btc_now = get_btc_price()
                gap       = btc_now - strike
                gap_abs   = abs(gap)
                add_gap_history(gap_abs)   # enregistre la volatilite observee
                gap_seuil = get_dynamic_gap_seuil(btc_now)

                if gap_abs >= gap_seuil:
                    target_outcome = "Up" if gap > 0 else "Down"
                    log.info("Gap " + str(round(gap, 1)) + "$ | "
                             + target_outcome + " | BTC: "
                             + str(round(btc_now)) + " vs strike "
                             + str(round(strike)))

                    market = get_btc_market(window_ts)
                    if market:
                        tokens = market.get("tokens", [])
                        target = next(
                            (t for t in tokens if t["outcome"] == target_outcome),
                            None)
                        if target:
                            # Vrai prix d'achat (best ask), fallback last-trade
                            price = get_best_ask(target["token_id"])
                            if price is None:
                                price = get_token_price(target["token_id"])
                            log.info(target_outcome + " token @ "
                                     + str(round(price, 3)))

                            if ENTRY_PRICE_MIN <= price <= ENTRY_PRICE_MAX:
                                bet_size = calculate_bet_size(price)
                                log.info("ENTREE VALIDEE | " + target_outcome
                                         + " @ " + str(round(price, 3))
                                         + " | Mise: " + str(bet_size) + " USDC")
                                if place_order(target["token_id"], target_outcome,
                                               price, bet_size, btc_now):
                                    traded_windows.add(window_ts)
                                    save_traded_window(window_ts)

                            elif price > ENTRY_PRICE_MAX:
                                log.info("Token trop cher ("
                                         + str(round(price, 3))
                                         + ") - trop tard, skip")
                            else:
                                log.info("Token trop bas ("
                                         + str(round(price, 3))
                                         + ") - skip")
                else:
                    log.info("Gap insuffisant (" + str(round(abs(gap), 1))
                             + "$ < " + str(round(gap_seuil, 1))
                             + "$) - attente signal")

            # Nettoyage memoire strikes (50 dernieres fenetres max)
            if len(strikes) > 50:
                for k in sorted(strikes.keys())[:-50]:
                    del strikes[k]

        except Exception as e:
            log.error("Erreur boucle principale: " + str(e))

        time.sleep(5)

if __name__ == "__main__":
    run()
