"""Validation and application of agent orders.

Every order goes through here. If it is invalid it is rejected with a
readable reason, which goes back to the agent on the next cycle: this is how
the LLM learns from its mistakes instead of repeating them.
"""
from __future__ import annotations

import config as C
from world import World

ORDER_TYPES = {
    "move", "gather", "build", "assist", "attack", "stop",
    "train", "rally",
    "message", "propose", "respond", "break_treaty", "declare_war", "tribute",
}


def apply_orders(world: World, faction: str, orders: list) -> list[str]:
    """Apply a list of orders. Returns the list of errors."""
    errors = []
    if not isinstance(orders, list):
        return ["'orders' must be a list"]
    for i, o in enumerate(orders[:40]):
        try:
            err = _apply_one(world, faction, o)
        except Exception as e:                      # noqa: BLE001
            err = f"exception: {type(e).__name__}: {e}"
        if err:
            errors.append(f"order #{i} ({o.get('type', '?') if isinstance(o, dict) else o}): {err}")
    return errors


def _own_unit(world, faction, uid):
    u = world.units.get(uid)
    if u is None:
        return None, f"unit '{uid}' does not exist (it may be dead)"
    if u.faction != faction:
        return None, f"unit '{uid}' is not yours"
    return u, None


def _own_building(world, faction, bid):
    b = world.buildings.get(bid)
    if b is None:
        return None, f"building '{bid}' does not exist"
    if b.faction != faction:
        return None, f"building '{bid}' is not yours"
    return b, None


def _coord(world, v, label="coordinate"):
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        return None, f"{label} must be [x, y]"
    try:
        x, y = int(v[0]), int(v[1])
    except (TypeError, ValueError):
        return None, f"{label} is not numeric"
    if not world.in_bounds(x, y):
        return None, f"{label} ({x},{y}) is off the map (0-{C.MAP_W - 1}, 0-{C.MAP_H - 1})"
    return (x, y), None


