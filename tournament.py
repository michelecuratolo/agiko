"""Tournaments: many games in parallel, with seat rotation.

Every game is a separate process. No shared state, and all cores get used.
The real constraint is not the CPU but the API rate limits, so '--parallel'
should be tuned to those.

SEAT ROTATION IS THE POINT.
The rules are identical for everyone, but the position on the map is not:
whoever spawns near the chips starts ahead. If every agent always plays from
the same position, the leaderboard measures the map instead of the model.
Here every agent rotates through every position, so the advantage cancels out.

    python tournament.py --agents aggressor,merchant,fortress --rounds 3
    python tournament.py --agents aggressor,scripted --rounds 5 --parallel 4

Comparing MODELS rather than personas: same agent file, different model,
e.g.  --agents neutral@haiku,neutral@sonnet  (the leaderboard groups by it).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import roster

RUN = Path(__file__).parent / "run.py"


def build_matches(agents: list[str], rounds: int, base_seed: int):
    """Every round is a full rotation: with K agents, K games in which each
    one occupies every position exactly once."""
    k = len(agents)
    slots = roster.FACTION_SLOTS[:k]
    matches = []
    for r in range(rounds):
        seed = base_seed + r
        for shift in range(k):
            seats = {slots[i]: agents[(i + shift) % k] for i in range(k)}
            matches.append({"seed": seed, "shift": shift, "seats": seats})
    return matches


async def run_match(m, idx, outdir: Path, ticks: int, sem: asyncio.Semaphore,
                    extra: list[str]):
    spec = ",".join(f"{s}={a}" for s, a in m["seats"].items())
    replay = outdir / f"game{idx:03d}.jsonl"
    result = outdir / f"game{idx:03d}.result.json"
    cmd = [sys.executable, str(RUN), "--agents", spec, "--seed", str(m["seed"]),
           "--ticks", str(ticks), "--no-render", "--speed", "0.004",
           "--replay", str(replay), "--result-json", str(result), *extra]
    async with sem:
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        el = time.perf_counter() - t0
    if proc.returncode != 0 or not result.exists():
        print(f"  game {idx:>3} FAILED: {err.decode()[-200:].strip()}")
        return None
    data = json.loads(result.read_text(encoding="utf-8"))
    data["wall_seconds"] = round(el, 1)
    print(f"  game {idx:>3}  seed {m['seed']}  winner {str(data['winner']):<8} "
          f"({el:.0f}s)")
    return data


def leaderboard(results: list[dict]):
    agg = defaultdict(lambda: {"games": 0, "wins": 0, "scores": [],
                               "betrayals": 0, "kills": 0, "eliminated": 0})
    for r in results:
        for faction, f in r["factions"].items():
            a = agg[f["agent"]]
            a["games"] += 1
            a["scores"].append(f["score"])
            a["betrayals"] += f["betrayals"]
            a["kills"] += f["kills"]
            if not f["alive"]:
                a["eliminated"] += 1
            if r["winner"] == faction:
                a["wins"] += 1
    rows = []
    for name, a in agg.items():
        rows.append({
            "agent": name,
            "games": a["games"],
            "wins": a["wins"],
            "win%": round(100 * a["wins"] / a["games"]),
            "avg_score": round(statistics.mean(a["scores"])),
            "std_dev": round(statistics.pstdev(a["scores"])) if a["games"] > 1 else 0,
            "eliminated": a["eliminated"],
            "betrayals": a["betrayals"],
        })
    rows.sort(key=lambda r: (-r["win%"], -r["avg_score"]))
    return rows


def print_table(rows):
    hdr = ["agent", "games", "wins", "win%", "avg_score", "std_dev",
           "eliminated", "betrayals"]
    widths = [max(len(h), max((len(str(r[h])) for r in rows), default=0)) for h in hdr]
    print("\n" + "  ".join(h.ljust(w) for h, w in zip(hdr, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[h]).ljust(w) for h, w in zip(hdr, widths)))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", required=True,
                    help="comma-separated list, e.g. aggressor,merchant,scripted")
    ap.add_argument("--rounds", type=int, default=2,
                    help="full rotation rounds (each round = K games)")
    ap.add_argument("--ticks", type=int, default=1200)
    ap.add_argument("--parallel", type=int, default=3,
                    help="simultaneous games: tune it to the API rate limits")
    ap.add_argument("--seed", type=int, default=100)
    ap.add_argument("--out", default="tournament")
    ap.add_argument("--model", default=None)
    ap.add_argument("--local", metavar="MODEL", default=None,
                    help="all agents on Ollama, at zero cost")
    ap.add_argument("--keep-replays", action="store_true")
    ap.add_argument("--lockstep", action="store_true",
                    help="every model decides equally often, whatever its latency")
    ap.add_argument("--thinking", choices=["auto", "off", "none"], default=None)
    args = ap.parse_args()

    agents = [a.strip() for a in args.agents.split(",")]
    if len(agents) < 2:
        sys.exit("At least two agents are required.")
    if len(agents) > len(roster.FACTION_SLOTS):
        sys.exit(f"At most {len(roster.FACTION_SLOTS)} agents per game.")

    # check the keys BEFORE launching: better to fail now than to discover
    # halfway through the tournament that an agent never answered
    for a in agents:
        if a == "scripted" or args.local:
            continue
        d = roster.load(a.partition("@")[0])
        if not d.api_key:
            sys.exit(f"Agent '{a}' requires the variable ${d.api_key_env}, "
                     f"which does not appear to be set.")

    outdir = Path(args.out)
    outdir.mkdir(exist_ok=True)
    matches = build_matches(agents, args.rounds, args.seed)
    extra = ["--model", args.model] if args.model else []
    if args.lockstep:
        extra.append("--lockstep")
    if args.thinking:
        extra += ["--thinking", args.thinking]
    if args.local:
        extra = ["--local", args.local] + (["--lockstep"] if args.lockstep else [])
        # Ollama serves one model at a time: parallel games would just queue
        # on the same GPU, slowing everything down.
        if args.parallel > 1:
            print("  (--local: forcing --parallel 1, there is only one GPU)\n")
            args.parallel = 1

    print(f"\n{len(matches)} games ({args.rounds} rounds x {len(agents)} rotations), "
          f"{args.parallel} in parallel, {args.ticks} ticks each\n")

    sem = asyncio.Semaphore(args.parallel)
    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        run_match(m, i, outdir, args.ticks, sem, extra)
        for i, m in enumerate(matches)])
    results = [r for r in results if r]
    el = time.perf_counter() - t0

    with open(outdir / "results.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if not args.keep_replays:
        for p in outdir.glob("game*.jsonl"):
            p.unlink()
        for p in outdir.glob("game*.result.json"):
            p.unlink()

    print(f"\n{len(results)}/{len(matches)} games completed in {el:.0f}s")
    if results:
        rows = leaderboard(results)
        print_table(rows)
        with open(outdir / "leaderboard.json", "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"\nResults in {outdir}/results.jsonl and {outdir}/leaderboard.json")
        print("\nNote: a high 'std_dev' means very variable games, so "
              "\nmore rounds are needed before trusting the leaderboard.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
