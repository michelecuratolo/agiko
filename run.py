"""Orchestrator. The world advances in real time; every agent thinks in
parallel at its own pace, never blocking the simulation.

    python run.py --agents red=aggressor,blue=merchant,green=scripted
    python run.py --agents red=scripted,blue=scripted --speed 0.05 --no-render
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import config as C
import observation as OBS
import roster
from agents import LLMAgent, ScriptedAgent
from orders import apply_orders
from world import World


TERRAIN_CH = {"plain": "·", "forest": "♣", "gold": "$", "berry": "*", "water": "~"}
ANSI = {"red": "\033[91m", "blue": "\033[94m", "green": "\033[92m",
        "yellow": "\033[93m", "purple": "\033[95m", "cyan": "\033[96m"}
RESET = "\033[0m"

if sys.platform == "win32":
    # Windows 10+ supports ANSI sequences but they must be enabled, otherwise
    # the map in the terminal is full of escape characters.
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:                                # noqa: BLE001
        pass


def render(world: World, agents: dict):
    grid = [[TERRAIN_CH[world.tile(x, y).terrain] for x in range(C.MAP_W)]
            for y in range(C.MAP_H)]
    for b in world.buildings.values():
        ch = {"town_center": "H", "house": "h", "farm": "f",
              "barracks": "B", "tower": "T", "ai_center": "A"}[b.kind]
        if not b.complete:
            ch = ch.lower() if ch.isupper() else "."
        for (cx, cy) in b.cells():
            grid[cy][cx] = ANSI[world.factions[b.faction].color] + ch + RESET
    for u in world.units.values():
        ch = {"worker": "w", "soldier": "S", "scout": "s"}[u.kind]
        grid[u.y][u.x] = ANSI[world.factions[u.faction].color] + ch + RESET

    out = ["\033[H\033[J", f"AGIKO — tick {world.tick}/{C.MAX_TICKS}", ""]
    out += ["  " + "".join(row) for row in grid]
    out.append("")
    for n, f in world.factions.items():
        if not f.alive:
            out.append(f"{ANSI[f.color]}{n:<8}{RESET} ELIMINATO")
            continue
        r = f.resources
        rel = " ".join(f"{o[:2]}:{world.rel(n, o)[:4]}" for o in world.factions if o != n)
        ag = agents.get(n)
        think = "…" if ag and ag.thinking else " "
        out.append(
            f"{ANSI[f.color]}{n:<8}{RESET}{think}{(ag.label if ag else ''):<14} "
            f"E{round(r['food']):>4} M{round(r['wood']):>4} C{round(r['gold']):>4} "
            f"pop {world.population(n):>2}/{world.pop_cap(n):<2} "
            f"pts {world.score(n):>7} betr {f.betrayals}  | {rel}")
    out.append("")
    out += world.public_log[-4:]
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


async def wait_ticks(world: World, n: int):
    """Wait n game ticks (not real seconds): this keeps the agents' pace
    consistent both at normal speed and in fast-forward."""
    target = world.tick + n
    while world.tick < target and not world.over:
        await asyncio.sleep(0.005)


async def agent_loop(world: World, name: str, agent, every_ticks: int, replay,
                     stagger: int = 0):
    """Every agent runs in its own loop: observe, think, send orders.
    While 'decide' is in progress the world keeps advancing: this is the key
    point that makes a slow LLM compatible with real time."""
    # Staggering: without this, agents with the same cadence would all call
    # the API in the same tick. With a single shared key that is a burst that
    # trips the rate limits.
    await wait_ticks(world, 2 + stagger)
    while not world.over and world.factions[name].alive:
        t0 = world.tick
        obs = OBS.build_observation(world, name)
        if isinstance(agent, LLMAgent):
            obs = OBS.compact(obs)
        wall0 = time.monotonic()
        try:
            orders = await agent.decide(obs)
        except Exception as e:                        # noqa: BLE001
            agent.last_errors = [f"agent crash: {type(e).__name__}: {e}"]
            orders = []
        # decide() may have recorded a network or parsing error: it must be
        # kept, otherwise a silent faction just looks passive.
        api_errors = list(agent.last_errors)
        if world.over:
            break
        errs = apply_orders(world, name, orders or [])
        agent.last_errors = api_errors + errs
        for e in api_errors:
            if not getattr(agent, "_warned", False):
                agent._warned = True
                print(f"\n!! agent '{name}' is not responding: {e[:160]}\n", flush=True)
        replay.write(json.dumps({
            "t": world.tick, "faction": name, "thought_at": t0,
            "orders": orders, "errors": agent.last_errors, "plan": agent.last_plan,
            "assessment": getattr(agent, "last_assessment", ""),
            "latency_ms": int(1000 * (time.monotonic() - wall0)),
            "out_tokens": getattr(agent, "last_out_tokens", None),
            "reply_chars": getattr(agent, "last_reply_chars", None),
            "reply_blocks": getattr(agent, "last_block_types", None),
            "reasoning": getattr(agent, "last_reasoning", "") or None,
            "source": "llm" if isinstance(agent, LLMAgent) else "scripted",
            # the decision's input: without this the data cannot be used to
            # improve any agent
            "observation": obs if isinstance(agent, LLMAgent) else None,
        }, ensure_ascii=False) + "\n")
        replay.flush()
        await wait_ticks(world, every_ticks)


_log_seen = 0


def snapshot(world: World) -> dict:
    """Compact world state, for replays and external viewers."""
    global _log_seen
    # Count with log_total, not len(public_log): the list is truncated to 60
    # lines, and comparing lengths silently dropped every event after the 60th.
    fresh = min(world.log_total - _log_seen, len(world.public_log))
    new_log = world.public_log[-fresh:] if fresh > 0 else []
    _log_seen = world.log_total
    res = [[x, y, round(world.tile(x, y).amount)]
           for y in range(C.MAP_H) for x in range(C.MAP_W)
           if world.tile(x, y).amount > 0]
    return {
        "t": world.tick,
        "res": res,
        "log": new_log,
        "units": [[u.id, u.faction, u.kind, u.x, u.y, round(u.hp)]
                  for u in world.units.values()],
        "buildings": [[b.id, b.faction, b.kind, b.x, b.y, round(b.hp), b.complete]
                      for b in world.buildings.values()],
        "factions": {n: {"res": {k: round(v) for k, v in f.resources.items()},
                         "score": world.score(n), "alive": f.alive,
                         "betrayals": f.betrayals,
                         "ai": (world.ai_center_status().get(n) or {}).get("percent")}
                     for n, f in world.factions.items()},
        "relations": {f"{a}|{b}": v for (a, b), v in world.relations.items()},
    }


async def world_loop(world: World, agents: dict, speed: float, do_render: bool,
                     replay=None, snap_every: int = 10, lockstep: bool = False):
    last = time.monotonic()
    while not world.over:
        if lockstep:
            # The world waits while any agent is thinking: every agent then
            # decides exactly every N ticks, however slow its model is. In real
            # time a model with 18 s latency sees the game 3x less often than
            # one with 6 s, and a behavioural comparison would mix the two.
            waited = False
            while any(getattr(a, "thinking", False) is True for a in agents.values()):
                waited = True
                await asyncio.sleep(0.02)
            if waited:
                last = time.monotonic()
        world.step()
        if replay is not None and (world.tick % snap_every == 0 or world.over):
            replay.write(json.dumps({"snapshot": snapshot(world)}) + "\n")
        if do_render and world.tick % 2 == 0:
            render(world, agents)
        last += speed
        delay = last - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            last = time.monotonic()
            await asyncio.sleep(0)


def _cost(agent):
    """Estimated USD spent by one agent, or None if the price is unknown."""
    p = C.PRICES.get(getattr(agent, "model", None))
    u = getattr(agent, "usage", None)
    if u and isinstance(u.get("cost_reported"), (int, float)) and not getattr(agent, "local", False):
        return round(u["cost_reported"], 4)                # the provider's own figure
    if not p or not u or getattr(agent, "local", False):
        return None
    cache_mult = p[2] if len(p) > 2 else 0.1
    return round((u["in"] * p[0] + u.get("cache_write", 0) * p[0] * 1.25
                  + u.get("cache_read", 0) * p[0] * cache_mult + u["out"] * p[1]) / 1e6, 4)


def parse_agents(spec: str):
    out = []
    for part in spec.split(","):
        name, _, kind = part.partition("=")
        out.append((name.strip(), (kind or "scripted").strip()))
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="red=scripted,blue=scripted,green=scripted",
                    help="e.g. red=aggressor,blue=merchant,green=scripted")
    ap.add_argument("--pick", action="store_true",
                    help="interactive agent selection screen")
    ap.add_argument("--list-agents", action="store_true",
                    help="list the agents available in roster/")
    ap.add_argument("--new-agent", metavar="NAME",
                    help="create a new agent file from a template and exit")
    ap.add_argument("--model", default=None,
                    help="force one model for everyone, ignoring the agent files "
                         "(also: haiku, sonnet, opus). For one seat only: red=neutral@sonnet")
    ap.add_argument("--local", metavar="MODEL", default=None,
                    help="run ALL agents locally on Ollama, at zero cost, keeping "
                         "their personalities (e.g. --local qwen3:8b)")
    ap.add_argument("--local-url", default="http://localhost:11434/v1/chat/completions")
    ap.add_argument("--speed", type=float, default=C.TICK_SECONDS,
                    help="real seconds per tick")
    ap.add_argument("--interval", type=int, default=None,
                    help="game ticks between two decisions of an LLM agent")
    ap.add_argument("--ticks", type=int, default=C.MAX_TICKS)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--replay", default=None,
                    help="where to save the replay (default: replays/game-DATE-TIME.jsonl, "
                         "so it shows up in the interface)")
    ap.add_argument("--result-json", default=None,
                    help="write the final result here as JSON (for tournaments)")
    ap.add_argument("--thinking", choices=["auto", "off", "none"], default=C.THINKING,
                    help="auto: models that reason by default return a readable summary, "
                         "saved in the replay; off: disable reasoning where allowed "
                         "(e.g. Sonnet 5); none: send no thinking parameter")
    ap.add_argument("--lockstep", action="store_true",
                    help="the world pauses while an agent is thinking: every model "
                         "decides equally often, whatever its latency (benchmarks)")
    ap.add_argument("--snapshot-every", type=int, default=10,
                    help="how often (in ticks) to save the world state in the replay")
    args = ap.parse_args()

    if args.list_agents:
        roster.print_list()
        return
    if args.new_agent:
        path = roster.scaffold(args.new_agent, args.model)
        print(f"Created {path}\nOpen it, write the persona, then:\n"
              f"  python run.py --agents red={args.new_agent},blue=expansionist")
        return

    C.MAX_TICKS = args.ticks
    spec = roster.pick_interactive() if args.pick else parse_agents(args.agents)
    world = World([n for n, _ in spec], seed=args.seed)

    agents, cadence = {}, {}
    for i, (name, kind) in enumerate(spec):
        if kind == "scripted":
            agents[name] = ScriptedAgent(name, aggression=0.3 + 0.2 * i)
            cadence[name] = 3
            continue
        # "neutral@haiku": agent file + model for this seat only. This is how
        # different models sit in the same game with the same persona.
        agent_name, _, seat_model = kind.partition("@")
        d = roster.load(agent_name)
        # --local reroutes every agent to Ollama without touching the files:
        # the personalities stay the same, only who runs them changes.
        prov = "openai" if args.local else d.provider
        base = args.local_url if args.local else d.api_base
        alias = C.MODEL_ALIASES
        model = (args.local or alias.get(seat_model, seat_model)
                 or alias.get(args.model, args.model) or d.model)
        try:
            agents[name] = LLMAgent(name, model=model,
                                    persona=d.persona, verbose=args.no_render,
                                    api_key_env=d.api_key_env, api_base=base,
                                    provider=prov)
        except RuntimeError as e:
            print(f"Cannot create agent '{name}' ({kind}): {e}\n"
                  f"Set the key with:  export {d.api_key_env}=...\n"
                  f"Or play for free with a local model: --local gpt-oss:20b\n"
                  f"Or against the bots: --agents {name}=scripted,...")
            return
        # locally, long replies cost seconds, not cents
        # Local reasoning models (gpt-oss, qwen3) think before the JSON: 600
        # tokens cut them off. Generous limit; they stop earlier on their own.
        agents[name].max_tokens = C.LOCAL_MAX_TOKENS if args.local else d.max_tokens
        agents[name].label = kind
        agents[name].thinking_mode = args.thinking
        cadence[name] = args.interval or (30 if args.local else d.think_every)

    if args.replay is None:
        # Save into replays/ by default: that is the folder the interface reads.
        rdir = Path(__file__).parent / "replays"
        rdir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        args.replay = str(rdir / f"game-{stamp}.jsonl")
        if args.result_json is None:
            args.result_json = str(rdir / f"game-{stamp}.result.json")

    with open(args.replay, "w", encoding="utf-8") as replay:
        terrain_code = {"plain": ".", "forest": "f", "gold": "g",
                        "berry": "b", "water": "w"}
        import hashlib
        participants = {}
        for n, a in agents.items():
            if isinstance(a, LLMAgent):
                participants[n] = {"kind": "agent", "name": a.label, "model": a.model,
                                   "thinking": getattr(a, "thinking_mode", None),
                                   "persona_sha": hashlib.sha1(a.persona.encode()).hexdigest()[:12],
                                   "provider": a.provider, "local": a.local}
            else:
                participants[n] = {"kind": "scripted", "name": "scripted", "model": None, "persona_sha": None}
        replay.write(json.dumps({"meta": {
            "map": [C.MAP_W, C.MAP_H],
            "agents": dict(spec),
            "participants": participants,
            "symmetric": C.SYMMETRIC_MAP, "lockstep": args.lockstep,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "colors": {n: f.color for n, f in world.factions.items()},
            "seed": args.seed, "max_ticks": C.MAX_TICKS,
            "terrain": ["".join(terrain_code[world.tile(x, y).terrain]
                                for x in range(C.MAP_W)) for y in range(C.MAP_H)],
        }}) + "\n")
        tasks = [asyncio.create_task(
            agent_loop(world, n, a, cadence[n], replay,
                       stagger=int(i * cadence[n] / max(1, len(agents)))))
            for i, (n, a) in enumerate(agents.items())]
        await world_loop(world, agents, args.speed, not args.no_render,
                         replay, args.snapshot_every, lockstep=args.lockstep)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if not args.no_render:
        render(world, agents)
    print(f"\n=== GAME OVER at tick {world.tick} ===")
    print(f"Winner: {world.winner}")
    for n in world.factions:
        f = world.factions[n]
        print(f"  {n:<8} score {world.score(n):>8}  gathered {round(f.gathered):>6}"
              f"  kills {f.kills:<3} betrayals {f.betrayals}"
              f"  {'alive' if f.alive else 'eliminated'}")
    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as fh:
            json.dump({
                "seed": args.seed, "ticks": world.tick,
                "seats": {n: k for n, k in spec},
                "winner": world.winner,
                "factions": {n: {"agent": dict(spec)[n], "score": world.score(n),
                                 "gathered": round(world.factions[n].gathered),
                                 "kills": world.factions[n].kills,
                                 "betrayals": world.factions[n].betrayals,
                                 "alive": world.factions[n].alive,
                                 "model": getattr(agents[n], "model", None),
                                 "decisions": getattr(agents[n], "calls", 0),
                                 "usage": dict(getattr(agents[n], "usage", {}) or {}),
                                 "cost_usd": _cost(agents[n])}
                             for n in world.factions},
            }, fh, ensure_ascii=False, indent=1)
    # Output tokens are ~75% of the spend: if it costs too much, the first
    # fix is shortening the replies, not trimming the observations.
    llm = {n: a for n, a in agents.items()
           if isinstance(a, LLMAgent) and not a.local}
    if llm:
        print("\nAPI usage:")
        tot = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
        for n, a in llm.items():
            u = a.usage
            for k in tot:
                tot[k] += u[k]
            c = _cost(a)
            print(f"  {n:<8} {a.calls:>4} calls  "
                  f"in {u['in']:>7}  cache {u['cache_read']:>7}  out {u['out']:>7}"
                  + (f"   ~${c:.2f}" if c is not None else ""))
        costs = [_cost(a) for a in llm.values()]
        total = sum(c for c in costs if c is not None)
        print(f"  {'TOTAL':<8} {sum(a.calls for a in llm.values()):>4} calls  "
              f"in {tot['in']:>7}  cache {tot['cache_read']:>7}  out {tot['out']:>7}"
              + (f"   ~${total:.2f}" if any(c is not None for c in costs) else ""))
        print("  (estimate from config.PRICES: check them against the current price list)")
    print(f"Replay saved to {args.replay}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
