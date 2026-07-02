"""
BOT ARBITRAGE YES+NO v3.1 - adapte au SDK polymarket-client 0.1.0b11
=====================================================================
API VERIFIEE par introspection du package (pas de devinette) :
  - AsyncSecureClient.create(private_key=..., wallet=...)
  - place_market_order(token_id=, side="BUY"/"SELL", shares=, max_price=/
      min_price=, order_type="FOK"/"FAK") -> AcceptedOrder | RejectedOrder
  - AcceptedOrder : ok=True, status "matched"/"live"/"delayed",
      making_amount / taking_amount  (montants REELS de l'execution)
  - RejectedOrder : ok=False, code (ex "fok_not_filled") -> rien sur le carnet
  - get_order(order_id=) -> OpenOrder(.size_matched, .status)
  - get_order_book(token_id=) -> OrderBook(.asks, .bids, .min_order_size,
      .tick_size) ; niveaux avec .price et .size

CE QUI CHANGE vs v2 :
  1. FOK natif : tout ou rien, jamais de fill partiel, jamais d'ordre qui
     traine, plus de course cancel/fill.
  2. COUT REEL : pour un BUY, making_amount = USDC payes, taking_amount =
     shares recues. Le PnL est compte sur ces montants, pas sur une
     estimation pre-trade.
  3. Client cree UNE fois. Carnets lus en parallele (asyncio.gather).
  4. Orpheline : revente FAK confirmee en escalier ; si invendable -> HALT.
  5. PnL et disjoncteurs vraiment journaliers. Buffer frais. Marge de
     profondeur. min_order_size / tick_size respectes.

RESTE IRREDUCTIBLE : entre la lecture du carnet et l'arrivee des ordres,
le marche bouge. Le FOK protege le capital, il ne garantit pas que les
2 jambes passent. L'orpheline reste possible ; elle est geree et limitee.

requirements.txt :  requests + polymarket-client   (PAS py-clob-client)

Env :
  PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS
  OBSERVE_ONLY=true (defaut)  ARB_SUM_MAX=0.97  ARB_FEE_BUFFER=0.01
  ARB_TRADE_USDC=10  ARB_DEPTH_MULT=2.0  ARB_MAX_CONCURRENT=3
  ARB_MIN_TIME_LEFT=60  ARB_SCAN_INTERVAL=1  ARB_STOP_LOSS_USDC=10
  ARB_MAX_ORPHANS_DAY=2  OBS_FILE=arb_observe.jsonl (volume persistant !)
"""

import os, sys, time, json, math, logging, asyncio
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

OBSERVE_ONLY  = os.getenv("OBSERVE_ONLY", "true").lower() != "false"
SUM_MAX       = float(os.getenv("ARB_SUM_MAX",        "0.97"))
FEE_BUFFER    = float(os.getenv("ARB_FEE_BUFFER",     "0.01"))
TRADE_USDC    = float(os.getenv("ARB_TRADE_USDC",     "10"))
DEPTH_MULT    = float(os.getenv("ARB_DEPTH_MULT",     "2.0"))
MAX_CONCUR    = int(os.getenv("ARB_MAX_CONCURRENT",   "3"))
MIN_TIME_LEFT = int(os.getenv("ARB_MIN_TIME_LEFT",    "60"))
SCAN_INTERVAL = float(os.getenv("ARB_SCAN_INTERVAL",  "1"))
STOP_LOSS     = float(os.getenv("ARB_STOP_LOSS_USDC", "10"))
MAX_ORPHANS   = int(os.getenv("ARB_MAX_ORPHANS_DAY",  "2"))
OBS_FILE      = os.getenv("OBS_FILE", "arb_observe.jsonl")

EFF_MAX = SUM_MAX - FEE_BUFFER     # seuil effectif frais inclus
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

MARKETS = {
    "BTC5": {"slug": "btc-updown-5m-", "window": 300},
}

SESSION = requests.Session()

