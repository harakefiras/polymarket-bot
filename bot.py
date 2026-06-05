import os, time, random, logging, requests, json, asyncio, math
from datetime import date, datetime, timezone
import threading

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "30"))
DAILY_TAKE_PROFIT = float(os.getenv("DAILY_TAKE_PROFIT", "60"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "15"))
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "100"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.30"))
TAKE_PROFIT_PRICE = float(os.getenv("TAKE_PROFIT_PRICE", "0.85"))
MAX_MARKET_HOURS = float(os.getenv("MAX_MARKET_HOURS", "2160"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "10"))
BET_SIZE_MIN = float(os.getenv("BET_SIZE_MIN", "5"))
BET_SIZE_MAX = float(os.getenv("BET_SIZE_MAX", "10"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE = int(os.getenv("CIRCUIT_BREAKER_PAUSE", "7200"))
TREND_SAMPLES = int(os.getenv("TREND_SAMPLES", "4"))
TREND_SAMPLE_INTERVAL = int(os.getenv("TREND_SAMPLE_INTERVAL", "10"))
TREND_MAX_DROP = float(os.getenv("TREND_MAX_DROP", "0.05"))
ENTRY_PRICE_MIN = float(os.getenv("ENTRY_PRICE_MIN", "0.52"))
ENTRY_PRICE_MAX = float(os.getenv("ENTRY_PRICE_MAX", "0.75"))
EXIT_REVERSAL = float(os.getenv("EXIT_REVERSAL", "0.10"))

ACTIVE_HOURS = list(range(7, 23))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
TRADED_FILE = "/app/traded_markets.txt"
PNL_FILE = "/app/daily_pnl.txt"

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

daily_pnl = load_daily_pnl()
pnl_date = date.today()
seen_trades = set()
traded_windows = set()
open_positions = []
positions_lock = threading.Lock()
consecutive_losses = 0
circuit_breaker_until = 0

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

def analyze_trend(token_id):
    prices = []
    log.info("Observation tendance (" + str(TREND_SAMPLES) + " mesures x " + str(TREND_SAMPLE_INTERVAL) + "s)...")
    for i in range(TREND_SAMPLES):
        p = get_token_price(token_id)
        if p > 0:
            prices.append(p)
            log.info("Mesure " + str(i+1) + "/" + str(TREND_SAMPLES) + " : " + str(round(p, 3)))
        if i < TREND_SAMPLES - 1:
            time.sleep(TREND_SAMPLE_INTERVAL)

    if len(prices) < TREND_SAMPLES:
        log.info("Pas assez de mesures - skip")
        return None, 0

    max_drop = max((prices[i-1] - prices[i]) for i in range(1, len(prices)))
    log.info("Chute max: " + str(round(max_drop, 3)) + " | Max autorise: " + str(TREND_MAX_DROP))

    prix_entree = prices[0]

    if not (ENTRY_PRICE_MIN <= prix_entree <= ENTRY_PRICE_MAX):
        log.info("Prix entree hors fourchette (" + str(round(prix_entree, 3)) + ") - skip")
        return None, 0

    if prices[-1] > prices[0] and max_drop <= TREND_MAX_DROP:
        log.info("Tendance confirmee - prix monte globalement")
        return "hausse", prix_entree
    else:
        log.info("Tendance non confirmee")
        return None, 0

def get_btc_market(window_ts):
    try:
        slug = "btc-updown-5m-" + str(window_ts)
        r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=10)
        if r.ok:
            data = r.json()
            market = data[0] if isinstance(data, list) and len(data) > 0 else None
            if market:
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

def get_active_markets():
    try:
        r = requests.get(GAMMA_API + "/markets", params={"active": "true", "limit": 50}, timeout=10)
        if not r.ok:
            return []
        markets = r.json()
        filtered = []
        for m in markets:
            end_date = m.get("endDateIso") or m.get("endDate")
            if not end_date:
                continue
            try:
                end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                hours_left = (end - now).total_seconds() / 3600
                if 0 < hours_left <= MAX_MARKET_HOURS:
                    filtered.append(m)
            except:
                continue
        log.info("Marches valides: " + str(len(filtered)))
        return filtered
    except Exception as e:
        log.error("Erreur marches: " + str(e))
        return []

def get_recent_trades(market_id):
    try:
        r = requests.get(DATA_API + "/trades", params={"market": market_id, "limit": 20}, timeout=10)
        if not r.ok:
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
            if notional >= MIN_WHALE_USDC and 0.35 <= price <= 0.75 and token_id:
                whales.append({
                    "id": tid, "market_id": mid, "market": question,
                    "token_id": token_id, "price": price, "notional": notional,
                    "outcome": outcome, "market_key": market_key
                })
                seen_market_tokens.add(market_key)
    return whales

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
                        log.info("STOP BTC DOWN!")
                        if sell_order(token_id, shares, "SL_BTC", current):
                            pnl = (current - entry) * shares
                            daily_pnl += pnl
                            save_daily_pnl(daily_pnl)
                            record_loss()
                            to_remove.append(pos)
                    elif side == "Down" and btc_current > btc_entry + BTC_DEVIATION:
                        log.info("STOP BTC UP!")
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
    global daily_pnl, pnl_date, traded_markets, consecutive_losses
    log.info("Bot Smart v6 - Courbe Polymarket - Scan continu - Mises 5/10 - Circuit 3 pertes 2h")
    log.info("SL: " + str(STOP_LOSS_USDC) + "$ | TP: " + str(DAILY_TAKE_PROFIT) + "$ | Fourchette: " + str(ENTRY_PRICE_MIN) + "-" + str(ENTRY_PRICE_MAX))
    log.info("PnL charge: " + str(round(daily_pnl, 2)))

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
                seen_trades.clear()
                traded_windows.clear()
                consecutive_losses = 0
                save_daily_pnl(0.0)

            if check_stop_loss():
                log.warning("Stop-loss journalier! Pause 1h.")
                time.sleep(3600)
                continue

            if check_take_profit():
                log.info("Take profit journalier! +" + str(round(daily_pnl, 2)) + "$ - Pause.")
                time.sleep(3600)
                continue

            if check_circuit_breaker():
                reste = int(circuit_breaker_until - time.time())
                log.warning("Circuit breaker - reprise dans " + str(reste // 60) + " min")
                time.sleep(60)
                continue

            hour_utc = datetime.now(timezone.utc).hour
            if hour_utc not in ACTIVE_HOURS:
                time.sleep(60)
                continue

            # STRATEGIE 1 : Copy Whales — 1 seule par cycle
            if open_val() < MAX_OPEN_USDC:
                markets = get_active_markets()
                whales = detect_whales(markets)
                if whales:
                    log.info(str(len(whales)) + " whale(s)!")
                    for w in whales:
                        if check_stop_loss() or check_take_profit() or check_circuit_breaker():
                            break
                        if open_val() + 5.0 > MAX_OPEN_USDC:
                            break
                        log.info("Whale: " + str(round(w["notional"])) + " USDC | " + w["market"][:40] + " | " + w["outcome"] + " @ " + str(round(w["price"], 2)))
                        time.sleep(5)
                        price = get_token_price(w["token_id"])
                        if price <= 0:
                            price = w["price"]
                        if 0.35 <= price <= 0.75:
                            btc_now = get_btc_price()
                            if place_order(w["token_id"], w["outcome"], price, 5.0, btc_now):
                                seen_trades.add(w["id"])
                                traded_markets.add(w["market_id"])
                                save_traded_market(w["market_id"])
                                break
                        else:
                            log.info("Prix hors fourchette: " + str(round(price, 2)))

            if check_stop_loss() or check_take_profit() or check_circuit_breaker():
                time.sleep(10)
                continue

            # STRATEGIE 2 : BTC 5M — scan continu
            if open_val() < MAX_OPEN_USDC:
                now = int(time.time())
                seconds_in_window = now % 300
                window_ts = now - seconds_in_window

                if window_ts not in traded_windows:
                    # Entre dans la fenetre entre 30s et 250s
                    if 30 <= seconds_in_window <= 250:
                        market = get_btc_market(window_ts)
                        if market:
                            tokens = market.get("tokens", [])
                            up_token = next((t for t in tokens if t["outcome"] == "Up"), None)
                            down_token = next((t for t in tokens if t["outcome"] == "Down"), None)

                            if up_token and down_token:
                                price_up = get_token_price(up_token["token_id"])
                                price_down = get_token_price(down_token["token_id"])
                                log.info("BTC | Up: " + str(round(price_up, 3)) + " | Down: " + str(round(price_down, 3)))

                                target_token = None
                                target_outcome = None

                                if ENTRY_PRICE_MIN <= price_down <= ENTRY_PRICE_MAX:
                                    target_token = down_token["token_id"]
                                    target_outcome = "Down"
                                    log.info("Candidat: Down @ " + str(round(price_down, 3)))
                                elif ENTRY_PRICE_MIN <= price_up <= ENTRY_PRICE_MAX:
                                    target_token = up_token["token_id"]
                                    target_outcome = "Up"
                                    log.info("Candidat: Up @ " + str(round(price_up, 3)))

                                if target_token:
                                    trend, prix_entree = analyze_trend(target_token)
                                    if trend == "hausse":
                                        bet_size = calculate_bet_size(prix_entree)
                                        btc_now = get_btc_price()
                                        log.info("ENTREE VALIDEE | " + target_outcome + " @ " + str(round(prix_entree, 3)) + " | Mise: " + str(bet_size) + " USDC")
                                        if place_order(target_token, target_outcome, prix_entree, bet_size, btc_now):
                                            traded_windows.add(window_ts)

        except Exception as e:
            log.error("Erreur: " + str(e))

        time.sleep(10)

if __name__ == "__main__":
    run()
