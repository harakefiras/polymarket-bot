import os, time, random, logging, requests, json, asyncio
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "5"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "200"))
TAKE_PROFIT_PRICE = float(os.getenv("TAKE_PROFIT_PRICE", "0.80"))
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.20"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BINANCE_API = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
seen_trades = set()
traded_windows = set()
open_positions = []

def check_stop_loss():
    return daily_pnl <= -STOP_LOSS_USDC

def get_active_markets():
    try:
        r = requests.get(GAMMA_API + "/markets", params={"active": "true", "limit": 20}, timeout=10)
        return r.json() if r.ok else []
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
    for m in markets[:20]:
        mid = m.get("conditionId") or m.get("id")
        question = m.get("question", "?")
        if not mid:
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
            if notional >= MIN_WHALE_USDC and 0.30 <= price <= 0.70 and token_id:
                whales.append({
                    "id": tid,
                    "market": question,
                    "token_id": token_id,
                    "price": price,
                    "notional": notional,
                    "outcome": outcome
                })
    return whales

def get_btc_analysis():
    try:
        r = requests.get(
            BINANCE_API + "/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 20},
            timeout=10
        )
        if not r.ok:
            return 0, 0
        candles = r.json()
        closes = [float(c[4]) for c in candles]
        opens = [float(c[1]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        last_price = closes[-1]

        trend = sum(1 if closes[i] > opens[i] else -1 for i in range(-6, 0))
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(diff if diff > 0 else 0)
            losses.append(abs(diff) if diff < 0 else 0)
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        rsi = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma_signal = 1 if ma5 > ma10 else -1
        avg_vol = sum(volumes[-10:]) / 10
        vol_ok = volumes[-1] > avg_vol

        score = 0
        if trend >= 2: score += 2
        elif trend <= -2: score -= 2
        if rsi > 55: score += 1
        elif rsi < 45: score -= 1
        if ma_signal > 0: score += 1
        else: score -= 1
        if vol_ok and score > 0: score += 1
        elif vol_ok and score < 0: score -= 1

        log.info("BTC: " + str(round(last_price, 0)) + " | RSI: " + str(round(rsi, 1)) + " | Score: " + str(score))
        return score, last_price
    except Exception as e:
        log.error("Erreur Binance: " + str(e))
        return 0, 0

def get_btc_market():
    try:
        now = int(time.time())
        window_ts = now - (now % 300)
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
                return market, window_ts
        return None, None
    except Exception as e:
        log.error("Erreur marche BTC: " + str(e))
        return None, None

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

async def sell_order_async(token_id, shares, reason):
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(
            private_key=PRIVATE_KEY,
            wallet=WALLET,
        ) as client:
            response = await client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=str(TAKE_PROFIT_PRICE if reason == "TP" else STOP_LOSS_PRICE),
                size=str(round(shares, 2)),
            )
            if response.ok:
                log.info("VENTE " + reason + " | order_id=" + str(response.order_id))
                return True
            else:
                log.error("Erreur vente: " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception vente: " + str(e))
        return False

def sell_order(token_id, shares, reason):
    return asyncio.run(sell_order_async(token_id, shares, reason))

async def place_order_async(token_id, outcome, price):
    global daily_pnl
    try:
        from polymarket import AsyncSecureClient
        shares = round(BET_SIZE_USDC / price, 2)
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
                log.info("TRADE " + outcome + " " + str(BET_SIZE_USDC) + " USDC @ " + str(round(price, 2)))
                open_positions.append({
                    "token_id": token_id,
                    "entry_price": price,
                    "shares": shares,
                    "size": BET_SIZE_USDC,
                    "outcome": outcome,
                })
                return True
            else:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price):
    return asyncio.run(place_order_async(token_id, outcome, price))

def monitor_positions():
    global daily_pnl, open_positions
    to_remove = []
    for pos in open_positions:
        token_id = pos["token_id"]
        current = get_token_price(token_id)
        if current <= 0:
            continue
        entry = pos["entry_price"]
        shares = pos["shares"]
        log.info("Position " + pos["outcome"] + " | Entry: " + str(round(entry, 2)) + " | Current: " + str(round(current, 2)))

        if current >= TAKE_PROFIT_PRICE:
            log.info("TAKE PROFIT! " + str(round(current, 2)))
            if sell_order(token_id, shares, "TP"):
                pnl = (current - entry) * shares
                daily_pnl += pnl
                log.info("PnL: +" + str(round(pnl, 2)) + " USDC")
                to_remove.append(pos)

        elif current <= STOP_LOSS_PRICE:
            log.info("STOP LOSS! " + str(round(current, 2)))
            if sell_order(token_id, shares, "SL"):
                pnl = (current - entry) * shares
                daily_pnl += pnl
                log.info("PnL: " + str(round(pnl, 2)) + " USDC")
                to_remove.append(pos)

    for pos in to_remove:
        if pos in open_positions:
            open_positions.remove(pos)

def run():
    global daily_pnl, pnl_date
    log.info("Bot Dual Strategy + TP/SL demarre!")
    log.info("Mise: " + str(BET_SIZE_USDC) + " | Stop-loss jour: " + str(STOP_LOSS_USDC) + " | TP: " + str(TAKE_PROFIT_PRICE) + " | SL/trade: " + str(STOP_LOSS_PRICE))

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

            # Surveille les positions ouvertes
            monitor_positions()

            # ─── STRATEGIE 1 : Copy Whales ───────────────────────
            log.info("=== COPY WHALES ===")
            markets = get_active_markets()
            whales = detect_whales(markets)
            if whales:
                log.info(str(len(whales)) + " whale(s)!")
                for w in whales:
                    if check_stop_loss():
                        break
                    log.info("Whale: " + str(round(w["notional"])) + " USDC | " + w["market"][:40] + " | " + w["outcome"] + " @ " + str(round(w["price"], 2)))
                    time.sleep(5)
                    price = get_token_price(w["token_id"])
                    if price <= 0:
                        price = w["price"]
                    if 0.30 <= price <= 0.70:
                        if place_order(w["token_id"], w["outcome"], price):
                            seen_trades.add(w["id"])
                    else:
                        log.info("Prix hors fourchette: " + str(round(price, 2)))
            else:
                log.info("Aucune whale")

            if check_stop_loss():
                continue

            # ─── STRATEGIE 2 : BTC 5m ────────────────────────────
            log.info("=== BTC 5M ===")
            score, btc_price = get_btc_analysis()
            market, window_ts = get_btc_market()

            if market and window_ts not in traded_windows:
                if score >= 3:
                    target = "Up"
                elif score <= -3:
                    target = "Down"
                else:
                    target = None
                    log.info("Signal BTC faible (score=" + str(score) + ")")

                if target:
                    for token in market.get("tokens", []):
                        if token["outcome"] == target:
                            token_id = token["token_id"]
                            price = get_token_price(token_id)
                            if price <= 0:
                                price = 0.5
                            log.info(target + " @ " + str(round(price, 2)))
                            time.sleep(15)
                            if 0.40 <= price <= 0.65:
                                if place_order(token_id, target, price):
                                    traded_windows.add(window_ts)
                            else:
                                log.info("Prix BTC hors fourchette: " + str(round(price, 2)))

        except Exception as e:
            log.error("Erreur: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait/60)) + " min")
        time.sleep(wait)

if __name__ == "__main__":
    run()
