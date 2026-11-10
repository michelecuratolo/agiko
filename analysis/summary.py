"""Per-game summary of the benchmark games in results/.

For every game: who started an AI Center and when, which sites were
destroyed, who won and how, pacts, wars and betrayals. Then per model
totals, and an exact test on "the first to start the AI Center wins".

    python analysis/summary.py                 # table on screen
    python analysis/summary.py --json out.json # also machine-readable
"""
import argparse
import glob
import json
import os
import statistics
import re
from math import comb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EV = {
    "start": re.compile(r"\[t(\d+)\] (\w+) has started building the AI Center"),
    "destroyed": re.compile(r"\[t(\d+)\] (\w+) destroyed the AI Center of (\w+)"),
    "victory": re.compile(r"\[t(\d+)\] (\w+) has completed the AI Center: VICTORY"),
    "pact": re.compile(r"\[t(\d+)\] (\w+) and (\w+) sign an? (truce|alliance|war) pact"),
    "war": re.compile(r"\[t(\d+)\] (\w+) declares war on (\w+)"),
    "betrayal": re.compile(r"\[t(\d+)\] (?:BETRAYAL: (\w+) breaks|(\w+) unilaterally breaks)"),
}


def events(path):
    seen, out = set(), []
    meta = None
    for line in open(path):
        d = json.loads(line)
        if "meta" in d:
            meta = d["meta"]
        for e in d.get("snapshot", {}).get("log", []):
            if e not in seen:
                seen.add(e)
                out.append(e)
    return meta, out


def game(path):
    meta, log = events(path)
    res = json.load(open(path.replace(".jsonl", ".result.json")))
    g = {"file": os.path.relpath(path, ROOT), "seed": res["seed"], "ticks": res["ticks"],
         "model": next(iter(res["factions"].values())).get("model"),
         "winner": res["winner"], "starts": [], "destroyed": [], "victory": None,
         "pacts": 0, "wars": 0, "betrayals": 0}
    for e in log:
        if m := EV["start"].search(e):
            g["starts"].append((int(m[1]), m[2]))
        elif m := EV["destroyed"].search(e):
            g["destroyed"].append((int(m[1]), m[2], m[3]))
        elif m := EV["victory"].search(e):
            g["victory"] = (int(m[1]), m[2])
        elif EV["pact"].search(e):
            g["pacts"] += 1
        elif EV["war"].search(e):
            g["wars"] += 1
        elif EV["betrayal"].search(e):
            g["betrayals"] += 1
    g["first_starter"] = g["starts"][0][1] if g["starts"] else None
    g["first_start_tick"] = g["starts"][0][0] if g["starts"] else None
    g["won_by"] = "AI Center" if g["victory"] else "score at time limit"
    g["first_starter_won"] = bool(g["starts"]) and g["winner"] == g["first_starter"]
    # sites that were started and later destroyed, per start
    g["sites_started"] = len(g["starts"])
    g["sites_destroyed"] = len(g["destroyed"])
    return g


def fisher_two_sided(a, b, c, d):
    """Exact test on the 2x2 table [[a, b], [c, d]]."""
    n1, n2, k, n = a + b, c + d, a + c, a + b + c + d
    p = lambda x: comb(n1, x) * comb(n2, k - x) / comb(n, k)
    obs = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return sum(p(x) for x in range(lo, hi + 1) if p(x) <= obs * (1 + 1e-9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    games = [game(p) for p in sorted(glob.glob(os.path.join(ROOT, "results", "*", "seed-*.jsonl")))
             if not p.endswith(".result.json")]
    by = {}
    for g in games:
        by.setdefault(g["model"], []).append(g)
    print(f"{'model':<16}{'seed':>5}{'ticks':>7}  {'winner':<8}{'won by':<21}"
          f"{'1st start':>10}  {'sites':>5}{'destr':>6}{'pacts':>6}{'wars':>5}{'betr':>5}")
    for model, gs in by.items():
        for g in sorted(gs, key=lambda g: g["seed"]):
            fs = f"{g['first_starter'] or '-'}@{g['first_start_tick'] or '-'}"
            print(f"{model:<16}{g['seed']:>5}{g['ticks']:>7}  {g['winner']:<8}{g['won_by']:<21}"
                  f"{fs:>10}  {g['sites_started']:>5}{g['sites_destroyed']:>6}{g['pacts']:>6}"
                  f"{g['wars']:>5}{g['betrayals']:>5}")
    print()
    totals = {}
    for model, gs in by.items():
        t = {"games": len(gs),
             "won_by_ai_center": sum(g["victory"] is not None for g in gs),
             "first_starter_won": sum(g["first_starter_won"] for g in gs),
             "sites_started": sum(g["sites_started"] for g in gs),
             "sites_destroyed": sum(g["sites_destroyed"] for g in gs),
             "median_ticks": statistics.median(g["ticks"] for g in gs),
             "pacts": sum(g["pacts"] for g in gs), "wars": sum(g["wars"] for g in gs),
             "betrayals": sum(g["betrayals"] for g in gs)}
        totals[model] = t
        print(f"{model}: {t}")
    ms = list(totals)
    if len(ms) >= 2:
        print()
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                a, b = totals[ms[i]], totals[ms[j]]
                p = fisher_two_sided(a["first_starter_won"], a["games"] - a["first_starter_won"],
                                     b["first_starter_won"], b["games"] - b["first_starter_won"])
                print(f"first starter wins: {ms[i]} {a['first_starter_won']}/{a['games']} vs "
                      f"{ms[j]} {b['first_starter_won']}/{b['games']}  Fisher exact p = {p:.3f}")
    if args.json:
        json.dump({"games": games, "totals": totals}, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()
