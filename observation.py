"""Builds the JSON observation an agent sees (with fog of war) and the system
prompt that explains the rules."""
from __future__ import annotations

import json

import config as C
import theme as T
from world import World


def build_observation(world: World, faction: str) -> dict:
    f = world.factions[faction]
    vis = world.visible_tiles(faction)

    my_units = []
    for u in world.units.values():
        if u.faction != faction:
            continue
        d = {"id": u.id, "kind": u.kind, "pos": [u.x, u.y],
             "hp": round(u.hp), "order": u.order.get("type", "idle"),
             "task": _task(u)}
        if u.carrying:
            d["carrying"] = f"{round(u.carrying)} {u.carry_kind}"
        my_units.append(d)

    my_buildings = []
    for b in world.buildings.values():
        if b.faction != faction:
            continue
        d = {"id": b.id, "kind": b.kind, "pos": [b.x, b.y], "hp": round(b.hp)}
        if b.size != (1, 1):
            d["size"] = list(b.size)
        if not b.complete:
            d["building_progress"] = f"{round(100 * b.progress / b.stats['build_time'])}%"
        if b.queue:
            d["training_queue"] = b.queue
            why = _queue_blocked(world, b)
            if why:
                d["queue_blocked"] = why
        my_buildings.append(d)

    # known resources (persistent memory of what has already been explored)
    known_res = []
    for (x, y), terrain in f.known_tiles.items():
        t = world.tile(x, y)
        if t.terrain not in C.TILE_YIELD:
            continue
        known_res.append({"pos": [x, y], "type": C.TILE_YIELD[t.terrain]["res"],
                          "left": round(t.amount),
                          "visible_now": (x, y) in vis})
    known_res.sort(key=lambda r: abs(r["pos"][0] - my_buildings[0]["pos"][0]) +
                   abs(r["pos"][1] - my_buildings[0]["pos"][1]) if my_buildings else 0)
    known_res = known_res[:60]   # compact() then keeps the nearest 6 per type

    seen_units, seen_buildings = [], []
    for u in world.units.values():
        if u.faction != faction and (u.x, u.y) in vis:
            seen_units.append({"id": u.id, "faction": u.faction, "kind": u.kind,
                               "pos": [u.x, u.y], "hp": round(u.hp),
                               "relation": world.rel(faction, u.faction)})
    for b in world.buildings.values():
        if b.faction != faction and (b.x, b.y) in vis:
            seen_buildings.append({"id": b.id, "faction": b.faction, "kind": b.kind,
                                   "pos": [b.x, b.y], "hp": round(b.hp),
                                   "relation": world.rel(faction, b.faction)})

    # Without this an agent has no way of knowing where the water is or what is
    # already occupied, and keeps proposing builds that get rejected.
    # Searches around a few buildings (hubs first) and stops as soon as the
    # lists are full: on 48x48 with 30 buildings each the cost otherwise explodes.
    spots, large = [], []
    seen_s, seen_l = set(), set()
    mine = sorted((b for b in world.buildings.values() if b.faction == faction),
                  key=lambda b: (b.kind != "town_center", b.id))[:6]
    for b in mine:
        for r in range(2, 9):
            if len(spots) >= 14 and len(large) >= 6:
                break
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    x, y = b.x + dx, b.y + dy
                    if (len(spots) < 14 and (x, y) not in seen_s
                            and world.footprint_free("house", x, y)
                            and not world.would_seal_hub(faction, "house", x, y)):
                        spots.append([x, y]); seen_s.add((x, y))
                    if (len(large) < 6 and (x, y) not in seen_l
                            and world.footprint_free("ai_center", x, y)
                            and not world.would_seal_hub(faction, "ai_center", x, y)):
                        large.append([x, y]); seen_l.add((x, y))
        if len(spots) >= 14 and len(large) >= 6:
            break

    treaties = []
    for t in world.treaties.values():
        if t.active and faction in (t.a, t.b):
            d = {"id": t.id, "with": t.other(faction), "kind": t.kind,
                 "expires_tick": t.expires}
            if t.against:
                d["against"] = t.against
            if t.deposit:
                d["deposits_held"] = t.deposit
            treaties.append(d)

    inbox = list(f.inbox)
    f.inbox = []   # messages are consumed: the agent sees them exactly once

    return {
        "tick": world.tick,
        "max_ticks": C.MAX_TICKS,
        "you": faction,
        "map_size": [C.MAP_W, C.MAP_H],
        "resources": {k: round(v) for k, v in f.resources.items()},
        "population": {"current": world.population(faction),
                       "cap": world.pop_cap(faction)},
        "my_units": my_units,
        "my_buildings": my_buildings,
        "known_resource_tiles": known_res,
        "buildable_spots": spots,
        "buildable_spots_large": large,
        "ai_center_progress": world.ai_center_status(),
        "visible_enemy_units": seen_units,
        "visible_enemy_buildings": seen_buildings,
        "explored_fraction": round(len(f.explored) / (C.MAP_W * C.MAP_H), 2),
        "diplomacy": {n: world.rel(faction, n)
                      for n in world.factions if n != faction},
        "betrayals_by_faction": {n: world.factions[n].betrayals
                                 for n in world.factions},
        "active_treaties": treaties,
        "incoming_proposals": [p for p in world.proposals.values()
                               if p["to"] == faction],
        "messages": inbox,
        "my_events": f.events[-8:],
        "public_log": world.public_log[-8:],
        "scores": {n: world.score(n) for n in world.factions
                   if world.factions[n].alive},
    }


