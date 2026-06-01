import os, time, logging, requests, hmac, hashlib, urllib.parse
from time import time as ts

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
SYMBOL = os.getenv("BINANCE_SYMBOL", "BNBUSDT")
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "150"))
DEVIATION_PCT = float(os.getenv("DEVIATION_PCT", "5"))
AVERAGE_DAYS = int(os.getenv("AVERAGE_DAYS", "7"))
ORDER_TIMEOUT_HOURS = float(os.getenv("ORDER_TIMEOUT_HOURS", "48"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

BINANCE_API = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("binance_bot")

def sign(params):
    query = urllib.parse.urlencode(params)
    sig = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def headers():
    return {"X-MBX-APIKEY": BINANCE_API_KEY}

def get_average_price(days=7):
    try:
        limit = days * 24
        r = requests.get(BINANCE_API + "/api/v3/klines",
            params={"symbol": SYMBOL, "interval": "1h", "limit": limit}, timeout=10)
        if not r.ok:
            return 0
        closes = [float(c[4]) for c in r.json()]
        avg = sum(closes) / len(closes)
        log.info("Moyenne " + str(days) + "j: " + str(round(avg, 2)) + "$")
        return avg
    except Exception as e:
        log.error("Erreur moyenne: " + str(e))
        return 0

def get_current_price():
    try:
        r = requests.get(BINANCE_API + "/api/v3/ticker/price",
            params={"symbol": SYMBOL}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def get_symbol_info():
    try:
        r = requests.get(BINANCE_API + "/api/v3/exchangeInfo",
            params={"symbol": SYMBOL}, timeout=10)
        if not r.ok:
            return None
        for s in r.json().get("symbols", []):
            if s["symbol"] == SYMBOL:
                return s
        return None
    except:
        return None

def round_quantity(qty, step_size):
    precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
    return round(qty, precision)

def round_price(price, tick_size):
    precision = len(str(tick_size).rstrip("0").split(".")[-1]) if "." in str(tick_size) else 0
    return round(price, precision)

def get_open_orders():
    try:
        params = {"symbol": SYMBOL, "timestamp": int(ts() * 1000)}
        params = sign(params)
        r = requests.get(BINANCE_API + "/api/v3/openOrders",
            params=params, headers=headers(), timeout=10)
        if r.ok:
            return r.json()
        return []
    except:
        return []

def place_limit_order(side, price, quantity):
    try:
        params = {
            "symbol": SYMBOL,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": str(quantity),
            "price": str(price),
            "timestamp": int(ts() * 1000),
        }
        params = sign(params)
        r = requests.post(BINANCE_API + "/api/v3/order",
            params=params, headers=headers(), timeout=10)
        if r.ok:
            data = r.json()
            log.info("ORDRE " + side + " | " + str(quantity) + " " + SYMBOL + " @ " + str(price) + "$ | ID: " + str(data.get("orderId")))
            return data.get("orderId")
        log.error("Erreur ordre: " + str(r.text))
        return None
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return None

def cancel_order(order_id):
    try:
        params = {"symbol": SYMBOL, "orderId": order_id, "timestamp": int(ts() * 1000)}
        params = sign(params)
        r = requests.delete(BINANCE_API + "/api/v3/order",
            params=params, headers=headers(), timeout=10)
        if r.ok:
            log.info("Ordre " + str(order_id) + " annule")
            return True
        return False
    except:
        return False

def get_order_status(order_id):
    try:
        params = {"symbol": SYMBOL, "orderId": order_id, "timestamp": int(ts() * 1000)}
        params = sign(params)
        r = requests.get(BINANCE_API + "/api/v3/order",
            params=params, headers=headers(), timeout=10)
        if r.ok:
            return r.json().get("status")
        return None
    except:
        return None

def get_asset_balance():
    try:
        params = {"timestamp": int(ts() * 1000)}
        params = sign(params)
        r = requests.get(BINANCE_API + "/api/v3/account",
            params=params, headers=headers(), timeout=10)
        if r.ok:
            asset = SYMBOL.replace("USDT", "")
            for b in r.json().get("balances", []):
                if b["asset"] == asset:
                    return float(b["free"])
        return 0
    except:
        return 0

def run():
    log.info("Bot Binance Grid - " + SYMBOL + " | Moyenne " + str(AVERAGE_DAYS) + "j | +-" + str(DEVIATION_PCT) + "% | Mise: " + str(TRADE_AMOUNT_USDT) + "$")

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        log.error("Cles API manquantes!")
        return

    step_size = 0.01
    tick_size = 0.1
    while True:
        symbol_info = get_symbol_info()
        if symbol_info:
            for f in symbol_info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                if f["filterType"] == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
            log.info("Symbole OK | step: " + str(step_size) + " | tick: " + str(tick_size))
            break
        log.error("Infos symbole indisponibles - retry dans 60s")
        time.sleep(60)

    buy_order_id = None
    sell_order_id = None
    buy_time = None
    sell_time = None
    fixed_buy_price = None
    fixed_sell_price = None
    fixed_quantity = None
    state = "IDLE"

    log.info("Verification des ordres existants sur Binance...")
    open_orders = get_open_orders()
    if open_orders:
        log.info(str(len(open_orders)) + " ordre(s) existant(s) trouve(s)")
        for o in open_orders:
            order_id = o.get("orderId")
            side = o.get("side")
            price = float(o.get("price", 0))
            qty = float(o.get("origQty", 0))
            order_time = o.get("time", 0)
            real_time = order_time / 1000 if order_time else time.time()
            heures_ecoule = round((time.time() - real_time) / 3600, 1)

            log.info("Reprise ordre: " + side + " @ " + str(price) + "$ | ID: " + str(order_id) + " | Age: " + str(heures_ecoule) + "h")

            if side == "BUY":
                buy_order_id = order_id
                fixed_buy_price = price
                fixed_quantity = qty
                buy_time = real_time
                avg = get_average_price(AVERAGE_DAYS)
                fixed_sell_price = round_price(avg * (1 + DEVIATION_PCT / 100), tick_size)
                state = "IDLE"
                log.info("Ordre ACHAT repris | Vente cible: " + str(fixed_sell_price) + "$ | Timeout dans " + str(round(ORDER_TIMEOUT_HOURS - heures_ecoule, 1)) + "h")

            elif side == "SELL":
                sell_order_id = order_id
                fixed_sell_price = price
                fixed_quantity = qty
                sell_time = real_time
                state = "HOLDING"
                log.info("Ordre VENTE repris @ " + str(price) + "$ | Timeout dans " + str(round(ORDER_TIMEOUT_HOURS - heures_ecoule, 1)) + "h")
    else:
        log.info("Aucun ordre existant - demarrage propre")

    while True:
        try:
            current_price = get_current_price()
            if current_price <= 0:
                log.error("Prix introuvable - retry dans 60s")
                time.sleep(60)
                continue

            if state == "IDLE":
                avg = get_average_price(AVERAGE_DAYS)
                if avg <= 0:
                    time.sleep(CHECK_INTERVAL)
                    continue

                buy_target = round_price(avg * (1 - DEVIATION_PCT / 100), tick_size)
                sell_target = round_price(avg * (1 + DEVIATION_PCT / 100), tick_size)
                quantity = round_quantity(TRADE_AMOUNT_USDT / buy_target, step_size)

                log.info("Prix: " + str(round(current_price, 2)) + "$ | Achat cible: " + str(buy_target) + "$ | Vente cible: " + str(sell_target) + "$")

                if buy_order_id is None:
                    log.info("Placement ordre ACHAT @ " + str(buy_target) + "$")
                    buy_order_id = place_limit_order("BUY", buy_target, quantity)
                    if buy_order_id:
                        buy_time = time.time()
                        fixed_buy_price = buy_target
                        fixed_sell_price = sell_target
                        fixed_quantity = quantity
                else:
                    status = get_order_status(buy_order_id)
                    heures = round((time.time() - buy_time) / 3600, 1) if buy_time else 0
                    log.info("Statut achat: " + str(status) + " @ " + str(fixed_buy_price) + "$ | Age: " + str(heures) + "h/" + str(ORDER_TIMEOUT_HOURS) + "h")

                    if status == "FILLED":
                        log.info("ACHAT EXECUTE @ " + str(fixed_buy_price) + "$ | Attente vente @ " + str(fixed_sell_price) + "$")
                        buy_order_id = None
                        state = "HOLDING"
                    elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                        log.info("Ordre achat annule - replacement")
                        buy_order_id = None
                        buy_time = None
                    elif buy_time and (time.time() - buy_time) > ORDER_TIMEOUT_HOURS * 3600:
                        log.info("Timeout " + str(ORDER_TIMEOUT_HOURS) + "h - annulation et replacement")
                        cancel_order(buy_order_id)
                        buy_order_id = None
                        buy_time = None

            elif state == "HOLDING":
                log.info("Prix: " + str(round(current_price, 2)) + "$ | Vente cible: " + str(fixed_sell_price) + "$")

                if sell_order_id is None:
                    balance = get_asset_balance()
                    sell_qty = round_quantity(min(fixed_quantity, balance), step_size)
                    log.info("Placement ordre VENTE @ " + str(fixed_sell_price) + "$")
                    sell_order_id = place_limit_order("SELL", fixed_sell_price, sell_qty)
                    if sell_order_id:
                        sell_time = time.time()
                else:
                    status = get_order_status(sell_order_id)
                    heures = round((time.time() - sell_time) / 3600, 1) if sell_time else 0
                    log.info("Statut vente: " + str(status) + " @ " + str(fixed_sell_price) + "$ | Age: " + str(heures) + "h/" + str(ORDER_TIMEOUT_HOURS) + "h")

                    if status == "FILLED":
                        profit = round((fixed_sell_price - fixed_buy_price) * fixed_quantity, 2)
                        log.info("VENTE EXECUTE @ " + str(fixed_sell_price) + "$ | Profit estime: +" + str(profit) + "$")
                        sell_order_id = None
                        sell_time = None
                        fixed_buy_price = None
                        fixed_sell_price = None
                        fixed_quantity = None
                        state = "IDLE"
                    elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                        sell_order_id = None
                        sell_time = None
                    elif sell_time and (time.time() - sell_time) > ORDER_TIMEOUT_HOURS * 3600:
                        log.info("Timeout vente - replacement au meme prix fixe")
                        cancel_order(sell_order_id)
                        sell_order_id = None
                        sell_time = None

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
