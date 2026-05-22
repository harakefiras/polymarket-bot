import os, time, random, logging, requests, json, asyncio
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "10"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "50"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "150"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "250"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
open_positions = []
traded_windows = set()

def get_btc_trend():
    try:
        r = requests.get(
            BINANCE_API + "/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 6},
            timeout=10
        )
        if r.ok:
            candles = r.json()
            trend = sum(1 if float(c[4]) > float(c[1]) else -1 for c in candles)
            last_price = float(candles[-1][4])
            log.info("BTC: " + str(round(last_price, 0)) + " | Tendance: " + str(trend))
            return trend, last_price
        return 0, 0
    except Exception as e:
        log.error("Erreur Binance: " + str(e))
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
                log.info("Tokens: " + str([(t["outcome"], t["token_id"][:10]) for t in tokens]))
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
                open_positions.append({"size": BET_SIZE_USDC})
                return True
            else:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, side, price):
    return asyncio.run(place_order_async(token_id, side, price))

def run():
    global daily_pnl, pnl_date
    log.info("Bot BTC 5m demarre!")
    log.info("Mise: " + str(BET_SIZE_USDC) + " | Stop-loss: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC))

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
                log.warning("Stop-loss atteint! Pause 1h.")
                time.sleep(3600)
                continue

            open_val = sum(p["size"] for p in open_positions)
            if open_val >= MAX_OPEN_USDC:
                log.info("Plafond atteint - attente...")
                time.sleep(POLL_INTERVAL)
                continue

            trend, btc_price = get_btc_trend()
            market, window_ts = get_btc_market()

            if not market or window_ts in traded_windows:
                time.sleep(POLL_INTERVAL)
                continue

            if trend >= 0:
                target = "Up"
            else:
                target = "Down"

            log.info("Signal: " + target + " (trend=" + str(trend) + ")")

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