def _apply_one(world: World, faction: str, o) -> str | None:
    if not isinstance(o, dict):
        return "the order must be a JSON object"
    t = o.get("type")
    if t not in ORDER_TYPES:
        return f"unknown type. Valid: {sorted(ORDER_TYPES)}"

    # ---------------- units ----------------
    if t == "move":
        u, e = _own_unit(world, faction, o.get("unit"))
        if e:
            return e
        pos, e = _coord(world, o.get("to"), "to")
        if e:
            return e
        u.order = {"type": "move", "to": list(pos)}
        u.path = []
        return None

    if t == "stop":
        u, e = _own_unit(world, faction, o.get("unit"))
        if e:
            return e
        u.order = {"type": "idle"}
        u.path = []
        return None

    if t == "gather":
        u, e = _own_unit(world, faction, o.get("unit"))
        if e:
            return e
        if u.kind != "worker":
            return "only 'worker' units can gather"
        pos, e = _coord(world, o.get("at"), "at")
        if e:
            return e
        tile = world.tile(*pos)
        if tile.terrain not in C.TILE_YIELD or tile.amount <= 0:
            return f"tile ({pos[0]},{pos[1]}) is '{tile.terrain}', it holds no resources"
        u.order = {"type": "gather", "at": list(pos),
                   "res_hint": C.TILE_YIELD[tile.terrain]["res"]}
        u.path = []
        return None

    if t == "build":
        u, e = _own_unit(world, faction, o.get("unit"))
        if e:
            return e
        if u.kind != "worker":
            return "only 'worker' units can build"
        kind = o.get("building")
        if kind not in C.BUILDING_STATS:
            return f"unknown building. Valid: {sorted(C.BUILDING_STATS)}"
        pos, e = _coord(world, o.get("at"), "at")
        if e:
            return e
        x, y = pos
        st = C.BUILDING_STATS[kind]
        w_, h_ = st.get("size", (1, 1))
        if not world.footprint_free(kind, x, y):
            return (f"{kind} occupies {w_}x{h_} tiles anchored at the top-left corner: "
                    f"every tile from ({x},{y}) to ({x + w_ - 1},{y + h_ - 1}) must be a free "
                    f"slab. Use 'buildable_spots_large' for large buildings.")
        if world.would_seal_hub(faction, kind, x, y):
            return (f"{kind} at ({x},{y}) would wall in your hub: drones could no longer "
                    f"reach it to deposit. Leave a passage free.")
        for req_kind, n in st.get("requires", {}).items():
            have = sum(1 for b in world.buildings.values()
                       if b.faction == faction and b.kind == req_kind and b.complete)
            if have < n:
                return (f"{kind} requires at least {n} completed {req_kind} "
                        f"(you have {have})")
        if st.get("victory") and any(b.faction == faction and b.kind == kind
                                     for b in world.buildings.values()):
            return f"you already have a {kind} under construction"
        cost = st["cost"]
        f = world.factions[faction]
        missing = {k: v - f.resources.get(k, 0) for k, v in cost.items()
                   if f.resources.get(k, 0) < v}
        if missing:
            return f"not enough resources for {kind}, missing {missing}"
        for k, v in cost.items():
            f.resources[k] -= v
        b = world.spawn_building(faction, kind, x, y, complete=False)
        world.displace_units_at(b)
        if st.get("victory"):
            world.log(f"{faction} has started building the AI Center at ({x},{y})")
            for other in world.factions:
                if other != faction:
                    world.notify(other, f"WARNING: {faction} is building the AI Center. "
                                        f"If they complete it, they win.")
        u.order = {"type": "build", "bid": b.id}
        u.path = []
        # other workers can join with the same build order on bid
        for extra in o.get("helpers", [])[:20]:
            hu, he = _own_unit(world, faction, extra)
            if hu and hu.kind == "worker":
                hu.order = {"type": "build", "bid": b.id}
                hu.path = []
        return None

    if t == "assist":
        u, e = _own_unit(world, faction, o.get("unit"))
        if e:
            return e
        if u.kind != "worker":
            return "only 'worker' units can build"
        b, e = _own_building(world, faction, o.get("building"))
        if e:
            return e
        if b.complete:
            return f"{b.kind} {b.id} is already complete"
        u.order = {"type": "build", "bid": b.id}
        u.path = []
        return None

    if t == "attack":
        u, e = _own_unit(world, faction, o.get("unit"))
        if e:
            return e
        target = o.get("target")
        if not target and o.get("at") is not None:
            # "attack at [x,y]" where a foreign building stands means "attack
            # that building". In the first neutral benchmark green sent 12
            # mechs "at" the rival AI Center: they walked there, found no one
            # at war with them, and stood idle on the site until it was done.
            pos, e = _coord(world, o.get("at"), "at")
            if not e:
                hit = next((b for b in world.buildings.values()
                            if b.faction != faction and b.dist(pos[0], pos[1]) == 0), None)
                if hit:
                    target = hit.id
        if target:
            tgt = world.units.get(target) or world.buildings.get(target)
            if tgt is None:
                return f"target '{target}' does not exist"
            if tgt.faction == faction:
                return "you cannot attack yourself"
            if world.allied(faction, tgt.faction) and not o.get("confirm_betrayal"):
                return ("you are allied with this faction: to betray them add "
                        "\"confirm_betrayal\": true (it costs reputation and points)")
            world.declare_war(faction, tgt.faction)
            u.order = {"type": "attack", "target": target}
        else:
            pos, e = _coord(world, o.get("at"), "at")
            if e:
                return e
            u.order = {"type": "attack", "at": list(pos)}
        u.path = []
        return None

    # ---------------- buildings ----------------
    if t == "train":
        b, e = _own_building(world, faction, o.get("building"))
        if e:
            return e
        if not b.complete:
            return "building not yet complete"
        kind = o.get("unit")
        if kind not in b.stats.get("trains", []):
            return f"{b.kind} can only train {b.stats.get('trains', [])}"
        n = int(o.get("count", 1))
        if n < 1:
            return "count must be >= 1"
        if len(b.queue) + n > 10:
            return f"queue full ({len(b.queue)}/10)"
        b.queue.extend([kind] * n)
        return None

    if t == "rally":
        b, e = _own_building(world, faction, o.get("building"))
        if e:
            return e
        pos, e = _coord(world, o.get("to"), "to")
        if e:
            return e
        b.rally = pos
        return None

    # ---------------- diplomacy ----------------
    if t == "message":
        to = o.get("to", "all")
        text = str(o.get("text", ""))[:C.MESSAGE_MAX_CHARS]
        if not text:
            return "empty text"
        targets = ([n for n in world.factions if n != faction]
                   if to == "all" else [to])
        for tg in targets:
            if tg not in world.factions:
                return f"faction '{tg}' does not exist. Valid: {sorted(world.factions)}"
            world.factions[tg].inbox.append(
                {"from": faction, "tick": world.tick, "text": text})
            world.factions[tg].inbox = world.factions[tg].inbox[-C.MESSAGE_LOG_KEEP:]
        return None

    if t == "propose":
        to = o.get("to")
        if to not in world.factions or to == faction:
            return f"invalid recipient. Valid: {sorted(n for n in world.factions if n != faction)}"
        kind = o.get("treaty")
        if kind not in C.TREATY_KINDS:
            return f"unknown treaty. Valid: {list(C.TREATY_KINDS)}"
        dur = int(o.get("duration", 200))
        if not (C.MIN_TREATY_DURATION <= dur <= C.MAX_TREATY_DURATION):
            return f"duration must be between {C.MIN_TREATY_DURATION} and {C.MAX_TREATY_DURATION} ticks"
        against = o.get("against")
        if kind == "war_pact":
            if against not in world.factions or against in (faction, to) \
                    or not world.factions[against].alive:
                return ("a war_pact needs \"against\": the faction to attack together "
                        f"(not you, not {to}, still alive)")
        # The deposit is taken when the pact is signed, not now: in the first
        # coalition attempt red's war pacts were refused because it had just
        # spent its chips, although it would have had them 20 ticks later.
        pid = world._nid("p")
        world.proposals[pid] = {
            "id": pid, "from": faction, "to": to, "treaty": kind,
            "duration": dur, "text": str(o.get("text", ""))[:C.MESSAGE_MAX_CHARS],
            "tick": world.tick, "expires_tick": world.tick + 120,
        }
        if kind == "war_pact":
            world.proposals[pid]["against"] = against
            what = f"a war pact against {against}"
        else:
            what = f"{'an' if kind[0] in 'aeiou' else 'a'} {kind} pact"
        world.notify(to, f"{faction} proposes {what} ({dur} ticks) [id {pid}]")
        return None

    if t == "respond":
        pid = o.get("id")
        p = world.proposals.get(pid)
        if p is None:
            return f"proposal '{pid}' does not exist or has expired"
        if p["to"] != faction:
            return "this proposal is not addressed to you"
        del world.proposals[pid]
        if not o.get("accept"):
            world.notify(p["from"], f"{faction} declined the {p['treaty']} pact")
            return None
        a, b = p["from"], faction
        if p.get("against") and world.allied(b, p["against"]) and not o.get("confirm_betrayal"):
            world.proposals[pid] = p                 # still open: decide again
            return (f"you are allied with {p['against']}: accepting this war pact betrays "
                    f"them. Add \"confirm_betrayal\": true to accept anyway")
        err = world.sign_treaty(a, b, p["treaty"], p["duration"], against=p.get("against"))
        if err:
            world.notify(a, f"the {p['treaty']} with {b} failed: {err}")
            return err
        return None

    if t == "break_treaty":
        to = o.get("to")
        if to not in world.factions:
            return "faction does not exist"
        tr = world.active_treaty_between(faction, to)
        if tr is None:
            return "no active treaty with this faction"
        world.end_treaty(tr, betrayer=faction)
        world.log(f"{faction} unilaterally breaks the {world._kind_name(tr.kind)} pact with {to}")
        for f in world.factions:
            world.notify(f, f"{faction} broke the pact with {to}")
        return None

    if t == "declare_war":
        to = o.get("to")
        if to not in world.factions or to == faction:
            return "invalid faction"
        if world.allied(faction, to) and not o.get("confirm_betrayal"):
            return "you are allied: \"confirm_betrayal\": true is required"
        world.declare_war(faction, to)
        return None

    if t == "tribute":
        to = o.get("to")
        if to not in world.factions or to == faction:
            return "invalid faction"
        res = o.get("resources", {})
        if not isinstance(res, dict) or not res:
            return "resources must look like {\"gold\": 50}"
        src = world.factions[faction]
        for k, v in res.items():
            if k not in C.RESOURCES:
                return f"unknown resource '{k}'. Valid: {list(C.RESOURCES)}"
            if src.resources.get(k, 0) < v:
                return f"you do not have {v} {k}"
        for k, v in res.items():
            src.resources[k] -= v
            world.factions[to].resources[k] += v
        world.notify(to, f"{faction} sent you a tribute: {res}")
        return None

    return "unhandled order"