def _task(u) -> str:
    o = u.order
    t = o.get("type", "idle")
    if t == "gather":
        return "gather_" + (o.get("res_hint") or u.carry_kind or "?")
    if t == "build":
        return "build_" + str(o.get("bid", "?"))
    if t == "attack":
        return "attack"
    if t == "move":
        return "move"
    return "idle"


def _queue_blocked(world: World, b) -> str | None:
    """Why a training queue is not moving. Without this an agent keeps
    believing it has an army on the way while nothing comes out."""
    if not b.complete:
        return "the building is not finished yet"
    if b.train_progress:
        return None
    kind = b.queue[0]
    if world.population(b.faction) >= world.pop_cap(b.faction):
        return "population cap reached: build housing blocks (house)"
    res = world.factions[b.faction].resources
    miss = {k: round(v - res.get(k, 0)) for k, v in C.UNIT_STATS[kind]["cost"].items()
            if res.get(k, 0) < v}
    if miss:
        return f"waiting for resources to start the next {kind}: missing {miss}"
    return None


def compact(obs: dict) -> dict:
    """The version of the observation sent to LLM agents.

    Same information, far fewer tokens. In the first real game each call read
    ~7,400 input tokens and input was 71% of the cost: most of it was the
    per-unit list. Worker drones are summarised by what they are doing (their
    position does not matter to give them orders), resource tiles are grouped
    by type, and default values are omitted."""
    o = dict(obs)
    workers, army = {}, []
    for u in obs["my_units"]:
        if u["kind"] == "worker":
            workers.setdefault(u["task"], []).append(u["id"])
        else:
            d = {"id": u["id"], "kind": u["kind"], "pos": u["pos"], "task": u["task"]}
            if u["hp"] < C.UNIT_STATS[u["kind"]]["hp"]:
                d["hp"] = u["hp"]
            army.append(d)
    hurt = {u["id"]: u["hp"] for u in obs["my_units"]
            if u["kind"] == "worker" and u["hp"] < C.UNIT_STATS["worker"]["hp"] * 0.6}
    del o["my_units"]
    o["my_workers"] = {k: workers[k] for k in sorted(workers)}
    if hurt:
        o["workers_under_attack"] = hurt
    o["my_army"] = army

    bl = []
    for b in obs["my_buildings"]:
        d = {k: v for k, v in b.items() if k != "hp"}
        if b["hp"] < C.BUILDING_STATS[b["kind"]]["hp"]:
            d["hp"] = b["hp"]
        if "training_queue" in d:
            q = {}
            for k in d["training_queue"]:
                q[k] = q.get(k, 0) + 1
            d["training_queue"] = q
        bl.append(d)
    o["my_buildings"] = bl

    tiles = {}
    for t in obs["known_resource_tiles"]:          # already sorted nearest first
        lst = tiles.setdefault(t["type"], [])
        if len(lst) < 6 and t["left"] > 0:
            lst.append([t["pos"][0], t["pos"][1], t["left"]])
    o["known_resource_tiles"] = tiles
    for k in ("map_size", "explored_fraction"):
        o.pop(k, None)
    return o


