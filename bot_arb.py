"""
BOT ARBITRAGE YES+NO v3
=======================
AVERTISSEMENT HONNETE : un code "sans faille" n'existe pas pour cette
strategie. Deux ordres ne peuvent pas etre atomiques sur Polymarket : entre
la lecture du carnet et l'arrivee de tes ordres, le marche bouge. v3 garantit
que les bugs de la v2 (positions fantomes, fills partiels ignores, orphelines
non confirmees, PnL estime) ne peuvent plus se produire, mais l'orpheline
elle-meme reste POSSIBLE. Elle est maintenant :
  - impossible en partiel (ordres FOK : tout ou rien, jamais d'ordre qui traine)
  - revendue avec CONFIRMATION, en escalier de prix
  - comptee en perte reelle
  - limitee par disjoncteur (MAX_ORPHANS_DAY -> arret du jour)

SDK : officiel py-clob-client  ->  pip install py-clob-client
Avant tout deploiement reel :
  1. laisse OBSERVE_ONLY=true 2-3 jours et lis le rapport [OBS]
  2. si tu passes en reel : ARB_TRADE_USDC=2 pendant 20 trades minimum
  3. verifie sur polymarket.com qu'aucune position/ordre orphelin ne traine

Variables d'env :
  PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS (proxy/funder address)
  OBSERVE_ONLY       = true      <- defaut, ne passe aucun ordre
  ARB_SUM_MAX        = 0.97
  ARB_FEE_BUFFER     = 0.01      <- marge frais/imprevus soustraite au seuil
  ARB_TRADE_USDC     = 10
  ARB_DEPTH_MULT     = 2.0       <- profondeur requise = mult x taille visee
  ARB_MAX_CONCURRENT = 3
  ARB_MIN_TIME_LEFT  = 60
  ARB_SCAN_INTERVAL  = 1
  ARB_STOP_LOSS_USDC = 10        <- par JOUR (reset a minuit UTC)
  ARB_MAX_ORPHANS_DAY= 2         <- disjoncteur orphelines
  OBS_FILE           = arb_observe.jsonl  (mets un volume persistant !)
  SIGNATURE_TYPE     = 1         <- 1 = email/proxy wallet, 2 = browser wallet
"""

import os, sys, time, json, math, logging, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", stream=sys.stdout)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
log = logging.getLogger("arb")

# ------------------------- Config -------------------------
PRIVATE_KEY   = os.environ.get("PRIVATE_KEY", "")
WALLET        = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
SIGNATURE_TYPE= int(os.getenv("SIGNATURE_TYPE", "1"))

OBSERVE_ONLY  = os.getenv("OBSERVE_ONLY", "true").lower() != "false"
SUM_MAX       = float(os.getenv("ARB_SUM_MAX",         "0.97"))
FEE_BUFFER    = float(os.getenv("ARB_FEE_BUFFER",      "0.01"))
TRADE_USDC    = float(os.getenv("ARB_TRADE_USDC",      "10"))
DEPTH_MULT    = float(os.getenv("ARB_DEPTH_MULT",      "2.0"))
MAX_CONCUR    = int(os.getenv("ARB_MAX_CONCURRENT",    "3"))
MIN_TIME_LEFT = int(os.getenv("ARB_MIN_TIME_LEFT",     "60"))
SCAN_INTERVAL = float(os.getenv("ARB_SCAN_INTERVAL",   "1"))
STOP_LOSS     = float(os.getenv("ARB_STOP_LOSS_USDC",  "10"))
MAX_ORPHANS   = int(os.getenv("ARB_MAX_ORPHANS_DAY",   "2"))
OBS_FILE      = os.getenv("OBS_FILE", "arb_observe.jsonl")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
CHAIN_ID  = 137

# Le seuil effectif inclut le buffer de frais : on n'entre que si
# eff_sum <= SUM_MAX - FEE_BUFFER
EFF_MAX = SUM_MAX - FEE_BUFFER

MARKETS = {
    "BTC5": {"slug": "btc-updown-5m-", "window": 300},
}

