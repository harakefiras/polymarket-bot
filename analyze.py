import json, os
from datetime import datetime

TRADES_FILE = "/app/trades_history.json"

def analyze():
    if not os.path.exists(TRADES_FILE):
        print("Pas de donnees disponibles")
        return

    with open(TRADES_FILE, "r") as f:
        trades = json.load(f)

    if not trades:
        print("Fichier vide")
        return

    print("=" * 50)
    print("ANALYSE DES TRADES - " + str(len(trades)) + " trades")
    print("=" * 50)

    wins = [t for t in trades if t.get("result") == "win"]
    losses = [t for t in trades if t.get("result") == "loss"]
    win_rate = len(wins) / len(trades) * 100

    print("\n--- GLOBAL ---")
    print("Gains    : " + str(len(wins)))
    print("Pertes   : " + str(len(losses)))
    print("Win rate : " + str(round(win_rate, 1)) + "%")
    print("PnL total: " + str(round(sum(t.get("pnl", 0) for t in trades), 2)) + " USDC")

    print("\n--- PAR ECART BTC (gap) ---")
    ranges = [
        ("0-20$",   0,   20),
        ("20-50$",  20,  50),
        ("50-100$", 50,  100),
        ("100-200$",100, 200),
        ("200$+",   200, 99999),
    ]
    for label, low, high in ranges:
        group = [t for t in trades if low <= abs(t.get("gap", 0)) < high]
        if not group:
            continue
        gw = [t for t in group if t.get("result") == "win"]
        wr = len(gw) / len(group) * 100
        pnl = sum(t.get("pnl", 0) for t in group)
        print(label + " | " + str(len(group)) + " trades | WR: " + str(round(wr, 1)) + "% | PnL: " + str(round(pnl, 2)))

    print("\n--- PAR PRIX ENTREE TOKEN ---")
    price_ranges = [
        ("0.45-0.50", 0.45, 0.50),
        ("0.50-0.55", 0.50, 0.55),
        ("0.55-0.60", 0.55, 0.60),
        ("0.60-0.65", 0.60, 0.65),
        ("0.65-0.70", 0.65, 0.70),
        ("0.70-0.80", 0.70, 0.80),
    ]
    for label, low, high in price_ranges:
        group = [t for t in trades if low <= t.get("entry_price", 0) < high]
        if not group:
            continue
        gw = [t for t in group if t.get("result") == "win"]
        wr = len(gw) / len(group) * 100
        pnl = sum(t.get("pnl", 0) for t in group)
        print(label + " | " + str(len(group)) + " trades | WR: " + str(round(wr, 1)) + "% | PnL: " + str(round(pnl, 2)))

    print("\n--- PAR HEURE ---")
    hours = {}
    for t in trades:
        h = t.get("hour", 0)
        if h not in hours:
            hours[h] = []
        hours[h].append(t)
    for h in sorted(hours.keys()):
        group = hours[h]
        gw = [t for t in group if t.get("result") == "win"]
        wr = len(gw) / len(group) * 100
        pnl = sum(t.get("pnl", 0) for t in group)
        print(str(h) + "h | " + str(len(group)) + " trades | WR: " + str(round(wr, 1)) + "% | PnL: " + str(round(pnl, 2)))

    print("\n--- MEILLEURS PARAMETRES ---")
    best_gap = max(ranges, key=lambda r: sum(t.get("pnl", 0) for t in trades if r[1] <= abs(t.get("gap", 0)) < r[2]))
    best_price = max(price_ranges, key=lambda r: sum(t.get("pnl", 0) for t in trades if r[1] <= t.get("entry_price", 0) < r[2]))
    best_hour = max(hours.keys(), key=lambda h: sum(t.get("pnl", 0) for t in hours[h]))

    print("Meilleur ecart  : " + best_gap[0])
    print("Meilleur prix   : " + best_price[0])
    print("Meilleure heure : " + str(best_hour) + "h")
    print("=" * 50)

if __name__ == "__main__":
    analyze()
