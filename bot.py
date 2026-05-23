import os, time, random, logging, requests, json, asyncio
from datetime import date, datetime, timezone

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
BET_SIZE_MIN = float(os.getenv("BET_SIZE_MIN", "3"))
BET_SIZE_MAX = float(os.getenv("BET_SIZE_MAX", "10"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "50"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "200"))
TAKE_PROFIT_PRICE = float(os.getenv("TAKE_PROFIT_PRICE", "0.80"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.20"))
MAX_MARKET_HOURS = float(os.getenv("MAX_MARKET_HOURS", "48"))
BTC_DEVIATION = float(os.getenv("BTC_DEVIATION", "150"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
TRADED_FILE = "/app/traded_markets.txt"

BLOCKED_KEYWORDS = ["nba", "nhl", "nfl", "mlb", "stanley", "finals", "championship", "season", "playoffs", "super bowl", "world series", "soccer", "football", "basketball", "hockey", "baseball"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
seen_trades = set()
traded_windows = set()
open_positions = []

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
        log.error("Erreur sauvegarde: " + str(e))

traded_markets = load_traded_markets()

def check_stop_loss():
    return daily_pnl <= -STOP_LOSS_USDC

def open_val():
    return sum(p["size"] for p in open_positions)

def calculate_bet_size(slope):
    abs_slope = abs(slope)
    if abs_slope >= 200:
        bet = BET_SIZE_MAX
    elif abs_slope >= 100:
        bet = BET_SIZE_MIN + (BET_SIZE_MAX - BET_SIZE_MIN) * 0.7
    elif abs_slope >= 50:
        bet = BET_SIZE_MIN + (BET_SIZE_MAX - BET_SIZE_MIN) * 0.4
    else:
        bet = BET_SIZE_MIN
    bet = round(bet, 1)
    log.info("Pente: " + str(round(slope)) + "$ | Mise: " + str(bet) + " USDC")
    return bet

def is_market_valid(market):
    question = (market.get("question") or "").lower()
    slug = (market.get("slug") or "").lower()
    for kw in BLOCKED_KEYWORDS:
        if kw in question or kw in slug:
            return False
    end_date = market.get("endDateIso") or market.get("endDate")
    if not end_date:
        return False
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours_left = (end - now).total_seconds() / 3600
        return 0 < hours_left <= MAX_MARKET_HOURS
    except:
        return False

def get_active_markets():
    try:
        r = requests.get(GAMMA_API + "/markets", params={"active": "true", "limit": 50}, timeout=10)
        if not r.ok:
            return []
        markets = r.json()
        filtered = [m for m in markets if is_market_valid(m)]
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
    except Exception as e:
        log.error("Erreur trades: " + str(e))
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
            if notional >= MIN_WHALE_USDC and 0.45 <= price <= 0.70 and token_id:
                whales.append({
                    "id": tid,
                    "market_id": mid,
                    "market": question,
                    "token_id": token_id,
                    "price": price,
                    "notional": notional,
                    "outcome": outcome,
                    "market_key": market_key
                })
                seen_market_tokens.add(market_key)
    return whales

def get_btc_price():
    try:
        r = requests.get(BINANCE_API + "/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

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
        log.error("Erreur marche BTC: " + str(e))
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
        async with await AsyncSecureClient.create(
            private_key=PRIVATE_KEY,
            wallet=WALLET,
        ) as client:
            response = await client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=str(round(price, 4)),
                size=str(round(shares, 2)),
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
    return asyncio.run(sell_order_async(token_id, shares, reason, price))

async def place_order_async(token_id, outcome, price, bet_size, btc_entry):
    try:
        from polymarket import AsyncSecureClient
        shares = round(bet_size / price, 2)
        async with await AsyncSecureClient.create(
            private_key=PRIVATE_KEY,
            wallet=WALLET,
        ) as client:
            response = await client.place_limit_order(
                token_id=token_id,
                side="BUY",
                price=str(round(price, 4)),
                size=str(shares),
            )
            if response.ok:
                log.info("TRADE " + outcome + " " + str(bet_size) + " USDC @ " + str(round(price, 2)) + " | BTC: " + str(round(btc_entry)))
                open_positions.append({
                    "token_id": token_id,
                    "entry_price": price,
                    "shares": shares,
                    "size": bet_size,
                    "outcome": outcome,
                    "btc_entry": btc_entry,
                    "side": outcome,
                })
                return True
            else:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price, bet_size, btc_entry):
    return asyncio.run(place_order_async(token_id, outcome, price, bet_size, btc_entry))

def monitor_positions():
    global daily_pnl, open_positions
    to_remove = []
    btc_current = get_btc_price()

    for pos in open_positions:
        token_id = pos["token_id"]
        current = get_token_price(token_id)
        if current <= 0:
            continue
        entry = pos["entry_price"]
        shares = pos["shares"]
        btc_entry = pos.get("btc_entry", 0)
        side = pos.get("side", "Up")

        log.info("Position " + side + " | Token: " + str(round(current, 2)) + " | BTC entry: " + str(round(btc_entry)) + " | BTC now: " + str(round(btc_current)))

        if current >= TAKE_PROFIT_PRICE:
            log.info("TAKE PROFIT! @ " + str(round(current, 2)))
            if sell_order(token_id, shares, "TP", current):
                daily_pnl += (current - entry) * shares
                log.info("PnL: +" + str(round((current - entry) * shares, 2)) + " USDC")
                to_remove.append(pos)

        elif current <= STOP_LOSS_PRICE:
            log.info("STOP LOSS! @ " + str(round(current, 2)))
            if sell_order(token_id, shares, "SL", current):
                daily_pnl += (current - entry) * shares
                log.info("PnL: " + str(round((current - entry) * shares, 2)) + " USDC")
                to_remove.append(pos)

        elif btc_entry > 0 and btc_current > 0:
            if side == "Up" and btc_current < btc_entry - BTC_DEVIATION:
                log.info("STOP BTC DOWN!")
                if sell_order(token_id, shares, "SL_BTC", current):
                    daily_pnl += (current - entry) * shares
                    to_remove.append(pos)
            elif side == "Down" and btc_current > btc_entry + BTC_DEVIATION:
                log.info("STOP BTC UP!")
                if sell_order(token_id, shares, "SL_BTC", current):
                    daily_pnl += (current - entry) * shares
                    to_remove.append(pos)

    for pos in to_remove:
        if pos in open_positions:
            open_positions.remove(pos)

def run():
    global daily_pnl, pnl_date, traded_markets
    log.info("Bot Final v5 - Synchronise BTC Windows demarre!")
    log.info("Mise: " + str(BET_SIZE_MIN) + "-" + str(BET_SIZE_MAX) + " USDC | Stop-loss: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return
    if not WALLET.startswith("0x"):
        log.error("POLYMARKET_WALLET_ADDRESS manquante!")
        return

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date = today
                seen_trades.clear()
                traded_windows.clear()

            if check_stop_loss():
                log.warning("Stop-loss journalier atteint! Pause 1h.")
                time.sleep(3600)
                continue

            monitor_positions()
            log.info("Positions: " + str(round(open_val(), 2)) + "/" + str(MAX_OPEN_USDC) + " | PnL: " + str(round(daily_pnl, 2)))

            # STRATEGIE 1 : Copy Whales
            if open_val() < MAX_OPEN_USDC:
                log.info("=== COPY WHALES ===")
                markets = get_active_markets()
                whales = detect_whales(markets)
                if whales:
                    log.info(str(len(whales)) + " whale(s)!")
                    for w in whales:
                        if check_stop_loss() or open_val() + BET_SIZE_MIN > MAX_OPEN_USDC:
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

            if check_stop_loss():
                continue

            # STRATEGIE 2 : BTC 5m SYNCHRONISE
            if open_val() < MAX_OPEN_USDC:
                log.info("=== BTC 5M ===")
                now = int(time.time())
                seconds_in_window = now % 300
                window_ts = now - seconds_in_window

                if window_ts in traded_windows:
                    log.info("Fenetre deja tradee")
                else:
                    if seconds_in_window > 30:
                        seconds_to_next = 300 - seconds_in_window
                        log.info("Trop tard (" + str(seconds_in_window) + "s) - attente " + str(seconds_to_next) + "s")
                        time.sleep(seconds_to_next)
                        now = int(time.time())
                        window_ts = now - (now % 300)
                        seconds_in_window = 0

                    log.info("Debut fenetre - attente 30s stabilisation...")
                    time.sleep(30)

                    slope = get_btc_slope()
                    btc_current = get_btc_price()
                    market = get_btc_market(window_ts)

                    if market:
                        if slope > 75:
                            target = "Up"
                            log.info("Signal UP +" + str(round(slope)) + "$")
                        elif slope < -75:
                            target = "Down"
                            log.info("Signal DOWN " + str(round(slope)) + "$")
                        else:
                            target = None
                            log.info("Pente faible (" + str(round(slope)) + "$) - pas de trade")

                        if target:
                            bet_size = calculate_bet_size(slope)
                            for token in market.get("tokens", []):
                                if token["outcome"] == target:
                                    token_id = token["token_id"]
                                    price = get_token_price(token_id)
                                    if price <= 0:
                                        price = 0.5
                                    log.info(target + " @ " + str(round(price, 2)) + " | Mise: " + str(bet_size) + " USDC")
                                    if 0.5 <= price <= 0.75:
                                        if place_order(token_id, target, price, bet_size, btc_current):
                                            traded_windows.add(window_ts)
                                    else:
                                        log.info("Prix hors fourchette: " + str(round(price, 2)))

        except Exception as e:
            log.error("Erreur: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait/60)) + " min")
        time.sleep(wait)

if __name__ == "__main__":
    run()
