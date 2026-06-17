import os, time, logging, requests, json, asyncio, math, threading
from datetime import date, datetime, timezone

# ============================================================
# BOT ARBITRAGE YES+NO - POLYMARKET TOUS MARCHES HORS CRYPTO
# Principe identique au Bot 3 mais cible :
# - Sport (foot, NBA, F1, tennis...)
# - Politique (élections, géopolitique)
# - Economie (Fed, inflation)
# - Pop culture, météo, divers
# Exclut BTC/ETH/SOL déjà couverts par Bot 3
# Seuil 0.95 au lieu de 0.97 car marchés moins efficients
# ============================================================

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET      = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

SUM_MAX        = float(os.getenv("SPORT_SUM_MAX",        "0.95"))
TRADE_USDC     = float(os.getenv("SPORT_TRADE_USDC",     "10"))
MAX_CONCURRENT = int(os.getenv("SPORT_MAX_CONCURRENT",   "5"))
STOP_LOSS_USDC = float(os.getenv("SPORT_STOP_LOSS_USDC", "20"))
SCAN_INTERVAL  = float(os.getenv("SPORT_SCAN_INTERVAL",  "30"))
MAX_DAYS_LEFT  = float(os.getenv("SPORT_MAX_DAYS",       "7"))
MIN_DAYS_LEFT  = float(os.getenv("SPORT_MIN_DAYS",       "0"))

# Mots-clés à EXCLURE (déjà couverts par Bot 3)
EXCLUDE_KEYWORDS = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol",
                    "crypto", "updown", "up-or-down", "up or down"]

ACTIVE_HOURS = list(range(int(os.getenv("SPORT_HOUR_START", "0")),
                          int(os.getenv("SPORT_HOUR_END",   "24"))))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
PNL_FILE  = "/app/sport_arb_daily_pnl.txt"

SESSION = requests.Session()

import sys
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("arb_sport")

# ============================================================
# MEMOIRE
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

def save_daily_pnl(pnl):
    try:
        with open(PNL_FILE, "w") as f:
            f.write(str(date.today()) + "\n" + str(round(pnl, 4)))
    except:
        pass

daily_pnl    = load_daily_pnl()
pnl_date     = date.today()
open_arbs    = []
arbs_lock    = threading.Lock()
done_markets = set()  # slugs déjà arbitrés aujourd'hui

# ============================================================
# DONNEES MARCHE
# ============================================================

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

def is_excluded(question):
    """Retourne True si le marché est lié à crypto (déjà couvert par Bot 3)."""
    q = question.lower()
    return any(kw in q for kw in EXCLUDE_KEYWORDS)

def scan_all_markets():
    """
    Scanne tous les marchés Polymarket actifs hors crypto.
    Cherche les opportunités Yes+No < SUM_MAX.
    """
    found = []
    try:
        # Plusieurs pages pour couvrir plus de marchés
        for offset in [0, 100, 200]:
            r = SESSION.get(GAMMA_API + "/markets",
                            params={"active": "true", "closed": "false",
                                    "limit": 100, "offset": offset,
                                    "order": "volume24hr",
                                    "ascending": "false"}, timeout=15)
            if not r.ok:
                continue
            for m in r.json():
                try:
                    question = m.get("question", "")

                    # Exclut crypto
                    if is_excluded(question):
                        continue

                    outcomes  = json.loads(m.get("outcomes", "[]")) \
                        if isinstance(m.get("outcomes"), str) \
                        else m.get("outcomes", [])
                    token_ids = json.loads(m.get("clobTokenIds", "[]")) \
                        if isinstance(m.get("clobTokenIds"), str) \
                        else m.get("clobTokenIds", [])

                    # Uniquement marchés binaires Yes/No
                    if len(outcomes) != 2 or len(token_ids) != 2:
                        continue

                    # Filtre sur la date de fin
                    end = m.get("endDate") or m.get("end_date_iso")
                    if end:
                        try:
                            end_ts    = datetime.fromisoformat(
                                end.replace("Z", "+00:00")).timestamp()
                            days_left = (end_ts - time.time()) / 86400
                            if days_left > MAX_DAYS_LEFT or days_left < MIN_DAYS_LEFT:
                                continue
                        except:
                            pass

                    # Déjà traité aujourd'hui
                    slug = m.get("slug", "")
                    if slug in done_markets:
                        continue

                    # Lit les deux carnets
                    a0, d0 = get_best_ask(token_ids[0])
                    a1, d1 = get_best_ask(token_ids[1])
                    if a0 is None or a1 is None:
                        continue

                    total = a0 + a1
                    if total <= SUM_MAX:
                        found.append({
                            "question": question[:70],
                            "slug":     slug,
                            "t0": token_ids[0], "p0": a0, "d0": d0,
                            "t1": token_ids[1], "p1": a1, "d1": d1,
                            "total": total,
                        })
                except:
                    continue
        time.sleep(1)  # évite le rate limit
    except Exception as e:
        log.error("Scan: " + str(e))
    return found

