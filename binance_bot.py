import os, time, logging, requests, hashlib, hmac, math, json
from datetime import date

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
DAILY_STOP_LOSS = float(os.getenv("BINANCE_STOP_LOSS", "50"))
STOP_LOSS_PCT = float(os.getenv("GRID_STOP_LOSS_PCT", "0.05"))
GRID_LEVELS = int(os.getenv("GRID_LEVELS", "10"))
GRID_SPACING_PCT = float(os.getenv("GRID_SPACING_PCT", "0.003"))
USDT_PER_LEVEL = float(os.getenv("USDT_PER_LEVEL", "30"))
POLL_INTERVAL = float(os.getenv("BINANCE_POLL_INTERVAL", "30"))

BASE_URL = "https://api.binance.com"
GRID_FILE = "/app/grid_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("grid_bot")

daily_pnl = 0.0
pnl_date = date.today()
grids = {}

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

def get_price(symbol):
    try:
        r = requests.get(BASE_URL + "/api/v3/ticker/price", params={"symbol": symbol}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def get_lot_size(symbol):
    try:
        r = requests.get(BASE_URL + "/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=10)
        if r.ok:
            filters = r.json()["symbols"][0]["filters"]
            for f in filters:
                if f["filterType"] == "LOT_SIZE":
                    return float(f["stepSize"]), float(f["minQty"])
        return 0.001, 0.001
    except:
        return 0.001, 0.001

def round_qty(qty, step):
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(math.floor(qty / step) * step, precision)

def get_open_orders(symbol):
    try:
        ts = int(time.time() * 1000)
        params = {"symbol": symbol, "timestamp": ts}
        query = sign(params)
        r = requests.get(BASE_URL + "/api/v3/openOrders?" + query, headers=get_headers(), timeout=10)
        if r.ok:
            return r.json()
        return []
    except:
        return []

def cancel_all_orders(symbol):
    try:
        ts = int(time.time() * 1000)
        params = {"symbol": symbol, "timestamp": ts}
        query = sign(params)
        r = requests.delete(BASE_URL + "/api/v3/openOrders?" + query, headers=get_headers(), timeout=10)
        return r.ok
    except:
        return False

def save_grid_state():
    try:
        state = {}
        for symbol, grid in grids.items():
            state[symbol] = {
                "entry_price": grid["entry_price"],
                "stop_loss": grid["stop_loss"],
                "pnl": grid["pnl"],
                "levels": [{
                    "buy_price": l["buy_price"],
                    "sell_price": l["sell_price"],
                    "buy_order": l["buy_order"],
                    "sell_order": l["sell_order"],
                    "filled_buy": l["filled_buy"],
                } for l in grid["levels"]]
            }
        with open(GRID_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error("Erreur save grid: " + str(e))

def load_grid_state():
    try:
        if os.path.exists(GRID_FILE):
            with open(GRID_FILE, "r") as f:
                return json.load(f)
        return {}
    except:
        return {}

def place_order(symbol, side, price, usdt_amount):
    try:
        current_price = get_price(symbol)
        step, min_qty = get_lot_size(symbol)
        qty = round_qty(usdt_amount / current_price, step)

        if qty < min_qty:
            log.warning("Quantite trop petite: " + str(qty))
            return None

        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty,
            "price": round(price, 2),
            "timestamp": ts
        }
        query = sign(params)
        r = requests.post(BASE_URL + "/api/v3/order?" + query, headers=get_headers(), timeout=10)
        if r.ok:
            order = r.json()
            log.info(side + " " + symbol + " | Qty: " + str(qty) + " @ " + str(round(price, 2)))
            return order
        else:
            log.error("Erreur ordre: " + str(r.text[:100]))
            return None
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return None

def check_order_filled(order_id, symbol):
    try:
        ts = int(time.time() * 1000)
        params = {"symbol": symbol, "orderId": order_id, "timestamp": ts}
        query = sign(params)
        r = requests.get(BASE_URL + "/api/v3/order?" + query, headers=get_headers(), timeout=10)
        if r.ok:
            return r.json().get("status") == "FILLED"
        return False
    except:
        return False

def cancel_order(order_id, symbol):
    try:
        ts = int(time.time() * 1000)
        params = {"symbol": symbol, "orderId": order_id, "timestamp": ts}
        query = sign(params)
        r = requests.delete(BASE_URL + "/api/v3/order?" + query, headers=get_headers(), timeout=10)
        return r.ok
    except:
        return False

def setup_grid(symbol, current_price):
    # Vérifie s'il y a déjà des ordres ouverts
    existing_orders = get_open_orders(symbol)
    if existing_orders:
        log.info(symbol + " - " + str(len(existing_orders)) + " ordres existants detectes - annulation...")
        cancel_all_orders(symbol)
        time.sleep(2)

    log.info("Setup grille " + symbol + " @ " + str(round(current_price, 2)))
    stop_loss_price = current_price * (1 - STOP_LOSS_PCT)

    levels = []
    for i in range(1, GRID_LEVELS + 1):
        buy_price = current_price * (1 - i * GRID_SPACING_PCT)
        sell_price = current_price * (1 + i * GRID_SPACING_PCT)
        levels.append({
            "buy_price": round(buy_price, 2),
            "sell_price": round(sell_price, 2),
            "buy_order": None,
            "sell_order": None,
            "filled_buy": False,
        })

    for level in levels:
        order = place_order(symbol, "BUY", level["buy_price"], USDT_PER_LEVEL)
        if order:
            level["buy_order"] = order.get("orderId")
        time.sleep(0.5)

    grids[symbol] = {
        "levels": levels,
        "entry_price": current_price,
        "stop_loss": stop_loss_price,
        "pnl": 0.0
    }
    save_grid_state()
    log.info("Grille " + symbol + " configuree | Stop-loss: " + str(round(stop_loss_price, 2)))

def manage_grid(symbol):
    global daily_pnl

    if symbol not in grids:
        return

    grid = grids[symbol]
    current_price = get_price(symbol)

    if current_price <= 0:
        return

    log.info(symbol + " @ " + str(round(current_price, 2)) + " | Grid PnL: " + str(round(grid["pnl"], 2)) + " USDT")

    if current_price <= grid["stop_loss"]:
        log.warning("STOP LOSS " + symbol + "! " + str(round(current_price, 2)) + " <= " + str(round(grid["stop_loss"], 2)))
        cancel_all_orders(symbol)
        del grids[symbol]
        save_grid_state()
        return

    for level in grid["levels"]:
        if level["buy_order"] and not level["filled_buy"]:
            if check_order_filled(level["buy_order"], symbol):
                log.info("BUY rempli @ " + str(level["buy_price"]) + " -> SELL @ " + str(level["sell_price"]))
                level["filled_buy"] = True
                sell_order = place_order(symbol, "SELL", level["sell_price"], USDT_PER_LEVEL)
                if sell_order:
                    level["sell_order"] = sell_order.get("orderId")
                save_grid_state()

        if level["sell_order"] and level["filled_buy"]:
            if check_order_filled(level["sell_order"], symbol):
                profit = USDT_PER_LEVEL * GRID_SPACING_PCT * 2
                grid["pnl"] += profit
                daily_pnl += profit
                log.info("PROFIT " + symbol + " +" + str(round(profit, 3)) + " USDT | Total: " + str(round(grid["pnl"], 2)))

                level["filled_buy"] = False
                level["sell_order"] = None
                buy_order = place_order(symbol, "BUY", level["buy_price"], USDT_PER_LEVEL)
                if buy_order:
                    level["buy_order"] = buy_order.get("orderId")
                save_grid_state()

def run():
    global daily_pnl, pnl_date

    log.info("Bot Grid Trading demarre!")
    log.info("Paires: BNB + BTC | Niveaux: " + str(GRID_LEVELS) + " | Espacement: " + str(GRID_SPACING_PCT * 100) + "% | Stop-loss: " + str(STOP_LOSS_PCT * 100) + "%")

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        log.error("Cles API manquantes!")
        return

    for symbol in ["BNBUSDT", "BTCUSDT"]:
        price = get_price(symbol)
        if price > 0:
            setup_grid(symbol, price)
        time.sleep(2)

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date = today

            if daily_pnl <= -DAILY_STOP_LOSS:
                log.warning("Stop-loss journalier! Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                continue

            log.info("PnL jour: " + str(round(daily_pnl, 2)) + " | Grilles: " + str(len(grids)))

            for symbol in list(grids.keys()):
                manage_grid(symbol)

            for symbol in ["BNBUSDT", "BTCUSDT"]:
                if symbol not in grids:
                    price = get_price(symbol)
                    if price > 0:
                        setup_grid(symbol, price)

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
