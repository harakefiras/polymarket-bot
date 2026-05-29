import os, time, random, logging, requests, json, asyncio
from datetime import date, datetime, timezone
import threading

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "50"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "100"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.25"))
WHALE_MAX_HOURS = float(os.getenv("WHALE_MAX_HOURS", "120"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "30"))
BASE_BET = float(os.getenv("BASE_BET", "3"))
MAX_BET = float(os.getenv("MAX_BET", "50"))
ARB_MAX_SUM = float(os.getenv("ARB_MAX_SUM", "0.96"))
ARB_BET = float(os.getenv("ARB_BET", "10"))
DAILY_TAKE_PROFIT = float(os.getenv("DAILY_TAKE_PROFIT", "40"))

ACTIVE_HOURS = list(range(7, 22))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
TRADED_FILE = "/app/traded_markets.txt"
PNL_FILE = "/app/daily_pnl.txt"
TRADES_FILE = "/app/trades_history.json"

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

def calculate_bet_size_kelly(slope):
    win_rate = 0.55
    patterns = analyze_patterns()
    if patterns and patterns["total_trades"] >= 20:
        win_rate = patterns["win_rate"]
        log.info("Kelly: win rate réel = " + str(round(win_rate * 100, 1)) + "%")
    else:
        log.info("Kelly: win rate par défaut = " + str(round(win_rate * 100, 1)) + "%")
    kelly = win_rate - (1 - win_rate)
    kelly = max(0.05, min(kelly, 0.3))
    signal_multiplier = min(abs(slope) / 100, 2.0)
    bet = BASE_BET + (MAX_BET - BASE_BET) * kelly * signal_multiplier
    bet = round(min(bet, MAX_BET), 1)
    log.info("Kelly | Slope: " + str(round(slope)) + "$ | Kelly%: " + str(round(kelly * 100, 1)) + "% | Mise: " + str(bet) + " USDC")
    return bet

def get_btc_slope():
    try:
        r = requests.get(
            BINANCE_API + "/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 5},
            timeout=10
        )
        if not r.ok:
            return 0
        candles = r.json()
        closes = [float(c[4]) for c in candles]
        slope = closes[-1] - closes[0]
        log.info("BTC courbe: " + str(round(closes[0])) + " -> " + str(round(closes[-1])) + " | Pente: " + str(round(slope)) + "$")
        return slope
    except Exception as e:
        log.error("Erreur courbe BTC: " + str(e))
        return 0

daily_pnl = load_daily_pnl()
pnl_date = date.today()
seen_trades = set()
traded_windows = set()
arb_traded = set()
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
    except Exception as e:
        log.error("Erreur sauvegarde marché: " + str(e))

traded_markets = load_traded_markets()

def check_stop_loss():
    return daily_pnl <= -STOP_LOSS_USDC

def check_take_profit():
    return daily_pnl >= DAILY_TAKE_PROFIT

def open_val():
    with positions_lock:
        return sum(p["size"] for p in open_positions)

def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

def get_hours_left(market):
    end_date = market.get("endDateIso") or market.get("endDate")
    if not end_date:
        return 0
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (end - now).total_seconds() / 3600
    except:
        return 0

def get_active_markets(max_hours=168):
    try:
        r = requests.get(GAMMA_API + "/markets", params={"active": "true", "limit": 100}, timeout=10)
        if not r.ok:
            return []
        markets = r.json()
        filtered = [m for m in markets if 0 < get_hours_left(m) <= max_hours]
        log.info("Marchés valides: " + str(len(filtered)))
        return filtered
    except Exception as e:
        log.error("Erreur marchés: " + str(e))
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
            if notional >= MIN_WHALE_USDC and 0.45 <= price <= 0.65 and token_id:
                whales.append({
                    "id": tid,
                    "market_id": mid,
                    "market": question,
                    "token_id": token_id,
                    "price": price,
                    "notional": notional,
                    "outcome": outcome,
                    "market_key": market_key,
                })
                seen_market_tokens.add(market_key)
    return whales

