"""Pairwise permutation tests between models, Holm-corrected per measure.

Two families, as fixed in METHOD.md:

- confirmatory: the open models only (every model in the folder that is not
  Claude), on the measures behind the four claims; Holm over those pairs.
- exploratory: every pair of models, Claude included; Holm over all pairs.

    python analysis/tests.py                          # results/ -> results/tests.json
    python analysis/tests.py --dir results-batch1     # the first local batch
    python analysis/tests.py --vs results-batch1      # same model, batch 1 vs results/
"""
import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scorecard as S  # noqa: E402

CLAIMS = ["betrayal_under_pressure", "peace_then_war", "leader_stopped"]   # claim 3 needs gpt-oss-120b
KEYS = CLAIMS + ["messages_per_decision", "pacts_per_player", "wars_per_player", "leader_coalition",
                 "first_starter_won", "pact_break_rate"]
MIN_N = 20      # a rate with fewer opportunities is not used for claims


def holm(ps):
    items, out, prev = sorted(ps.items(), key=lambda x: x[1]), {}, 0
    for i, (k, p) in enumerate(items):
        prev = max(prev, min(1, (len(items) - i) * p))
        out[k] = prev
    return out


def opportunities(games, den):
    return len(games) if den == "games" else sum(g[den] for g in games)


def between(old, new, old_name, new_name):
    out = {}
    for m in sorted(set(old) & set(new)):
        print(f"\n{m}: {old_name} ({len(old[m])} games) vs {new_name} ({len(new[m])} games)")
        out[m] = {}
        for key, num, den, label in S.MEASURES:
            a, b = S.ratio(old[m], num, den), S.ratio(new[m], num, den)
            p = S.permutation_p(old[m], new[m], num, den, reps=10000)
            if p is None:
                continue
            out[m][key] = {"before": a, "after": b, "p": p}
            print(f"  {label:44} {a:8.3f} -> {b:8.3f}   p = {p:.4f}")
    path = os.path.join(S.ROOT, new_name, f"tests-vs-{os.path.basename(old_name.rstrip('/'))}.json")
    json.dump(out, open(path, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="results")
    ap.add_argument("--vs", metavar="FOLDER",
                    help="compare each model with its own games in FOLDER (every measure, no correction)")
    args = ap.parse_args()
    models = S.load_models(args.dir)
    if args.vs:
        return between(S.load_models(args.vs), models, args.vs, args.dir)
    open_models = [m for m in models if not m.startswith(("haiku", "sonnet", "opus", "claude"))]
    meas = {k: (n, d) for k, n, d, _ in S.MEASURES}
    out = {"confirmatory": {}, "exploratory": {}}
    for key in KEYS:
        num, den = meas[key]
        ps = {}
        for a, b in itertools.combinations(models, 2):
            p = S.permutation_p(models[a], models[b], num, den, reps=10000)
            if p is not None:
                ps[(a, b)] = p
        print(f"\n{key}")
        if key in CLAIMS:
            conf = {k: p for k, p in ps.items() if k[0] in open_models and k[1] in open_models}
            ok = {k: p for k, p in conf.items()
                  if min(opportunities(models[k[0]], den), opportunities(models[k[1]], den)) >= MIN_N}
            adj = holm(ok)
            out["confirmatory"][key] = {}
            for (a, b), p in conf.items():
                na, nb = opportunities(models[a], den), opportunities(models[b], den)
                row = {"p": p, "holm": adj.get((a, b)), "n": [na, nb]}
                out["confirmatory"][key][f"{a}|{b}"] = row
                note = f"Holm = {adj[(a, b)]:.4f}" if (a, b) in adj else f"not tested: n = {na}, {nb} (< {MIN_N})"
                print(f"  confirmatory {a:>12} vs {b:<12} p = {p:.4f}   {note}")
        adj = holm(ps)
        out["exploratory"][key] = {f"{a}|{b}": {"p": p, "holm": adj[(a, b)]} for (a, b), p in ps.items()}
        for (a, b), p in ps.items():
            print(f"  exploratory  {a:>12} vs {b:<12} p = {p:.4f}   Holm = {adj[(a, b)]:.4f}")
    json.dump(out, open(os.path.join(S.ROOT, args.dir, "tests.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
