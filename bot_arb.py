import os, time, logging, requests, json, asyncio, math, threading
from datetime import date, datetime, timezone

# ============================================================
# BOT ARBITRAGE YES+NO v3 - POLYMARKET
# CORRECTIONS MAJEURES vs v2 :
# 1. Re-vérification de la somme JUSTE avant l'achat (anti-slippage)
# 2. Seuil intégrant les frais réels (gain net garanti)
# 3. Achat des deux jambes au prix EXACT du carnet (pas de +0.01)
# 4. Marge de sécurité sur le slippage
# 5. Abandon immédiat si la somme dépasse le seuil au moment d'acheter
# 6. Fix solde : fail-safe si indisponible, ABANDON si insuffisant
# ============================================================

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET      = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

SUM_MAX        = float(os.getenv("ARB_SUM_MAX",        "0.98"))
FEE_PER_LEG    = float(os.getenv("ARB_FEE_PER_LEG",   "0.01"))
MIN_NET_GAIN   = float(os.getenv("ARB_MIN_NET_GAIN",  "0.015"))
TRADE_USDC     = float(os.getenv("ARB_TRADE_USDC",     "10"))
MAX_CONCURRENT = int(os.getenv("ARB_MAX_CONCURRENT",   "3"))
STOP_LOSS_USDC = float(os.getenv("ARB_STOP_LOSS_USDC", "15"))
SCAN_INTERVAL  = float(os.getenv("ARB_SCAN_INTERVAL",  "5"))
MAX_DAYS_LEFT  = float(os.getenv("ARB_MAX_DAYS",       "7"))
MAX_SLIPPAGE   = float(os.getenv("ARB_MAX_SLIPPAGE",  "0.01"))

ACTIVE_HOURS = list(range(int(os.getenv("ARB_HOUR_START", "0")),
                          int(os.getenv("ARB_HOUR_END",   "24"))))

SHORT_MARKETS = {
    "BTC5":  {"slug": "btc-updown-5m-",  "window": 300},
    "ETH15": {"slug": "eth-updown-15m-", "window": 900},
    "SOL15": {"slug": "sol-updown-15m-", "window": 900},
}

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
PNL_FILE  = "/app/arb_daily_pnl.txt"

SESSION = requests.Session()

import sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("arb_v3")

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
        r = SESSION.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=8)
        if r.ok:
            data = r.json()
            m = data[0] if isinstance(data, list) and len(data) > 0 else None
            if m and m.get("slug") == slug:
                outcomes  = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                token_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
                return [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
        return None
    except:
        return None

def get_best_ask(token_id):
    try:
        r = SESSION.get(CLOB_API + "/book", params={"token_id": token_id}, timeout=8)
        if r.ok:
            asks = r.json().get("asks", [])
            if asks:
                best = min(asks, key=lambda a: float(a["price"]))
                return float(best["price"]), float(best["size"])
        return None, 0
    except:
        return None, 0

def read_both_asks(up_id, down_id):
    res = {}
    def _read(side, tid):
        res[side] = get_best_ask(tid)
    t1 = threading.Thread(target=_read, args=("up", up_id))
    t2 = threading.Thread(target=_read, args=("down", down_id))
    t1.start(); t2.start(); t1.join(); t2.join()
    return res.get("up", (None, 0)), res.get("down", (None, 0))

def net_gain_after_fees(up_px, down_px, fee_per_leg):
    cout = up_px * (1 + fee_per_leg) + down_px * (1 + fee_per_leg)
    return 1.0 - cout

async def execute_arb_async(up_id, up_px_scan, down_id, down_px_scan, shares, market_key):
    global daily_pnl
    try:
        from polymarket import AsyncSecureClient

        # ── RE-VERIFICATION JUSTE AVANT L'ACHAT (anti-slippage) ──
        (up_px, up_depth), (down_px, down_depth) = read_both_asks(up_id, down_id)
        if up_px is None or down_px is None:
            log.warning("[" + market_key + "] Prix indisponibles — abandon")
            return False, 0

        total = up_px + down_px
        net   = net_gain_after_fees(up_px, down_px, FEE_PER_LEG)

        if total > SUM_MAX:
            log.warning("[" + market_key + "] Somme remontée à " + str(round(total, 3))
                        + " > " + str(SUM_MAX) + " — ABANDON (anti-slippage)")
            return False, 0

        if net < MIN_NET_GAIN:
            log.warning("[" + market_key + "] Gain net " + str(round(net, 4))
                        + " < " + str(MIN_NET_GAIN) + " — ABANDON")
            return False, 0

        shares = min(shares,
                     math.floor(TRADE_USDC / total * 100) / 100,
                     math.floor(min(up_depth, down_depth) * 100) / 100)
        if shares < 1:
            log.warning("[" + market_key + "] Profondeur insuffisante — abandon")
            return False, 0

        # ── VERIFICATION DU SOLDE (fix v3) ────────────────────────────────────
        cout_total = (up_px + down_px) * shares
        solde = get_usdc_balance()
        if solde is None:
            log.warning("[" + market_key + "] Solde indisponible — ABANDON par précaution")
            return False, 0
        if solde < cout_total * 1.05:
            log.warning("[" + market_key + "] Solde insuffisant ("
                        + str(round(solde, 2)) + "$ < "
                        + str(round(cout_total * 1.05, 2)) + "$ requis) — ABANDON")
            return False, 0
        log.info("[" + market_key + "] Solde OK: " + str(round(solde, 2))
                 + "$ pour " + str(round(cout_total, 2)) + "$ requis")

        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:

            async def buy(tid, px):
                try:
                    resp = await client.place_limit_order(
                        token_id=tid, side="BUY",
                        price=str(round(px, 4)), size=str(shares))
                    if resp.ok:
                        return True, getattr(resp, "order_id", None) or getattr(resp, "id", None)
                    return False, None
                except Exception as e:
                    log.error("Achat jambe: " + str(e))
                    return False, None

            ok_up,   oid_up   = await buy(up_id,   up_px)
            ok_down, oid_down = await buy(down_id, down_px)

            if not ok_up and not ok_down:
                log.warning("[" + market_key + "] Aucune jambe exécutée")
                return False, 0

            await asyncio.sleep(6)

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
                            oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                            ids.append(oid)
                    cancel = getattr(client, "cancel_order", None)
                    if oid_up and oid_up in ids:
                        filled["up"] = False
                        if cancel: await cancel(order_id=oid_up)
                    if oid_down and oid_down in ids:
                        filled["down"] = False
                        if cancel: await cancel(order_id=oid_down)
            except Exception as ve:
                log.warning("Vérif jambes: " + str(ve))

            if filled["up"] and filled["down"]:
                gain_net = net_gain_after_fees(up_px, down_px, FEE_PER_LEG) * shares
                log.info("[" + market_key + "] ✅ ARB VERROUILLÉ | somme "
                         + str(round(total, 3)) + " | " + str(shares) + " shares"
                         + " | gain net +" + str(round(gain_net, 2)) + "$")
                return True, (up_px + down_px) * shares

            async def sell(tid, px):
                try:
                    sp = max(0.01, round(px - 0.03, 4))
                    ss = max(0.01, round(shares - 0.01, 2))
                    await client.place_limit_order(token_id=tid, side="SELL", price=str(sp), size=str(ss))
                except Exception as e:
                    log.error("Revente: " + str(e))

            if filled["up"] and not filled["down"]:
                log.warning("[" + market_key + "] Jambe DOWN manquante — revente UP")
                await sell(up_id, up_px)
                perte = 0.04 * shares
                daily_pnl -= perte
                save_daily_pnl(daily_pnl)
                return False, 0
            if filled["down"] and not filled["up"]:
                log.warning("[" + market_key + "] Jambe UP manquante — revente DOWN")
                await sell(down_id, down_px)
                perte = 0.04 * shares
                daily_pnl -= perte
                save_daily_pnl(daily_pnl)
                return False, 0

            return False, 0
    except Exception as e:
        log.error("Exception arb: " + str(e))
        return False, 0

def execute_arb(up_id, up_px, down_id, down_px, shares, market_key):
    return asyncio.run(execute_arb_async(up_id, up_px, down_id, down_px, shares, market_key))

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
                    gain = arb["gain_net"]
                    daily_pnl += gain
                    save_daily_pnl(daily_pnl)
                    log.info("[" + arb["market"] + "] 💰 ARB RÉGLÉ | +"
                             + str(round(gain, 2)) + "$ | PnL jour: "
                             + str(round(daily_pnl, 2)) + "$")
                    with arbs_lock:
                        if arb in open_arbs:
                            open_arbs.remove(arb)
        except Exception as e:
            log.error("Erreur règlement: " + str(e))
        time.sleep(10)

def run():
    global daily_pnl, pnl_date, done_windows

    log.info("Bot ARBITRAGE v3 démarré")
    log.info("Seuil somme: " + str(SUM_MAX) + " | Gain net min: " + str(MIN_NET_GAIN)
             + " | Frais/jambe: " + str(FEE_PER_LEG)
             + " | Trade: " + str(TRADE_USDC) + "$"
             + " | Max simultanés: " + str(MAX_CONCURRENT))
    log.info("Marchés: " + ", ".join(SHORT_MARKETS.keys())
             + " | PnL: " + str(round(daily_pnl, 2)))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("WALLET manquant!")
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

            for mkey, mcfg in SHORT_MARKETS.items():
                wsize     = mcfg["window"]
                window_ts = now - (now % wsize)
                time_left = (window_ts + wsize) - now
                cache_key = mkey + "_" + str(window_ts)

                if cache_key in done_windows:
                    continue
                if time_left < 60:
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

                (up_px, up_depth), (down_px, down_depth) = read_both_asks(up["token_id"], down["token_id"])
                if up_px is None or down_px is None:
                    continue

                total = up_px + down_px
                net   = net_gain_after_fees(up_px, down_px, FEE_PER_LEG)

                if time.time() - last_heartbeat >= 300:
                    log.info("[" + mkey + "] scan | somme " + str(round(total, 3))
                             + " | net " + str(round(net, 4))
                             + " | seuil " + str(SUM_MAX)
                             + " | PnL: " + str(round(daily_pnl, 2)) + "$")
                    last_heartbeat = time.time()

                if total <= SUM_MAX and net >= MIN_NET_GAIN:
                    shares_cap   = math.floor(TRADE_USDC / total * 100) / 100
                    shares_depth = math.floor(min(up_depth, down_depth) * 100) / 100
                    shares       = min(shares_cap, shares_depth)
                    if shares < 1:
                        continue

                    log.info("[" + mkey + "] 🎯 OPPORTUNITÉ | somme " + str(round(total, 3))
                             + " | gain net +" + str(round(net * shares, 2)) + "$"
                             + " | " + str(shares) + " shares")

                    ok, cout = execute_arb(up["token_id"], up_px,
                                           down["token_id"], down_px, shares, mkey)
                    done_windows.add(cache_key)
                    if ok:
                        with arbs_lock:
                            open_arbs.append({
                                "market":   mkey,
                                "gain_net": net * shares,
                                "shares":   shares,
                                "expiry":   window_ts + wsize,
                            })

        except Exception as e:
            log.error("Erreur boucle: " + str(e))
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