SESSION = requests.Session()
POOL = ThreadPoolExecutor(max_workers=4)

# ------------------------- Etat journalier -------------------------
class DayState:
    """PnL et compteurs remis a zero chaque jour UTC. Corrige le bug v2
    ou le 'stop-loss jour' etait en fait cumule depuis le demarrage."""
    def __init__(self):
        self.lock = threading.Lock()
        self.day = None
        self.pnl = 0.0
        self.orphans = 0
        self.halted = False          # HALT dur : orpheline invendable
        self._roll()

    def _roll(self):
        today = datetime.now(timezone.utc).date()
        if self.day != today:
            self.day = today
            self.pnl = 0.0
            self.orphans = 0
            # NB : self.halted N'EST PAS reset automatiquement.
            # Une orpheline invendable exige une intervention manuelle.

    def add_pnl(self, x):
        with self.lock:
            self._roll(); self.pnl += x; return self.pnl

    def add_orphan(self):
        with self.lock:
            self._roll(); self.orphans += 1; return self.orphans

    def trading_allowed(self):
        with self.lock:
            self._roll()
            if self.halted:
                return False, "HALT : position orpheline non revendue, verifie ton compte"
            if self.pnl <= -STOP_LOSS:
                return False, "stop-loss du jour atteint"
            if self.orphans >= MAX_ORPHANS:
                return False, "disjoncteur orphelines du jour atteint"
            return True, ""

STATE = DayState()

open_arbs  = []
arbs_lock  = threading.Lock()
done_windows = {}        # ckey -> window_end (pour purge, corrige fuite memoire v2)
obs_seen = 0
obs_executable = 0
obs_hypo_pnl = 0.0

# ------------------------- Carnet -------------------------
def get_book(token_id):
    """Retourne (asks tries croissant, bids tries decroissant) ou ([], [])."""
    try:
        r = SESSION.get(CLOB_API + "/book",
                        params={"token_id": token_id}, timeout=5)
        if not r.ok:
            return [], []
        j = r.json()
        asks = sorted(((float(a["price"]), float(a["size"]))
                       for a in j.get("asks", [])), key=lambda x: x[0])
        bids = sorted(((float(b["price"]), float(b["size"]))
                       for b in j.get("bids", [])), key=lambda x: -x[0])
        return asks, bids
    except Exception:
        return [], []

def get_books_parallel(token_up, token_down):
    """Lecture QUASI simultanee des deux carnets (corrige la desynchro v2
    qui fabriquait de fausses opportunites)."""
    f1 = POOL.submit(get_book, token_up)
    f2 = POOL.submit(get_book, token_down)
    return f1.result(), f2.result()

def walk_book(levels, shares_wanted):
    """Cout reel pour 'shares_wanted' en consommant le carnet niveau par
    niveau. Retourne (prix_moyen, shares_disponibles, pire_prix)."""
    if shares_wanted <= 0 or not levels:
        return None, 0.0, None
    remaining, cost, filled = shares_wanted, 0.0, 0.0
    worst = levels[0][0]
    for px, size in levels:
        take = min(remaining, size)
        cost += take * px
        filled += take
        remaining -= take
        worst = px
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return None, 0.0, None
    return cost / filled, filled, worst