# ============================================================
# ORDRES
# ============================================================

async def buy_leg(client, token_id, px, shares):
    try:
        response = await client.place_limit_order(
            token_id=token_id, side="BUY",
            price=str(round(px, 4)), size=str(shares))
        if response.ok:
            oid = getattr(response, "order_id", None) \
                or getattr(response, "id", None)
            return True, oid
        log.error("Echec jambe: " + str(response.message))
        return False, None
    except Exception as e:
        log.error("Exception jambe: " + str(e))
        return False, None

async def sell_leg(client, token_id, shares, px):
    try:
        sell_px     = max(0.01, round(px - 0.04, 4))
        shares_safe = max(0.01, round(shares - 0.01, 2))
        response    = await client.place_limit_order(
            token_id=token_id, side="SELL",
            price=str(sell_px), size=str(shares_safe))
        return response.ok
    except Exception as e:
        log.error("Exception revente: " + str(e))
        return False

async def execute_arb_async(t0, p0, t1, p1, shares, question, expiry_ts):
    global daily_pnl
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(
                private_key=PRIVATE_KEY, wallet=WALLET) as client:

            buy0 = min(0.99, round(p0 + 0.01, 4))
            buy1 = min(0.99, round(p1 + 0.01, 4))

            ok0, oid0 = await buy_leg(client, t0, buy0, shares)
            ok1, oid1 = await buy_leg(client, t1, buy1, shares)

            if not ok0 and not ok1:
                log.warning("Aucune jambe exécutée — abandon")
                return False, 0

            await asyncio.sleep(8)

            filled = {"t0": ok0, "t1": ok1}
            try:
                open_orders = None
                for meth in ("get_orders", "get_open_orders"):
                    fn = getattr(client, meth, None)
                    if fn:
                        open_orders = await fn()
                        break
                if open_orders is not None:
                    items = getattr(open_orders, "orders", open_orders)
                    ids   = []
                    if isinstance(items, list):
                        for o in items:
                            oid = o.get("id") if isinstance(o, dict) \
                                else getattr(o, "id", None)
                            ids.append(oid)
                    cancel = getattr(client, "cancel_order", None)
                    if oid0 and oid0 in ids:
                        filled["t0"] = False
                        if cancel:
                            await cancel(order_id=oid0)
                    if oid1 and oid1 in ids:
                        filled["t1"] = False
                        if cancel:
                            await cancel(order_id=oid1)
            except Exception as ve:
                log.warning("Vérif jambes: " + str(ve))

            if filled["t0"] and filled["t1"]:
                gain_prevu = (1.0 - buy0 - buy1) * shares
                log.info("ARB VERROUILLÉ | " + question[:50]
                         + " | somme " + str(round(buy0 + buy1, 3))
                         + " | gain prévu +" + str(round(gain_prevu, 2)) + "$")
                return True, (buy0 + buy1) * shares

            # Jambe orpheline
            if filled["t0"] and not filled["t1"]:
                log.warning("Jambe T1 manquante — revente T0")
                await sell_leg(client, t0, shares, buy0)
                daily_pnl -= 0.05 * shares
                save_daily_pnl(daily_pnl)
                return False, 0
            if filled["t1"] and not filled["t0"]:
                log.warning("Jambe T0 manquante — revente T1")
                await sell_leg(client, t1, shares, buy1)
                daily_pnl -= 0.05 * shares
                save_daily_pnl(daily_pnl)
                return False, 0

            return False, 0
    except Exception as e:
        log.error("Exception arb: " + str(e))
        return False, 0