def get_midpoint_price(token_id):
    try:
        r = requests.get(CLOB_API + "/midpoint", params={"token_id": token_id}, timeout=10)
        if r.ok:
            mid = float(r.json().get("mid", 0))
            if mid > 0:
                return mid
        r2 = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r2.ok:
            return float(r2.json().get("price", 1))
        return 1
    except:
        return 1

def find_arb_opportunities(markets):
    opportunities = []
    for m in markets:
        mid = m.get("conditionId") or m.get("id")
        if not mid or mid in arb_traded:
            continue
        try:
            outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
            token_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
            if len(outcomes) != 2 or len(token_ids) != 2:
                continue
            prices = [get_midpoint_price(tid) for tid in token_ids]
            if len(prices) == 2:
                total = prices[0] + prices[1]
                profit = 1 - total
                if total <= ARB_MAX_SUM and profit > 0:
                    log.info("ARB TROUVE! " + m.get("question", "?")[:40] + " | " + str(round(prices[0], 3)) + " + " + str(round(prices[1], 3)) + " = " + str(round(total, 3)) + " | Profit: " + str(round(profit * 100, 1)) + "%")
                    opportunities.append({
                        "market_id": mid,
                        "question": m.get("question", "?"),
                        "token_yes": token_ids[0],
                        "token_no": token_ids[1],
                        "price_yes": prices[0],
                        "price_no": prices[1],
                        "total": total,
                        "profit_pct": profit
                    })
        except Exception as e:
            log.error("Erreur ARB scan: " + str(e))
            continue
    return opportunities

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
            if market:
                outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
                token_ids = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
                tokens = [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
                market["tokens"] = tokens
                return market
        return None
    except Exception as e:
        log.error("Erreur marché BTC: " + str(e))
        return None

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

async def sell_order_async(token_id, shares, reason, price):
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="SELL",
                price=str(round(price, 4)), size=str(round(shares, 2)),
            )
            if response.ok:
                log.info("VENTE " + reason + " @ " + str(round(price, 2)))
                return True
            else:
                log.error("Erreur vente: " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception vente: " + str(e))
        return False

def sell_order(token_id, shares, reason, price):
    return run_async(sell_order_async(token_id, shares, reason, price))

async def place_order_async(token_id, outcome, price, bet_size, btc_entry, slope):
    try:
        from polymarket import AsyncSecureClient
        shares = round(bet_size / price, 2)
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            response = await client.place_limit_order(
                token_id=token_id, side="BUY",
                price=str(round(price, 4)), size=str(shares),
            )
            if response.ok:
                log.info("TRADE " + outcome + " " + str(bet_size) + " USDC @ " + str(round(price, 2)) + " | BTC: " + str(round(btc_entry)))
                with positions_lock:
                    open_positions.append({
                        "token_id": token_id,
                        "entry_price": price,
                        "shares": shares,
                        "size": bet_size,
                        "outcome": outcome,
                        "btc_entry": btc_entry,
                        "side": outcome,
                        "slope": slope,
                        "hour": datetime.now().hour,
                    })
                return True
            else:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, bet_size, btc_entry, slope=0):
    return run_async(place_order_async(token_id, outcome, price, bet_size, btc_entry, slope))

async def place_arb_async(opp):
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET) as client:
            shares_yes = round(ARB_BET / opp["price_yes"], 2)
            shares_no = round(ARB_BET / opp["price_no"], 2)
            r1 = await client.place_limit_order(
                token_id=opp["token_yes"], side="BUY",
                price=str(round(opp["price_yes"], 4)), size=str(shares_yes)
            )
            r2 = await client.place_limit_order(
                token_id=opp["token_no"], side="BUY",
                price=str(round(opp["price_no"], 4)), size=str(shares_no)
            )
            if r1.ok and r2.ok:
                profit = round(opp["profit_pct"] * ARB_BET * 2, 3)
                log.info("ARB EXECUTE! " + opp["question"][:40] + " | Profit: +" + str(profit) + " USDC")
                return True
        return False
    except Exception as e:
        log.error("Exception ARB: " + str(e))
        return False

def place_arb(opp):
    return run_async(place_arb_async(opp))

