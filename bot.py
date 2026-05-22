import os, time, random, logging, requests
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "500"))
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "10"))
MIN_PROB = float(os.getenv("MIN_PROB", "0.20"))
COPY_DELAY_S = float(os.getenv("COPY_DELAY_S", "5"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "50"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "60"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "150"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
seen_trades = set()
open_positions = []

def get_markets():
    try:
        r = requests.get(GAMMA_API + "/markets", params={"active": "true", "limit": 20}, timeout=10)
        return r.json() if r.ok else []
    except Exception as e:
        log.error("Erreur marches: " + str(e))
        return []

def get_trades(market_id):
    try:
        r = requests.get(CLOB_API + "/trades", params={"market": market_id, "limit": 20}, timeout=10)
        return r.json().get("data", []) if r.ok else []
    except Exception as e:
        log.error("Erreur trades: " + str(e))
        return []

def open_value():
    return sum(p["size"] for p in open_positions)

def detect_whales(markets):
    whales = []
    for m in markets[:20]:
        mid = m.get("conditionId") or m.get("id")
        if not mid:
            continue
        for t in get_trades(mid):
            tid = t.get("id")
            if tid in seen_trades:
                continue
            size = float(t.get("size", 0))
            price = float(t.get("price", 0.5))
            notional = size * price
            if notional >= MIN_WHALE_USDC and MIN_PROB <= price <= (1 - MIN_PROB):
                whales.append({"id": tid, "market": m.get("question", "?"), "token_id": mid, "price": price, "notional": notional})
    return whales

def place_order(trade):
    try:
        from eth_account import Account
        account = Account.from_key(PRIVATE_KEY)
        order = {"market": trade["token_id"], "side": "BUY", "price": round(trade["price"], 4), "size": round(BET_SIZE_USDC / trade["price"], 2), "type": "GTC"}
        r = requests.post(CLOB_API + "/order", json=order, headers={"POLY_ADDRESS": account.address}, timeout=10)
        if r.ok:
            log.info("Trade copie: " + str(BET_SIZE_USDC) + " USDC sur " + trade["market"])
            seen_trades.add(trade["id"])
            open_positions.append({"id": trade["id"], "size": BET_SIZE_USDC})
        else:
            log.error("Erreur ordre: " + str(r.status_code) + " " + r.text[:100])
    except Exception as e:
        log.error("Exception ordre: " + str(e))

def run():
    global daily_pnl, pnl_date
    log.info("Bot demarre!")
    log.info("Whale min: " + str(MIN_WHALE_USDC) + " | Mise: " + str(BET_SIZE_USDC) + " | Plafond: " + str(MAX_OPEN_USDC))
    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date = today
            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss atteint! Pause 1h.")
                time.sleep(3600)
                continue
            val = open_value()
            log.info("Positions ouvertes: " + str(round(val, 2)) + "/" + str(MAX_OPEN_USDC) + " USDC")
            if val >= MAX_OPEN_USDC:
                log.info("Plafond atteint - attente retours...")
                time.sleep(POLL_INTERVAL)
                continue
            log.info("Scan en cours...")
            markets = get_markets()
            log.info("Marches trouves: " + str(len(markets)))
            whales = detect_whales(markets)
            if whales:
                log.info(str(len(whales)) + " whale(s) detectee(s)!")
                for w in whales:
                    if open_value() + BET_SIZE_USDC > MAX_OPEN_USDC:
                        log.info("Plafond atteint - stop mises")
                        break
                    log.info("Whale: " + str(round(w["notional"])) + " USDC sur " + w["market"])
                    time.sleep(COPY_DELAY_S)
                    place_order(w)
            else:
                log.info("Aucune whale detectee")
        except Exception as e:
            log.error("Erreur: " + str(e))
        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait)) + "s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
