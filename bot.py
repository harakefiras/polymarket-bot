"""
Polymarket Copy Bot — copie les gros traders automatiquement
"""

import os, time, random, logging
from datetime import datetime, date
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, Side

# ─── Config (depuis variables d'environnement Railway) ──────────────────────
PRIVATE_KEY   = os.environ["PRIVATE_KEY"]          # ta clé privée 0x...
CHAIN_ID      = int(os.getenv("CHAIN_ID", "137"))  # 137 = Polygon
HOST          = os.getenv("HOST", "https://clob.polymarket.com")

MIN_WHALE_USDC = float(os.getenv("MIN_WHALE_USDC", "500"))   # taille trade whale min
BET_SIZE_USDC  = float(os.getenv("BET_SIZE_USDC",  "10"))    # ta mise par trade
MIN_PROB       = float(os.getenv("MIN_PROB",        "0.20"))  # prob min du marché
COPY_DELAY_S   = float(os.getenv("COPY_DELAY_S",   "5"))     # délai avant copie
STOP_LOSS_USDC = float(os.getenv("STOP_LOSS_USDC", "50"))    # stop-loss journalier
POLL_INTERVAL  = float(os.getenv("POLL_INTERVAL",  "15"))    # secondes entre checks

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("copybot")

# ─── État session ────────────────────────────────────────────────────────────
daily_pnl   = 0.0
pnl_date    = date.today()
seen_trades = set()   # évite de copier le même trade deux fois

# ─── Client Polymarket ───────────────────────────────────────────────────────
def make_client() -> ClobClient:
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client

# ─── Logique principale ───────────────────────────────────────────────────────
def reset_daily_pnl_if_new_day():
    global daily_pnl, pnl_date
    today = date.today()
    if today != pnl_date:
        log.info(f"Nouveau jour — PnL hier: {daily_pnl:+.2f} USDC. Remise à zéro.")
        daily_pnl = 0.0
        pnl_date  = today

def fetch_recent_large_trades(client: ClobClient) -> list:
    """Récupère les trades récents et filtre les gros montants."""
    try:
        # Récupère les marchés actifs
        markets = client.get_markets()
        large_trades = []

        for market in markets.get("data", [])[:30]:  # vérifie les 30 premiers marchés
            token_id = market.get("condition_id")
            if not token_id:
                continue

            trades = client.get_trades({"market": token_id, "limit": 10})
            for trade in trades.get("data", []):
                trade_id = trade.get("id")
                if trade_id in seen_trades:
                    continue

                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))
                notional = size * price

                if notional >= MIN_WHALE_USDC and MIN_PROB <= price <= (1 - MIN_PROB):
                    large_trades.append({
                        "id":        trade_id,
                        "market":    market.get("question", "?"),
                        "token_id":  token_id,
                        "side":      trade.get("side", "BUY"),
                        "price":     price,
                        "notional":  notional,
                        "outcome":   trade.get("outcome", "YES"),
                    })

        return large_trades

    except Exception as e:
        log.warning(f"Erreur fetch trades: {e}")
        return []

def copy_trade(client: ClobClient, trade: dict):
    """Exécute une copie du trade détecté."""
    global daily_pnl

    log.info(f"🐋 Whale: {trade['notional']:.0f} USDC sur '{trade['market']}' → {trade['outcome']} @ {trade['price']:.2f}")

    if COPY_DELAY_S > 0:
        log.info(f"⏱  Attente {COPY_DELAY_S}s...")
        time.sleep(COPY_DELAY_S)

    try:
        # Calcule la taille en shares
        shares = BET_SIZE_USDC / trade["price"]

        order_args = OrderArgs(
            token_id  = trade["token_id"],
            price     = trade["price"],
            size      = round(shares, 2),
            side      = Side.BUY,
            order_type= OrderType.GTC,
        )

        resp = client.create_and_post_order(order_args)
        log.info(f"✅ Trade copié: {BET_SIZE_USDC} USDC → {trade['outcome']} | order_id={resp.get('orderID','?')}")
        seen_trades.add(trade["id"])

    except Exception as e:
        log.error(f"❌ Échec copie trade: {e}")

def run():
    log.info("🚀 Polymarket Copy Bot démarré")
    log.info(f"   Whale min:    {MIN_WHALE_USDC} USDC")
    log.info(f"   Mise/trade:   {BET_SIZE_USDC} USDC")
    log.info(f"   Prob min:     {MIN_PROB*100:.0f}%")
    log.info(f"   Stop-loss:    -{STOP_LOSS_USDC} USDC/jour")
    log.info(f"   Délai copie:  {COPY_DELAY_S}s")

    client = make_client()
    log.info("✅ Connecté à Polymarket")

    while True:
        try:
            reset_daily_pnl_if_new_day()

            # Stop-loss
            if daily_pnl <= -STOP_LOSS_USDC:
                log.warning(f"🛑 Stop-loss atteint ({daily_pnl:.2f} USDC). Pause 1h.")
                time.sleep(3600)
                continue

            trades = fetch_recent_large_trades(client)

            if trades:
                log.info(f"👁  {len(trades)} whale(s) détectée(s)")
                for trade in trades:
                    copy_trade(client, trade)
            else:
                log.info("👁  Surveillance — aucune whale")

        except Exception as e:
            log.error(f"Erreur boucle principale: {e}")

        # Attente aléatoire pour éviter le rate-limiting
        wait = POLL_INTERVAL + random.uniform(0, 5)
        time.sleep(wait)

if __name__ == "__main__":
    run()