def execute_arb(t0, p0, t1, p1, shares, question, expiry_ts):
    return asyncio.run(execute_arb_async(t0, p0, t1, p1, shares, question, expiry_ts))

# ============================================================
# THREAD REGLEMENT
# ============================================================

def settle_loop():
    global daily_pnl
    log.info("Thread règlement démarré")
    while True:
        try:
            now = int(time.time())
            with arbs_lock:
                arbs_copy = list(open_arbs)
            for arb in arbs_copy:
                if now >= arb["expiry"] + 30:
                    gain = (1.0 - arb["sum_paid"]) * arb["shares"]
                    daily_pnl += gain
                    save_daily_pnl(daily_pnl)
                    log.info("ARB RÉGLÉ | " + arb.get("question", "")[:40]
                             + " | +" + str(round(gain, 2))
                             + "$ | PnL: " + str(round(daily_pnl, 2)) + "$")
                    with arbs_lock:
                        if arb in open_arbs:
                            open_arbs.remove(arb)
        except Exception as e:
            log.error("Erreur règlement: " + str(e))
        time.sleep(10)

# ============================================================
# BOUCLE PRINCIPALE
# ============================================================

def run():
    global daily_pnl, pnl_date, done_markets

    log.info("Bot Arbitrage Sport/Politique/Divers démarré")
    log.info("Seuil: " + str(SUM_MAX) + " | Trade: " + str(TRADE_USDC)
             + "$ | Max simultanés: " + str(MAX_CONCURRENT)
             + " | SL: " + str(STOP_LOSS_USDC) + "$"
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
            # Reset journalier
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl    = 0.0
                pnl_date     = today
                done_markets = set()
                save_daily_pnl(0.0)

            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss atteint - pause 1h")
                time.sleep(3600)
                continue

            hour_utc = datetime.now(timezone.utc).hour
            if hour_utc not in ACTIVE_HOURS:
                time.sleep(60)
                continue

            with arbs_lock:
                n_open = len(open_arbs)
            if n_open >= MAX_CONCURRENT:
                time.sleep(SCAN_INTERVAL)
                continue

            # Heartbeat toutes les 5 min
            if time.time() - last_heartbeat >= 300:
                log.info("Scan actif | Arbs ouverts: " + str(n_open)
                         + "/" + str(MAX_CONCURRENT)
                         + " | PnL: " + str(round(daily_pnl, 2)) + "$")
                last_heartbeat = time.time()

            # Scan
            opps = scan_all_markets()

            if not opps:
                log.info("Aucune opportunité trouvée")
            else:
                for o in opps:
                    log.info("OPPORTUNITÉ | " + o["question"]
                             + " | somme " + str(round(o["total"], 3))
                             + " | gain brut +" + str(round((1 - o["total"]) * 100, 1)) + "%"
                             + " | profondeur " + str(round(min(o["d0"], o["d1"]), 1)))

                    with arbs_lock:
                        if len(open_arbs) >= MAX_CONCURRENT:
                            break

                    shares_cap   = math.floor(TRADE_USDC / o["total"] * 100) / 100
                    shares_depth = math.floor(min(o["d0"], o["d1"]) * 100) / 100
                    shares       = min(shares_cap, shares_depth)
                    if shares < 1:
                        log.info("Profondeur insuffisante (" + str(shares) + ") — skip")
                        continue

                    # Expiry estimée : 7 jours max en mémoire
                    expiry_ts = int(time.time()) + 7 * 86400

                    ok, cout = execute_arb(o["t0"], o["p0"], o["t1"], o["p1"],
                                           shares, o["question"], expiry_ts)
                    done_markets.add(o["slug"])

                    if ok:
                        with arbs_lock:
                            open_arbs.append({
                                "question": o["question"],
                                "sum_paid": round(o["p0"] + o["p1"] + 0.02, 4),
                                "shares":   shares,
                                "expiry":   expiry_ts,
                            })

        except Exception as e:
            log.error("Erreur boucle: " + str(e))

        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
