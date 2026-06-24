import os, time, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

# ============================================================
# BOT ARBITRAGE YES+NO - POLYMARKET MARCHES COURTS
# Principe : quand ask(Up) + ask(Down) < 1$ (ex: 0.97),
# on achete LES DEUX cotes. A l'expiration, l'un vaut 1.00.
# Gain garanti = 1.00 - somme payee, SANS pari directionnel.
# Risque unique : jambe orpheline (un seul cote execute)
# -> gere par verification + revente immediate.
# ============================================================

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET      = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

# Seuil d'arbitrage : somme max payee pour les deux cotes
# 0.97 = 3% de gain brut minimum (couvre les frais ~2%)
SUM_MAX        = float(os.getenv("ARB_SUM_MAX", "0.97"))

# Capital par trade (sur CHAQUE arbitrage, les deux jambes comprises)
TRADE_USDC     = float(os.getenv("ARB_TRADE_USDC", "10"))

# Nombre max d'arbitrages simultanes en attente d'expiration
MAX_CONCURRENT = int(os.getenv("ARB_MAX_CONCURRENT", "3"))

# Stop si les pertes du jour (jambes orphelines) depassent ce seuil
STOP_LOSS_USDC = float(os.getenv("ARB_STOP_LOSS_USDC", "10"))

# Ne pas entrer dans les X dernieres secondes d'une fenetre
# (le temps que les deux jambes s'executent)
MIN_TIME_LEFT  = int(os.getenv("ARB_MIN_TIME_LEFT", "60"))

# Frequence de scan (secondes) - rapide, les fenetres durent peu
SCAN_INTERVAL  = float(os.getenv("ARB_SCAN_INTERVAL", "1"))

# Plage horaire active (UTC)
ACTIVE_HOURS = list(range(int(os.getenv("ARB_HOUR_START", "0")),
                          int(os.getenv("ARB_HOUR_END",   "24"))))

# Marches scannes : slug + duree de fenetre en secondes
MARKETS = {
    "BTC5":  {"slug": "btc-updown-5m-",  "window": 300},
    "ETH15": {"slug": "eth-updown-15m-", "window": 900},
    "SOL15": {"slug": "sol-updown-15m-", "window": 900},
}

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

PNL_FILE  = "/app/arb_daily_pnl.txt"

SESSION = requests.Session()

import sys
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("arb")

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

def save_daily_pnl(pnl):
    try:
        with open(PNL_FILE, "w") as f:
            f.write(str(date.today()) + "\n" + str(round(pnl, 4)))
    except:
        pass

daily_pnl   = load_daily_pnl()
pnl_date    = date.today()
open_arbs   = []      # arbitrages en attente d'expiration
arbs_lock   = threading.Lock()
done_windows = set()  # fenetres deja arbitrees (cle market+ts)

# ============================================================
# DONNEES MARCHE
# ============================================================

def get_market_tokens(slug_prefix, window_ts):
    """Retourne [{outcome, token_id}] pour la fenetre donnee."""
    try:
        slug = slug_prefix + str(window_ts)
        r = SESSION.get(GAMMA_API + "/markets",
                         params={"slug": slug}, timeout=8)
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
                return [{"outcome": outcomes[i], "token_id": token_ids[i]}
                        for i in range(len(outcomes))]
        return None
    except:
        return None

def get_best_ask(token_id):
    """Lit le carnet d'ordres : meilleur prix d'achat dispo + profondeur."""
    try:
        r = SESSION.get(CLOB_API + "/book",
                         params={"token_id": token_id}, timeout=8)
        if r.ok:
            book = r.json()
            asks = book.get("asks", [])
            if asks:
                # asks tries du moins cher au plus cher selon l'API ;
                # on prend le meilleur (prix le plus bas)
                best = min(asks, key=lambda a: float(a["price"]))
                return float(best["price"]), float(best["size"])
        return None, 0
    except:
        return None, 0

def get_token_price(token_id):
    try:
        r = SESSION.get(CLOB_API + "/last-trade-price",
                         params={"token_id": token_id}, timeout=8)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

# ============================================================
# ORDRES
# ============================================================

async def buy_leg(client, token_id, px, shares):
    """Achete une jambe. Retourne (ok, order_id)."""
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
    """Revend une jambe orpheline, agressivement (px - 0.04)."""
    try:
        sell_px = max(0.01, round(px - 0.04, 4))
        shares_safe = max(0.01, round(shares - 0.01, 2))
        response = await client.place_limit_order(
            token_id=token_id, side="SELL",
            price=str(sell_px), size=str(shares_safe))
        return response.ok
    except Exception as e:
        log.error("Exception revente orpheline: " + str(e))
        return False

