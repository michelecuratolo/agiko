"""Map fairness test.

The idea: on a symmetric map, IDENTICAL agents should finish almost level.
If they don't, the advantage comes from the map and any leaderboard would
measure geography instead of strategy.

A "draw" between identical agents is not a flaw: it is proof that the playing
field is level.
"""
import asyncio
import config as C
from world import World
from orders import apply_orders
import observation as OBS
from agents import ScriptedAgent


async def game(n, seed, ticks=700):
    names = [f"f{i}" for i in range(n)]
    w = World(names, seed=seed)
    ags = {x: ScriptedAgent(x) for x in names}
    for t in range(ticks):
        w.step()
        if t % 3 == 0:
            for x, a in ags.items():
                if w.factions[x].alive:
                    apply_orders(w, x, await a.decide(OBS.build_observation(w, x)))
        if w.over:
            break
    return [w.score(x) for x in names]


async def main():
    print("IDENTICAL agents: any remaining spread is entirely the map's fault.\n")
    print(f"{'factions':>8}  {'map':<12} {'scores':<34} {'spread':>8}")
    print("-" * 68)
    for n in (2, 4, 3):
        for sym in (False, True):
            C.SYMMETRIC_MAP = sym
            scarti = []
            ultimo = None
            for seed in (100, 101, 102):
                s = await game(n, seed)
                scarti.append((max(s) - min(s)) / (sum(s) / len(s)) * 100)
                if seed == 100:
                    ultimo = s
            media = sum(scarti) / len(scarti)
            etichetta = "symmetric" if sym else "random"
            print(f"{n:>8}  {etichetta:<12} {str([round(v) for v in ultimo]):<34} "
                  f"{media:>7.1f}%")
        print()
    print("With 2 and 4 factions the symmetry is exact and the draw is almost perfect.")
    print("With 3 (and 6) a residue remains: a square grid cannot rotate by 120")
    print("degrees without rounding. Seat rotation is still needed there.")

asyncio.run(main())