def measure(asks_up, asks_down, bids_up, bids_down, capital):
    """Mesure l'arb en taille reelle, avec marge de profondeur et frais.
    Ne place rien. Retourne dict ou None."""
    if not asks_up or not asks_down:
        return None
    best_sum = asks_up[0][0] + asks_down[0][0]
    target = math.floor(capital / max(best_sum, 1e-6) * 100) / 100
    if target < 5:
        target = 5.0   # Polymarket : taille minimum d'ordre ~5 shares
    # profondeur exigee : DEPTH_MULT x target sur CHAQUE cote,
    # pour survivre a la desynchro entre mesure et execution
    need = target * DEPTH_MULT
    avg_up,   fill_up,   worst_up   = walk_book(asks_up, need)
    avg_down, fill_down, worst_down = walk_book(asks_down, need)
    if avg_up is None or avg_down is None:
        return None
    # cout effectif calcule sur la taille visee (pas la profondeur)
    au, fu, wu = walk_book(asks_up, target)
    ad, fd, wd = walk_book(asks_down, target)
    if au is None or ad is None:
        return None
    eff_sum = au + ad
    deep_enough = fill_up >= need and fill_down >= need
    # liquidite de SORTIE : sans bid des deux cotes, une orpheline
    # serait invendable -> on n'entre pas
    exit_ok = bool(bids_up) and bool(bids_down)
    spread_up   = (asks_up[0][0] - bids_up[0][0]) if bids_up else 1.0
    spread_down = (asks_down[0][0] - bids_down[0][0]) if bids_down else 1.0
    executable = (eff_sum <= EFF_MAX and deep_enough and exit_ok
                  and fu >= target and fd >= target)
    hypo = (1.0 - eff_sum - FEE_BUFFER) * target if executable else 0.0
    return {
        "best_sum": best_sum, "eff_sum": eff_sum, "target": target,
        "avg_up": au, "avg_down": ad, "worst_up": wu, "worst_down": wd,
        "fill_up": fu, "fill_down": fd, "deep_enough": deep_enough,
        "exit_ok": exit_ok, "spread_up": spread_up, "spread_down": spread_down,
        "executable": executable, "hypo_pnl": max(0.0, hypo),
    }

# ------------------------- Marches -------------------------
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

# ------------------------- Client CLOB (cree UNE fois) -------------------------
CLIENT = None

def init_client():
    """Client authentifie une seule fois au demarrage (corrige les secondes
    de latence par trade de la v2)."""
    global CLIENT
    from py_clob_client.client import ClobClient
    CLIENT = ClobClient(CLOB_API, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                        signature_type=SIGNATURE_TYPE, funder=WALLET)
    CLIENT.set_api_creds(CLIENT.create_or_derive_api_creds())
    log.info("Client CLOB authentifie")

def order_filled_size(order_id):
    """Lit size_matched d'un ordre. 0.0 si introuvable/erreur."""
    try:
        od = CLIENT.get_order(order_id)
        if isinstance(od, dict):
            return float(od.get("size_matched", 0) or 0)
    except Exception:
        pass
    return 0.0

def place_fok_buy(token_id, price, size):
    """Ordre FOK : rempli EN ENTIER immediatement ou annule par l'exchange.
    Corrige a la racine : fills partiels, ordres qui trainent, cancel-race.
    Retourne (rempli: bool, order_id)."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    try:
        args = OrderArgs(price=round(price, 3), size=round(size, 2),
                         side=BUY, token_id=token_id)
        signed = CLIENT.create_order(args)
        resp = CLIENT.post_order(signed, OrderType.FOK)
        oid = None
        ok = False
        if isinstance(resp, dict):
            oid = resp.get("orderID") or resp.get("orderId")
            ok = bool(resp.get("success")) and \
                 str(resp.get("status", "")).lower() == "matched"
        # verification best-effort par l'API (defense en profondeur)
        if oid and not ok:
            ok = order_filled_size(oid) >= size - 0.01
        return ok, oid
    except Exception as e:
        log.error("place_fok_buy: " + str(e))
        return False, None

def sell_confirmed(token_id, shares, floor_px=0.01, tries=5):
    """Revend une position avec CONFIRMATION, en escalier : on tape le
    meilleur bid, on verifie le fill, on descend le prix si necessaire.
    Corrige la v2 qui posait un ordre limite et 'esperait'.
    Retourne (shares_vendues, produit_usdc)."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    remaining = shares
    proceeds = 0.0
    for i in range(tries):
        if remaining < 0.01:
            break
        _, bids = get_book(token_id)
        if not bids:
            time.sleep(1.0)
            continue
        # a chaque essai on tape un peu plus bas pour forcer le fill
        px = max(floor_px, round(bids[0][0] - 0.01 * i, 3))
        try:
            args = OrderArgs(price=px, size=round(remaining, 2),
                             side=SELL, token_id=token_id)
            signed = CLIENT.create_order(args)
            resp = CLIENT.post_order(signed, OrderType.FAK)  # partiel OK en sortie
            oid = None
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("orderId")
            time.sleep(0.7)
            sold = order_filled_size(oid) if oid else 0.0
            if sold <= 0 and isinstance(resp, dict) and \
               str(resp.get("status", "")).lower() == "matched":
                sold = remaining   # matched sans lecture possible : FAK complet
            proceeds += sold * px       # prix limite = borne basse du produit
            remaining -= sold
        except Exception as e:
            log.error("sell_confirmed: " + str(e))
            time.sleep(1.0)
    return shares - remaining, proceeds

