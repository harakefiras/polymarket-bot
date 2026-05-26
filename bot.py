import os, time, random, logging, requests, json, asyncio, re
from datetime import date, datetime, timezone
import threading

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "30"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "200"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.30"))
MAX_MARKET_HOURS = float(os.getenv("MAX_MARKET_HOURS", "168"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "30"))
BASE_BET = float(os.getenv("BASE_BET", "3"))
MAX_BET = float(os.getenv("MAX_BET", "50"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAINLINK_API = "https://api.real-time-attest.truflation.com/chainlink/btcusd"
TRADED_FILE = "/app/traded_markets.txt"
SEEN_TRADES_FILE = "/app/seen_trades.txt"
TRADED_WINDOWS_FILE = "/app/traded_windows.txt"
PNL_FILE = "/app/daily_pnl.txt"
TRADES_FILE = "/app/trades_history.json"

BLOCKED_KEYWORDS = ["nba", "nhl", "nfl", "mlb", "stanley", "finals", "championship", "season", "playoffs", "super bowl", "world series", "soccer", "football", "basketball", "hockey", "baseball"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

window_ref_prices = {}

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

def load_seen_trades():
    try:
        if os.path.exists(SEEN_TRADES_FILE):
            with open(SEEN_TRADES_FILE, "r") as f:
                return set(line.strip() for line in f if line.strip())
        return set()
    except:
        return set()

def save_seen_trade(trade_id):
    try:
        with open(SEEN_TRADES_FILE, "a") as f:
            f.write(trade_id + "\n")
    except:
        pass

def load_traded_windows():
    try:
        if os.path.exists(TRADED_WINDOWS_FILE):
            with open(TRADED_WINDOWS_FILE, "r") as f:
                lines = f.read().strip().split("\n")
                now = int(time.time())
                result = set()
                for line in lines:
                    line = line.strip()
                    if line:
                        ts = int(line)
                        if now - ts < 86400:
                            result.add(ts)
                return result
        return set()
    except:
        return set()

def save_traded_window(window_ts):
    try:
        with open(TRADED_WINDOWS_FILE, "a") as f:
            f.write(str(window_ts) + "\n")
    except:
        pass

def load_trades_history():
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                return json.load(f)
        return []
    except:
        return []

def save_trade(trade_data):
    try:
        history = load_trades_history()
        history.append(trade_data)
        if len(history) > 200:
            history = history[-200:]
        with open(TRADES_FILE, "w") as f:
            json.dump(history, f)
    except Exception as e:
        log.error("Erreur sauvegarde trade: " + str(e))

def analyze_patterns():
    history = load_trades_history()
    if len(history) < 10:
        return None
    winning_trades = [t for t in history if t.get("result") == "win"]
    if not winning_trades:
        return None
    win_rate = len(winning_trades) / len(history)
    log.info("Patterns: " + str(len(history)) + " trades | Win rate: " + str(round(win_rate * 100, 1)) + "%")
    return {"win_rate": win_rate, "total_trades": len(history)}

def calculate_smart_bet(gap, patterns):
    base = BASE_BET
    if abs(gap) >= 200:
        bet = MAX_BET
    elif abs(gap) >= 100:
        bet = base * 3
    elif abs(gap) >= 50:
        bet = base * 2
    else:
        bet = base
    if patterns and patterns.get("win_rate", 0) >= 0.60:
        bet = min(bet * 1.5, MAX_BET)
        log.info("Bonus win rate: mise x1.5")
    bet = round(min(bet, MAX_BET), 1)
    log.info("Gap: " + str(round(gap)) + "$ | Mise smart: " + str(bet) + " USDC")
    return bet

daily_pnl = load_daily_pnl()
pnl_date = date.today()
traded_windows = load_traded_windows()
open_positions = []
positions_lock = threading.Lock()

def load_traded_markets():
    try:
        if os.path.exists(TRADED_FILE):
            with open(TRADED_FILE, "r") as f:
                return set(line.strip() for line in f if line.strip())
        return set()
    except:
        return set()

def save_traded_market(market_id):
    try:
        with open(TRADED_FILE, "a") as f:
            f.write(market_id + "\n")
    except:
        pass

traded_markets = load_traded_markets()
seen_trades = load_seen_trades()

def check_stop_loss():
    return daily_pnl <= -STOP_LOSS_USDC

def open_val():
    with positions_lock:
        return sum(p["size"] for p in open_positions)

def api_get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.ok:
                return r
        except Exception as e:
            log.warning("Retry " + str(i+1) + "/" + str(retries) + " | " + str(e))
            time.sleep(2)
    return None

def get_chainlink_btc_price():
    try:
        r = api_get(CHAINLINK_API)
        if r:
            data = r.json()
            price = float(data.get("price") or data.get("result") or data.get("answer") or 0)
            if price > 0:
                log.info("Chainlink BTC: " + str(round(price)))
                return price
        r2 = api_get("https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        if r2:
            price = float(r2.json().get("price", 0))
            log.info("Binance BTC (fallback): " + str(round(price)))
            return price
        return 0
    except Exception as e:
        log.error("Erreur prix BTC: " + str(e))
        return 0

def is_market_valid(market):
    question = (market.get("question") or "").lower()
    slug = (market.get("slug") or "").lower()
    for kw in BLOCKED_KEYWORDS:
        if kw in question or kw in slug:
            return False
    end_date = market.get("endDateIso") or market.get("endDate")
    if not end_date:
        return False
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours_left = (end - now).total_seconds() / 3600
        return 0 < hours_left <= MAX_MARKET_HOURS
    except:
        return False

def get_active_markets():
    try:
        r = api_get(GAMMA_API + "/markets", {"active": "true", "limit": 50})
        if not r:
            return []
        markets = r.json()
        filtered = [m for m in markets if is_market_valid(m)]
        log.info("Marches valides: " + str(len(filtered)))
        return filtered
    except Exception as e:
        log.error("Erreur marches: " + str(e))
        return []

def get_recent_trades(market_id):
    try:
        r = api_get(DATA_API + "/trades", {"market": market_id, "limit": 20})
        if not r:
            return []
        trades = r.json()
        return trades if isinstance(trades, list) else []
    except:
        return []

def detect_whales(markets):
    whales = []
    seen_market_tokens = set()
    for m in markets:
        mid = m.get("conditionId") or m.get("id")
        question = m.get("question", "?")
        if not mid or mid in traded_markets:
            continue
        trades = get_recent_trades(mid)
        if not isinstance(trades, list):
            continue
        for t in trades:
            tid = t.get("transactionHash")
            if not tid or tid in seen_trades:
                continue
            size = float(t.get("size", 0))
            price = float(t.get("price", 0.5))
            notional = size * price
            token_id = str(t.get("asset", ""))
            outcome = t.get("outcome", "Yes")
            market_key = mid + "_" + outcome
            if market_key in seen_market_tokens:
                continue
            if notional >= MIN_WHALE_USDC and 0.45 <= price <= 0.65 and token_id:
                whales.append({
                    "id": tid,
                    "market_id": mid,
                    "market": question,
                    "token_id": token_id,
                    "price": price,
                    "notional": notional,
                    "outcome": outcome,
                    "market_key": market_key
                })
                seen_market_tokens.add(market_key)
    return whales

def get_btc_market(window_ts):
    try:
        slug = "btc-updown-5m-" + str(window_ts)
        r = api_get(GAMMA_API + "/markets", {"slug": slug})
        if r:
            data = r.json()
            market = data[0]