async def execute_arb_async(up_id, up_px, down_id, down_px,
                             shares, market_key, window_ts):
    """
    Execute l'arbitrage : achete les DEUX jambes.
    Si une seule s'execute -> revend l'orpheline immediatement.
    Retourne (ok, cout_total) ; ok=True si les deux jambes tenues.
    """
    global daily_pnl
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(
                private_key=PRIVATE_KEY, wallet=WALLET) as client:

            # Achat agressif des deux jambes (+0.01 pour execution sure)
            up_buy   = min(0.99, round(up_px + 0.01, 4))
            down_buy = min(0.99, round(down_px + 0.01, 4))

            # CORRECTION : achete les DEUX jambes EN PARALLELE (asyncio.gather)
            # Avant, elles partaient l'une apres l'autre -> le prix de la 2e bougeait
            # et l'arbitrage echouait. La, elles partent en meme temps.
            (ok_up, oid_up), (ok_down, oid_down) = await asyncio.gather(
                buy_leg(client, up_id, up_buy, shares),
                buy_leg(client, down_id, down_buy, shares),
            )

            if not ok_up and not ok_down:
                log.warning("[" + market_key + "] Aucune jambe executee - abandon")
                return False, 0

            # Attente courte puis verification des deux jambes
            await asyncio.sleep(8)

            filled = {"up": ok_up, "down": ok_down}
            try:
                open_orders = None
                for meth in ("get_orders", "get_open_orders"):
                    fn = getattr(client, meth, None)
                    if fn:
                        open_orders = await fn()
                        break
                if open_orders is not None:
                    items = getattr(open_orders, "orders", open_orders)
                    ids = []
                    if isinstance(items, list):
                        for o in items:
                            oid = o.get("id") if isinstance(o, dict) \
                                else getattr(o, "id", None)
                            ids.append(oid)
                    cancel = getattr(client, "cancel_order", None)
                    if oid_up and oid_up in ids:
                        filled["up"] = False
                        if cancel:
                            await cancel(order_id=oid_up)
                    if oid_down and oid_down in ids:
                        filled["down"] = False
                        if cancel:
                            await cancel(order_id=oid_down)
            except Exception as ve:
                log.warning("Verif jambes impossible: " + str(ve))

            # Cas 1 : les deux jambes tenues -> arbitrage verrouille
            if filled["up"] and filled["down"]:
                cout = (up_buy + down_buy) * shares
                gain_prevu = (1.0 - up_buy - down_buy) * shares
                log.info("[" + market_key + "] ARB VERROUILLE | somme "
                         + str(round(up_buy + down_buy, 3))
                         + " | " + str(shares) + " shares | gain prevu +"
                         + str(round(gain_prevu, 2)) + "$")
                return True, cout

            # Cas 2 : jambe orpheline -> revente immediate
            if filled["up"] and not filled["down"]:
                log.warning("[" + market_key + "] Jambe DOWN manquante"
                            + " - revente UP immediate")
                await sell_leg(client, up_id, shares, up_buy)
                perte = 0.05 * shares   # estimation pessimiste du cout
                daily_pnl -= perte
                save_daily_pnl(daily_pnl)
                return False, 0
            if filled["down"] and not filled["up"]:
                log.warning("[" + market_key + "] Jambe UP manquante"
                            + " - revente DOWN immediate")
                await sell_leg(client, down_id, shares, down_buy)
                perte = 0.05 * shares
                daily_pnl -= perte
                save_daily_pnl(daily_pnl)
                return False, 0

            return False, 0
    except Exception as e:
        log.error("Exception arbitrage: " + str(e))
        return False, 0

def execute_arb(up_id, up_px, down_id, down_px, shares, market_key, window_ts):
    return asyncio.run(execute_arb_async(up_id, up_px, down_id, down_px,
                                          shares, market_key, window_ts))

# ============================================================
# THREAD : comptabilise les arbitrages expires
# ============================================================

