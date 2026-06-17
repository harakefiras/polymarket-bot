import os, time, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

PRIVATE_KEY  = os.environ.get("PRIVATE_KEY", "")
WALLET       = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

STOP_LOSS_USDC    = float(os.getenv("STOP_LOSS_USDC",    "15"))
DAILY_TAKE_PROFIT = float(os.getenv("DAILY_TAKE_PROFIT", "60"))
BET_SIZE          = float(os.getenv("BET_SIZE",          "5"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE  = int(os.getenv("CIRCUIT_BREAKER_PAUSE",  "7200"))
GAP_MIN          = float(os.getenv("GAP_MIN",          "30"))
GAP_PCT          = float(os.getenv("GAP_PCT",          "0"))
ENTRY_PRICE_MIN  = float(os.getenv("ENTRY_PRICE_MIN",  "0.52"))
ENTRY_PRICE_MAX  = float(os.getenv("ENTRY_PRICE_MAX",  "0.80"))
ENTRY_WINDOW_MAX = int(os.getenv("ENTRY_WINDOW_MAX",   "30"))
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "5"))
ACTIVE_HOURS     = list(range(7, 22))

# Règles de sortie
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT",  "0.30"))   # +30%
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",    "0.10"))   # -10%
# Trailing : vente dès que le prix redescend du pic

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
WINDOWS_FILE = "/app/traded_windows.txt"
PNL_FILE     = "/app/daily_pnl.txt"
STRIKE_FILE  = "/app/strikes.txt"

import sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except:
    pass
log = logging.getLogger("bot_btc")

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

def load_traded_windows():
    try:
        if os.path.exists(WINDOWS_FILE):
            with open(WINDOWS_FILE, "r") as f:
                lines = f.read().strip().split("\n")
                today = str(date.today())
                w = set()
                for line in lines:
                    if line.strip() and line.startswith(today):
                        parts = line.strip().split(",")
                        if len(parts) == 2:
                            w.add(int(parts[1]))
                return w
        return set()
    except:
        return set()

def save_traded_window(window_ts):
    try:
        with open(WINDOWS_FILE, "a") as f:
            f.write(str(date.today()) + "," + str(window_ts) + "\n")
    except:
        pass

strikes = {}

def load_strikes():
    try:
        if os.path.exists(STRIKE_FILE):
            with open(STRIKE_FILE, "r") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) == 2:
                        strikes[int(parts[0])] = float(parts[1])
    except:
        pass

def save_strike(window_ts, price):
    try:
        with open(STRIKE_FILE, "a") as f:
            f.write(str(window_ts) + "," + str(price) + "\n")
    except:
        pass

daily_pnl          = load_daily_pnl()
pnl_date           = date.today()
traded_windows     = load_traded_windows()
open_positions     = []
positions_lock     = threading.Lock()
consecutive_losses = 0
circuit_breaker_until = 0

def get_btc_price():
    try:
        r = requests.get(COINBASE_SPOT, timeout=10)
        if r.ok:
            return float(r.json()["data"]["amount"])
        return 0
    except:
        return 0

def get_best_ask(token_id):
    try:
        r = requests.get(CLOB_API + "/book", params={"token_id": token_id}, timeout=8)
        if r.ok:
            asks = r.json().get("asks", [])
            if asks:
                return float(min(asks, key=lambda a: float(a["price"]))["price"])
        return None
    except:
        return None

