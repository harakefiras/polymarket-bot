"""
BOT ARBITRAGE YES+NO v2 - corrige et mesurable
==============================================
Principe (inchange, et c'est le seul des 4 bots avec un vrai edge potentiel) :
  quand ask(Up) + ask(Down) < 1, acheter LES DEUX cotes ; a l'expiration
  l'un vaut 1.00, donc gain = 1 - somme_payee, sans pari directionnel.

CE QUE LA v1 FAISAIT MAL (et qui a produit le faux "8.87") :
  - confirmation de fill basee sur un snapshot /positions a +5s, en plus
    NON AUTHENTIFIE (CLOB_API_KEY n'existait pas) -> renvoyait vide ->
    le bot croyait "rien execute", annulait dans le vide, et laissait une
    jambe ORPHELINE non revendue. Le gain venait du hasard (la jambe seule
    gagnait), pas de l'arbitrage.
  - taille calculee sur le MEILLEUR ask seulement : l'arb affiche a 0.97
    n'existe que sur 2-3 shares ; en taille reelle la somme repasse > 1.
  - achat a px+0.01 -> on paie plus que le prix affiche -> l'edge de 3%
    est mange avant meme les frais.

CE QUE LA v2 CORRIGE :
  1. FILL REEL : on interroge le STATUT de chaque ordre (size_matched) avec
     retry, via le client authentifie. Jamais un snapshot /positions, jamais
     un DELETE 200 pris pour "non execute".
  2. ORPHELINE : si une seule jambe remplit, on revend l'orpheline tout de
     suite et on compte la VRAIE perte (pas une estimation 0.05).
  3. TAILLE REELLE : on "marche le carnet" sur les deux cotes pour la taille
     visee, on calcule la somme EFFECTIVE (slippage inclus), et on n'entre
     que si cette somme effective passe encore sous le seuil.

MODE OBSERVATION (OBSERVE_ONLY=true, defaut) :
  le bot scanne, calcule pour chaque "opportunite" combien elle survit EN
  TAILLE REELLE, et le PnL qu'il AURAIT verrouille - sans passer aucun ordre.
  Apres 2-3 jours tu sauras si l'arb a un edge reel ou s'il n'existe qu'a
  l'ecran. Passe OBSERVE_ONLY=false seulement si les chiffres sont positifs.

Variables d'env :
  OBSERVE_ONLY      = true     <-- garde 'true' tant que pas mesure
  ARB_SUM_MAX       = 0.97
  ARB_TRADE_USDC    = 10
  ARB_MAX_CONCURRENT= 3
  ARB_MIN_TIME_LEFT = 60
  ARB_SCAN_INTERVAL = 1
  ARB_ENABLE_SPORT  = false    <-- scanner sport DESACTIVE par defaut (dangereux)
  OBS_FILE          = arb_observe.jsonl   (mets un volume Railway !)
"""

import os, sys, time, json, math, logging, threading, asyncio
from datetime import date, datetime, timezone

import requests
SESSION = requests.Session()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
log = logging.getLogger("arb")

# ------------------------- Config -------------------------
PRIVATE_KEY  = os.environ.get("PRIVATE_KEY", "")
WALLET       = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")

