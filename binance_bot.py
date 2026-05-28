import os, time, logging, requests
from datetime import datetime
from binance.client import Client
from binance.enums import *

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
SYMBOL = "BNBUSDT"
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "200"))
TARGET_PROFIT_PCT = float(os.getenv("TARGET_PROFIT_PCT", "0.003"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.002"))
DAILY_STOP_LOSS = float(os.getenv("DAILY_STOP_LOSS", "50"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("binance_bot")

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

daily_pnl = 0.0
position = None

def get_bnb_price():
    try:
        ticker = client.get_symbol_ticker(symbol=SYMBOL)
        return float(ticker["price"])
    except Exception as e:
        log.error("Erreur prix: " + str(e))
        return 0

def get_bnb_momentum():
    try:
        candles = client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=3)
        closes = [float(c[4]) for c in candles]
        move = (closes[-1] - closes[0]) / closes[0] * 100
        log.info("BNB momentum 1m: " + str(round(move, 3)) + "%")
        return move
    except Exception as e:
        log.error("Erreur momentum: " + str(e))
        return 0

def buy_bnb(price):
    try:
        qty = round(TRADE_AMOUNT_USDT / price, 2)
        order = client.order_market_buy(symbol=SYMBOL, quantity=qty)
        log.info("ACHAT BNB | Qty: " + str(qty) + " @ " + str(round(price, 2)))
        return {"qty": qty, "entry": price}
    except Exception as e:
        log.error("Erreur achat: " + str(e))
        return None

def sell_bnb(qty, reason):
    try:
        order = client.order_market_sell(symbol=SYMBOL, quantity=qty)
        price = get_bnb_price()
        log.info("VENTE " + reason + " | Qty: " + str(qty) + " @ " + str(round(price, 2)))
        return price
    except Exception as e:
        log.error("Erreur vente: " + str(e))
        return 0

def run():
    global daily_pnl, position
    log.info("Bot Scalping BNB demarre!")
    log.info("Mise: " + str(TRADE_AMOUNT_USDT) + "$ | TP: " + str(TARGET_PROFIT_PCT*100) + "% | SL: " + str(STOP_LOSS_PCT*100) + "%")

    if not BINANCE_API_KEY:
        log.error("BINANCE_API_KEY manquante!")
        return

    while True:
        try:
            if daily_pnl <= -DAILY_STOP_LOSS:
                log.warning("Stop-loss journalier atteint! Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                continue

            price = get_bnb_price()
            if price <= 0:
                time.sleep(5)
                continue

            if position is None:
                momentum = get_bnb_momentum()

                if momentum >= 0.2:
                    log.info("Signal UP fort! Achat...")
                    position = buy_bnb(price)

                elif momentum <= -0.2:
                    log.info("Momentum négatif - on attend")

            else:
                entry = position["entry"]
                qty   = position["qty"]
                pct   = (price - entry) / entry

                log.info("Position | Entry: " + str(round(entry, 2)) + " | Now: " + str(round(price, 2)) + " | PnL: " + str(round(pct*100, 3)) + "%")

                if pct >= TARGET_PROFIT_PCT:
                    sell_price = sell_bnb(qty, "TP")
                    pnl = (sell_price - entry) * qty
                    daily_pnl += pnl
                    log.info("GAIN! +" + str(round(pnl, 2)) + "$ | Total: " + str(round(daily_pnl, 2)) + "$")
                    position = None

                elif pct <= -STOP_LOSS_PCT:
                    sell_price = sell_bnb(qty, "SL")
                    pnl = (sell_price - entry) * qty
                    daily_pnl += pnl
                    log.info("STOP LOSS | " + str(round(pnl, 2)) + "$ | Total: " + str(round(daily_pnl, 2)) + "$")
                    position = None

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(30)

if __name__ == "__main__":
    run()
