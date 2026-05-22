import os, time, random, logging, requests, json, asyncio
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
WALLET = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "5"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "20"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "50"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3600"))
MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "500"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
open_positions = []
seen_trades = set()

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
        return r.json() if r.ok else []
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
            tid = t.get("id") or t.get("transactionHash")
            if not tid or tid in seen_trades:
                continue
            size = float(t.get("size", 0))
            price = float(t.get("price", 0.5))
            notional = size * price
            if notional >= MIN_WHALE_USDC and 0.20 <= price <= 0.80:
                whales.append({
                    "id": tid,
                    "market": question,
                    "token_id": t.get("asset_id", mid),
                    "price": price,
                    "notional": notional,
                    "outcome": t.get("outcome", "YES")
                })
    return whales

def get_token_price(token_id):
    try:
        r = requests.get(CLOB_API + "/last-trade-price", params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

async def place_order_async(token_id, outcome, price):
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
                log.info("TRADE " + outcome + " " + str(BET_SIZE_USDC) + " USDC @ " + str(round(price, 2)) + " | order_id=" + str(response.order_id))
                open_positions.append({"size": BET_SIZE_USDC, "token_id": token_id})
                return True
            else:
                log.error("Erreur ordre: " + str(response.code) + " " + str(response.message))
                return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def place_order(token_id, outcome, price):
    return asyncio.run(place_order_async(token_id, outcome, price))

def run():
    global daily_pnl, pnl_date
    log.info("Bot Copy Whales demarre!")
    log.info("Mise: " + str(BET_SIZE_USDC) + " | Stop-loss: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC) + " | Whale min: " + str(MIN_WHALE_USDC))

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

            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss atteint! Pause 1h.")
                time.sleep(3600)
                continue

            open_val = sum(p["size"] for p in open_positions)
            log.info("Positions ouvertes: " + str(round(open_val, 2)) + "/" + str(MAX_OPEN_USDC) + " USDC")

            if open_val >= MAX_OPEN_USDC:
                log.info("Plafond atteint - attente retours...")
                time.sleep(POLL_INTERVAL)
                continue

            log.info("Scan des marches...")
            markets = get_active_markets()
            log.info("Marches trouves: " + str(len(markets)))

            whales = detect_whales(markets)

            if whales:
                log.info(str(len(whales)) + " whale(s) detectee(s)!")
                for w in whales:
                    open_val = sum(p["size"] for p in open_positions)
                    if open_val + BET_SIZE_USDC > MAX_OPEN_USDC:
                        log.info("Plafond atteint")
                        break
                    if daily_pnl <= -STOP_LOSS_USDC:
                        break
                    log.info("Whale: " + str(round(w["notional"])) + " USDC sur '" + w["market"] + "' @ " + str(round(w["price"], 2)))
                    time.sleep(5)
                    price = get_token_price(w["token_id"])
                    if price <= 0:
                        price = w["price"]
                    if 0.20 <= price <= 0.80:
                        if place_order(w["token_id"], w["outcome"], price):
                            seen_trades.add(w["id"])
                    else:
                        log.info("Prix hors fourchette: " + str(round(price, 2)))
            else:
                log.info("Aucune whale detectee")

        except Exception as e:
            log.error("Erreur: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 60)
        log.info("Prochain scan dans " + str(int(wait/60)) + " min")
        time.sleep(wait)

if __name__ == "__main__":
    run()