OBSERVE_ONLY = os.getenv("OBSERVE_ONLY", "true").lower() != "false"
SUM_MAX      = float(os.getenv("ARB_SUM_MAX",        "0.97"))
TRADE_USDC   = float(os.getenv("ARB_TRADE_USDC",     "10"))
MAX_CONCUR   = int(os.getenv("ARB_MAX_CONCURRENT",   "3"))
MIN_TIME_LEFT= int(os.getenv("ARB_MIN_TIME_LEFT",    "60"))
SCAN_INTERVAL= float(os.getenv("ARB_SCAN_INTERVAL",  "1"))
ENABLE_SPORT = os.getenv("ARB_ENABLE_SPORT", "false").lower() == "true"
OBS_FILE     = os.getenv("OBS_FILE", "arb_observe.jsonl")
STOP_LOSS    = float(os.getenv("ARB_STOP_LOSS_USDC", "10"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

MARKETS = {
    "BTC5":  {"slug": "btc-updown-5m-",  "window": 300},
    "ETH15": {"slug": "eth-updown-15m-", "window": 900},
    "SOL15": {"slug": "sol-updown-15m-", "window": 900},
}

# Etat
daily_pnl   = 0.0
open_arbs   = []
arbs_lock   = threading.Lock()
done_windows = set()
# compteurs observation
obs_seen = 0
obs_executable = 0
obs_hypo_pnl = 0.0

# ------------------------- Carnet -------------------------
def get_asks(token_id):
    """Retourne la liste triee [(prix, taille), ...] du cote ASK, ou []."""
    try:
        r = SESSION.get(CLOB_API + "/book",
                        params={"token_id": token_id}, timeout=8)
        if not r.ok:
            return []
        asks = r.json().get("asks", [])
        out = [(float(a["price"]), float(a["size"])) for a in asks]
        out.sort(key=lambda x: x[0])      # du moins cher au plus cher
        return out
    except Exception:
        return []

def walk_book(asks, shares_wanted):
    """
    Cout reel pour acheter 'shares_wanted' en consommant le carnet niveau
    par niveau. Retourne (prix_moyen_effectif, shares_remplissables).
    C'est LA correction cle : on ne suppose plus que tout se fait au meilleur ask.
    """
    if shares_wanted <= 0 or not asks:
        return None, 0.0, None
    remaining = shares_wanted
    cost = 0.0
    filled = 0.0
    worst_px = asks[0][0]
    for px, size in asks:
        take = min(remaining, size)
        cost += take * px
        filled += take
        remaining -= take
        worst_px = px
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return None, 0.0, None
    return cost / filled, filled, worst_px

def best_executable(asks_up, asks_down, capital, sum_max):
    """
    Cherche la taille reelle de l'arb : on vise la taille permise par le
    capital, puis on regarde si la somme EFFECTIVE (slippage des deux cotes)
    passe sous le seuil et combien de shares sont reellement disponibles.
    Retourne un dict de mesure (sans rien executer).
    """
    if not asks_up or not asks_down:
        return None
    best_up, best_down = asks_up[0][0], asks_down[0][0]
    best_sum = best_up + best_down
    target = math.floor(capital / max(best_sum, 1e-6) * 100) / 100
    if target < 1:
        target = 1.0
    avg_up, fill_up, worst_up = walk_book(asks_up, target)
    avg_down, fill_down, worst_down = walk_book(asks_down, target)
    if avg_up is None or avg_down is None:
        return None
    fillable = min(fill_up, fill_down)
    eff_sum = avg_up + avg_down
    executable = (eff_sum <= sum_max) and (fillable >= 1.0)
    hypo_pnl = (1.0 - eff_sum) * min(target, fillable) if executable else 0.0
    return {
        "best_up": best_up, "best_down": best_down, "best_sum": best_sum,
        "target": target, "avg_up": avg_up, "avg_down": avg_down,
        "eff_sum": eff_sum, "fill_up": fill_up, "fill_down": fill_down,
        "fillable": fillable, "worst_up": worst_up, "worst_down": worst_down,
        "executable": executable, "hypo_pnl": hypo_pnl,
    }

# ------------------------- Marche -------------------------
def get_tokens(slug_prefix, window_ts):
    try:
        slug = slug_prefix + str(window_ts)
        r = SESSION.get(GAMMA_API + "/markets",
                        params={"slug": slug}, timeout=8)
        if r.ok:
            data = r.json()
            m = data[0] if isinstance(data, list) and data else None
            if m and m.get("slug") == slug:
                oc = m.get("outcomes")
                oc = json.loads(oc) if isinstance(oc, str) else (oc or [])
                tk = m.get("clobTokenIds")
                tk = json.loads(tk) if isinstance(tk, str) else (tk or [])
                if len(oc) == 2 and len(tk) == 2:
                    return [{"outcome": oc[i], "token_id": tk[i]}
                            for i in range(2)]
    except Exception:
        pass
    return None

# ------------------------- Execution reelle (OBSERVE_ONLY=false) -------------------------
async def confirm_fill(client, order_id, want):
    """
    Confirme l'execution REELLE : on lit le statut de l'ORDRE (size_matched)
    avec retry. PAS un snapshot /positions. Renvoie shares remplies.
    NB : adapte 'get_order' / champs au SDK polymarket si besoin.
    """
    if not order_id:
        return 0.0
    for _ in range(8):
        try:
            od = await client.get_order(order_id)
            status = (od.get("status") or "").upper()
            filled = float(od.get("size_matched", 0) or 0)
            if status in ("MATCHED", "FILLED") or filled >= want - 1e-9:
                return filled
            if status in ("CANCELED", "CANCELLED") and filled == 0:
                return 0.0
        except Exception:
            pass
        await asyncio.sleep(1.5)
    # derniere lecture best-effort
    try:
        od = await client.get_order(order_id)
        return float(od.get("size_matched", 0) or 0)
    except Exception:
        return 0.0

async def buy_leg(client, token_id, limit_px, shares):
    try:
        resp = await client.place_limit_order(
            token_id=token_id, side="BUY",
            price=str(round(limit_px, 4)), size=str(shares))
        if resp.ok:
            return getattr(resp, "order_id", None) or getattr(resp, "id", None)
    except Exception as e:
        log.error("buy_leg: " + str(e))
    return None

async def cancel_leg(client, order_id):
    """Annule un ordre non rempli pour qu'il ne traine pas sur le carnet
    et ne devienne pas une position fantome remplie apres coup."""
    if not order_id:
        return
    try:
        cancel = getattr(client, "cancel_order", None)
        if cancel:
            await cancel(order_id=order_id)
    except Exception as e:
        log.error("cancel_leg: " + str(e))

async def sell_orphan(client, token_id, shares, ref_px):
    """Revend une jambe orpheline immediatement, agressivement."""
    try:
        sell_px = max(0.01, round(ref_px - 0.05, 4))
        sh = max(0.01, round(shares - 0.01, 2))
        resp = await client.place_limit_order(
            token_id=token_id, side="SELL",
            price=str(sell_px), size=str(sh))
        return bool(resp.ok), sell_px
    except Exception as e:
        log.error("sell_orphan: " + str(e))
        return False, 0.0

async def execute_real(up_id, down_id, meas, mkey):
    """Execute l'arb pour de vrai, avec fill confirme et orpheline geree."""
    global daily_pnl
    from polymarket import AsyncSecureClient
    target = meas["target"]
    # limite = pire prix qu'on accepte de payer en marchant le carnet
    up_lim   = min(0.99, round(meas["worst_up"], 4))
    down_lim = min(0.99, round(meas["worst_down"], 4))
    async with await AsyncSecureClient.create(
            private_key=PRIVATE_KEY, wallet=WALLET) as client:
        oid_up, oid_down = await asyncio.gather(
            buy_leg(client, up_id, up_lim, target),
            buy_leg(client, down_id, down_lim, target),
        )
        f_up, f_down = await asyncio.gather(
            confirm_fill(client, oid_up, target),
            confirm_fill(client, oid_down, target),
        )
        # CORRECTION CLE : on annule TOUT ordre non confirme rempli, AVANT de
        # decider quoi que ce soit. Sinon un ordre limite reste pose sur le
        # carnet, remplit apres notre fenetre de verif, et devient une position
        # fantome que le bot ne suit pas (bug observe). On annule puis on relit
        # une derniere fois pour rattraper la course (jambe remplie juste avant
        # l'annulation).
        if f_up < 1.0:
            await cancel_leg(client, oid_up)
            f_up = await confirm_fill(client, oid_up, target)
        if f_down < 1.0:
            await cancel_leg(client, oid_down)
            f_down = await confirm_fill(client, oid_down, target)
        log.info("[" + mkey + "] fills | UP " + str(round(f_up, 2))
                 + " | DOWN " + str(round(f_down, 2))
                 + " | vise " + str(target))

        both = f_up >= 1.0 and f_down >= 1.0
        if both:
            shares = min(f_up, f_down)
            sum_paid = meas["avg_up"] + meas["avg_down"]
            # si fills inegaux, l'exces d'un cote est une mini-orpheline a revendre
            if abs(f_up - f_down) >= 1.0:
                big, big_id, ref = (f_up, up_id, up_lim) if f_up > f_down \
                    else (f_down, down_id, down_lim)
                await sell_orphan(client, big_id, big - shares, ref)
            return True, shares, sum_paid

        # une seule jambe -> orpheline, revente immediate, perte reelle comptee
        if f_up >= 1.0:
            ok, spx = await sell_orphan(client, up_id, f_up, up_lim)
            perte = (up_lim - spx) * f_up if ok else up_lim * f_up
            daily_pnl -= perte
            log.warning("[" + mkey + "] ORPHELINE UP revendue, perte ~"
                        + str(round(perte, 2)) + "$")
            return False, 0, 0
        if f_down >= 1.0:
            ok, spx = await sell_orphan(client, down_id, f_down, down_lim)
            perte = (down_lim - spx) * f_down if ok else down_lim * f_down
            daily_pnl -= perte
            log.warning("[" + mkey + "] ORPHELINE DOWN revendue, perte ~"
                        + str(round(perte, 2)) + "$")
            return False, 0, 0
        log.info("[" + mkey + "] aucune jambe remplie")
        return False, 0, 0

# ------------------------- Observation -------------------------
def record_obs(mkey, window_ts, meas):
    global obs_seen, obs_executable, obs_hypo_pnl
    obs_seen += 1
    if meas["executable"]:
        obs_executable += 1
        obs_hypo_pnl += meas["hypo_pnl"]
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market": mkey, "window_ts": window_ts,
            "best_sum": round(meas["best_sum"], 4),
            "eff_sum": round(meas["eff_sum"], 4),
            "slippage": round(meas["eff_sum"] - meas["best_sum"], 4),
            "target": meas["target"], "fillable": round(meas["fillable"], 2),
            "executable": meas["executable"],
            "hypo_pnl": round(meas["hypo_pnl"], 3),
        }
        with open(OBS_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.error("record_obs: " + str(e))

def obs_report():
    if obs_seen == 0:
        log.info("[OBS] aucune opportunite vue pour l'instant")
        return
    pct = 100.0 * obs_executable / obs_seen
    log.info("[OBS] ===== {} opportunites vues =====".format(obs_seen))
    log.info("[OBS] reellement executables EN TAILLE : {}/{} ({:.0f}%)"
             .format(obs_executable, obs_seen, pct))
    log.info("[OBS] PnL hypothetique cumule (verrouille) : {:+.2f}$"
             .format(obs_hypo_pnl))
    if obs_seen >= 20 and obs_executable == 0:
        log.info("[OBS] >>> 0 arb survit en taille : l'edge n'existe qu'a "
                 "l'ecran. Ne PAS passer en reel.")
    elif obs_executable > 0:
        log.info("[OBS] >>> certains arbs survivent : regarde le PnL hypo "
                 "et la part executable avant de passer OBSERVE_ONLY=false.")

# ------------------------- Reglement -------------------------
def settle_loop():
    global daily_pnl
    while True:
        try:
            now = int(time.time())
            with arbs_lock:
                copy = list(open_arbs)
            for arb in copy:
                if now >= arb["expiry"] + 30:
                    gain = (1.0 - arb["sum_paid"]) * arb["shares"]
                    daily_pnl += gain
                    log.info("[" + arb["market"] + "] ARB REGLE +"
                             + str(round(gain, 2)) + "$ | PnL jour "
                             + str(round(daily_pnl, 2)) + "$")
                    with arbs_lock:
                        if arb in open_arbs:
                            open_arbs.remove(arb)
        except Exception as e:
            log.error("settle: " + str(e))
        time.sleep(10)

# ------------------------- Boucle principale -------------------------
def run():
    mode = "OBSERVATION (aucun ordre)" if OBSERVE_ONLY else "REEL (argent !)"
    log.info("Bot ARB v2 demarre | MODE=" + mode
             + " | seuil " + str(SUM_MAX) + " | trade " + str(TRADE_USDC) + "$"
             + " | sport " + ("ON" if ENABLE_SPORT else "OFF"))
    if not OBSERVE_ONLY:
        log.warning("*** MODE REEL : verifie d'abord ton rapport [OBS] ***")
        if not PRIVATE_KEY.startswith("0x") or not WALLET.startswith("0x"):
            log.error("Cles manquantes - arret")
            return

    threading.Thread(target=settle_loop, daemon=True).start()
    token_cache = {}
    last_report = 0

    while True:
        try:
            if time.time() - last_report > 600:
                obs_report()
                last_report = time.time()

            if daily_pnl <= -STOP_LOSS:
                log.warning("stop-loss jour atteint - pause")
                time.sleep(300); continue

            with arbs_lock:
                if len(open_arbs) >= MAX_CONCUR:
                    time.sleep(SCAN_INTERVAL); continue

            now = int(time.time())
            for mkey, mcfg in MARKETS.items():
                wsize = mcfg["window"]
                window_ts = now - (now % wsize)
                time_left = (window_ts + wsize) - now
                ckey = mkey + "_" + str(window_ts)
                if ckey in done_windows or time_left < MIN_TIME_LEFT:
                    continue
                if ckey not in token_cache:
                    tk = get_tokens(mcfg["slug"], window_ts)
                    if not tk:
                        continue
                    token_cache[ckey] = tk
                    if len(token_cache) > 30:
                        for k in list(token_cache.keys())[:-30]:
                            del token_cache[k]
                tokens = token_cache[ckey]
                up   = next((t for t in tokens if t["outcome"] == "Up"), None)
                down = next((t for t in tokens if t["outcome"] == "Down"), None)
                if not up or not down:
                    continue

                asks_up   = get_asks(up["token_id"])
                asks_down = get_asks(down["token_id"])
                if not asks_up or not asks_down:
                    continue
                if asks_up[0][0] + asks_down[0][0] > SUM_MAX:
                    continue   # meme le meilleur ask ne passe pas le seuil

                meas = best_executable(asks_up, asks_down, TRADE_USDC, SUM_MAX)
                if meas is None:
                    continue

                log.info("[" + mkey + "] OPPORTUNITE | best_sum "
                         + str(round(meas["best_sum"], 3))
                         + " -> eff_sum " + str(round(meas["eff_sum"], 3))
                         + " (slippage " + str(round(meas["eff_sum"]
                                                     - meas["best_sum"], 3))
                         + ") | dispo " + str(round(meas["fillable"], 1))
                         + " sh | executable: " + str(meas["executable"]))
                record_obs(mkey, window_ts, meas)
                done_windows.add(ckey)

                if OBSERVE_ONLY:
                    continue
                if not meas["executable"]:
                    log.info("[" + mkey + "] non executable en taille - skip")
                    continue
                ok, shares, sum_paid = asyncio.run(
                    execute_real(up["token_id"], down["token_id"], meas, mkey))
                if ok:
                    with arbs_lock:
                        open_arbs.append({
                            "market": mkey, "sum_paid": round(sum_paid, 4),
                            "shares": shares, "expiry": window_ts + wsize,
                        })
        except Exception as e:
            log.error("boucle: " + str(e))
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