# ------------------------- Etat journalier -------------------------
class DayState:
    """Reset chaque jour UTC (corrige le stop-loss 'jour' cumule de la v2).
    halted N'EST PAS reset : une orpheline invendable = intervention manuelle."""
    def __init__(self):
        self.day = None; self.pnl = 0.0; self.orphans = 0; self.halted = False
        self._roll()
    def _roll(self):
        t = datetime.now(timezone.utc).date()
        if self.day != t:
            self.day = t; self.pnl = 0.0; self.orphans = 0
    def add_pnl(self, x):
        self._roll(); self.pnl += x; return self.pnl
    def add_orphan(self):
        self._roll(); self.orphans += 1; return self.orphans
    def trading_allowed(self):
        self._roll()
        if self.halted:
            return False, "HALT : orpheline non revendue, verifie ton compte"
        if self.pnl <= -STOP_LOSS:
            return False, "stop-loss du jour atteint"
        if self.orphans >= MAX_ORPHANS:
            return False, "disjoncteur orphelines atteint"
        return True, ""

STATE = DayState()
open_arbs = []
done_windows = {}
obs_seen = 0; obs_executable = 0; obs_hypo_pnl = 0.0

# ------------------------- Carnet -------------------------
def _levels(levels, reverse=False):
    out = [(float(l.price), float(l.size)) for l in levels]
    out.sort(key=lambda x: -x[0] if reverse else x[0])
    return out

async def get_books(client, tid_up, tid_down):
    """Lecture des 2 carnets en PARALLELE via le SDK (corrige la desynchro
    v2 qui fabriquait de fausses opportunites). Retourne (book_up, book_down)
    ou (None, None)."""
    try:
        b1, b2 = await asyncio.gather(
            client.get_order_book(token_id=tid_up),
            client.get_order_book(token_id=tid_down))
        return b1, b2
    except Exception as e:
        log.debug("get_books: " + str(e))
        return None, None

def get_books_public(tid_up, tid_down):
    """Version REST publique pour le mode observation (pas besoin de cles)."""
    def one(tid):
        try:
            r = SESSION.get(CLOB_API + "/book", params={"token_id": tid},
                            timeout=5)
            if not r.ok:
                return None
            j = r.json()
            asks = sorted(((float(a["price"]), float(a["size"]))
                           for a in j.get("asks", [])), key=lambda x: x[0])
            bids = sorted(((float(b["price"]), float(b["size"]))
                           for b in j.get("bids", [])), key=lambda x: -x[0])
            return {"asks": asks, "bids": bids, "min_size": 5.0}
        except Exception:
            return None
    return one(tid_up), one(tid_down)

def walk_book(levels, shares_wanted):
    if shares_wanted <= 0 or not levels:
        return None, 0.0, None
    remaining, cost, filled = shares_wanted, 0.0, 0.0
    worst = levels[0][0]
    for px, size in levels:
        take = min(remaining, size)
        cost += take * px; filled += take; remaining -= take; worst = px
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return None, 0.0, None
    return cost / filled, filled, worst

def measure(asks_up, asks_down, bids_up, bids_down, capital, min_size):
    if not asks_up or not asks_down:
        return None
    best_sum = asks_up[0][0] + asks_down[0][0]
    target = math.floor(capital / max(best_sum, 1e-6) * 100) / 100
    target = max(target, float(min_size))     # respecte min_order_size
    need = target * DEPTH_MULT                # marge anti-desynchro
    _, deep_up, _ = walk_book(asks_up, need)
    _, deep_dn, _ = walk_book(asks_down, need)
    au, fu, wu = walk_book(asks_up, target)
    ad, fd, wd = walk_book(asks_down, target)
    if au is None or ad is None:
        return None
    eff_sum = au + ad
    deep_enough = deep_up >= need and deep_dn >= need
    exit_ok = bool(bids_up) and bool(bids_down)
    executable = (eff_sum <= EFF_MAX and deep_enough and exit_ok
                  and fu >= target and fd >= target)
    hypo = (1.0 - eff_sum - FEE_BUFFER) * target if executable else 0.0
    return {"best_sum": best_sum, "eff_sum": eff_sum, "target": target,
            "worst_up": wu, "worst_down": wd, "deep_enough": deep_enough,
            "exit_ok": exit_ok, "executable": executable,
            "hypo_pnl": max(0.0, hypo)}

