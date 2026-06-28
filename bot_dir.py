"""
BOT DIRECTIONNEL BTC UPDOWN v2 - avec mesure d'edge
===================================================
Difference cle avec la v1 :

  v1 : "achete le favori si son ask <= 0.65"
       -> ne verifie JAMAIS si 0.65 est un bon prix.
       -> EV inconnue, souvent negative (on paie ~fair value).

  v2 : "achete le favori SEULEMENT si P_estimee - ask > MARGE"
       -> on estime la vraie proba (modele de volatilite + temps restant)
       -> on n'achete que si le marche nous vend MOINS CHER que notre proba.
       -> et par defaut on est en PAPER MODE : aucun ordre reel,
          on ENREGISTRE le pari hypothetique et on mesure apres coup si
          P_est etait calibree et si elle bat le prix du marche.

C'est le seul moyen honnete de savoir s'il y a un edge : le mesurer
sur des dizaines de trades simules AVANT de risquer de l'argent reel.

------------------------------------------------------------------
LE MODELE DE PROBABILITE
------------------------------------------------------------------
Le marche resout "Up" si  S_fin >= K  (K = strike = prix au debut de la
fenetre). A 'tau' secondes de la fin, prix actuel S :

    ln(S_fin) ~ Normale( ln(S) , sigma_tau^2 )      (drift ~ 0 sur 5 min)

    sigma_tau = sigma_1m * sqrt(tau / 60)            (vol par minute -> par tau)

    P(Up) = Phi( ln(S / K) / sigma_tau )

sigma_1m est ESTIMEE en direct sur les dernieres bougies 1m de Binance
(pas une constante devinee). Le favori est le cote vers lequel BTC penche,
sa proba est toujours >= 0.5 :

    P_favori = Phi( |ln(S / K)| / sigma_tau )

------------------------------------------------------------------
AVERTISSEMENT DE BASE (lis-le)
------------------------------------------------------------------
Le marche se resout sur le flux CHAINLINK BTC/USD, pas Binance. Ce bot
utilise Binance pour S et pour K (source CONSISTANTE des deux cotes, donc
le biais de niveau s'annule en grande partie), mais il reste un ecart de
"basis" residuel Binance vs Chainlink. Tu ne peux pas le supprimer
facilement sans lire le flux Chainlink on-chain. BONNE NOUVELLE : le mode
paper le MESURE pour toi. Si ton P_est (base Binance) est mal calibre face
a la resolution (base Chainlink), les stats de calibration le montreront
noir sur blanc. Ne passe JAMAIS en live tant que le rapport paper ne
montre pas un edge realise positif et stable sur un echantillon suffisant.

Variables d'env (Railway) :
  PAPER_MODE        = true   <-- garde 'true' tant que pas mesure !
  DIR_MISE          = 5
  DIR_MARGE         = 0.05   (edge minimum P_est - ask exige)
  DIR_PRIX_MAX      = 0.92   (garde-fou dur : ne jamais payer au-dessus)
  DIR_MAX_TRADES    = 3
  DIR_ENTRY_WINDOW  = 90
  DIR_VOL_LOOKBACK  = 60     (bougies 1m pour estimer la vol)
  DIR_SL_JOUR       = 15
  PAPER_FILE        = paper_log.jsonl   (mets un volume Railway pour le garder)
"""

import os, sys, time, json, math, logging, threading
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

import requests
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ------------------------- Config -------------------------
PRIVATE_KEY  = os.getenv("PRIVATE_KEY", "")
WALLET       = os.getenv("WALLET", "")
GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
BINANCE      = "https://api.binance.com/api/v3"

