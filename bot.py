import os, time, random, logging, requests
from datetime import date

MIN_WHALE_USDC  = float(os.getenv("MIN_WHALE_USDC", "500"))
BET_SIZE_USDC   = float(os.getenv("BET_SIZE_USDC",  "10"))
MIN_PROB        = float(os.getenv("MIN_PROB",        "0.20"))
COPY_DELAY_S    = float(os.getenv("COPY_DELAY_S",   "5"))
STOP_LOSS_USDC  = float(os.getenv("STOP_LOSS_USDC", "50"))
POLL_INTERVAL   = float(os.getenv("POLL_INTERVAL",  "3600"))
PRIVATE_KEY     = os.environ.get("PRIVATE_KEY", "")
MAX_OPEN_USDC   = float(os.getenv("MAX_OPEN_USDC",  "150"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("copybot")

daily_pnl    = 0.0
pnl_date     = date.today()
seen_trades  = set()
open_positions = []

def get_active_markets():
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"active": "true", "limit": 20}, timeout=10)
        return r.json() if r.ok else []
    except Exception as e:
        log.warning("Erreur marches: " + str(e))
        return []

def get_recent_trades(market_id):
    try:
        r = requests.get(f"{CLOB_API}/trades", params={"market": market_id, "limit": 20}, timeout=10)
        return r.json().get("data", []) if r.ok else []
    except Exception as e:
        log.warning("Erreur trades: " + str(e))
        return []

def get_open_positions_value():
    try:
        from eth_account import Account
        account = Account.from_key(PRIVATE_KEY)
        r = requests.get(f"{CLOB_API}/positions", params={"user": account.address}, timeout=10)
        if r.ok:
            positions = r.json().get("data", [])
            total = sum(float(p.get("currentValue", 0)) for p in positions)
            return total
        return sum(p["size"] for p in open_positions)
    except:
        return sum(p["size"] for p in open_positions)

def detect_whales(markets):
    whales = []
    for market in markets[:20]:
        market_id = market.get("conditionId") or market.get("id")
        question  = market.get("question", "?")
        if not market_id:
            continue
