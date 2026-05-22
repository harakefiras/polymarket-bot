import os, time, random, logging, requests, json, asyncio
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "10"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "50"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "150"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "250"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.80"))
STOP_PER_TRADE = float(os.getenv("STOP_PER_TRADE", "0.25"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
open_positions = []
traded_windows = set()

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

        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma_signal = 1 if ma5 > ma10 else -1

        avg_volume = sum(volumes[-10:]) / 10
        vol_confirm = volumes[-1] > avg_volume

        score = 0
        if trend >= 2:
            score += 2
        elif trend <= -2:
            score -= 2
        if rsi > 55:
            score += 1
        elif rsi < 45:
            score -= 1
        if ma_signal > 0:
            score += 1
        else:
            score -= 1

        log.info("BTC: " + str(round(last_price, 0)) + " | RSI: " + str(round(rsi, 1)) + " | MA: " + ("UP" if ma_signal > 0 else "DOWN") + " | Trend: " + str(trend) + " | Score: " + str(score))
        return score, last_price

    except Exception as e:
        log.error("Erreur analyse: " + str(e))
        return 0, 0

def get_btc_market():
    try:
        now = int(time.time())
        window_ts = now - (now % 300)
        slug = "btc-updown-5m-" + str(window_ts)
        log.info("Slug: " + slug)
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
        log.error("Erreur marche: " + str(e))
        return None, None

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

async def sell_position_async(token_id, size, reason):
    try:
        from polymarket import AsyncSecureClient
        async with await AsyncSecureClient.create(
            private_key=PRIVATE_KEY,
            wallet=WALLET,
        ) as client:
            response = await client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price="0.95",
                size=str(size),
            )
            if response.ok:
                log.info("VENDU (" + reason + ") token=" + token_id[:10] + " | order_id=" + str(response.order_id))
                return True
            else:
                log.error("Erreur vente: " + str(response.code))
                return False
    except Exception as e:
        log.error("Exception vente: " + str(e))
        return False

def sell_position(token_id, size, reason):
    return asyncio.run(sell_position_async(token_id, size, reason))

async def place_order_async(token_id, side, price):
    try:
        from polymarket import AsyncSecureClient
        size = str(round(BET_SIZE_USDC / price, 2))
        async with await AsyncSecureClient.create(
            private_key=PRIVATE_KEY,
            wallet=WALLET,
        ) as client:
            response = await client.place_limit_order(
                token_id=token_id,
                side="BUY",
                price=str(round(price, 4)),
                size=size,
            )
            if response.ok:
                log.info("TRADE " + side + " " + str(BET_SIZE_USDC) + " USDC @ " + str(round(price, 2)) + " | order_id=" + str(response.order_id))
                open_positions.append({
                    "size": BET_SIZE_USDC,
                    "token_id": token_id,
                    "entry_price": price,
                    "shares": round(BET_SIZE_USDC / price, 2),
                })
                return True
            else:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, side, price):
    return asyncio.run(place_order_async(token_id, side, price))

def monitor_positions():
    global open_positions, daily_pnl
    if not open_positions:
        return
    positions_to_remove = []
    for pos in open_positions:
        token_id = pos.get("token_id")
        if not token_id:
            continue
        current_price = get_token_price(token_id)
        if current_price <= 0:
            continue
        entry = pos.get("entry_price", 0.5)
        shares = pos.get("shares", 0)
        log.info("Position " + token_id[:10] + " | Entry: " + str(round(entry, 2)) + " | Current: " + str(round(current_price, 2)))

        if current_price >= TAKE_PROFIT:
            log.info("TAKE PROFIT @ " + str(current_price))
            if sell_position(token_id, shares, "TAKE_PROFIT"):
                pnl = (current_price - entry) * shares
                daily_pnl += pnl
                log.info("PnL position: +" + str(round(pnl, 2)) + " USDC")
                positions_to_remove.append(pos)

        elif current_price <= STOP_PER_TRADE:
            log.info("STOP LOSS POSITION @ " + str(current_price))
            if sell_position(token_id, shares, "STOP_LOSS"):
                pnl = (current_price - entry) * shares
                daily_pnl += pnl
                log.info("PnL position: " + str(round(pnl, 2)) + " USDC")
                positions_to_remove.append(pos)

    for pos in positions_to_remove:
        if pos in open_positions:
            open_positions.remove(pos)

def run():
    global daily_pnl, pnl_date
    log.info("Bot BTC 5m v4 - TP/SL + RSI + MA demarre!")
    log.info("Mise: " + str(BET_SIZE_USDC) + " | Take Profit: " + str(TAKE_PROFIT) + " | Stop/trade: " + str(STOP_PER_TRADE))

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
                traded_windows.clear()

            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss journalier atteint! Pause 1h.")
                time.sleep(3600)
                continue

            monitor_positions()

            open_val = sum(p["size"] for p in open_positions)
            if open_val >= MAX_OPEN_USDC:
                log.info("Plafond atteint - attente...")
                time.sleep(60)
                continue

            score, btc_price = get_btc_analysis()
            market, window_ts = get_btc_market()

            if not market or window_ts in traded_windows:
                time.sleep(POLL_INTERVAL)
                continue

            if score >= 2:
                target = "Up"
                log.info("Signal fort UP (score=" + str(score) + ")")
            elif score <= -2:
                target = "Down"
                log.info("Signal fort DOWN (score=" + str(score) + ")")
            else:
                log.info("Signal faible (score=" + str(score) + ") - pas de trade")
                time.sleep(POLL_INTERVAL)
                continue

            for token in market.get("tokens", []):
                if token["outcome"] == target:
                    token_id = token["token_id"]
                    price = get_token_price(token_id)
                    if price <= 0:
                        price = 0.5
                    log.info(target + " @ " + str(round(price, 2)))
                    log.info("Attente 15s avant ordre...")
                    time.sleep(15)
                    if 0.30 <= price <= 0.75:
                        if place_order(token_id, target, price):
                            traded_windows.add(window_ts)
                    else:
                        log.info("Prix hors fourchette: " + str(round(price, 2)))

        except Exception as e:
            log.error("Erreur: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait)) + "s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
