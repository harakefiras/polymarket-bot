import os, time, logging
from datetime import datetime
from binance.client import Client

# ── Config ──────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")

TRADE_AMOUNT_USDT  = float(os.getenv("TRADE_AMOUNT_USDT", "200"))
TARGET_PROFIT_PCT  = float(os.getenv("TARGET_PROFIT_PCT", "0.003"))   # 0.3%
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT", "0.002"))        # 0.2%
DAILY_STOP_LOSS    = float(os.getenv("DAILY_STOP_LOSS", "50"))

# Paires à surveiller — modifiable depuis Railway sans toucher au code
SYMBOLS            = os.getenv("SYMBOLS", "BNBUSDT,ETHUSDT,BTCUSDT,SOLUSDT,XRPUSDT").split(",")

MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.20"))
RSI_OVERBOUGHT     = float(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD       = float(os.getenv("RSI_OVERSOLD", "30"))
COOLDOWN_SECONDS   = int(os.getenv("COOLDOWN_SECONDS", "300"))
TRAILING_OFFSET    = float(os.getenv("TRAILING_OFFSET", "0.0015"))
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("binance_bot")

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# ── État global ──────────────────────────────────────────────────────────────
daily_pnl   = 0.0
positions   = {}   # {symbol: {entry, qty, peak}}
cooldowns   = {}   # {symbol: timestamp fin cooldown}

# ── Indicateurs ─────────────────────────────────────────────────────────────
def get_closes(symbol, interval, limit):
    candles = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    return [float(c[4]) for c in candles]

def get_momentum(symbol):
    """Momentum sur 3 bougies 1 minute"""
    try:
        closes = get_closes(symbol, Client.KLINE_INTERVAL_1MINUTE, 3)
        move = (closes[-1] - closes[0]) / closes[0] * 100
        log.info(f"{symbol} momentum 1m: {round(move, 3)}%")
        return move
    except Exception as e:
        log.error(f"Erreur momentum {symbol}: {e}")
        return 0

def get_trend(symbol):
    """Tendance sur 15 bougies 1 minute — True si haussière"""
    try:
        closes = get_closes(symbol, Client.KLINE_INTERVAL_1MINUTE, 15)
        # EMA courte vs EMA longue
        ema5  = sum(closes[-5:])  / 5
        ema15 = sum(closes) / 15
        return ema5 > ema15
    except Exception as e:
        log.error(f"Erreur tendance {symbol}: {e}")
        return False

def get_rsi(symbol, period=14):
    """RSI sur bougies 1 minute"""
    try:
        closes = get_closes(symbol, Client.KLINE_INTERVAL_1MINUTE, period + 1)
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i-1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100
        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    except Exception as e:
        log.error(f"Erreur RSI {symbol}: {e}")
        return 50

def get_price(symbol):
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception as e:
        log.error(f"Erreur prix {symbol}: {e}")
        return 0

def get_step_size(symbol):
    """Récupère la précision de quantité pour le symbole"""
    try:
        info = client.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                precision = len(str(step).rstrip("0").split(".")[-1])
                return precision
    except:
        return 2

# ── Ordres ───────────────────────────────────────────────────────────────────
def buy(symbol, price):
    try:
        precision = get_step_size(symbol)
        qty = round(TRADE_AMOUNT_USDT / price, precision)
        client.order_market_buy(symbol=symbol, quantity=qty)
        log.info(f"✅ ACHAT {symbol} | Qty: {qty} @ {round(price, 4)}")
        return {"qty": qty, "entry": price, "peak": price}
    except Exception as e:
        log.error(f"Erreur achat {symbol}: {e}")
        return None

def sell(symbol, qty, reason, entry):
    try:
        client.order_market_sell(symbol=symbol, quantity=qty)
        price = get_price(symbol)
        pnl   = (price - entry) * qty
        log.info(f"{'✅' if pnl > 0 else '❌'} VENTE {reason} {symbol} @ {round(price, 4)} | PnL: {round(pnl, 2)}$")
        return price, pnl
    except Exception as e:
        log.error(f"Erreur vente {symbol}: {e}")
        return 0, 0

# ── Boucle principale ─────────────────────────────────────────────────────────
def run():
    global daily_pnl

    log.info("🚀 Bot Scalping Multi-Paires démarré!")
    log.info(f"Paires: {', '.join(SYMBOLS)}")
    log.info(f"Mise: {TRADE_AMOUNT_USDT}$ | TP: {TARGET_PROFIT_PCT*100}% | SL: {STOP_LOSS_PCT*100}%")
    log.info(f"Trailing Stop: {TRAILING_OFFSET*100}% | Cooldown SL: {COOLDOWN_SECONDS//60}min")

    if not BINANCE_API_KEY:
        log.error("BINANCE_API_KEY manquante!")
        return

    while True:
        try:
            # ── Stop-loss journalier ─────────────────────────────────────────
            if daily_pnl <= -DAILY_STOP_LOSS:
                log.warning(f"⛔ Stop-loss journalier atteint ({round(daily_pnl,2)}$). Pause 1h.")
                time.sleep(3600)
                daily_pnl = 0.0
                continue

            now = time.time()

            for symbol in SYMBOLS:
                try:
                    price = get_price(symbol)
                    if price <= 0:
                        continue

                    # ── Position ouverte → surveille TP / Trailing SL ────────
                    if symbol in positions:
                        pos   = positions[symbol]
                        entry = pos["entry"]
                        qty   = pos["qty"]
                        peak  = pos["peak"]
                        pct   = (price - entry) / entry

                        # Met à jour le peak (plus haut atteint)
                        if price > peak:
                            positions[symbol]["peak"] = price
                            peak = price

                        # Trailing Stop Loss
                        trailing_sl = peak * (1 - TRAILING_OFFSET)

                        log.info(f"Positions: {symbol} | Entry: {round(entry,4)} | Now: {round(price,4)} | PnL: {round(pct*100,3)}% | Trail SL: {round(trailing_sl,4)}")

                        # Take Profit
                        if pct >= TARGET_PROFIT_PCT:
                            sell_price, pnl = sell(symbol, qty, "TP", entry)
                            daily_pnl += pnl
                            log.info(f"💰 Total jour: {round(daily_pnl, 2)}$")
                            del positions[symbol]

                        # Trailing Stop Loss déclenché
                        elif price <= trailing_sl and pct > 0:
                            sell_price, pnl = sell(symbol, qty, "TRAIL SL", entry)
                            daily_pnl += pnl
                            log.info(f"📊 Total jour: {round(daily_pnl, 2)}$")
                            del positions[symbol]

                        # Stop Loss classique
                        elif pct <= -STOP_LOSS_PCT:
                            sell_price, pnl = sell(symbol, qty, "SL", entry)
                            daily_pnl += pnl
                            cooldowns[symbol] = now + COOLDOWN_SECONDS
                            log.info(f"⏳ Cooldown {symbol} 5min | Total: {round(daily_pnl, 2)}$")
                            del positions[symbol]

                    # ── Pas de position → cherche signal ────────────────────
                    else:
                        # Vérifie cooldown
                        if symbol in cooldowns and now < cooldowns[symbol]:
                            reste = int(cooldowns[symbol] - now)
                            log.info(f"⏳ {symbol} cooldown: {reste}s restantes")
                            continue

                        # Max positions simultanées
                        if len(positions) >= MAX_POSITIONS:
                            continue

                        momentum = get_momentum(symbol)

                        if momentum >= MOMENTUM_THRESHOLD:
                            # Filtre tendance
                            if not get_trend(symbol):
                                log.info(f"⚠️ {symbol} momentum OK mais tendance baissière — skip")
                                continue

                            # Filtre RSI
                            rsi = get_rsi(symbol)
                            log.info(f"{symbol} RSI: {round(rsi, 1)}")
                            if rsi > RSI_OVERBOUGHT:
                                log.info(f"⚠️ {symbol} RSI surachat ({round(rsi,1)}) — skip")
                                continue

                            log.info(f"🎯 Signal {symbol} | Momentum: {round(momentum,3)}% | RSI: {round(rsi,1)} | Achat!")
                            pos = buy(symbol, price)
                            if pos:
                                positions[symbol] = pos

                except Exception as e:
                    log.error(f"Erreur {symbol}: {e}")
                    continue

            log.info(f"📈 Positions: {len(positions)}/{len(SYMBOLS)} | PnL jour: {round(daily_pnl, 2)}$")

        except Exception as e:
            log.error(f"Erreur globale: {e}")

        time.sleep(30)

if __name__ == "__main__":
    run()