def settle_loop():
    global daily_pnl
    log.info("Thread reglement demarre")
    while True:
        try:
            now = int(time.time())
            with arbs_lock:
                arbs_copy = list(open_arbs)
            for arb in arbs_copy:
                # expire ?
                if now >= arb["expiry"] + 30:
                    # l'un des deux tokens vaut 1.00 a l'expiration
                    gain = (1.0 - arb["sum_paid"]) * arb["shares"]
                    daily_pnl += gain
                    save_daily_pnl(daily_pnl)
                    log.info("[" + arb["market"] + "] ARB REGLE | +"
                             + str(round(gain, 2)) + "$ | PnL jour: "
                             + str(round(daily_pnl, 2)) + "$")
                    with arbs_lock:
                        if arb in open_arbs:
                            open_arbs.remove(arb)
        except Exception as e:
            log.error("Erreur reglement: " + str(e))
        time.sleep(10)


# ============================================================
# SCANNER GLOBAL : tous les marches actifs (sport, CdM, etc.)
# Les marches peu surveilles gardent parfois Yes+No < 1 longtemps
# ============================================================

GLOBAL_SCAN_INTERVAL = int(os.getenv("ARB_GLOBAL_SCAN_INTERVAL", "60"))
GLOBAL_MIN_DAYS_LEFT = 0      # marches qui se resolvent bientot acceptes
GLOBAL_MAX_DAYS_LEFT = float(os.getenv("ARB_GLOBAL_MAX_DAYS", "7"))  # max 7 jours de blocage capital

def scan_global_markets():
    """Scanne les marches actifs tries par volume, cherche Yes+No < seuil."""
    found = []
    try:
        r = SESSION.get(GAMMA_API + "/markets",
                        params={"active": "true", "closed": "false",
                                "limit": 100, "order": "volume24hr",
                                "ascending": "false"}, timeout=15)
        if not r.ok:
            return found
        for m in r.json():
            try:
                outcomes  = json.loads(m.get("outcomes", "[]")) \
                    if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                token_ids = json.loads(m.get("clobTokenIds", "[]")) \
                    if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
                if len(outcomes) != 2 or len(token_ids) != 2:
                    continue
                # date de fin : eviter de bloquer le capital trop longtemps
                end = m.get("endDate") or m.get("end_date_iso")
                if end:
                    try:
                        end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                        days_left = (end_ts - time.time()) / 86400
                        if days_left > GLOBAL_MAX_DAYS_LEFT or days_left < 0:
                            continue
                    except:
                        pass
                a0, d0 = get_best_ask(token_ids[0])
                a1, d1 = get_best_ask(token_ids[1])
                if a0 is None or a1 is None:
                    continue
                total = a0 + a1
                if total <= SUM_MAX:
                    found.append({
                        "question": m.get("question", "?")[:60],
                        "t0": token_ids[0], "p0": a0, "d0": d0,
                        "t1": token_ids[1], "p1": a1, "d1": d1,
                        "total": total,
                    })
            except Exception:
                continue
    except Exception as e:
        log.error("Scan global: " + str(e))
    return found

def global_scan_loop():
    log.info("Scanner global demarre (sport/CdM/tous marches) - toutes les "
             + str(GLOBAL_SCAN_INTERVAL) + "s")
    global daily_pnl
    while True:
        try:
            if daily_pnl > -STOP_LOSS_USDC:
                opps = scan_global_markets()
                for o in opps:
                    log.info("[GLOBAL] OPPORTUNITE | " + o["question"]
                             + " | somme " + str(round(o["total"], 3))
                             + " | profondeur " + str(round(min(o["d0"], o["d1"]), 1)))
                    with arbs_lock:
                        n = len(open_arbs)
                    if n >= MAX_CONCURRENT:
                        continue
                    shares_cap   = math.floor(TRADE_USDC / o["total"] * 100) / 100
                    shares_depth = math.floor(min(o["d0"], o["d1"]) * 100) / 100
                    shares       = min(shares_cap, shares_depth)
                    if shares < 1:
                        continue
                    ok, cout = execute_arb(o["t0"], o["p0"], o["t1"], o["p1"],
                                            shares, "GLOBAL", 0)
                    if ok:
                        with arbs_lock:
                            open_arbs.append({
                                "market":   "GLOBAL",
                                "sum_paid": round(o["p0"] + o["p1"] + 0.02, 4),
                                "shares":   shares,
                                # regle a la resolution : on garde 7 jours max en memoire
                                "expiry":   int(time.time()) + 7 * 86400,
                            })
        except Exception as e:
            log.error("Erreur scan global: " + str(e))
        time.sleep(GLOBAL_SCAN_INTERVAL)

