import os, time, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

# ============================================================
# BOT ARBITRAGE YES+NO - POLYMARKET MARCHES COURTS
# Principe : quand ask(Up) + ask(Down) < 1$ (ex: 0.97),
# on achete LES DEUX cotes. A l'expiration, l'un vaut 1.00.
# Gain garanti = 1.00 - somme payee, SANS pari directionnel.
# Fix 1 : verification solde avant achat (fail-safe si indisponible)
# Fix 2 : filtre marche trop desequilibre (min 0.20 par cote)
# ============================================================

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET      = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

SUM_MAX        = float(os.getenv("ARB_SUM_MAX", "0.97"))
TRADE_USDC     = float(os.getenv("ARB_TRADE_USDC", "10"))
MAX_CONCURRENT = int(os.getenv("ARB_MAX_CONCURRENT", "3"))
STOP_LOSS_USDC = float(os.getenv("ARB_STOP_LOSS_USDC", "10"))
MIN_TIME_LEFT  = int(os.getenv("ARB_MIN_TIME_LEFT", "60"))
SCAN_INTERVAL  = float(os.getenv("ARB_SCAN_INTERVAL", "1"))
MIN_LEG_PRICE  = float(os.getenv("ARB_MIN_LEG_PRICE", "0.20"))  # filtre desequilibre

ACTIVE_HOURS = list(range(int(os.getenv("ARB_HOUR_START", "0")),
                          int(os.getenv("ARB_HOUR_END",   "24"))))

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
done_windows = set()


def get_usdc_balance():
    try:
        r = SESSION.get("https://data-api.polymarket.com/value",
                        params={"user": WALLET}, timeout=8)
        if r.ok:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return float(data[0].get("value", 0))
            if isinstance(data, dict):
                return float(data.get("value", data.get("balance", 0)))
        return None
    except Exception as e:
        log.warning("Solde indisponible: " + str(e))
        return None

def get_market_tokens(slug_prefix, window_ts):
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


async def execute_arb_async(up_id, up_px, down_id, down_px,
                             shares, market_key, window_ts):
    global daily_pnl
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(
                private_key=PRIVATE_KEY, wallet=WALLET) as client:

            up_buy   = min(0.99, round(up_px + 0.01, 4))
            down_buy = min(0.99, round(down_px + 0.01, 4))

            # ── FIX SOLDE ─────────────────────────────────────────────────────
            cout_total = (up_buy + down_buy) * shares
            solde = get_usdc_balance()
            if solde is None:
                log.warning("[" + market_key + "] Solde indisponible — ABANDON")
                return False, 0
            if solde < cout_total * 1.05:
                log.warning("[" + market_key + "] Solde insuffisant ("
                            + str(round(solde, 2)) + "$ < "
                            + str(round(cout_total * 1.05, 2)) + "$ requis) — ABANDON")
                return False, 0
            log.info("[" + market_key + "] Solde OK: " + str(round(solde, 2))
                     + "$ pour " + str(round(cout_total, 2)) + "$ requis")

            async def buy_leg(token_id, px):
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

            async def sell_leg(token_id, shares, px):
                try:
                    sell_px = max(0.01, round(px - 0.04, 4))
                    shares_safe = max(0.01, round(shares - 0.01, 2))
                    response = await client.place_limit_order(
                        token_id=token_id, side="SELL",
                        price=str(sell_px), size=str(shares_safe))
                    return response.ok
                except Exception as e:
                    log.error("Exception revente: " + str(e))
                    return False

            ok_up,   oid_up   = await buy_leg(up_id,   up_buy)
            ok_down, oid_down = await buy_leg(down_id, down_buy)

            if not ok_up and not ok_down:
                log.warning("[" + market_key + "] Aucune jambe executee - abandon")
                return False, 0

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
                        if cancel: await cancel(order_id=oid_up)
                    if oid_down and oid_down in ids:
                        filled["down"] = False
                        if cancel: await cancel(order_id=oid_down)
            except Exception as ve:
                log.warning("Verif jambes: " + str(ve))

            if filled["up"] and filled["down"]:
                cout = (up_buy + down_buy) * shares
                gain_prevu = (1.0 - up_buy - down_buy) * shares
                log.info("[" + market_key + "] ARB VERROUILLE | somme "
                         + str(round(up_buy + down_buy, 3))
                         + " | " + str(shares) + " shares | gain prevu +"
                         + str(round(gain_prevu, 2)) + "$")
                return True, cout

            if filled["up"] and not filled["down"]:
                log.warning("[" + market_key + "] Jambe DOWN manquante - revente UP")
                await sell_leg(up_id, shares, up_buy)
                perte = 0.05 * shares
                daily_pnl -= perte
                save_daily_pnl(daily_pnl)
                return False, 0
            if filled["down"] and not filled["up"]:
                log.warning("[" + market_key + "] Jambe UP manquante - revente DOWN")
                await sell_leg(down_id, shares, down_buy)
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

def settle_loop():
    global daily_pnl
    log.info("Thread reglement demarre")
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
                    log.info("[" + arb["market"] + "] ARB REGLE | +"
                             + str(round(gain, 2)) + "$ | PnL jour: "
                             + str(round(daily_pnl, 2)) + "$")
                    with arbs_lock:
                        if arb in open_arbs:
                            open_arbs.remove(arb)
        except Exception as e:
            log.error("Erreur reglement: " + str(e))
        time.sleep(10)

def run():
    global daily_pnl, pnl_date

    log.info("Bot ARBITRAGE Yes+No demarre")
    log.info("Seuil somme: " + str(SUM_MAX)
             + " | Trade: " + str(TRADE_USDC)
             + "$ | Max simultanes: " + str(MAX_CONCURRENT)
             + " | SL jour: " + str(STOP_LOSS_USDC)
             + "$ | Min leg: " + str(MIN_LEG_PRICE))
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

    token_cache    = {}
    last_heartbeat = 0

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date  = today
                done_windows.clear()
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

            now = int(time.time())

            for mkey, mcfg in MARKETS.items():
                wsize     = mcfg["window"]
                window_ts = now - (now % wsize)
                time_left = (window_ts + wsize) - now
                cache_key = mkey + "_" + str(window_ts)

                if cache_key in done_windows:
                    continue
                if time_left < MIN_TIME_LEFT:
                    continue

                if cache_key not in token_cache:
                    tokens = get_market_tokens(mcfg["slug"], window_ts)
                    if not tokens or len(tokens) < 2:
                        continue
                    token_cache[cache_key] = tokens
                    if len(token_cache) > 30:
                        for k in list(token_cache.keys())[:-30]:
                            del token_cache[k]
                tokens = token_cache[cache_key]

                up   = next((t for t in tokens if t["outcome"] == "Up"), None)
                down = next((t for t in tokens if t["outcome"] == "Down"), None)
                if not up or not down:
                    continue

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

                if time.time() - last_heartbeat >= 300:
                    log.info("[" + mkey + "] scan actif | somme "
                             + str(round(total, 3)) + " | seuil "
                             + str(SUM_MAX) + " | PnL jour: "
                             + str(round(daily_pnl, 2)) + "$")
                    last_heartbeat = time.time()

                # ── FIX DESEQUILIBRE ──────────────────────────────────────────
                if up_px < MIN_LEG_PRICE or down_px < MIN_LEG_PRICE:
                    log.info("[" + mkey + "] Marche desequilibre ("
                             + str(round(up_px, 3)) + "/"
                             + str(round(down_px, 3)) + ") — skip")
                    continue

                if total <= SUM_MAX:
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
