import os, time, logging, requests, json, asyncio, threading
from datetime import date, datetime, timezone

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
BET = float(os.getenv("BET", "3"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.30"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "10"))
LOSS_COOLDOWN = int(os.getenv("LOSS_COOLDOWN", "900"))   # 15 min
MONITOR_INTERVAL = 30

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
PNL_FILE = "/app/daily_pnl.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
last_loss_time = 0
open_positions = []
traded_windows = set()
lock = threading.Lock()

def load_pnl():
    try:
        if os.path.exists(PNL_FILE):
            with open(PNL_FILE) as f:
                l = f.read().strip().split("\n")
                if len(l) == 2 and l[0] == str(date.today()):
                    return float(l[1])
    except: pass
    return 0.0

def save_pnl(p):
    try:
        with open(PNL_FILE, "w") as f:
            f.write(str(date.today()) + "\n" + str(round(p, 2)))
    except: pass

daily_pnl = load_pnl()

def run_async(coro):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        r = loop.run_until_complete(coro)
        loop.close()
        return r
    except Exception as e:
        log.error("async: " + str(e))
        return False

def get_btc_slope():
    try:
        r = requests.get(BINANCE_API + "/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 5}, timeout=10)
        c = r.json()
        closes = [float(x[4]) for x in c]
        s = closes[-1] - closes[0]
        log.info("BTC " + str(round(closes[0])) + " -> " + str(round(closes[-1])) + " | pente " + str(round(s)) + "$")
        return s
    except: return 0

def get_btc_price():
    try:
        r = requests.get(BINANCE_API + "/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
        return float(r.json().get("price", 0))
    except: return 0

def get_btc_market(ts):
    try:
        r = requests.get(GAMMA_API + "/markets", params={"slug": "btc-updown-5m-" + str(ts)}, timeout=10)
        d = r.json()
        m = d[0] if isinstance(d, list) and d else None
        if m:
            o = json.loads(m.get("outcomes","[]")) if isinstance(m.get("outcomes"),str) else m.get("outcomes",[])
            t = json.loads(m.get("clobTokenIds","[]")) if isinstance(m.get("clobTokenIds"),str) else m.get("clobTokenIds",[])
            m["tokens"] = [{"outcome": o[i], "token_id": t[i]} for i in range(len(o))]
            return m
    except Exception as e:
        log.error("marche: " + str(e))
    return None

def get_token_price(tid):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": tid}, timeout=10)
        return float(r.json().get("price", 0))
    except: return 0

async def order_async(tid, side, price, shares):
    from polymarket import AsyncSecureClient
    async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as c:
        r = await c.place_limit_order(token_id=tid, side=side,
            price=str(round(price,4)), size=str(round(shares,2)))
        return r.ok

def buy(tid, outcome, price):
    shares = round(BET / price, 2)
    if run_async(order_async(tid, "BUY", price, shares)):
        log.info("ACHAT " + outcome + " " + str(BET) + "$ @ " + str(round(price,2)))
        with lock:
            open_positions.append({"tid": tid, "entry": price, "shares": shares, "side": outcome})
        return True
    return False

def sell(tid, shares, price):
    return run_async(order_async(tid, "SELL", price, shares))

def monitor():
    global daily_pnl, last_loss_time
    log.info("Monitor 30s demarre")
    while True:
        try:
            with lock:
                positions = list(open_positions)
            for pos in positions:
                cur = get_token_price(pos["tid"])
                if cur <= 0: continue
                log.info("Suivi " + pos["side"] + " | token " + str(round(cur,2)))
                if cur >= 0.98:
                    gain = (1.0 - pos["entry"]) * pos["shares"]
                    daily_pnl += gain; save_pnl(daily_pnl)
                    log.info("GAGNE +" + str(round(gain,2)) + " | jour " + str(round(daily_pnl,2)))
                    with lock: open_positions.remove(pos)
                elif cur <= 0.02:
                    loss = (cur - pos["entry"]) * pos["shares"]
                    daily_pnl += loss; save_pnl(daily_pnl)
                    last_loss_time = time.time()
                    log.info("PERDU | jour " + str(round(daily_pnl,2)))
                    with lock: open_positions.remove(pos)
                elif cur <= STOP_LOSS_PRICE:
                    if sell(pos["tid"], pos["shares"], cur):
                        loss = (cur - pos["entry"]) * pos["shares"]
                        daily_pnl += loss; save_pnl(daily_pnl)
                        last_loss_time = time.time()
                        log.info("VENTE STOP @ " + str(round(cur,2)) + " | jour " + str(round(daily_pnl,2)))
                        with lock: open_positions.remove(pos)
        except Exception as e:
            log.error("monitor: " + str(e))
        time.sleep(MONITOR_INTERVAL)

def run():
    global daily_pnl, pnl_date
    log.info("Bot BTC 5min - mise fixe " + str(BET) + "$ | stop " + str(STOP_LOSS_PRICE) + " | perte max jour " + str(DAILY_LOSS_LIMIT) + "$")
    log.info("NOTE: PnL = estimation interne, verifie ton vrai solde wallet")
    if not PRIVATE_KEY.startswith("0x") or not WALLET.startswith("0x"):
        log.error("Cles manquantes!"); return

    threading.Thread(target=monitor, daemon=True).start()

    while True:
        try:
            if date.today() != pnl_date:
                daily_pnl = 0.0; pnl_date = date.today(); traded_windows.clear(); save_pnl(0.0)
                log.info("Nouveau jour")

            if daily_pnl <= -DAILY_LOSS_LIMIT:
                log.info("Perte max jour atteinte (" + str(round(daily_pnl,2)) + "$) - pause jusqu'a demain")
                time.sleep(3600); continue

            # Cooldown 15 min apres une perte
            if time.time() - last_loss_time < LOSS_COOLDOWN:
                reste = int(LOSS_COOLDOWN - (time.time() - last_loss_time))
                log.info("Pause post-perte: " + str(reste) + "s")
                time.sleep(min(reste, 60)); continue

            with lock:
                busy = len(open_positions) > 0
            if busy:
                time.sleep(30); continue

            now = int(time.time())
            sec = now % 300
            ts = now - sec
            if ts in traded_windows:
                time.sleep(30); continue

            if sec > 90:
                time.sleep(300 - sec + 2); continue
            if sec < 30:
                log.info("Stabilisation 30s..."); time.sleep(30 - sec + 5)

            slope = get_btc_slope()
            if slope > 25: target = "Up"
            elif slope < -25: target = "Down"
            else:
                log.info("Pente faible - pas de trade"); time.sleep(60); continue

            m = get_btc_market(ts)
            if not m:
                time.sleep(30); continue
            for tok in m.get("tokens", []):
                if tok["outcome"] == target:
                    price = get_token_price(tok["token_id"]) or 0.5
                    log.info(target + " @ " + str(round(price,2)))
                    if 0.45 <= price <= 0.60:
                        if buy(tok["token_id"], target, price):
                            traded_windows.add(ts)
                    else:
                        log.info("Prix hors zone (45-60) - skip")
        except Exception as e:
            log.error("run: " + str(e))
        time.sleep(30)

if __name__ == "__main__":
    run()
