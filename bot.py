import os, time, logging, requests, json, asyncio, threading
from datetime import date, datetime

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "30"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.30"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))
BASE_BET = float(os.getenv("BASE_BET", "3"))
MAX_BET = float(os.getenv("MAX_BET", "50"))
MIN_GAP = float(os.getenv("MIN_GAP", "25"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
TRADES_FILE = "/app/trades_history.json"
PNL_FILE = "/app/daily_pnl.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
open_positions = []
positions_lock = threading.Lock()
window_ref_prices = {}
traded_windows = set()

def load_daily_pnl():
    try:
        if os.path.exists(PNL_FILE):
            with open(PNL_FILE) as f:
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

def load_history():
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE) as f:
                return json.load(f)
        return []
    except:
        return []

def save_trade(result, gap, entry_price, pnl):
    try:
        history = load_history()
        history.append({
            "date": str(date.today()),
            "hour": datetime.now().hour,
            "result": result,
            "gap": round(gap, 1),
            "entry_price": round(entry_price, 3),
            "pnl": round(pnl, 3)
        })
        if len(history) > 500:
            history = history[-500:]
        with open(TRADES_FILE, "w") as f:
            json.dump(history, f)
        log.info("Trade enregistre: " + result + " | gap=" + str(round(gap)) + "$ | pnl=" + str(round(pnl, 2)))
    except Exception as e:
        log.error("Erreur save trade: " + str(e))

def get_smart_params():
    history = load_history()
    if len(history) < 10:
        return MIN_GAP, BASE_BET
    wins = [t for t in history if t["result"] == "win"]
    win_rate = len(wins) / len(history)
    if len(wins) >= 5:
        win_gaps = [abs(t["gap"]) for t in wins]
        best_gap = max(MIN_GAP, sum(win_gaps) / len(win_gaps) * 0.7)
        best_gap = min(best_gap, 100)
    else:
        best_gap = MIN_GAP
    if win_rate >= 0.65:
        bet = min(BASE_BET * 3, MAX_BET)
    elif win_rate >= 0.55:
        bet = min(BASE_BET * 2, MAX_BET)
    else:
        bet = BASE_BET
    log.info("Smart params | gap=" + str(round(best_gap)) + "$ | mise=" + str(bet) + " | WR=" + str(round(win_rate*100)) + "% | " + str(len(history)) + " trades")
    return best_gap, bet

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
        slug = "btc-updown-5m-" + str(window_ts)
        r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=10)
        if not r.ok:
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        market = data[0]
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        token_ids = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
        market["tokens"] = [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
        return market
    except Exception as e:
        log.error("Erreur marche: " + str(e))
        return None

async def place_order_async(token_id, outcome, price, btc_ref, gap, bet):
    try:
        from polymarket import AsyncSecureClient
        shares = round(bet / price, 2)
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="BUY",
                price=str(round(price, 4)), size=str(shares)
            )
            if response.ok:
                log.info("TRADE " + outcome + " " + str(bet) + " USDC @ " + str(round(price, 2)) + " | Gap: " + str(round(gap)) + "$")
                with positions_lock:
                    open_positions.append({
                        "token_id": token_id, "entry_price": price,
                        "shares": shares, "size": bet,
                        "side": outcome, "btc_ref": btc_ref, "gap": gap
                    })
                return True
            log.error("Erreur ordre: " + str(response.message))
            return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, btc_ref, gap, bet):
    return asyncio.run(place_order_async(token_id, outcome, price, btc_ref, gap, bet))

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
    global daily_pnl
    log.info("Monitor 30s demarre")
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
                    entry = pos["entry_price"]
                    shares = pos["shares"]
                    side = pos["side"]
                    log.info("Monitor | " + side + " | Token: " + str(round(current, 2)) + " | BTC: " + str(round(btc_current)))
                    if current >= 0.98:
                        pnl = (1.0 - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        save_trade("win", pos["gap"], entry, pnl)
                        log.info("GAIN! +" + str(round(pnl, 2)) + " | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)
                    elif current <= 0.02:
                        pnl = (current - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        save_trade("loss", pos["gap"], entry, pnl)
                        log.info("PERTE | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)
                    elif current <= STOP_LOSS_PRICE:
                        if sell_order(pos["token_id"], shares, "SL", current):
                            pnl = (current - entry) * shares
                            daily_pnl += pnl
                            save_daily_pnl(daily_pnl)
                            save_trade("loss", pos["gap"], entry, pnl)
                            log.info("STOP LOSS | Total: " + str(round(daily_pnl, 2)))
                            to_remove.append(pos)
                    elif pos["btc_ref"] > 0 and btc_current > 0:
                        if side == "Up" and btc_current < pos["btc_ref"] - BTC_DEVIATION:
                            if sell_order(pos["token_id"], shares, "SL_BTC", current):
                                pnl = (current - entry) * shares
                                daily_pnl += pnl
                                save_daily_pnl(daily_pnl)
                                save_trade("loss", pos["gap"], entry, pnl)
                                to_remove.append(pos)
                        elif side == "Down" and btc_current > pos["btc_ref"] + BTC_DEVIATION:
                            if sell_order(pos["token_id"], shares, "SL_BTC", current):
                                pnl = (current - entry) * shares
                                daily_pnl += pnl
                                save_daily_pnl(daily_pnl)
                                save_trade("loss", pos["gap"], entry, pnl)
                                to_remove.append(pos)
                with positions_lock:
                    for pos in to_remove:
                        if pos in open_positions:
                            open_positions.remove(pos)
        except Exception as e:
            log.error("Monitor erreur: " + str(e))
        time.sleep(30)

def ref_price_loop():
    log.info("Thread prix reference demarre")
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
                        log.info("Ref | " + str(window_ts) + " | " + str(round(price)))
                for k in list(window_ref_prices.keys()):
                    if k < now - 600:
                        del window_ref_prices[k]
        except Exception as e:
            log.error("Ref erreur: " + str(e))
        time.sleep(1)

def run():
    global daily_pnl, traded_windows
    daily_pnl = load_daily_pnl()
    log.info("Bot Evolutif v2 demarre!")
    log.info("SL: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC) + " | SL trade: " + str(STOP_LOSS_PRICE) + " | PnL: " + str(round(daily_pnl, 2)))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=ref_price_loop, daemon=True).start()

    while True:
        try:
            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss! Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                traded_windows.clear()
                save_daily_pnl(0.0)
                continue

            with positions_lock:
                open_val = sum(p["size"] for p in open_positions)

            log.info("Positions: " + str(round(open_val, 2)) + "/" + str(MAX_OPEN_USDC) + " | PnL: " + str(round(daily_pnl, 2)))

            if open_val >= MAX_OPEN_USDC:
                time.sleep(30)
                continue

            now = int(time.time())
            sec = now % 300
            window_ts = now - sec

            if window_ts in traded_windows:
                wait = 300 - sec + 5
                log.info("Fenetre tradee - attente " + str(wait) + "s")
                time.sleep(wait)
                continue

            if sec > 120:
                wait = 300 - sec + 5
                log.info("Trop tard (" + str(sec) + "s) - attente " + str(wait) + "s")
                time.sleep(wait)
                continue

            if sec < 60:
                wait = 60 - sec
                log.info("Stabilisation " + str(wait) + "s...")
                time.sleep(wait)

            now = int(time.time())
            sec = now % 300
            window_ts = now - sec

            ref_price = window_ref_prices.get(window_ts, 0)

            if ref_price == 0:
                ref_price = get_btc_price()
                if ref_price > 0:
                    window_ref_prices[window_ts] = ref_price
                    log.info("Ref enregistree maintenant: " + str(round(ref_price)))

            btc_current = get_btc_price()
            min_gap, bet_size = get_smart_params()

            log.info("Ref: " + str(round(ref_price)) + "$ | BTC: " + str(round(btc_current)) + "$ | Seuil: " + str(round(min_gap)) + "$")

            if ref_price > 0 and btc_current > 0:
                gap = btc_current - ref_price
                log.info("Gap: " + str(round(gap)) + "$")

                if gap >= min_gap:
                    target = "Up"
                    log.info("Signal UP +" + str(round(gap)) + "$")
                elif gap <= -min_gap:
                    target = "Down"
                    log.info("Signal DOWN " + str(round(gap)) + "$")
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
                                log.info(target + " @ " + str(round(price, 2)) + " | Mise: " + str(bet_size))
                                if 0.45 <= price <= 0.65:
                                    if place_order(token["token_id"], target, price, ref_price, gap, bet_size):
                                        traded_windows.add(window_ts)
                                else:
                                    log.info("Prix hors fourchette: " + str(round(price, 2)))

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(30)

if __name__ == "__main__":
    run()
