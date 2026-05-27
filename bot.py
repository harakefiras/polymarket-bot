import os, time, logging, requests, json, asyncio
from datetime import date, datetime, timezone
import threading

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "30"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.30"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))
BASE_BET = float(os.getenv("BASE_BET", "3"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
open_positions = []
positions_lock = threading.Lock()
window_ref_prices = {}
traded_windows = set()

def get_btc_price():
    try:
        r = requests.get(BINANCE_API + "/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def get_btc_market(window_ts):
    try:
        import json as j
        slug = "btc-updown-5m-" + str(window_ts)
        r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=10)
        if r.ok:
            data = r.json()
            market = data[0] if isinstance(data, list) and data else None
            if market:
                outcomes = j.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
                token_ids = j.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
                market["tokens"] = [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
                return market
        return None
    except Exception as e:
        log.error("Erreur marche: " + str(e))
        return None

async def place_order_async(token_id, outcome, price, btc_ref, gap):
    try:
        from polymarket import AsyncSecureClient
        shares = round(BASE_BET / price, 2)
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="BUY",
                price=str(round(price, 4)), size=str(shares)
            )
            if response.ok:
                log.info("TRADE " + outcome + " " + str(BASE_BET) + " USDC @ " + str(round(price, 2)) + " | Gap: " + str(round(gap)) + "$")
                with positions_lock:
                    open_positions.append({
                        "token_id": token_id, "entry_price": price,
                        "shares": shares, "size": BASE_BET,
                        "side": outcome, "btc_ref": btc_ref, "gap": gap
                    })
                return True
            else:
                log.error("Erreur ordre: " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, btc_ref, gap):
    return asyncio.run(place_order_async(token_id, outcome, price, btc_ref, gap))

async def sell_order_async(token_id, shares, reason, price):
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="SELL",
                price=str(round(price, 4)), size=str(round(shares, 2))
            )
            if response.ok:
                log.info("VENTE " + reason + " @ " + str(round(price, 2)))
                return True
        return False
    except Exception as e:
        log.error("Exception vente: " + str(e))
        return False

def sell_order(token_id, shares, reason, price):
    return asyncio.run(sell_order_async(token_id, shares, reason, price))

def monitor_loop():
    global daily_pnl, open_positions
    log.info("Monitor demarre")
    while True:
        try:
            with positions_lock:
                positions_copy = list(open_positions)
            if positions_copy:
                btc_current = get_btc_price()
                to_remove = []
                for pos in positions_copy:
                    current = get_token_price(pos["token_id"])
                    if current <= 0:
                        continue
                    log.info("Position " + pos["side"] + " | Token: " + str(round(current, 2)) + " | BTC: " + str(round(btc_current)))
                    if current >= 0.98:
                        log.info("GAIN!")
                        daily_pnl += (1.0 - pos["entry_price"]) * pos["shares"]
                        to_remove.append(pos)
                    elif current <= 0.02:
                        log.info("PERTE")
                        daily_pnl += (current - pos["entry_price"]) * pos["shares"]
                        to_remove.append(pos)
                    elif current <= STOP_LOSS_PRICE:
                        log.info("STOP LOSS!")
                        if sell_order(pos["token_id"], pos["shares"], "SL", current):
                            daily_pnl += (current - pos["entry_price"]) * pos["shares"]
                            to_remove.append(pos)
                    elif pos["btc_ref"] > 0:
                        if pos["side"] == "Up" and btc_current < pos["btc_ref"] - BTC_DEVIATION:
                            if sell_order(pos["token_id"], pos["shares"], "SL_BTC", current):
                                daily_pnl += (current - pos["entry_price"]) * pos["shares"]
                                to_remove.append(pos)
                        elif pos["side"] == "Down" and btc_current > pos["btc_ref"] + BTC_DEVIATION:
                            if sell_order(pos["token_id"], pos["shares"], "SL_BTC", current):
                                daily_pnl += (current - pos["entry_price"]) * pos["shares"]
                                to_remove.append(pos)
                with positions_lock:
                    for pos in to_remove:
                        if pos in open_positions:
                            open_positions.remove(pos)
        except Exception as e:
            log.error("Monitor erreur: " + str(e))
        time.sleep(30)

def ref_price_loop():
    global window_ref_prices
    log.info("Prix reference demarre")
    while True:
        try:
            now = int(time.time())
            sec = now % 300
            if sec <= 5:
                window_ts = now - sec
                if window_ts not in window_ref_prices:
                    price = get_btc_price()
                    if price > 0:
                        window_ref_prices[window_ts] = price
                        log.info("Ref fenetre " + str(window_ts) + " : " + str(round(price)))
        except Exception as e:
            log.error("Ref erreur: " + str(e))
        time.sleep(1)

def run():
    global daily_pnl, traded_windows
    log.info("Bot Simple v1 demarre | SL: " + str(STOP_LOSS_USDC) + " | Mise: " + str(BASE_BET))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=ref_price_loop, daemon=True).start()

    while True:
        try:
            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss atteint! Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                traded_windows.clear()
                continue

            with positions_lock:
                open_val = sum(p["size"] for p in open_positions)

            log.info("Positions: " + str(round(open_val, 2)) + "/" + str(MAX_OPEN_USDC) + " | PnL: " + str(round(daily_pnl, 2)))

            if open_val < MAX_OPEN_USDC:
                now = int(time.time())
                sec = now % 300
                window_ts = now - sec

                if window_ts not in traded_windows:
                    if sec > 120:
                        wait = 300 - sec
                        log.info("Trop tard - attente " + str(wait) + "s")
                        time.sleep(wait)
                        now = int(time.time())
                        window_ts = now - (now % 300)
                        sec = 0

                    if sec < 60:
                        wait = 60 - sec
                        log.info("Attente " + str(wait) + "s...")
                        time.sleep(wait)

                    ref_price = window_ref_prices.get(window_ts, 0)
                    btc_current = get_btc_price()

                    log.info("Ref: " + str(round(ref_price)) + "$ | BTC: " + str(round(btc_current)) + "$")

                    if ref_price > 0 and btc_current > 0:
                        gap = btc_current - ref_price
                        log.info("Gap: " + str(round(gap)) + "$")

                        if gap >= 50:
                            target = "Up"
                        elif gap <= -50:
                            target = "Down"
                        else:
                            target = None
                            log.info("Gap insuffisant - pas de trade")

                        if target:
                            market = get_btc_market(window_ts)
                            if market:
                                for token in market.get("tokens", []):
                                    if token["outcome"] == target:
                                        price = get_token_price(token["token_id"])
                                        if price <= 0:
                                            price = 0.5
                                        log.info(target + " @ " + str(round(price, 2)))
                                        if 0.45 <= price <= 0.65:
                                            if place_order(token["token_id"], target, price, ref_price, gap):
                                                traded_windows.add(window_ts)
                                        else:
                                            log.info("Prix hors fourchette")
                    else:
                        log.info("Prix reference pas disponible")

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(300)

if __name__ == "__main__":
    run()
