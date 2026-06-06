import os, time, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "30"))
DAILY_TAKE_PROFIT = float(os.getenv("DAILY_TAKE_PROFIT", "60"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "15"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.30"))
TAKE_PROFIT_PRICE = float(os.getenv("TAKE_PROFIT_PRICE", "0.85"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "10"))
BET_SIZE_MIN = float(os.getenv("BET_SIZE_MIN", "5"))
BET_SIZE_MAX = float(os.getenv("BET_SIZE_MAX", "10"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE = int(os.getenv("CIRCUIT_BREAKER_PAUSE", "7200"))

# Criteres strategie "prix a battre"
GAP_MIN = float(os.getenv("GAP_MIN", "30"))            # ecart BTC min vs prix a battre (30$)
ENTRY_PRICE_MIN = float(os.getenv("ENTRY_PRICE_MIN", "0.52"))  # prix token min
ENTRY_PRICE_MAX = float(os.getenv("ENTRY_PRICE_MAX", "0.75"))  # prix token max (plafond)
ENTRY_WINDOW_MAX = int(os.getenv("ENTRY_WINDOW_MAX", "60"))    # entree uniquement 1ere minute
EXIT_REVERSAL = float(os.getenv("EXIT_REVERSAL", "0.10"))

ACTIVE_HOURS = list(range(7, 23))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
WINDOWS_FILE = "/app/traded_windows.txt"
PNL_FILE = "/app/daily_pnl.txt"
STRIKE_FILE = "/app/strikes.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

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

# Memoire des prix a battre captures (window_ts -> strike)
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

daily_pnl = load_daily_pnl()
pnl_date = date.today()
traded_windows = load_traded_windows()
open_positions = []
positions_lock = threading.Lock()
consecutive_losses = 0
circuit_breaker_until = 0
load_strikes()

def check_stop_loss():
    return daily_pnl <= -STOP_LOSS_USDC

def check_take_profit():
    return daily_pnl >= DAILY_TAKE_PROFIT

def check_circuit_breaker():
    return time.time() < circuit_breaker_until

def open_val():
    with positions_lock:
        return sum(p["size"] for p in open_positions)

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def get_btc_price():
    try:
        r = requests.get(BINANCE_API + "/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
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
                outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
                token_ids = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
                tokens = [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
                market["tokens"] = tokens
                return market
        return None
    except:
        return None

def calculate_bet_size(price):
    if price <= 0.65:
        return BET_SIZE_MAX
    else:
        return BET_SIZE_MIN

async def sell_order_async(token_id, shares, reason, price):
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(token_id=token_id, side="SELL",
                price=str(round(price, 4)), size=str(round(shares, 2)))
            if response.ok:
                log.info("VENTE " + reason + " @ " + str(round(price, 2)))
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
        shares = math.floor(bet_size / price * 100) / 100
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(token_id=token_id, side="BUY",
                price=str(round(price, 4)), size=str(shares))
            if response.ok:
                log.info("TRADE " + outcome + " " + str(bet_size) + " USDC @ " + str(round(price, 2)) + " | BTC: " + str(round(btc_entry)))
                with positions_lock:
                    open_positions.append({
                        "token_id": token_id, "entry_price": price, "shares": shares,
                        "size": bet_size, "outcome": outcome, "btc_entry": btc_entry,
                        "side": outcome, "peak_price": price,
                    })
                return True
            log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
            return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, bet_size, btc_entry):
    return asyncio.run(place_order_async(token_id, outcome, price, bet_size, btc_entry))

def record_loss():
    global consecutive_losses, circuit_breaker_until
    consecutive_losses += 1
    log.info("Pertes consecutives: " + str(consecutive_losses) + "/" + str(MAX_CONSECUTIVE_LOSSES))
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        circuit_breaker_until = time.time() + CIRCUIT_BREAKER_PAUSE
        log.warning("CIRCUIT BREAKER! " + str(MAX_CONSECUTIVE_LOSSES) + " pertes - pause 2h")
        consecutive_losses = 0

def monitor_loop():
    global daily_pnl, open_positions
    log.info("Thread surveillance demarre - toutes les " + str(int(MONITOR_INTERVAL)) + "s")
    while True:
        try:
            with positions_lock:
                positions_copy = list(open_positions)
            if not positions_copy:
                time.sleep(MONITOR_INTERVAL)
                continue
            btc_current = get_btc_price()
            to_remove = []
            for pos in positions_copy:
                token_id = pos["token_id"]
                current = get_token_price(token_id)
                if current <= 0:
                    pos["zero_count"] = pos.get("zero_count", 0) + 1
                    if pos["zero_count"] >= 3:
                        log.info("Position fermee manuellement - suppression")
                        to_remove.append(pos)
                    continue
                pos["zero_count"] = 0
                entry = pos["entry_price"]
                shares = pos["shares"]
                peak = pos.get("peak_price", entry)
                side = pos.get("side", "Up")
                if current > peak:
                    pos["peak_price"] = current
                    peak = current
                log.info("Monitor | " + side + " | Token: " + str(round(current, 3)) + " | Peak: " + str(round(peak, 3)))
                if current <= 0.02:
                    log.info("Marche expire - PERTE")
                    pnl = (current - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    record_loss()
                    to_remove.append(pos)
                    continue
                elif current >= 0.98:
                    log.info("Marche expire - GAIN!")
                    pnl = (current - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    consecutive_losses = 0
                    log.info("PnL: +" + str(round(pnl, 2)) + " | Total: " + str(round(daily_pnl, 2)))
                    to_remove.append(pos)
                    continue
                elif current >= TAKE_PROFIT_PRICE:
                    log.info("TAKE PROFIT! @ " + str(round(current, 3)))
                    if sell_order(token_id, shares, "TP", current):
                        pnl = (current - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        consecutive_losses = 0
                        log.info("PnL TP: +" + str(round(pnl, 2)) + " | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)
                elif current <= STOP_LOSS_PRICE:
                    log.info("STOP LOSS! @ " + str(round(current, 3)))
                    if sell_order(token_id, shares, "SL", current):
                        pnl = (current - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        record_loss()
                        to_remove.append(pos)
                elif peak - current >= EXIT_REVERSAL and current > entry:
                    log.info("SORTIE RETOURNEMENT!")
                    if sell_order(token_id, shares, "REVERSAL", current):
                        pnl = (current - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        if pnl < 0:
                            record_loss()
                        else:
                            consecutive_losses = 0
                        log.info("PnL: " + str(round(pnl, 2)) + " | Total: " + str(round(daily_pnl, 2)))
                        to_remove.append(pos)
                elif pos.get("btc_entry", 0) > 0 and btc_current > 0:
                    btc_entry = pos["btc_entry"]
                    if side == "Up" and btc_current < btc_entry - BTC_DEVIATION:
                        if sell_order(token_id, shares, "SL_BTC", current):
                            pnl = (current - entry) * shares
                            daily_pnl += pnl
                            save_daily_pnl(daily_pnl)
                            record_loss()
                            to_remove.append(pos)
                    elif side == "Down" and btc_current > btc_entry + BTC_DEVIATION:
                        if sell_order(token_id, shares, "SL_BTC", current):
                            pnl = (current - entry) * shares
                            daily_pnl += pnl
                            save_daily_pnl(daily_pnl)
                            record_loss()
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
    log.info("Bot Smart v8 - Prix a Battre - Gap 30$ - 1ere minute - Token 0.52-0.75")
    log.info("SL: " + str(STOP_LOSS_USDC) + "$ | TP: " + str(DAILY_TAKE_PROFIT) + "$ | Gap min: " + str(GAP_MIN) + "$")
    log.info("PnL: " + str(round(daily_pnl, 2)) + " | Fenetres tradees: " + str(len(traded_windows)))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("POLYMARKET_WALLET_ADDRESS manquante!")
        return

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL hier: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date = today
                traded_windows.clear()
                consecutive_losses = 0
                save_daily_pnl(0.0)

            if check_stop_loss():
                log.warning("Stop-loss journalier! Pause 1h.")
                time.sleep(3600)
                continue
            if check_take_profit():
                log.info("Take profit! +" + str(round(daily_pnl, 2)) + "$ - Pause.")
                time.sleep(3600)
                continue
            if check_circuit_breaker():
                reste = int(circuit_breaker_until - time.time())
                log.warning("Circuit breaker - " + str(reste // 60) + " min")
                time.sleep(60)
                continue

            hour_utc = datetime.now(timezone.utc).hour
            if hour_utc not in ACTIVE_HOURS:
                time.sleep(60)
                continue

            now = int(time.time())
            seconds_in_window = now % 300
            window_ts = now - seconds_in_window

            # Capture du prix a battre a la seconde 0 (dans les 5 premieres secondes)
            if window_ts not in strikes and seconds_in_window <= 5:
                btc_strike = get_btc_price()
                if btc_strike > 0:
                    strikes[window_ts] = btc_strike
                    save_strike(window_ts, btc_strike)
                    log.info("Nouvelle fenetre | Prix a battre capture: " + str(round(btc_strike)))

            # Entree uniquement 1ere minute, fenetre pas tradee, on a le strike
            if (window_ts not in traded_windows and window_ts in strikes
                    and seconds_in_window <= ENTRY_WINDOW_MAX
                    and open_val() < MAX_OPEN_USDC):

                strike = strikes[window_ts]
                btc_now = get_btc_price()
                gap = btc_now - strike

                if abs(gap) >= GAP_MIN:
                    target_outcome = "Up" if gap > 0 else "Down"
                    log.info("Gap " + str(round(gap, 1)) + "$ | Cible: " + target_outcome + " | BTC: " + str(round(btc_now)) + " vs strike " + str(round(strike)))

                    market = get_btc_market(window_ts)
                    if market:
                        tokens = market.get("tokens", [])
                        target = next((t for t in tokens if t["outcome"] == target_outcome), None)
                        if target:
                            price = get_token_price(target["token_id"])
                            log.info(target_outcome + " token @ " + str(round(price, 3)))
                            if ENTRY_PRICE_MIN <= price <= ENTRY_PRICE_MAX:
                                bet_size = calculate_bet_size(price)
                                log.info("ENTREE VALIDEE | " + target_outcome + " @ " + str(round(price, 3)) + " | Mise: " + str(bet_size) + " USDC")
                                if place_order(target["token_id"], target_outcome, price, bet_size, btc_now):
                                    traded_windows.add(window_ts)
                                    save_traded_window(window_ts)
                            elif price > ENTRY_PRICE_MAX:
                                log.info("Token trop cher (" + str(round(price, 3)) + ") - trop tard, skip")
                            else:
                                log.info("Token trop bas (" + str(round(price, 3)) + ") - skip")

            # Nettoyage memoire strikes (garde les 50 dernieres fenetres)
            if len(strikes) > 50:
                for k in sorted(strikes.keys())[:-50]:
                    del strikes[k]

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(5)

if __name__ == "__main__":
    run()
