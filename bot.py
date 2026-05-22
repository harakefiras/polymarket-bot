import os, time, random, logging, requests
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "10"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "50"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "150"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "60"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

daily_pnl = 0.0
pnl_date = date.today()
open_positions = []
traded_markets = set()

def get_btc_markets():
    try:
        import time as t
        now = int(t.time())
        window_ts = now - (now % 300)
        slug = "btc-updown-5m-" + str(window_ts)
        log.info("Cherche slug: " + slug)
        r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=10)
        if r.ok:
            data = r.json()
            if isinstance(data, list):
                log.info("Marches BTC 5m trouves: " + str(len(data)))
                return data
            elif isinstance(data, dict):
                return [data]
        return []
    except Exception as e:
        log.error("Erreur marches: " + str(e))
        return []

def get_market_orderbook(token_id):
    try:
        r = requests.get(
            CLOB_API + "/book",
            params={"token_id": token_id},
            timeout=10
        )
        if r.ok:
            return r.json()
        return None
    except Exception as e:
        log.error("Erreur orderbook: " + str(e))
        return None

def get_market_last_trade_price(token_id):
    try:
        r = requests.get(
            CLOB_API + "/last-trade-price",
            params={"token_id": token_id},
            timeout=10
        )
        if r.ok:
            return float(r.json().get("price", 0))
        return 0
    except:
        return 0

def place_order(market, token_id, side, price):
    try:
        from eth_account import Account
        account = Account.from_key(PRIVATE_KEY)
        size = round(BET_SIZE_USDC / price, 2)
        order = {
            "market": token_id,
            "side": side,
            "price": round(price, 4),
            "size": size,
            "type": "GTC"
        }
        r = requests.post(
            CLOB_API + "/order",
            json=order,
            headers={"POLY_ADDRESS": account.address},
            timeout=10
        )
        if r.ok:
            log.info("Trade place: " + side + " " + str(BET_SIZE_USDC) + " USDC @ " + str(round(price,2)) + " sur " + market)
            open_positions.append({"size": BET_SIZE_USDC})
            traded_markets.add(token_id)
            return True
        else:
            log.error("Erreur ordre: " + str(r.status_code) + " " + r.text[:100])
            return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def analyze_and_trade(market):
    try:
        tokens = market.get("tokens", [])
        if not tokens:
            return

        question = market.get("question", "?")
        market_id = market.get("conditionId", "")

        for token in tokens:
            token_id = token.get("token_id", "")
            outcome = token.get("outcome", "")

            if token_id in traded_markets:
                continue

            price = get_market_last_trade_price(token_id)
            if price <= 0:
                price = float(token.get("price", 0))

            if price <= 0:
                continue

            log.info("BTC 5m - " + outcome + " @ " + str(round(price, 2)) + " | " + question)

            # Strategie: miser sur UP si prix < 0.55 (favorable)
            if outcome.upper() in ["UP", "YES"] and 0.30 <= price <= 0.65:
                log.info("Signal UP detecte @ " + str(round(price, 2)))
                open_val = sum(p["size"] for p in open_positions)
                if open_val + BET_SIZE_USDC <= MAX_OPEN_USDC:
                    place_order(question, token_id, "BUY", price)
                else:
                    log.info("Plafond atteint")

    except Exception as e:
        log.error("Erreur analyse: " + str(e))

def run():
    global daily_pnl, pnl_date
    log.info("Bot BTC 5m demarre!")
    log.info("Mise: " + str(BET_SIZE_USDC) + " USDC | Stop-loss: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC))

    if not PRIVATE_KEY.startswith("0x"):
        log.error("PRIVATE_KEY manquante!")
        return

    while True:
        try:
            today = date.today()
            if today != pnl_date:
                log.info("Nouveau jour - PnL: " + str(round(daily_pnl, 2)))
                daily_pnl = 0.0
                pnl_date = today
                traded_markets.clear()

            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning("Stop-loss atteint! Pause 1h.")
                time.sleep(3600)
                continue

            open_val = sum(p["size"] for p in open_positions)
            log.info("Positions: " + str(round(open_val, 2)) + "/" + str(MAX_OPEN_USDC) + " USDC")

            if open_val >= MAX_OPEN_USDC:
                log.info("Plafond atteint - attente...")
                time.sleep(POLL_INTERVAL)
                continue

            markets = get_btc_markets()
            if not markets:
                log.info("Aucun marche BTC 5m actif")
            else:
                for m in markets:
                    analyze_and_trade(m)

        except Exception as e:
            log.error("Erreur boucle: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait)) + "s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