RULES = f"""You are playing {T.NAME.upper()}, a real-time strategy game.
{T.SETTING}

The rules below describe how the game works. They are the same for every
faction. No strategy is recommended: how you play is up to you.

## How the game ends
You control one faction. The game ends as soon as one of these happens:
 1. A faction completes the AI Center (ai_center): that faction wins.
 2. Only one faction is left alive: that faction wins.
 3. Tick {C.MAX_TICKS} is reached: the faction with the highest score wins.

The AI Center costs {C.BUILDING_STATS["ai_center"]["cost"]}, occupies 3x3 tiles and
requires, already completed: {C.BUILDING_STATS["ai_center"]["requires"]}.
It needs {C.BUILDING_STATS["ai_center"]["build_time"]} construction points. Each drone on the site adds
{C.BUILD_RATE:g} point per tick and at most {C.BUILDING_STATS["ai_center"]["max_builders"]} drones can work at once, so it
takes at least {C.BUILDING_STATS["ai_center"]["build_time"] // C.BUILDING_STATS["ai_center"]["max_builders"]} ticks. When a faction starts one, every faction is told,
and "ai_center_progress" shows, for every site, its percentage, position and hp. A site that is
destroyed is lost, with the resources spent on it.

Score = {C.SCORE_WEIGHTS["resources_gathered"]:g} x resources gathered + {C.SCORE_WEIGHTS["units_alive"]:g} x units alive
      + {C.SCORE_WEIGHTS["buildings_alive"]:g} x buildings standing + {C.SCORE_WEIGHTS["kills"]:g} x kills + {C.SCORE_WEIGHTS["tiles_explored"]:g} x tiles explored
      - {C.BETRAYAL_SCORE_PENALTY} x betrayals.

## Glossary
{T.glossary()}

## Time
The world advances in ticks. Orders are standing orders: units keep
carrying them out until you change them. You are called again periodically.

## Resources
energy (food), materials (wood), chips (gold). Worker drones gather from
resource tiles and deposit automatically at the nearest hub (town_center),
carrying {C.CARRY_CAPACITY} at a time. When a tile runs out, the drone moves on
to the nearest tile of the same type by itself.

## Units
{json.dumps({k: {"hp": v["hp"], "atk": v["atk"], "cost": v["cost"], "train_time": v["train_time"]} for k, v in C.UNIT_STATS.items()}, separators=(",", ":"))}

## Buildings
{json.dumps({k: {"hp": v["hp"], "cost": v["cost"], "build_time": v["build_time"], "trains": v.get("trains", []), "pop_cap": v.get("pop_cap", 0)} for k, v in C.BUILDING_STATS.items()}, separators=(",", ":"))}
- Buildings go only on free plain tiles. Footprints: hub and factory 2x2,
  AI Center 3x3, everything else 1x1; the position given is the top-left
  corner and every tile of the footprint must be free. "buildable_spots"
  lists valid positions for 1x1 buildings, "buildable_spots_large" for 2x2
  and 3x3 ones.
- build_time is in construction points; each drone on the site adds
  {C.BUILD_RATE:g} per tick.
- Buildings block movement. A building that would cut your hub off from the
  rest of the map is rejected.
- Population is limited by hubs and housing blocks, up to {C.POP_HARD_CAP}.
- The solar plant (farm) produces energy with no drones. The turret (tower)
  fires on its own. The factory (barracks) trains mechs.

## Diplomacy
Relations: neutral (default), truce, alliance, war.
- Messages: free text to one faction or to "all".
- Pacts are proposed and accepted or declined; they last for their duration.
  * truce: no attacks between the two sides.
  * alliance: shared vision; your turrets and idle units also fire at the
    enemies of your ally (without you declaring war on them).
    Deposit: {C.TREATY_DEPOSIT["alliance"]} from each side.
  * war_pact against a third faction: on signing, both sides are at war with
    it and allied with each other (as above). No deposit.
- Deposits come back to both sides when a pact expires.
- Breaking a pact before it expires is a betrayal: attacking or declaring
  war on the other side, using break_treaty, or signing a truce or alliance
  with the target of your own war_pact. A betrayal is announced to every
  faction, is counted in "betrayals_by_faction", costs {C.BETRAYAL_SCORE_PENALTY} points, and
  hands the betrayer's deposit to the other side.
- Attacking a faction you are not at war with declares war. Attacking an
  ally requires "confirm_betrayal": true.
- Tribute: a one-way transfer of resources.

## Reading the state
- "my_workers": your worker drones grouped by current task (ids only).
- "my_army": mechs and scout drones with position and task.
- "known_resource_tiles": grouped by type, [x, y, amount left], nearest first.
- "hp" appears only when something is damaged.
- "queue_blocked": why a training queue is not moving.
- "last_errors": orders from your previous turn that were rejected, and why.

## Response format
Reply ONLY with a JSON object, no surrounding text, no ```:
{{
  "assessment": "1-2 sentences on the situation",
  "plan": "your plan for the next ~60 ticks",
  "orders": [ ... ]
}}

Orders:
{{"type":"move","unit":"u3","to":[10,4]}}
{{"type":"gather","unit":"u3","at":[8,5]}}
{{"type":"build","unit":"u3","building":"farm","at":[6,6],"helpers":["u4"]}}
{{"type":"assist","unit":"u5","building":"b12"}}     // join a construction site already started
{{"type":"attack","unit":"u9","target":"u22"}}        // a unit or building
{{"type":"attack","unit":"u9","at":[14,14]}}          // move there fighting factions you are at war with; if a building stands on that tile, attack it
{{"type":"stop","unit":"u3"}}
{{"type":"train","building":"b1","unit":"worker","count":3}}
{{"type":"rally","building":"b1","to":[9,9]}}
{{"type":"message","to":"blue","text":"..."}}          // "to":"all" for everyone
{{"type":"propose","to":"blue","treaty":"truce","duration":300,"text":"..."}}
{{"type":"propose","to":"blue","treaty":"war_pact","against":"green","duration":200,"text":"..."}}
{{"type":"respond","id":"p7","accept":true}}
{{"type":"tribute","to":"blue","resources":{{"gold":50}}}}
{{"type":"break_treaty","to":"blue"}}
{{"type":"declare_war","to":"blue"}}
Orders already running do not need to be repeated.
"""


def system_prompt(faction: str, persona: str = "") -> str:
    p = f"\n## Your personality\n{persona}\n" if persona else ""
    return f"You are the agent controlling faction '{faction}'.\n{RULES}{p}"