PAPER_MODE   = os.getenv("PAPER_MODE", "true").lower() != "false"
MISE         = float(os.getenv("DIR_MISE",        "5"))
MARGE        = float(os.getenv("DIR_MARGE",       "0.05"))
PRIX_MAX     = float(os.getenv("DIR_PRIX_MAX",    "0.92"))
MAX_TRADES   = int(os.getenv("DIR_MAX_TRADES",    "3"))
ENTRY_WINDOW = int(os.getenv("DIR_ENTRY_WINDOW",  "90"))
VOL_LOOKBACK = int(os.getenv("DIR_VOL_LOOKBACK",  "60"))
SCAN_SEC     = int(os.getenv("DIR_SCAN_SEC",      "5"))
SL_JOUR      = float(os.getenv("DIR_SL_JOUR",     "15"))
PAPER_FILE   = os.getenv("PAPER_FILE", "paper_log.jsonl")

SEC_PER_YEAR = 365 * 24 * 3600

# Etat
daily_pnl    = 0.0
trades_today = 0
open_positions = []
pos_lock     = threading.Lock()
traded_markets = set()

# ------------------------- Maths -------------------------
def norm_cdf(x):
    """Phi(x), CDF normale standard."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def prob_up(S, K, tau_sec, sigma_1m):
    """P(S_fin >= K) a tau secondes de la fin."""
    if S <= 0 or K <= 0 or tau_sec <= 0 or sigma_1m <= 0:
        return None
    sigma_tau = sigma_1m * math.sqrt(tau_sec / 60.0)
    if sigma_tau <= 0:
        return None
    z = math.log(S / K) / sigma_tau
    return norm_cdf(z)

def estimate_sigma_1m():
    """Ecart-type des log-returns 1m sur les dernieres bougies (vol realisee)."""
    try:
        r = SESSION.get(BINANCE + "/klines",
                        params={"symbol": "BTCUSDT", "interval": "1m",
                                "limit": VOL_LOOKBACK + 1}, timeout=8)
        if not r.ok:
            return None
        closes = [float(c[4]) for c in r.json()]
        if len(closes) < 10:
            return None
        rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        n = len(rets)
        mean = sum(rets) / n
        var = sum((x - mean) ** 2 for x in rets) / (n - 1)
        return math.sqrt(var)
    except Exception as e:
        log.error("vol: " + str(e))
        return None

# ------------------------- Donnees marche -------------------------
def get_btc_price():
    try:
        r = SESSION.get(BINANCE + "/ticker/price",
                        params={"symbol": "BTCUSDT"}, timeout=8)
        if r.ok:
            return float(r.json()["price"])
    except Exception as e:
        log.error("btc: " + str(e))
    return None

def get_strike(window_ts):
    """Open Binance de la bougie 1m au debut de la fenetre (meme source que S)."""
    try:
        r = SESSION.get(BINANCE + "/klines",
                        params={"symbol": "BTCUSDT", "interval": "1m",
                                "startTime": window_ts * 1000, "limit": 1},
                        timeout=8)
        if r.ok and r.json():
            return float(r.json()[0][1])
    except Exception:
        pass
    return None

def get_book(token_id):
    """Retourne (best_ask, best_bid, mid). mid sert de proba implicite du marche."""
    try:
        r = SESSION.get(CLOB_API_URL + "/book",
                        params={"token_id": token_id}, timeout=8)
        if not r.ok:
            return None, None, None
        book = r.json()
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]),
                      reverse=True)
        best_ask = float(asks[0]["price"]) if asks else None
        best_bid = float(bids[0]["price"]) if bids else None
        if best_ask is not None and best_bid is not None:
            mid = (best_ask + best_bid) / 2.0
        else:
            mid = best_ask or best_bid
        return best_ask, best_bid, mid
    except Exception:
        return None, None, None

def scan_markets():
    found = []
    try:
        r = SESSION.get(GAMMA_API + "/markets",
                        params={"active": "true", "closed": "false",
                                "slug_startswith": "btc-updown-5m",
                                "limit": 10, "order": "endDate",
                                "ascending": "true"}, timeout=15)
        if not r.ok:
            return found
        now = time.time()
        for m in r.json():
            try:
                slug = m.get("slug", "")
                if slug in traded_markets:
                    continue
                window_ts = int(slug.split("-")[-1])
                end_ts = window_ts + 300
                secs_left = end_ts - now
                if secs_left <= 0 or secs_left > ENTRY_WINDOW:
                    continue
                tk = m.get("clobTokenIds")
                tk = json.loads(tk) if isinstance(tk, str) else (tk or [])
                oc = m.get("outcomes")
                oc = json.loads(oc) if isinstance(oc, str) else (oc or [])
                if len(tk) != 2 or len(oc) != 2:
                    continue
                found.append({"slug": slug, "window_ts": window_ts,
                              "end_ts": end_ts, "secs_left": secs_left,
                              "token_ids": tk, "outcomes": oc})
            except Exception:
                continue
    except Exception as e:
        log.error("scan: " + str(e))
    return found

# ------------------------- Execution -------------------------
def confirm_fill(client_place_result, token_id, want_shares):
    """
    Confirme l'execution REELLE au lieu de croire 'response.ok'.
    Renvoie le nombre de shares effectivement remplies (0 si rien).
    NOTE : adapte les noms de methode a ton SDK polymarket. L'idee fixe :
    on interroge le statut/les fills de l'ORDRE avec retry, on NE se fie
    PAS au 200 d'acceptation, et on NE traite PAS un DELETE 200 comme preuve
    de non-execution.
    """
    try:
        import asyncio
        from polymarket import AsyncSecureClient
        order_id = getattr(client_place_result, "order_id", None) \
            or (client_place_result.get("orderID")
                if isinstance(client_place_result, dict) else None)
        if not order_id:
            return 0.0

        async def _poll():
            async with await AsyncSecureClient.create(
                    private_key=PRIVATE_KEY, wallet=WALLET) as client:
                for _ in range(8):                # ~8 tentatives
                    od = await client.get_order(order_id)
                    status = (od.get("status") or "").upper()
                    filled = float(od.get("size_matched", 0) or 0)
                    if status in ("MATCHED", "FILLED") or filled >= want_shares:
                        return filled
                    if status in ("CANCELED", "CANCELLED") and filled == 0:
                        return 0.0
                    await asyncio.sleep(1.5)       # laisse le temps au match
                return filled
        return asyncio.run(_poll())
    except Exception as e:
        log.error("confirm_fill: " + str(e))
        return 0.0

def place_live(token_id, prix, shares):
    try:
        import asyncio
        from polymarket import AsyncSecureClient

        async def _buy():
            async with await AsyncSecureClient.create(
                    private_key=PRIVATE_KEY, wallet=WALLET) as client:
                buy_px = min(PRIX_MAX, round(prix + 0.01, 4))
                return await client.place_limit_order(
                    token_id=token_id, side="BUY",
                    price=str(buy_px), size=str(shares))
        return asyncio.run(_buy())
    except Exception as e:
        log.error("place_live: " + str(e))
        return None

def enter_trade(mk, fav_idx, fav_name, prix, p_est, mid):
    """Enregistre (paper) ou execute (live) le pari sur le favori."""
    global trades_today
    token_id = mk["token_ids"][fav_idx]
    shares = math.floor(MISE / prix * 100) / 100
    if shares < 1:
        return False

    if PAPER_MODE:
        filled = shares          # simulation : remplissage suppose au best ask
        mode = "PAPER"
    else:
        res = place_live(token_id, prix, shares)
        if res is None:
            return False
        filled = confirm_fill(res, token_id, shares)
        if filled < 1:
            log.info("[LIVE] non rempli (annule/expire) - aucune position")
            return False
        mode = "LIVE"

    cout = round(filled * prix, 2)
    with pos_lock:
        open_positions.append({
            "slug": mk["slug"], "outcome": fav_name,
            "shares": filled, "prix": prix, "cout": cout,
            "p_est": round(p_est, 4), "mid": round(mid, 4),
            "secs_left": int(mk["secs_left"]),
            "expiry": int(time.time()) + 400, "mode": mode,
        })
    trades_today += 1
    traded_markets.add(mk["slug"])
    log.info("[" + mode + "] PARI " + fav_name + " | " + str(filled)
             + " sh @ " + str(prix) + " | P_est " + str(round(p_est, 3))
             + " | mid " + str(round(mid, 3)) + " | edge "
             + str(round(p_est - prix, 3)) + " | cout " + str(cout) + "$ | "
             + mk["slug"][-10:] + " | trade " + str(trades_today) + "/"
             + str(MAX_TRADES))
    return True

# ------------------------- Resolution + stats -------------------------
def check_resolution(slug, outcome):
    try:
        r = SESSION.get(GAMMA_API + "/markets",
                        params={"slug": slug, "limit": 1}, timeout=10)
        if r.ok and r.json():
            m = r.json()[0]
            if not m.get("closed"):
                return None
            prices = m.get("outcomePrices", "")
            pr = json.loads(prices) if isinstance(prices, str) else prices
            if not pr:
                return None
            winner = "Up" if float(pr[0]) > 0.5 else "Down"
            return "win" if winner == outcome else "loss"
    except Exception as e:
        log.error("resolution: " + str(e))
    return None

def record_paper(pos, result):
    """Append une ligne JSONL pour l'analyse de calibration/edge."""
    try:
        won = 1 if result == "win" else 0
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "slug": pos["slug"], "outcome": pos["outcome"],
            "p_est": pos["p_est"], "mid": pos["mid"], "prix": pos["prix"],
            "secs_left": pos["secs_left"], "shares": pos["shares"],
            "cout": pos["cout"], "won": won,
            "pnl": round(pos["shares"] * won - pos["cout"], 2),
            # edge realise = (resultat 0/1) - prix paye. >0 en moyenne = vrai edge
            "edge_realise": round(won - pos["prix"], 4),
            "mode": pos["mode"],
        }
        with open(PAPER_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.error("record_paper: " + str(e))

def reglement_loop():
    global daily_pnl
    log.info("[REG] thread reglement demarre")
    while True:
        try:
            now = int(time.time())
            with pos_lock:
                copy = list(open_positions)
            for pos in copy:
                if now < pos["expiry"] - 350:
                    continue
                result = check_resolution(pos["slug"], pos["outcome"])
                if result is None:
                    if now > pos["expiry"] + 3600:
                        with pos_lock:
                            if pos in open_positions:
                                open_positions.remove(pos)
                    continue
                pnl = pos["shares"] * (1.0 if result == "win" else 0.0) - pos["cout"]
                daily_pnl += pnl
                record_paper(pos, result)
                tag = "GAGNE" if result == "win" else "PERDU"
                log.info("[" + pos["mode"] + "] " + tag + " " + pos["outcome"]
                         + " | " + ("+" if pnl >= 0 else "") + str(round(pnl, 2))
                         + "$ | P_est etait " + str(pos["p_est"])
                         + " | PnL jour " + str(round(daily_pnl, 2)) + "$")
                with pos_lock:
                    if pos in open_positions:
                        open_positions.remove(pos)
        except Exception as e:
            log.error("reglement: " + str(e))
        time.sleep(30)

def print_edge_report():
    """Lit paper_log.jsonl et dit s'il y a un edge MESURABLE."""
    try:
        rows = []
        with open(PAPER_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        n = len(rows)
        if n < 10:
            log.info("[EDGE] " + str(n) + " trades - trop peu, continue a mesurer")
            return
        wins = sum(r["won"] for r in rows)
        mean_p = sum(r["p_est"] for r in rows) / n
        mean_edge = sum(r["edge_realise"] for r in rows) / n
        # erreur-type de l'edge realise (Bernoulli sur prix paye)
        var = sum((r["edge_realise"] - mean_edge) ** 2 for r in rows) / (n - 1)
        se = math.sqrt(var / n)
        total_pnl = sum(r["pnl"] for r in rows)
        # calibration : le P_est moyen devrait ~ egaler le taux de gain reel
        hit = wins / n
        log.info("[EDGE] ===== RAPPORT ({} trades) =====".format(n))
        log.info("[EDGE] taux gain reel : {:.1%} | P_est moyen : {:.1%}"
                 " (ecart calibration {:+.1%})".format(hit, mean_p, hit - mean_p))
        log.info("[EDGE] edge realise moyen : {:+.3f} +/- {:.3f} (1 se)"
                 .format(mean_edge, se))
        log.info("[EDGE] PnL paper cumule : {:+.2f}$".format(total_pnl))
        if mean_edge - 2 * se > 0:
            log.info("[EDGE] >>> edge POSITIF significatif (>2 se). "
                     "Envisageable en live, petite taille.")
        elif mean_edge + 2 * se < 0:
            log.info("[EDGE] >>> edge NEGATIF significatif. Ne PAS passer en live.")
        else:
            log.info("[EDGE] >>> indistinguable de 0. Pas d'edge prouve, "
                     "continue en paper.")
    except FileNotFoundError:
        log.info("[EDGE] pas encore de donnees")
    except Exception as e:
        log.error("edge_report: " + str(e))

# ------------------------- Boucle principale -------------------------
def main_loop():
    global daily_pnl, trades_today
    log.info("[DIR] demarrage | MODE=" + ("PAPER" if PAPER_MODE else "LIVE")
             + " | mise " + str(MISE) + "$ | marge edge " + str(MARGE)
             + " | prix max " + str(PRIX_MAX))
    if not PAPER_MODE:
        log.warning("[DIR] *** LIVE : argent reel. Verifie ton rapport edge ***")
    pnl_date = datetime.now(timezone.utc).date()
    last_report = 0

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != pnl_date:
                log.info("[DIR] nouveau jour | PnL hier "
                         + str(round(daily_pnl, 2)) + "$")
                daily_pnl = 0.0; trades_today = 0
                traded_markets.clear(); pnl_date = today

            if time.time() - last_report > 600:   # rapport edge /10 min
                print_edge_report()
                last_report = time.time()

            if trades_today >= MAX_TRADES or daily_pnl <= -SL_JOUR:
                time.sleep(SCAN_SEC); continue

            btc = get_btc_price()
            sigma = estimate_sigma_1m()
            if btc is None or sigma is None:
                time.sleep(SCAN_SEC); continue

            for mk in scan_markets():
                K = get_strike(mk["window_ts"])
                if K is None:
                    continue
                fav_idx, fav_name = (0, "Up") if btc >= K else (1, "Down")
                p_est = prob_up(btc, K, mk["secs_left"], sigma)
                if p_est is None:
                    continue
                if fav_name == "Down":
                    p_est = 1.0 - p_est            # proba du cote favori
                ask, bid, mid = get_book(mk["token_ids"][fav_idx])
                if ask is None or mid is None:
                    continue
                edge = p_est - ask
                log.info("[DIR] " + mk["slug"][-10:] + " | BTC " + str(round(btc))
                         + " vs K " + str(round(K)) + " | " + fav_name
                         + " P_est " + str(round(p_est, 3)) + " ask "
                         + str(round(ask, 3)) + " mid " + str(round(mid, 3))
                         + " | edge " + str(round(edge, 3)) + " | "
                         + str(int(mk["secs_left"])) + "s")
                # CONDITION CLE : on achete seulement si on paie MOINS que notre proba
                if edge > MARGE and ask <= PRIX_MAX:
                    enter_trade(mk, fav_idx, fav_name, ask, p_est, mid)

        except Exception as e:
            log.error("main: " + str(e))
        time.sleep(SCAN_SEC)

if __name__ == "__main__":
    threading.Thread(target=reglement_loop, daemon=True).start()
    main_loop()