def monitor_loop():
    global daily_pnl, open_positions
    log.info("Thread surveillance démarré - toutes les " + str(int(MONITOR_INTERVAL)) + "s")
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
                    continue
                entry = pos["entry_price"]
                shares = pos["shares"]
                btc_entry = pos.get("btc_entry", 0)
                side = pos.get("side", "Up")
                log.info("Monitor | " + side + " | Token: " + str(round(current, 2)) + " | BTC: " + str(round(btc_current)))

                if current >= 0.98:
                    log.info("Marché expiré - GAIN!")
                    pnl = (1.0 - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    save_trade({"result": "win", "slope": pos.get("slope", 0), "entry_price": entry, "exit_price": 1.0, "hour": pos.get("hour", 0), "pnl": round(pnl, 2)})
                    log.info("PnL: +" + str(round(pnl, 2)) + " | Total: " + str(round(daily_pnl, 2)))
                    to_remove.append(pos)
                    continue

                elif current <= 0.02:
                    log.info("Marché expiré - PERTE")
                    pnl = (current - entry) * shares
                    daily_pnl += pnl
                    save_daily_pnl(daily_pnl)
                    save_trade({"result": "loss", "slope": pos.get("slope", 0), "entry_price": entry, "exit_price": current, "hour": pos.get("hour", 0), "pnl": round(pnl, 2)})
                    to_remove.append(pos)
                    continue

                elif current <= STOP_LOSS_PRICE:
                    log.info("STOP LOSS! @ " + str(round(current, 2)))
                    if sell_order(token_id, shares, "SL", current):
                        pnl = (current - entry) * shares
                        daily_pnl += pnl
                        save_daily_pnl(daily_pnl)
                        save_trade({"result": "loss", "slope": pos.get("slope", 0), "entry_price": entry, "exit_price": current, "hour": pos.get("hour", 0), "pnl": round(pnl, 2)})
                        to_remove.append(pos)

                elif btc_entry > 0 and btc_current > 0:
                    if side == "Up" and btc_current < btc_entry - BTC_DEVIATION:
                        log.info("STOP BTC DOWN!")
                        if sell_order(token_id, shares, "SL_BTC", current):
                            pnl = (current - entry) * shares
                            daily_pnl += pnl
                            save_daily_pnl(daily_pnl)
                            save_trade({"result": "loss", "slope": pos.get("slope", 0), "entry_price": entry, "exit_price": current, "hour": pos.get("hour", 0), "pnl": round(pnl, 2)})
                            to_remove.append(pos)
                    elif side == "Down" and btc_current > btc_entry + BTC_DEVIATION:
                        log.info("STOP BTC UP!")
                        if sell_order(token_id, shares, "SL_BTC", current):
                            pnl = (current - entry) * shares
                            daily_pnl += pnl
                            save_daily_pnl(daily_pnl)
                            save_trade({"result": "loss", "slope": pos.get("slope", 0), "entry_price": entry, "exit_price": current, "hour": pos.get("hour", 0), "pnl": round(pnl, 2)})
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
    global daily_pnl, pnl_date, traded_markets
    log.info("Bot Smart v6 - Kelly + ARB + Whales 120h + TP journalier 40$ + 7h-19h UTC")
    log.info("Mise: " + str(BASE_BET) + "-" + str(MAX_BET) + " USDC | SL: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC))
    log.info("ARB: mise " + str(ARB_BET) + " USDC | seuil " + str(ARB_MAX_SUM) + " | TP jour: " + str(DAILY_TAKE_PROFIT))
    log.info("PnL chargé: " + str(round(daily_pnl, 2)))

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
                arb_traded.clear()
                save_daily_pnl(0.0)

            if check_stop_loss():
                log.warning("Stop-loss journalier atteint! Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                save_daily_pnl(0.0)
                continue

            if check_take_profit():
                log.info("TAKE PROFIT JOURNALIER! +" + str(round(daily_pnl, 2)) + " USDC - Pause jusqu'a demain!")
                time.sleep(3600 * 8)
                continue

            log.info("Positions: " + str(round(open_val(), 2)) + "/" + str(MAX_OPEN_USDC) + " | PnL: " + str(round(daily_pnl, 2)))

            markets = get_active_markets(max_hours=WHALE_MAX_HOURS)

            # STRATEGIE 1 : Arbitrage YES/NO
            log.info("=== ARB YES/NO ===")
            opportunities = find_arb_opportunities(markets)
            if opportunities:
                for opp in opportunities:
                    if check_stop_loss() or check_take_profit():
                        break
                    if open_val() + ARB_BET * 2 > MAX_OPEN_USDC:
                        break
                    if place_arb(opp):
                        arb_traded.add(opp["market_id"])
                        daily_pnl += opp["profit_pct"] * ARB_BET * 2
                        save_daily_pnl(daily_pnl)
            else:
                log.info("Aucune opportunité ARB")

            # STRATEGIE 2 : Copy Whales
            if open_val() < MAX_OPEN_USDC and not check_take_profit():
                log.info("=== COPY WHALES (120h max) ===")
                whales = detect_whales(markets)
                if whales:
                    log.info(str(len(whales)) + " whale(s)!")
                    for w in whales:
                        if check_stop_loss() or check_take_profit() or open_val() + 5.0 > MAX_OPEN_USDC:
                            break
                        log.info("Whale: " + str(round(w["notional"])) + " USDC | " + w["market"][:40] + " | " + w["outcome"] + " @ " + str(round(w["price"], 2)))
                        time.sleep(5)
                        price = get_token_price(w["token_id"])
                        if price <= 0:
                            price = w["price"]
                        if 0.45 <= price <= 0.70:
                            btc_now = get_btc_price()
                            if place_order(w["token_id"], w["outcome"], price, 5.0, btc_now):
                                seen_trades.add(w["id"])
                                traded_markets.add(w["market_id"])
                                save_traded_market(w["market_id"])
                        else:
                            log.info("Prix hors fourchette: " + str(round(price, 2)))
                else:
                    log.info("Aucune whale")

            if check_stop_loss() or check_take_profit():
                continue

            # STRATEGIE 3 : BTC 5m - 7h-19h UTC uniquement
            if open_val() < MAX_OPEN_USDC:
                log.info("=== BTC 5M ===")
                hour_utc = datetime.now(timezone.utc).hour
                if hour_utc not in ACTIVE_HOURS:
                    log.info("Heure creuse (" + str(hour_utc) + "h UTC) - BTC suspendu")
                else:
                    now = int(time.time())
                    seconds_in_window = now % 300
                    window_ts = now - seconds_in_window

                    if window_ts in traded_windows:
                        log.info("Fenêtre déjà tradée")
                    else:
                        if seconds_in_window > 90:
                            seconds_to_next = 300 - seconds_in_window
                            log.info("Trop tard (" + str(seconds_in_window) + "s) - attente " + str(seconds_to_next) + "s")
                            time.sleep(seconds_to_next)
                            now = int(time.time())
                            window_ts = now - (now % 300)
                            seconds_in_window = 0

                        if seconds_in_window < 30:
                            log.info("Attente 30s stabilisation...")
                            time.sleep(30)

                        slope = get_btc_slope()
                        btc_current = get_btc_price()
                        market = get_btc_market(window_ts)

                        if market:
                            if slope > 20:
                                target = "Up"
                                log.info("Signal UP +" + str(round(slope)) + "$")
                            elif slope < -20:
                                target = "Down"
                                log.info("Signal DOWN " + str(round(slope)) + "$")
                            else:
                                target = None
                                log.info("Pente faible (" + str(round(slope)) + "$) - pas de trade")

                            if target:
                                bet_size = calculate_bet_size_kelly(slope)
                                for token in market.get("tokens", []):
                                    if token["outcome"] == target:
                                        token_id = token["token_id"]
                                        price = get_token_price(token_id)
                                        if price <= 0:
                                            price = 0.5
                                        log.info(target + " @ " + str(round(price, 2)) + " | Mise: " + str(bet_size) + " USDC")
                                        if 0.50 <= price <= 0.65:
                                            if place_order(token_id, target, price, bet_size, btc_current, slope):
                                                traded_windows.add(window_ts)
                                        else:
                                            log.info("Prix hors fourchette: " + str(round(price, 2)))

        except Exception as e:
            log.error("Erreur: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait / 60)) + " min")
        time.sleep(wait)

if __name__ == "__main__":
    run()