# ------------------------- Execution reelle -------------------------
def execute_real(up_id, down_id, meas, mkey):
    """Deux FOK en parallele. Cas possibles APRES (grace au FOK, il n'y a
    plus jamais de partiel ni d'ordre residuel) :
      2 remplis  -> arb verrouille
      1 rempli   -> orpheline : revente confirmee + compteur + disjoncteur
      0 rempli   -> rien perdu (le marche a bouge avant nous)
    """
    target = meas["target"]
    up_lim, down_lim = meas["worst_up"], meas["worst_down"]

    f1 = POOL.submit(place_fok_buy, up_id, up_lim, target)
    f2 = POOL.submit(place_fok_buy, down_id, down_lim, target)
    ok_up, _ = f1.result()
    ok_down, _ = f2.result()
    log.info("[{}] FOK -> UP {} | DOWN {}".format(
        mkey, "REMPLI" if ok_up else "annule", "REMPLI" if ok_down else "annule"))

    if ok_up and ok_down:
        # borne haute honnete du cout : les prix limites (le reel est <=)
        sum_paid = up_lim + down_lim
        return True, target, sum_paid

    if not ok_up and not ok_down:
        return False, 0, 0

    # ---- ORPHELINE ----
    orphan_id, paid_px, name = (up_id, up_lim, "UP") if ok_up \
        else (down_id, down_lim, "DOWN")
    sold, proceeds = sell_confirmed(orphan_id, target)
    if sold >= target - 0.01:
        loss = paid_px * target - proceeds
        STATE.add_pnl(-loss)
        n = STATE.add_orphan()
        log.warning("[{}] ORPHELINE {} revendue | perte {:.2f}$ | orphelines "
                    "aujourd'hui: {}/{}".format(mkey, name, loss, n, MAX_ORPHANS))
    else:
        # invendable : HALT dur, intervention manuelle obligatoire.
        # On NE compte PAS une perte inventee (bug v2), on bloque tout.
        with STATE.lock:
            STATE.halted = True
        log.error("[{}] ORPHELINE {} NON REVENDUE ({:.2f}/{:.2f} sh) -> HALT. "
                  "Va revendre manuellement sur polymarket.com puis redemarre."
                  .format(mkey, name, sold, target))
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
            "target": meas["target"],
            "deep_enough": meas["deep_enough"], "exit_ok": meas["exit_ok"],
            "spread_up": round(meas["spread_up"], 3),
            "spread_down": round(meas["spread_down"], 3),
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
    log.info("[OBS] executables en taille (seuil {:.2f} frais inclus) : "
             "{}/{} ({:.0f}%)".format(EFF_MAX, obs_executable, obs_seen, pct))
    log.info("[OBS] PnL hypothetique cumule : {:+.2f}$".format(obs_hypo_pnl))
    log.info("[OBS] RAPPEL : ce PnL suppose que les DEUX FOK passent. En reel "
             "une partie echouera (marche plus rapide) et il y aura des "
             "orphelines. Considere ce chiffre comme un PLAFOND, pas une attente.")
    if obs_seen >= 20 and obs_executable == 0:
        log.info("[OBS] >>> 0 arb ne survit en taille : n'active PAS le reel.")