def get_best_bid(token_id):
    try:
        r = requests.get(CLOB_API + "/book", params={"token_id": token_id}, timeout=8)
        if r.ok:
            bids = r.json().get("bids", [])
            if bids:
                return float(max(bids, key=lambda b: float(b["price"]))["price"])
        return None
    except:
        return None

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
        if r.ok:
            data = r.json()
            market = data[0] if isinstance(data, list) and len(data) > 0 else None
            if market and market.get("slug") == slug:
                outcomes  = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
                token_ids = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
                market["tokens"] = [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
                return market
        return None
    except:
        return None

async def sell_order_async(token_id, shares, reason, price):
    try:
        from polymarket import AsyncSecureClient
        shares_safe = max(0.01, round(shares - 0.01, 2))
        sell_px = max(0.01, round(price - 0.04, 4))
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(token_id=token_id, side="SELL", price=str(sell_px), size=str(shares_safe))
            if response.ok:
                log.info("VENTE " + reason + " @ " + str(round(price, 3)))
                return True
            log.error("Erreur vente: " + str(response.message))
            return False
    except Exception as e:
        log.error("Exception vente: " + str(e))
        return False

def sell_order(token_id, shares, reason, price):
    return asyncio.run(sell_order_async(token_id, shares, reason, price))

async def place_order_async(token_id, outcome, price, bet_size, btc_entry):
    try:
        from polymarket import AsyncSecureClient
        buy_px = min(0.99, round(price + 0.02, 4))
        shares = math.floor(bet_size / buy_px * 100) / 100
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(token_id=token_id, side="BUY", price=str(buy_px), size=str(shares))
            if not response.ok:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
            order_id = getattr(response, "order_id", None) or getattr(response, "id", None)
            await asyncio.sleep(12)
            executed = True
            try:
                open_orders = None
                for meth in ("get_orders", "get_open_orders"):
                    fn = getattr(client, meth, None)
                    if fn:
                        open_orders = await fn()
                        break
                if open_orders is not None and order_id:
                    ids = []
                    items = getattr(open_orders, "orders", open_orders)
                    if isinstance(items, list):
                        for o in items:
                            oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                            ids.append(oid)
                    if order_id in ids:
                        executed = False
                        try:
                            cancel = getattr(client, "cancel_order", None)
                            if cancel:
                                await cancel(order_id=order_id)
                                log.warning("ACHAT NON EXECUTE - annule")
                        except Exception as ce:
                            log.warning("Annulation: " + str(ce))
            except Exception as ve:
                log.warning("Verif achat impossible: " + str(ve))
            if not executed:
                return False
            log.info("TRADE " + outcome + " " + str(bet_size) + " USDC @ " + str(buy_px) + " | BTC: " + str(round(btc_entry)))
            with positions_lock:
                open_positions.append({
                    "token_id":    token_id,
                    "entry_price": buy_px,
                    "shares":      shares,
                    "size":        bet_size,
                    "outcome":     outcome,
                    "btc_entry":   btc_entry,
                    "peak_price":  buy_px,
                    "zero_count":  0,
                })
            return True
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, bet_size, btc_entry):
    return asyncio.run(place_order_async(token_id, outcome, price, bet_size, btc_entry))

def record_loss():
    global consecutive_losses, circuit_breaker_until
    consecutive_losses += 1
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        circuit_breaker_until = time.time() + CIRCUIT_BREAKER_PAUSE
        log.warning("CIRCUIT BREAKER! Pause 2h")
        consecutive_losses = 0

