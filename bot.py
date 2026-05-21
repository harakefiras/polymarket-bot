import os, time, random, logging, requests
from datetime import date

MIN_WHALE_USDC  = float(os.getenv("MIN_WHALE_USDC", "500"))
BET_SIZE_USDC   = float(os.getenv("BET_SIZE_USDC",  "10"))
MIN_PROB        = float(os.getenv("MIN_PROB",        "0.20"))
COPY_DELAY_S    = float(os.getenv("COPY_DELAY_S",   "5"))
STOP_LOSS_USDC  = float(os.getenv("STOP_LOSS_USDC", "50"))
POLL_INTERVAL   = float(os.getenv("POLL_INTERVAL",  "3600"))
PRIVATE_KEY     = os.environ.get("PRIVATE_KEY", "")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("copybot")

daily_pnl = 0.0
pnl_date  = date.today()
seen_trades = set()

def get_active_markets():
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"active": "true", "limit": 20}, timeout=10)
        return r.json() if r.ok else []
    except Exception as e:
        log.warning(f"Erreur marches: {e}")
        return []

def get_recent_trades(market_id):
    try:
        r = requests.get(f"{CLOB_API}/trades", params={"market": market_id, "limit": 20}, timeout=10)
        return r.json().get("data", []) if r.ok else []
    except Exception as e:
        log.warning(f"Erreur trades: {e}")
        return []

def detect_whales(markets):
    whales = []
    for market in markets[:20]:
        market_id = market.get("conditionId") or market.get("id")
        question  = market.get("question", "?")
        if not market_id:
            continue
        for t in get_recent_trades(market_id):
            tid = t.get("id")
            if tid in seen_trades:
                continue
            size  = float(t.get("size", 0))
            price = float(t.get("price", 0.5))
            notional = size * price
            if notional >= MIN_WHALE_USDC and MIN_PROB <= price <= (1 - MIN_PROB):
                whales.append({"id": tid, "market": question, "token_id": market_id, "price": price, "notional": notional, "outcome": t.get("outcome", "YES")})
    return whales

def place_order(trade):
    try:
        from eth_account import Account
        account = Account.from_key(PRIVATE_KEY)
        order = {"market": trade["token_id"], "side": "BUY", "price": round(trade["price"], 4), "size": round(BET_SIZE_USDC / trade["price"], 2), "type": "GTC"}
        r = requests.post(f"{CLOB_API}/order", json=order, headers={"POLY_ADDRESS": account.address}, timeout=10)
        if r.ok:
            log.info(f"OK Trade copie: {BET_SIZE_USDC} USDC sur '{trade['market']}'")
            seen_trades.add(trade["id"])
        else:
            log.error(f"Erreur ordre: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log.error(f"Exception ordre: {e}")

def run():
    global daily_pnl, pnl_date
    log.info("Polymarket Copy Bot v2 demarre")
    log.info("Whale min: " + str(MIN_WHALE_USDC) + " USDC | Mise: " + str(BET_SIZE_USDC) + " USDC | Stop-loss: -" + str(STOP_LOSS_USDC) + " USDC/jour")