# ------------------------- Marches -------------------------
def get_tokens(slug_prefix, window_ts):
    try:
        slug = slug_prefix + str(window_ts)
        r = SESSION.get(GAMMA_API + "/markets", params={"slug": slug},
                        timeout=8)
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

# ------------------------- Execution -------------------------
async def fok_buy(client, token_id, shares, max_price):
    """FOK d'achat. Retourne (shares_recues, usdc_payes) REELS via
    taking_amount / making_amount. (0,0) si non rempli - et dans ce cas
    RIEN ne reste sur le carnet, c'est la garantie du FOK."""
    try:
        resp = await client.place_market_order(
            token_id=token_id, side="BUY", shares=round(shares, 2),
            max_price=round(max_price, 3), order_type="FOK")
        if getattr(resp, "ok", False):
            if resp.status == "delayed":
                # execution differee : on verifie via get_order
                await asyncio.sleep(2.0)
                try:
                    od = await client.get_order(order_id=resp.order_id)
                    got = float(od.size_matched)
                    return (got, got * max_price) if got > 0 else (0.0, 0.0)
                except Exception:
                    return 0.0, 0.0
            # BUY : making = USDC donnes, taking = shares recues
            return float(resp.taking_amount), float(resp.making_amount)
        else:
            code = getattr(resp, "code", "?")
            if code != "fok_not_filled":
                log.warning("FOK rejete: " + str(code) + " "
                            + str(getattr(resp, "message", "")))
            return 0.0, 0.0
    except Exception as e:
        log.error("fok_buy: " + str(e))
        return 0.0, 0.0

async def sell_confirmed(client, token_id, shares, tries=5):
    """Revente FAK confirmee, en escalier de prix plancher descendant.
    Retourne (shares_vendues, usdc_recus) REELS."""
    remaining = shares; got_usdc = 0.0
    for i in range(tries):
        if remaining < 0.01:
            break
        try:
            book = await client.get_order_book(token_id=token_id)
            bids = _levels(book.bids, reverse=True)
            if not bids:
                await asyncio.sleep(1.0); continue
            floor = max(0.01, round(bids[0][0] - 0.01 * (i + 1), 3))
            resp = await client.place_market_order(
                token_id=token_id, side="SELL", shares=round(remaining, 2),
                min_price=floor, order_type="FAK")
            if getattr(resp, "ok", False):
                # SELL : making = shares donnees, taking = USDC recus
                sold = float(resp.making_amount)
                got_usdc += float(resp.taking_amount)
                remaining -= sold
        except Exception as e:
            log.error("sell_confirmed: " + str(e))
            await asyncio.sleep(1.0)
    return shares - remaining, got_usdc

async def execute_real(client, up_id, down_id, meas, mkey):
    """Deux FOK simultanes. Grace au FOK : jamais de partiel, jamais
    d'ordre residuel. Cas restants : 2 fills (arb), 1 fill (orpheline
    geree), 0 fill (rien perdu)."""
    target = meas["target"]
    (sh_up, usd_up), (sh_dn, usd_dn) = await asyncio.gather(
        fok_buy(client, up_id, target, meas["worst_up"]),
        fok_buy(client, down_id, target, meas["worst_down"]))
    log.info("[{}] FOK -> UP {:.2f}sh/{:.2f}$ | DOWN {:.2f}sh/{:.2f}$"
             .format(mkey, sh_up, usd_up, sh_dn, usd_dn))

    if sh_up > 0 and sh_dn > 0:
        shares = min(sh_up, sh_dn)
        cost = usd_up + usd_dn                     # cout REEL, pas estime
        return True, shares, cost

    if sh_up == 0 and sh_dn == 0:
        return False, 0, 0

    # ---- ORPHELINE ----
    oid, sh, paid, name = (up_id, sh_up, usd_up, "UP") if sh_up > 0 \
        else (down_id, sh_dn, usd_dn, "DOWN")
    sold, got = await sell_confirmed(client, oid, sh)
    if sold >= sh - 0.01:
        loss = paid - got                          # perte REELLE
        STATE.add_pnl(-loss)
        n = STATE.add_orphan()
        log.warning("[{}] ORPHELINE {} revendue | perte {:.2f}$ | "
                    "orphelines: {}/{}".format(mkey, name, loss, n,
                                               MAX_ORPHANS))
    else:
        STATE.halted = True
        log.error("[{}] ORPHELINE {} NON REVENDUE ({:.2f}/{:.2f} sh) -> "
                  "HALT. Revends manuellement sur polymarket.com puis "
                  "redemarre.".format(mkey, name, sold, sh))
    return False, 0, 0