# ------------------------- Reglement -------------------------
def settle_loop():
    """Comptabilise le gain d'un arb complet a l'expiration.
    LIMITES ASSUMEES (a savoir) :
      - suppose que le marche se resout normalement a 1.00 ;
      - le redeem on-chain des positions gagnantes n'est PAS automatise ici,
        Polymarket le fait generalement apres resolution, verifie ton solde."""
    while True:
        try:
            now = int(time.time())
            with arbs_lock:
                copy = list(open_arbs)
            for arb in copy:
                if now >= arb["expiry"] + 30:
                    gain = (1.0 - arb["sum_paid"]) * arb["shares"]
                    pnl = STATE.add_pnl(gain)
                    log.info("[{}] ARB REGLE +{:.2f}$ (borne basse) | PnL jour "
                             "{:+.2f}$".format(arb["market"], gain, pnl))
                    with arbs_lock:
                        if arb in open_arbs:
                            open_arbs.remove(arb)
        except Exception as e:
            log.error("settle: " + str(e))
        time.sleep(10)

# ------------------------- Boucle principale -------------------------
def purge(now):
    for k in [k for k, end in done_windows.items() if now > end + 120]:
        del done_windows[k]

def run():
    mode = "OBSERVATION (aucun ordre)" if OBSERVE_ONLY else "REEL (argent !)"
    log.info("Bot ARB v3 | MODE={} | seuil {:.2f} (- {:.2f} frais = {:.2f}) | "
             "trade {}$ | depth x{} | max orphelines/j {}"
             .format(mode, SUM_MAX, FEE_BUFFER, EFF_MAX, TRADE_USDC,
                     DEPTH_MULT, MAX_ORPHANS))

    if not OBSERVE_ONLY:
        if not PRIVATE_KEY.startswith("0x") or not WALLET.startswith("0x"):
            log.error("Cles manquantes - arret")
            return
        try:
            init_client()
        except Exception as e:
            log.error("init client impossible : " + str(e))
            return
        log.warning("*** MODE REEL : commence avec ARB_TRADE_USDC=2 ***")

    threading.Thread(target=settle_loop, daemon=True).start()
    token_cache = {}
    last_report = 0.0

    while True:
        try:
            now_f = time.time()
            if now_f - last_report > 600:
                obs_report()
                last_report = now_f

            allowed, why = STATE.trading_allowed()
            if not OBSERVE_ONLY and not allowed:
                log.warning("trading suspendu : " + why)
                time.sleep(60)
                continue

            with arbs_lock:
                if len(open_arbs) >= MAX_CONCUR:
                    time.sleep(SCAN_INTERVAL)
                    continue

            now = int(now_f)
            purge(now)
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

                (asks_up, bids_up), (asks_down, bids_down) = \
                    get_books_parallel(up["token_id"], down["token_id"])
                if not asks_up or not asks_down:
                    continue
                if asks_up[0][0] + asks_down[0][0] > SUM_MAX:
                    continue

                meas = measure(asks_up, asks_down, bids_up, bids_down,
                               TRADE_USDC)
                if meas is None:
                    continue

                log.info("[{}] OPPORTUNITE | best {:.3f} -> eff {:.3f} | "
                         "depth {} | exit {} | executable: {}".format(
                             mkey, meas["best_sum"], meas["eff_sum"],
                             "OK" if meas["deep_enough"] else "insuffisante",
                             "OK" if meas["exit_ok"] else "NON",
                             meas["executable"]))
                record_obs(mkey, window_ts, meas)

                if OBSERVE_ONLY:
                    # en observation on ne 'consomme' PAS la fenetre :
                    # on echantillonne toutes les opportunites (corrige le
                    # biais v2 d'une seule mesure par fenetre de 5 min)
                    time.sleep(2)
                    continue
                if not meas["executable"]:
                    continue

                done_windows[ckey] = window_ts + wsize
                ok, shares, sum_paid = execute_real(
                    up["token_id"], down["token_id"], meas, mkey)
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
