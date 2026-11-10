"""Build the data for the static replay viewer in docs/ (GitHub Pages).

Copies every benchmark game in results/<model>/seed-N.jsonl (and the first
local batch, results-batch1/) into
docs/replays/, dropping the per-decision observation (the full state the
agent saw: large, and not needed to watch a game — it stays in results/),
and writes docs/games.json, the list the viewer's menu is built from.

    python tools/build_site.py
"""
import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
LABEL = {"claude-haiku-4-5-20251001": "Claude Haiku 4.5", "claude-sonnet-5": "Claude Sonnet 5",
         "claude-opus-5": "Claude Opus 5", "gpt-oss:20b": "gpt-oss 20B (local)",
         "gemma4:26b": "Gemma 4 26B (local)", "qwen3.6:27b": "Qwen 3.6 27B (local)",
         "qwen3.6:35b-a3b": "Qwen 3.6 35B (local)"}
SETS = [("results", "", ""), ("results-batch1", "batch1-", " · batch 1, 4k cap")]
DROP = ("observation",)


def main():
    os.makedirs(os.path.join(DOCS, "replays"), exist_ok=True)
    for old in glob.glob(os.path.join(DOCS, "replays", "*.jsonl")):
        os.remove(old)
    games = []
    paths = [(p, prefix, suffix) for base, prefix, suffix in SETS
             for p in sorted(glob.glob(os.path.join(ROOT, base, "*", "seed-*.jsonl")))]
    for path, prefix, suffix in paths:
        folder = prefix + os.path.basename(os.path.dirname(path))
        seed = int(re.search(r"seed-(\d+)", path)[1])
        res = json.load(open(path.replace(".jsonl", ".result.json")))
        model = next(iter(res["factions"].values())).get("model", folder)
        gid = f"{folder}/seed-{seed}"
        out = os.path.join(DOCS, "replays", f"{folder}-seed-{seed}.jsonl")
        victory = None
        with open(path) as src, open(out, "w") as dst:
            for line in src:
                d = json.loads(line)
                if "faction" in d:
                    for k in DROP:
                        d.pop(k, None)
                for e in d.get("snapshot", {}).get("log", []):
                    m = re.search(r"\[t(\d+)\] (\w+) has completed the AI Center", e)
                    if m:
                        victory = int(m[1])
                dst.write(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n")
        games.append({"id": gid, "file": f"replays/{folder}-seed-{seed}.jsonl",
                      "model": model, "label": LABEL.get(model, model) + suffix, "seed": seed,
                      "winner": res["winner"], "ticks": res["ticks"],
                      "won_by": "AI Center" if victory else "score",
                      "kb": os.path.getsize(out) // 1024})
    games.sort(key=lambda g: (g["label"], g["seed"]))
    json.dump(games, open(os.path.join(DOCS, "games.json"), "w"), indent=1)
    print(f"{len(games)} games -> docs/replays, "
          f"{sum(g['kb'] for g in games) // 1024} MB total")


if __name__ == "__main__":
    main()