# ------------------------- Observation -------------------------
def record_obs(mkey, window_ts, meas):
    global obs_seen, obs_executable, obs_hypo_pnl
    obs_seen += 1
    if meas["executable"]:
        obs_executable += 1; obs_hypo_pnl += meas["hypo_pnl"]
    try:
        row = {"ts": datetime.now(timezone.utc).isoformat(),
               "market": mkey, "window_ts": window_ts,
               "best_sum": round(meas["best_sum"], 4),
               "eff_sum": round(meas["eff_sum"], 4),
               "target": meas["target"],
               "deep_enough": meas["deep_enough"],
               "exit_ok": meas["exit_ok"],
               "executable": meas["executable"],
               "hypo_pnl": round(meas["hypo_pnl"], 3)}
        with open(OBS_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.error("record_obs: " + str(e))

def obs_report():
    if obs_seen == 0:
        log.info("[OBS] aucune opportunite vue pour l'instant"); return
    pct = 100.0 * obs_executable / obs_seen
    log.info("[OBS] ===== {} vues | executables {}/{} ({:.0f}%) | PnL hypo "
             "{:+.2f}$ =====".format(obs_seen, obs_executable, obs_seen,
                                     pct, obs_hypo_pnl))
    log.info("[OBS] Ce PnL suppose que les DEUX FOK passent : c'est un "
             "PLAFOND. En reel il y aura des echecs et des orphelines.")
    if obs_seen >= 20 and obs_executable == 0:
        log.info("[OBS] >>> 0 arb ne survit en taille : n'active PAS le reel.")

# ------------------------- Reglement -------------------------
async def settle_loop():
    """Gain = shares*1.00 - cout reel, a l'expiration.
    NB : le redeem on-chain n'est pas automatise (le SDK a
    client.redeem_positions si tu veux l'ajouter plus tard) ;
    suppose une resolution normale du marche."""
    while True:
        try:
            now = int(time.time())
            for arb in list(open_arbs):
                if now >= arb["expiry"] + 30:
                    gain = arb["shares"] * 1.0 - arb["cost"]
                    pnl = STATE.add_pnl(gain)
                    log.info("[{}] ARB REGLE {:+.2f}$ | PnL jour {:+.2f}$"
                             .format(arb["market"], gain, pnl))
                    if arb in open_arbs:
                        open_arbs.remove(arb)
        except Exception as e:
            log.error("settle: " + str(e))
        await asyncio.sleep(10)

# ------------------------- Boucle principale -------------------------
async def main():
    mode = "OBSERVATION (aucun ordre)" if OBSERVE_ONLY else "REEL (argent !)"
    log.info("Bot ARB v3.1 (SDK polymarket-client) | MODE={} | seuil {:.2f} "
             "(-{:.2f} frais = {:.2f}) | trade {}$ | depth x{} | "
             "max orphelines/j {}".format(mode, SUM_MAX, FEE_BUFFER, EFF_MAX,
                                          TRADE_USDC, DEPTH_MULT, MAX_ORPHANS))
    client = None
    if not OBSERVE_ONLY:
        if not PRIVATE_KEY.startswith("0x") or not WALLET.startswith("0x"):
            log.error("Cles manquantes - arret"); return
        from polymarket import AsyncSecureClient
        client = await AsyncSecureClient.create(
            private_key=PRIVATE_KEY, wallet=WALLET)
        log.info("Client authentifie (cree une seule fois)")
        log.warning("*** MODE REEL : commence avec ARB_TRADE_USDC=2 ***")

    asyncio.get_event_loop().create_task(settle_loop())
    token_cache = {}
    last_report = 0.0

    while True:
        try:
            now_f = time.time()
            if now_f - last_report > 600:
                obs_report(); last_report = now_f

            allowed, why = STATE.trading_allowed()
            if not OBSERVE_ONLY and not allowed:
                log.warning("trading suspendu : " + why)
                await asyncio.sleep(60); continue
            if len(open_arbs) >= MAX_CONCUR:
                await asyncio.sleep(SCAN_INTERVAL); continue

            now = int(now_f)
            for k in [k for k, e in done_windows.items() if now > e + 120]:
                del done_windows[k]

            for mkey, mcfg in MARKETS.items():
                wsize = mcfg["window"]
                window_ts = now - (now % wsize)
                time_left = (window_ts + wsize) - now
                ckey = mkey + "_" + str(window_ts)
                if ckey in done_windows or time_left < MIN_TIME_LEFT:
                    continue
                if ckey not in token_cache:
                    tk = await asyncio.to_thread(get_tokens, mcfg["slug"],
                                                 window_ts)
                    if not tk:
                        continue
                    token_cache[ckey] = tk
                    if len(token_cache) > 30:
                        for k in list(token_cache.keys())[:-30]:
                            del token_cache[k]
                tokens = token_cache[ckey]
                up = next((t for t in tokens if t["outcome"] == "Up"), None)
                dn = next((t for t in tokens if t["outcome"] == "Down"), None)
                if not up or not dn:
                    continue

                # --- carnets ---
                if client is not None:
                    b_up, b_dn = await get_books(client, up["token_id"],
                                                 dn["token_id"])
                    if b_up is None or b_dn is None:
                        continue
                    asks_up = _levels(b_up.asks)
                    asks_dn = _levels(b_dn.asks)
                    bids_up = _levels(b_up.bids, reverse=True)
                    bids_dn = _levels(b_dn.bids, reverse=True)
                    min_size = max(float(b_up.min_order_size),
                                   float(b_dn.min_order_size))
                else:
                    r_up, r_dn = await asyncio.to_thread(
                        get_books_public, up["token_id"], dn["token_id"])
                    if not r_up or not r_dn:
                        continue
                    asks_up, bids_up = r_up["asks"], r_up["bids"]
                    asks_dn, bids_dn = r_dn["asks"], r_dn["bids"]
                    min_size = 5.0

                if not asks_up or not asks_dn:
                    continue
                if asks_up[0][0] + asks_dn[0][0] > SUM_MAX:
                    continue

                meas = measure(asks_up, asks_dn, bids_up, bids_dn,
                               TRADE_USDC, min_size)
                if meas is None:
                    continue
                log.info("[{}] OPPORTUNITE | best {:.3f} -> eff {:.3f} | "
                         "depth {} | exit {} | executable: {}".format(
                             mkey, meas["best_sum"], meas["eff_sum"],
                             "OK" if meas["deep_enough"] else "KO",
                             "OK" if meas["exit_ok"] else "KO",
                             meas["executable"]))
                record_obs(mkey, window_ts, meas)

                if OBSERVE_ONLY:
                    await asyncio.sleep(2)    # echantillonne toute la fenetre
                    continue
                if not meas["executable"]:
                    continue

                done_windows[ckey] = window_ts + wsize
                ok, shares, cost = await execute_real(
                    client, up["token_id"], dn["token_id"], meas, mkey)
                if ok:
                    open_arbs.append({"market": mkey, "cost": round(cost, 4),
                                      "shares": shares,
                                      "expiry": window_ts + wsize})
        except Exception as e:
            log.error("boucle: " + str(e))
        await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
