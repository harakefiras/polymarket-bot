import os, time, random, logging, requests
from datetime import date

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "10"))
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "50"))
MAX_OPEN_USDC = float(os.getenv("MAX_OPEN_USDC", "150"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))

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
            opens  = [float(c[1]) for c in candles]
            closes = [float(c[4]) for c in candles]
            trend = sum(1 if closes[i] > opens[i] else -1 for i in range(len(candles)))
            last_price = closes[-1]
            log.info("BTC prix: " + str(round(last_price, 0)) + " | Tendance: " + str(trend))
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
        log.info("Cherche slug: " + slug)
        r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=10)
        if r.ok:
            data = r.json()
            market = data[0] if isinstance(data, list) and len(data) > 0 else None
            if market:
                log.info("Marche trouve! tokens: " + str(market.keys()))
                outcomes = market.get("outcomes", "[]")
                token_ids = market.get("clobTokenIds", "[]")
                import json
                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                token_ids = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
                tokens = [{"outcome": outcomes[i], "token_id": token_ids[i]} for i in range(len(outcomes))]
                market["tokens"] = tokens
                log.info("Tokens: " + str(tokens))
                return market, window_ts
        return None, None
    except Exception as e:
        log.error("Erreur marche: " + str(e))
        return None, None

def get_token_price(token_id):
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

def place_order(token_id, side, price, market_name):
    try:
        from eth_account import Account
        account = Account.from_key(PRIVATE_KEY)
        size = round(BET_SIZE_USDC / price, 2)
        order = {
            "market": token_id,
            "side": "BUY",
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
            log.info("TRADE: " + side + " " + str(BET_SIZE_USDC) + " USDC @ " + str(round(price, 2)))
            open_positions.append({"size": BET_SIZE_USDC})
            return True
        else:
            log.error("Erreur ordre: " + str(r.status_code) + " " + r.text[:150])
            return False
    except Exception as e:
        log.error("Exception ordre: " + str(e))
        return False

def run():
    global daily_pnl, pnl_date
    log.info("Bot BTC 5m + Algo Binance demarre!")
    log.info("Mise: " + str(BET_SIZE_USDC) + " | Stop-loss: " + str(STOP_LOSS_USDC) + " | Plafond: " + str(MAX_OPEN_USDC))

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

            # Analyse tendance BTC sur Binance
            trend, btc_price = get_btc_trend()

            # Recupere marche BTC 5m actuel
            market, window_ts = get_btc_market()

            if not market or window_ts in traded_windows:
                log.info("Marche deja trade ou introuvable")
                time.sleep(POLL_INTERVAL)
                continue

            tokens = market.get("tokens", [])
            if not tokens:
                log.info("Pas de tokens dans ce marche")
                time.sleep(POLL_INTERVAL)
                continue

            # Decide UP ou DOWN selon tendance
            if trend >= 0:
                target_outcome = "UP"
                log.info("Signal: UP (tendance haussiere " + str(trend) + ")")
            elif trend < 0:
                target_outcome = "DOWN"
                log.info("Signal: DOWN (tendance baissiere " + str(trend) + ")")
            else:
                log.info("Tendance neutre (" + str(trend) + ") - pas de trade")
                time.sleep(POLL_INTERVAL)
                continue

            # Trouve le token correspondant
            for token in tokens:
                outcome = token.get("outcome", "").upper()
                if outcome == target_outcome:
                    token_id = token.get("token_id", "")
                    price = get_token_price(token_id)
                    if price <= 0:
                        price = float(token.get("price", 0.5))

                    log.info(target_outcome + " @ " + str(round(price, 2)))

                    # Trade seulement si prix entre 0.35 et 0.75
                    if 0.35 <= price <= 0.75:
                        success = place_order(token_id, target_outcome, price, market.get("question", ""))
                        if success:
                            traded_windows.add(window_ts)
                    else:
                        log.info("Prix hors fourchette: " + str(round(price, 2)) + " - pas de trade")

        except Exception as e:
            log.error("Erreur boucle: " + str(e))

        wait = POLL_INTERVAL + random.uniform(0, 10)
        log.info("Prochain scan dans " + str(int(wait)) + "s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