# ============================================================
# BOUCLE PRINCIPALE
# ============================================================

def run():
    global daily_pnl, pnl_date

    log.info("Bot ARBITRAGE Yes+No demarre")
    log.info("Seuil somme: " + str(SUM_MAX) + " | Trade: "
             + str(TRADE_USDC) + "$ | Max simultanes: "
             + str(MAX_CONCURRENT) + " | SL jour: "
             + str(STOP_LOSS_USDC) + "$")
    log.info("Marches: " + ", ".join(MARKETS.keys())
             + " | PnL: " + str(round(daily_pnl, 2)))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("POLYMARKET_WALLET_ADDRESS manquante!")
        return

    settle_thread = threading.Thread(target=settle_loop, daemon=True)
    settle_thread.start()
    global_thread = threading.Thread(target=global_scan_loop, daemon=True)
    global_thread.start()

    # cache des tokens par fenetre pour eviter les requetes repetees
    token_cache = {}
    last_heartbeat = 0

    while True:
        try:
            # Reset journalier
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date  = today
                done_windows.clear()
                save_daily_pnl(0.0)

            # Stop loss jambes orphelines
            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss arbitrage atteint - pause 1h")
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

            now = int(time.time())

            for mkey, mcfg in MARKETS.items():
                wsize     = mcfg["window"]
                window_ts = now - (now % wsize)
                time_left = (window_ts + wsize) - now
                cache_key = mkey + "_" + str(window_ts)

                # fenetre deja traitee ou trop proche de l'expiration
                if cache_key in done_windows:
                    continue
                if time_left < MIN_TIME_LEFT:
                    continue

                # tokens de la fenetre (avec cache)
                if cache_key not in token_cache:
                    tokens = get_market_tokens(mcfg["slug"], window_ts)
                    if not tokens or len(tokens) < 2:
                        continue
                    token_cache[cache_key] = tokens
                    # nettoie le cache
                    if len(token_cache) > 30:
                        for k in list(token_cache.keys())[:-30]:
                            del token_cache[k]
                tokens = token_cache[cache_key]

                up    = next((t for t in tokens if t["outcome"] == "Up"), None)
                down  = next((t for t in tokens if t["outcome"] == "Down"), None)
                if not up or not down:
                    continue

                # lit les deux carnets EN PARALLELE (gain ~50% latence)
                res = {}
                def _read(side, tid):
                    res[side] = get_best_ask(tid)
                t1 = threading.Thread(target=_read, args=("up", up["token_id"]))
                t2 = threading.Thread(target=_read, args=("down", down["token_id"]))
                t1.start(); t2.start(); t1.join(); t2.join()
                up_px, up_depth     = res.get("up", (None, 0))
                down_px, down_depth = res.get("down", (None, 0))
                if up_px is None or down_px is None:
                    continue

                total = up_px + down_px

                # Heartbeat : toutes les 5 min, log de la somme observee
                if time.time() - last_heartbeat >= 300:
                    log.info("[" + mkey + "] scan actif | somme "
                             + str(round(total, 3)) + " | seuil "
                             + str(SUM_MAX) + " | PnL jour: "
                             + str(round(daily_pnl, 2)) + "$")
                    last_heartbeat = time.time()

                if total <= SUM_MAX:
                    # taille : limitee par le capital ET la profondeur dispo
                    shares_cap   = math.floor(TRADE_USDC / total * 100) / 100
                    shares_depth = math.floor(min(up_depth, down_depth) * 100) / 100
                    shares       = min(shares_cap, shares_depth)
                    if shares < 1:
                        continue

                    gain_potentiel = (1.0 - total) * shares
                    log.info("[" + mkey + "] OPPORTUNITE | Up "
                             + str(up_px) + " + Down " + str(down_px)
                             + " = " + str(round(total, 3))
                             + " | gain potentiel +"
                             + str(round(gain_potentiel, 2)) + "$")

                    ok, cout = execute_arb(up["token_id"], up_px,
                                            down["token_id"], down_px,
                                            shares, mkey, window_ts)
                    done_windows.add(cache_key)
                    if ok:
                        with arbs_lock:
                            open_arbs.append({
                                "market":   mkey,
                                "sum_paid": round(up_px + down_px + 0.02, 4),
                                "shares":   shares,
                                "expiry":   window_ts + wsize,
                            })

        except Exception as e:
            log.error("Erreur boucle: " + str(e))

        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
