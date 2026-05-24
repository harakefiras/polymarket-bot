import os, time, logging, requests, hashlib, hmac, math
from datetime import date

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
BET_USDC = float(os.getenv("BINANCE_BET_USDC", "10"))
TAKE_PROFIT_PCT = float(os.getenv("BINANCE_TAKE_PROFIT", "0.02"))
STOP_LOSS_PCT = float(os.getenv("BINANCE_STOP_LOSS_PCT", "0.01"))
DAILY_STOP_LOSS = float(os.getenv("BINANCE_STOP_LOSS", "30"))
POLL_INTERVAL = float(os.getenv("BINANCE_POLL_INTERVAL", "60"))

BASE_URL = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("binance_bot")

daily_pnl = 0.0
pnl_date = date.today()
open_trade = None

def sign(params):
    query = "&".join([str(k) + "=" + str(v) for k, v in params.items()])
    signature = hmac.new(
        BINANCE_SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return query + "&signature=" + signature

def get_headers():
    return {"X-MBX-APIKEY": BINANCE_API_KEY}

def get_price():
    try:
        r = requests.get(BASE_URL + "/api/v3/ticker/price", params={"symbol": SYMBOL}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def get_slope():
    try:
        r = requests.get(
            BASE_URL + "/api/v3/klines",
            params={"symbol": SYMBOL, "interval": "1m", "limit": 5},
            timeout=10
        )
        if not r.ok:
            return 0
        candles = r.json()
        closes = [float(c[4]) for c in candles]
        slope = closes[-1] - closes[0]
        log.info("BTC courbe: " + str(round(closes[0])) + " -> " + str(round(closes[-1])) + " | Pente: " + str(round(slope)) + "$")
        return slope
    except:
        return 0

def get_account_balance():
    try:
        ts = int(time.time() * 1000)
        params = {"timestamp": ts}
        query = sign(params)
        r = requests.get(BASE_URL + "/api/v3/account?" + query, headers=get_headers(), timeout=10)
        if r.ok:
            balances = r.json().get("balances", [])
            for b in balances:
                if b["asset"] == "USDT":
                    return float(b["free"])
        return 0
    except Exception as e:
        log.error("Erreur balance: " + str(e))
        return 0

def get_lot_size():
    try:
        r = requests.get(BASE_URL + "/api/v3/exchangeInfo", params={"symbol": SYMBOL}, timeout=10)
        if r.ok:
            filters = r.json()["symbols"][0]["filters"]
            for f in filters:
                if f["filterType"] == "LOT_SIZE":
                    return float(f["stepSize"])
        return 0.001
    except:
        return 0.001

def round_qty(qty, step):
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(math.floor(qty / step) * step, precision)

def place_buy_order(price):
    try:
        step = get_lot_size()
        qty = round_qty(BET_USDC / price, step)
        ts = int(time.time() * 1000)
        params = {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "timestamp": ts
        }
        query = sign(params)
        r = requests.post(BASE_URL + "/api/v3/order?" + query, headers=get_headers(), timeout=10)
        if r.ok:
            log.info("BUY " + str(qty) + " BTC @ ~" + str(round(price)) + " | " + str(BET_USDC) + " USDT")
            return {"qty": qty, "entry_price": price}
        else:
            log.error("Erreur BUY: " + str(r.text))
            return None
    except Exception as e:
        log.error("Exception BUY: " + str(e))
        return None

def place_sell_order(qty):
    try:
        ts = int(time.time() * 1000)
        params = {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
            "timestamp": ts
        }
        query = sign(params)
        r = requests.post(BASE_URL + "/api/v3/order?" + query, headers=get_headers(), timeout=10)
        if r.ok:
            log.info("SELL " + str(qty) + " BTC")
            return True
        else:
            log.error("Erreur SELL: " + str(r.text))
            return False
    except Exception as e:
        log.error("Exception SELL: " + str(e))
        return False

def run():
    global daily_pnl, pnl_date, open_trade
    log.info("Bot Binance Scalping demarre!")
    log.info("Symbol: " + SYMBOL + " | Mise: " + str(BET_USDC) + " USDT | TP: " + str(TAKE_PROFIT_PCT*100) + "% | SL: " + str(STOP_LOSS_PCT*100) + "%")

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        log.error("Cles API Binance manquantes!")
        return

    balance = get_account_balance()
    log.info("Balance USDT: " + str(round(balance, 2)))

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date = today

            if daily_pnl <= -DAILY_STOP_LOSS:
                log.warning("Stop-loss journalier atteint! Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                continue

            current_price = get_price()
            if current_price <= 0:
                time.sleep(POLL_INTERVAL)
                continue

            if open_trade:
                entry = open_trade["entry_price"]
                qty = open_trade["qty"]
                pnl_pct = (current_price - entry) / entry

                log.info("Position BTC | Entry: " + str(round(entry)) + " | Current: " + str(round(current_price)) + " | PnL: " + str(round(pnl_pct * 100, 2)) + "%")

                if pnl_pct >= TAKE_PROFIT_PCT:
                    log.info("TAKE PROFIT! +" + str(round(pnl_pct * 100, 2)) + "%")
                    if place_sell_order(qty):
                        pnl = BET_USDC * pnl_pct
                        daily_pnl += pnl
                        log.info("PnL: +" + str(round(pnl, 2)) + " USDT | Total jour: " + str(round(daily_pnl, 2)))
                        open_trade = None

                elif pnl_pct <= -STOP_LOSS_PCT:
                    log.info("STOP LOSS! " + str(round(pnl_pct * 100, 2)) + "%")
                    if place_sell_order(qty):
                        pnl = BET_USDC * pnl_pct
                        daily_pnl += pnl
                        log.info("PnL: " + str(round(pnl, 2)) + " USDT | Total jour: " + str(round(daily_pnl, 2)))
                        open_trade = None

            else:
                slope = get_slope()

                if slope > 30:
                    log.info("Signal UP +" + str(round(slope)) + "$ - BUY!")
                    trade = place_buy_order(current_price)
                    if trade:
                        open_trade = trade

                elif slope < -30:
                    log.info("Signal DOWN " + str(round(slope)) + "$ - pas de trade")

                else:
                    log.info("Pente faible (" + str(round(slope)) + "$) - attente")

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