def monitor_loop():
    global daily_pnl, open_positions, consecutive_losses
    log.info("Thread surveillance BTC démarré")
    while True:
        try:
            with positions_lock:
                positions_copy = list(open_positions)
            if not positions_copy:
                time.sleep(MONITOR_INTERVAL)
                continue

            to_remove = []
            for pos in positions_copy:
                token_id = pos["token_id"]
                current  = get_best_bid(token_id)
                if current is None:
                    current = get_token_price(token_id)
                entry = pos["entry_price"]
                shares = pos["shares"]
                peak  = pos.get("peak_price", entry)

                if current <= 0:
                    pos["zero_count"] = pos.get("zero_count", 0) + 1
                    if pos["zero_count"] >= 3:
                        pnl = (0 - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        record_loss()
                        log.warning("EXPIRATION PERTE TOTALE " + str(round(pnl, 2)))
                        to_remove.append(pos)
                    continue
                pos["zero_count"] = 0

                # Mise à jour du pic
                if current > peak:
                    pos["peak_price"] = current
                    peak = current

                gain_pct = (current - entry) / entry
                log.info("Monitor | " + pos["outcome"]
                         + " | Prix: " + str(round(current, 3))
                         + " | Pic: " + str(round(peak, 3))
                         + " | Gain: " + str(round(gain_pct * 100, 1)) + "%"
                         + " | PnL jour: " + str(round(daily_pnl, 2)))

                # 1. Expiration en perte
                if current <= 0.02:
                    pnl = (max(0.01, current - 0.04) - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    record_loss()
                    to_remove.append(pos)

                # 2. Expiration en gain
                elif current >= 0.98:
                    pnl = (max(0.01, current - 0.04) - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    consecutive_losses = 0
                    log.info("EXPIRATION GAIN +" + str(round(pnl, 2)) + "$ | Total: " + str(round(daily_pnl, 2)))
                    to_remove.append(pos)

                # 3. TAKE PROFIT +30%
                elif gain_pct >= TAKE_PROFIT_PCT:
                    log.info("TAKE PROFIT +30%! @ " + str(round(current, 3)))
                    if sell_order(token_id, shares, "TP+30%", current):
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        consecutive_losses = 0
                        log.info("PnL TP: +" + str(round(pnl, 2)) + "$ | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)

                # 4. STOP LOSS -10%
                elif gain_pct <= -STOP_LOSS_PCT:
                    log.info("STOP LOSS -10%! @ " + str(round(current, 3)))
                    if sell_order(token_id, shares, "SL-10%", current):
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        record_loss()
                        log.info("PnL SL: " + str(round(pnl, 2)) + "$ | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)

                # 5. TRAILING : prix redescend du pic
                elif current < peak and peak > entry:
                    log.info("TRAILING STOP! Pic: " + str(round(peak, 3))
                             + " -> " + str(round(current, 3)))
                    if sell_order(token_id, shares, "TRAIL", current):
                        pnl = (max(0.01, current - 0.04) - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        if pnl > 0:
                            consecutive_losses = 0
                        else:
                            record_loss()
                        log.info("PnL TRAIL: " + str(round(pnl, 2)) + "$ | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)

            if to_remove:
                with positions_lock:
                    for pos in to_remove:
                        if pos in open_positions:
                            open_positions.remove(pos)

        except Exception as e:
            log.error("Erreur monitor: " + str(e))
        time.sleep(MONITOR_INTERVAL)

def run():
    global daily_pnl, pnl_date, traded_windows, consecutive_losses
    load_strikes()
    log.info("Bot BTC 5min v2 - TP+30% | SL-10% | Trailing")
    log.info("Gap: " + str(GAP_MIN) + "$ | Entrée: " + str(ENTRY_PRICE_MIN) + "-" + str(ENTRY_PRICE_MAX))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("WALLET manquant!")
        return

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date  = today
                traded_windows.clear()
                consecutive_losses = 0
                save_daily_pnl(0.0)

            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss journalier! Pause 1h.")
                time.sleep(3600)
                continue
            if daily_pnl >= DAILY_TAKE_PROFIT:
                log.info("Take profit journalier! Pause 1h.")
                time.sleep(3600)
                continue
            if time.time() < circuit_breaker_until:
                time.sleep(60)
                continue

            hour_utc = datetime.now(timezone.utc).hour
            if hour_utc not in ACTIVE_HOURS:
                time.sleep(60)
                continue

            now               = int(time.time())
            seconds_in_window = now % 300
            window_ts         = now - seconds_in_window

            if window_ts not in strikes and seconds_in_window <= 5:
                btc_strike = get_btc_price()
                if btc_strike > 0:
                    strikes[window_ts] = btc_strike
                    save_strike(window_ts, btc_strike)
                    log.info("Strike capture: " + str(round(btc_strike)))

            if (window_ts not in traded_windows
                    and window_ts in strikes
                    and seconds_in_window <= ENTRY_WINDOW_MAX
                    and len(open_positions) == 0):

                strike    = strikes[window_ts]
                btc_now   = get_btc_price()
                gap       = btc_now - strike
                gap_seuil = (btc_now * GAP_PCT / 100) if GAP_PCT > 0 else GAP_MIN

                if abs(gap) >= gap_seuil:
                    target_outcome = "Up" if gap > 0 else "Down"
                    log.info("Gap " + str(round(gap, 1)) + "$ | " + target_outcome)
                    market = get_btc_market(window_ts)
                    if market:
                        tokens = market.get("tokens", [])
                        target = next((t for t in tokens if t["outcome"] == target_outcome), None)
                        if target:
                            price = get_best_ask(target["token_id"])
                            if price is None:
                                price = get_token_price(target["token_id"])
                            if ENTRY_PRICE_MIN <= price <= ENTRY_PRICE_MAX:
                                log.info("ENTREE | " + target_outcome + " @ " + str(round(price, 3)))
                                if place_order(target["token_id"], target_outcome, price, BET_SIZE, btc_now):
                                    traded_windows.add(window_ts)
                                    save_traded_window(window_ts)
                            elif price > ENTRY_PRICE_MAX:
                                log.info("Token trop cher (" + str(round(price, 3)) + ") - skip")
                            else:
                                log.info("Token trop bas (" + str(round(price, 3)) + ") - skip")
                else:
                    log.info("Gap insuffisant (" + str(round(abs(gap), 1)) + "$ < " + str(round(gap_seuil, 1)) + "$) - attente signal")

            if len(strikes) > 50:
                for k in sorted(strikes.keys())[:-50]:
                    del strikes[k]

        except Exception as e:
            log.error("Erreur boucle: " + str(e))
        time.sleep(5)

if __name__ == "__main__":
    run()
