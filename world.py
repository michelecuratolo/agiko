"""Simulation engine: map, units, buildings, combat, diplomacy.

The world advances in discrete ticks. Agents do NOT control individual ticks:
they give units "standing orders", which the units carry out on their own until
new orders arrive. This is what makes real-time possible with slow LLMs.
"""
from __future__ import annotations

import heapq
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import config as C
import theme as T

BLOCKING_TERRAIN = {"water"}


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------
@dataclass
class Tile:
    terrain: str = "plain"     # plain | forest | gold | berry | water
    amount: float = 0.0        # resource remaining


@dataclass
class Unit:
    id: str
    faction: str
    kind: str
    x: int
    y: int
    hp: float
    order: dict = field(default_factory=lambda: {"type": "idle"})
    path: list = field(default_factory=list)
    path_goal: Optional[tuple] = None
    path_retry: int = 0
    carrying: float = 0.0
    carry_kind: Optional[str] = None
    move_cd: int = 0
    atk_cd: int = 0
    phase: str = "idle"        # internal state-machine state

    @property
    def stats(self):
        return C.UNIT_STATS[self.kind]


@dataclass
class Building:
    id: str
    faction: str
    kind: str
    x: int
    y: int
    hp: float
    complete: bool = False
    progress: float = 0.0
    queue: list = field(default_factory=list)   # list of unit kinds to train
    train_progress: float = 0.0
    atk_cd: int = 0
    rally: Optional[tuple] = None
    work_tick: int = -1          # for max_builders: the tick being counted
    work_done: int = 0           # drones that already worked in that tick

    @property
    def stats(self):
        return C.BUILDING_STATS[self.kind]

    @property
    def size(self):
        return self.stats.get("size", (1, 1))

    def cells(self):
        w, h = self.size
        return [(self.x + dx, self.y + dy) for dy in range(h) for dx in range(w)]

    def dist(self, x, y):
        """Chebyshev distance from the nearest tile of the footprint."""
        w, h = self.size
        dx = max(self.x - x, 0, x - (self.x + w - 1))
        dy = max(self.y - y, 0, y - (self.y + h - 1))
        return max(dx, dy)

    def nearest_cell(self, x, y):
        w, h = self.size
        return (min(max(x, self.x), self.x + w - 1), min(max(y, self.y), self.y + h - 1))


def dist_to(ent, x, y):
    """Distance from any entity (unit, or building with a footprint)."""
    if isinstance(ent, Building):
        return ent.dist(x, y)
    return max(abs(ent.x - x), abs(ent.y - y))


@dataclass
class Treaty:
    id: str
    kind: str          # truce | alliance | war_pact
    a: str
    b: str
    start: int
    expires: int
    active: bool = True
    against: Optional[str] = None                 # war_pact: the common enemy
    deposit: dict = field(default_factory=dict)   # chips held for each side

    def other(self, f):
        return self.b if f == self.a else self.a


@dataclass
class Faction:
    name: str
    color: str
    resources: dict = field(default_factory=lambda: dict(C.START_RESOURCES))
    gathered: float = 0.0
    kills: int = 0
    explored: set = field(default_factory=set)
    known_tiles: dict = field(default_factory=dict)   # (x,y) -> remembered terrain
    inbox: list = field(default_factory=list)         # received messages
    events: list = field(default_factory=list)        # recent events (private log)
    alive: bool = True
    betrayals: int = 0


# --------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------
class World:
    def __init__(self, faction_names: list[str], seed: int = 0):
        self.rng = random.Random(seed)
        self.tick = 0
        self.grid = [[Tile() for _ in range(C.MAP_W)] for _ in range(C.MAP_H)]
        self.units: dict[str, Unit] = {}
        self.buildings: dict[str, Building] = {}
        self.factions: dict[str, Faction] = {}
        self.relations: dict[tuple, str] = {}
        self.treaties: dict[str, Treaty] = {}
        self.proposals: dict[str, dict] = {}
        self.public_log: list[str] = []
        self.log_total = 0          # lines ever logged (public_log is truncated)
        self._ids = 0
        self._occ_cache = None
        self.winner: Optional[str] = None
        self.over = False

        colors = ["red", "blue", "green", "yellow", "purple", "cyan"]
        for i, n in enumerate(faction_names):
            self.factions[n] = Faction(name=n, color=colors[i % len(colors)])
        for a in faction_names:
            for b in faction_names:
                if a < b:
                    self.relations[(a, b)] = C.DEFAULT_RELATION

        self._generate_map()
        if C.SYMMETRIC_MAP:
            self._symmetrize(len(faction_names))
        self._place_starts(faction_names)

    # ---------------- generation ----------------
    def _nid(self, pre):
        self._ids += 1
        return f"{pre}{self._ids}"

    def _generate_map(self):
        r = self.rng
        # lakes
        for _ in range(max(3, int(3 * (C.MAP_W * C.MAP_H) / (24 * 24)))):
            cx, cy = r.randrange(C.MAP_W), r.randrange(C.MAP_H)
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    if dx * dx + dy * dy <= 4 and self.in_bounds(cx + dx, cy + dy):
                        self.grid[cy + dy][cx + dx].terrain = "water"
        # resource blobs, proportional to the area (14/10/8 on 24x24)
        scale = (C.MAP_W * C.MAP_H) / (24 * 24)
        for terrain, blobs, size in (("forest", int(14 * scale), 5),
                                     ("berry", int(10 * scale), 3),
                                     ("gold", int(8 * scale), 2)):
            for _ in range(blobs):
                cx, cy = r.randrange(C.MAP_W), r.randrange(C.MAP_H)
                for _ in range(size):
                    x = max(0, min(C.MAP_W - 1, cx + r.randint(-1, 1)))
                    y = max(0, min(C.MAP_H - 1, cy + r.randint(-1, 1)))
                    t = self.grid[y][x]
                    if t.terrain == "plain":
                        t.terrain = terrain
                        t.amount = C.TILE_YIELD[terrain]["amount"]

    # ---------------- symmetry ----------------
    def _rotate(self, x, y, k, n):
        """Rotate (x,y) by k/n of a turn around the map centre."""
        cx, cy = (C.MAP_W - 1) / 2, (C.MAP_H - 1) / 2
        ang = 2 * math.pi * k / n
        dx, dy = x - cx, y - cy
        rx = dx * math.cos(ang) - dy * math.sin(ang)
        ry = dx * math.sin(ang) + dy * math.cos(ang)
        return (int(round(cx + rx)), int(round(cy + ry)))

    def _canonical(self, x, y, n):
        """The representative of the orbit of (x,y): the rotated image that
        falls in the base wedge. All cells of the same orbit share exactly one,
        so they receive the same terrain."""
        cx, cy = (C.MAP_W - 1) / 2, (C.MAP_H - 1) / 2
        best, best_ang = None, 1e9
        for k in range(n):
            px, py = self._rotate(x, y, k, n)
            if not self.in_bounds(px, py):
                continue
            ang = math.atan2(py - cy, px - cx) % (2 * math.pi)
            if ang < best_ang:
                best, best_ang = (px, py), ang
        return best or (x, y)

    def _symmetrize(self, n):
        """Make the map symmetric under rotation by 1/n of a turn: every
        faction sees exactly the same landscape, rotated. Removes the
        positional advantage instead of merely averaging it out.

        A square only has 2-way and 4-way symmetry: with 3, 5 or 6 factions the
        corners cannot have a counterpart, so the playable area is reduced to
        the inscribed circle, which is symmetric for any n.
        """
        if n < 2:
            return
        if n not in (2, 4):
            cx, cy = (C.MAP_W - 1) / 2, (C.MAP_H - 1) / 2
            r = min(C.MAP_W, C.MAP_H) / 2 - 0.5
            for y in range(C.MAP_H):
                for x in range(C.MAP_W):
                    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
                        self.grid[y][x].terrain = "water"
                        self.grid[y][x].amount = 0
        src = [[(t.terrain, t.amount) for t in row] for row in self.grid]
        for y in range(C.MAP_H):
            for x in range(C.MAP_W):
                cxy = self._canonical(x, y, n)
                terrain, amount = src[cxy[1]][cxy[0]]
                self.grid[y][x].terrain = terrain
                self.grid[y][x].amount = amount

    def _place_starts(self, names):
        """Equidistant bases on a circle.

        The first base is built, the others are its ROTATED copy: computing
        them independently reintroduces rounding asymmetries exactly where
        symmetry matters most.
        """
        n = len(names)
        cx, cy = C.MAP_W / 2, C.MAP_H / 2
        radius = min(C.MAP_W, C.MAP_H) * 0.34
        x0 = max(2, min(C.MAP_W - 3, int(cx + radius)))
        y0 = max(2, min(C.MAP_H - 3, int(cy)))

        # base "stamp": what to modify, in absolute coordinates
        stamp = []
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                stamp.append((x0 + dx, y0 + dy, "plain", 0))
        for terrain, offs in (("forest", [(3, 0), (3, 1), (2, 3)]),
                              ("berry", [(-3, 0), (-3, 1)]),
                              ("gold", [(0, 3), (1, 3)])):
            for dx, dy in offs:
                stamp.append((x0 + dx, y0 + dy, terrain,
                              C.TILE_YIELD[terrain]["amount"]))
        unit_spots = [(x0 + dx, y0 + dy) for dx, dy in
                      ((1, 1), (-1, 1), (1, -1), (-1, -1))]

        for i, name in enumerate(names):
            rot = (lambda p: p) if i == 0 else (
                lambda p, k=i: self._rotate(p[0], p[1], k, n))
            for (sx, sy, terrain, amount) in stamp:
                px, py = rot((sx, sy))
                if self.in_bounds(px, py):
                    self.grid[py][px].terrain = terrain
                    self.grid[py][px].amount = amount
            # the rotated 2x2 footprint: the anchor is the top-left corner of
            # the rotated cells, so the symmetry stays exact
            w0, h0 = C.BUILDING_STATS["town_center"]["size"]
            rc = [rot((x0 + dx, y0 + dy)) for dx in range(w0) for dy in range(h0)]
            bx = max(1, min(C.MAP_W - w0 - 1, min(p[0] for p in rc)))
            by = max(1, min(C.MAP_H - h0 - 1, min(p[1] for p in rc)))
            for dx in range(w0):
                for dy in range(h0):
                    self.grid[by + dy][bx + dx].terrain = "plain"
                    self.grid[by + dy][bx + dx].amount = 0
            self.spawn_building(name, "town_center", bx, by, complete=True)
            for sp in unit_spots:
                ux, uy = rot(sp)
                self.spawn_unit(name, "worker", ux, uy)

    # ---------------- helpers ----------------
    def in_bounds(self, x, y):
        return 0 <= x < C.MAP_W and 0 <= y < C.MAP_H

    def tile(self, x, y) -> Tile:
        return self.grid[y][x]

    def walkable(self, x, y):
        if not self.in_bounds(x, y):
            return False
        if self.grid[y][x].terrain in BLOCKING_TERRAIN:
            return False
        return (x, y) not in self._occupied

    @property
    def _occupied(self):
        """Tiles covered by buildings. Cached: it is queried hundreds of
        thousands of times per tick and buildings rarely change."""
        if self._occ_cache is None:
            occ = set()
            for b in self.buildings.values():
                occ.update(b.cells())
            self._occ_cache = occ
        return self._occ_cache

    def _invalidate_occ(self):
        self._occ_cache = None

    def footprint_free(self, kind, x, y):
        """Are all tiles of the footprint free slabs inside the map?"""
        w, h = C.BUILDING_STATS[kind].get("size", (1, 1))
        occ = self._occupied
        for dx in range(w):
            for dy in range(h):
                px, py = x + dx, y + dy
                if not self.in_bounds(px, py) or (px, py) in occ:
                    return False
                if self.tile(px, py).terrain != "plain":
                    return False
        return True

    def would_seal_hub(self, faction, kind, x, y) -> bool:
        """Would placing this building cut one of the faction's hubs off from
        the rest of the map? In the first LLM game two factions walled their
        own hub in by tick 900: every drone outside could no longer deposit,
        and the economy stopped dead for the remaining 600 ticks."""
        w, h = C.BUILDING_STATS[kind].get("size", (1, 1))
        extra = {(x + dx, y + dy) for dx in range(w) for dy in range(h)}
        occ = self._occupied
        W, H = C.MAP_W, C.MAP_H

        def free(p):
            px, py = p
            return (0 <= px < W and 0 <= py < H and p not in occ and p not in extra
                    and self.grid[py][px].terrain not in BLOCKING_TERRAIN)

        need = 60      # a pocket smaller than this counts as sealed
        for b in self.buildings.values():
            if b.faction != faction or b.kind != "town_center":
                continue
            start = [(cx + dx, cy + dy) for cx, cy in b.cells()
                     for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
            start = [p for p in set(start) if free(p)]
            seen, stack = set(start), list(start)
            while stack and len(seen) < need:
                px, py = stack.pop()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                               (1, 1), (1, -1), (-1, 1), (-1, -1)):
                    n = (px + dx, py + dy)
                    if n not in seen and free(n):
                        seen.add(n)
                        stack.append(n)
            if len(seen) < need:
                return True
        return False

    def spawn_unit(self, faction, kind, x, y):
        # find the nearest free tile
        spot = self._nearest_free(x, y)
        if spot is None:
            return None
        u = Unit(id=self._nid("u"), faction=faction, kind=kind,
                 x=spot[0], y=spot[1], hp=C.UNIT_STATS[kind]["hp"])
        self.units[u.id] = u
        return u

    def _nearest_free(self, x, y, limit=6):
        for r in range(limit):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    if self.walkable(x + dx, y + dy):
                        return (x + dx, y + dy)
        return None

    def spawn_building(self, faction, kind, x, y, complete=False):
        st = C.BUILDING_STATS[kind]
        b = Building(id=self._nid("b"), faction=faction, kind=kind, x=x, y=y,
                     hp=st["hp"] if complete else st["hp"] * 0.2, complete=complete,
                     progress=st["build_time"] if complete else 0.0)
        self.buildings[b.id] = b
        self._invalidate_occ()
        return b

    def displace_units_at(self, b):
        """Move units standing on the tiles of a building just placed."""
        cells = set(b.cells())
        for u in self.units.values():
            if (u.x, u.y) in cells:
                spot = self._nearest_free(u.x, u.y)
                if spot:
                    u.x, u.y = spot
                    u.path = []

    def population(self, faction):
        return sum(1 for u in self.units.values() if u.faction == faction)

    def pop_cap(self, faction):
        cap = sum(b.stats.get("pop_cap", 0) for b in self.buildings.values()
                  if b.faction == faction and b.complete)
        return min(cap, C.POP_HARD_CAP)

    # ---------------- diplomacy ----------------
    def rel(self, a, b):
        if a == b:
            return "self"
        return self.relations[(a, b)] if a < b else self.relations[(b, a)]

    def set_rel(self, a, b, value):
        key = (a, b) if a < b else (b, a)
        self.relations[key] = value
        self._def_cache = {}

    def hostile(self, a, b):
        return a != b and self.rel(a, b) == "war"

    def allied(self, a, b):
        return a != b and self.rel(a, b) == "alliance"

    def defends_against(self, a, b):
        """Do a's turrets and idle units fire at b? Yes if a is at war with b,
        or if one of a's allies is. An alliance is a defensive bond: your
        ally's enemies are treated as yours, without declaring war."""
        cache = self.__dict__.setdefault("_def_cache", {})
        key = (a, b)
        if key not in cache:
            if a == b or self.allied(a, b):
                cache[key] = False
            else:
                cache[key] = self.hostile(a, b) or any(
                    self.allied(a, c) and self.hostile(c, b) for c in self.factions)
        return cache[key]

    def log(self, text):
        self.public_log.append(f"[t{self.tick}] {text}")
        self.public_log = self.public_log[-60:]
        self.log_total += 1

    def notify(self, faction, text):
        f = self.factions[faction]
        f.events.append(f"[t{self.tick}] {text}")
        f.events = f.events[-25:]

    def active_treaty_between(self, a, b):
        for t in self.treaties.values():
            if t.active and {t.a, t.b} == {a, b}:
                return t
        return None

    def declare_war(self, attacker, target, betrayal_allowed=True):
        if self.rel(attacker, target) == "war":
            return
        t = self.active_treaty_between(attacker, target)
        if t:
            self.end_treaty(t, betrayer=attacker)
            self.log(f"BETRAYAL: {attacker} breaks the {self._kind_name(t.kind)} pact with {target}!")
            for f in self.factions:
                self.notify(f, f"{attacker} betrayed {target} ({self._kind_name(t.kind)} pact broken)")
        else:
            self.log(f"{attacker} declares war on {target}")
            self.notify(target, f"{attacker} has declared war on you")
        self.set_rel(attacker, target, "war")

    @staticmethod
    def _kind_name(kind):
        return "war" if kind == "war_pact" else kind

    def end_treaty(self, t, betrayer=None):
        """Close a pact and settle the deposits: both back on expiry, the
        betrayer's deposit to the victim on betrayal."""
        if not t.active:
            return
        t.active = False
        victim = t.other(betrayer) if betrayer else None
        for side, dep in t.deposit.items():
            to = victim if side == betrayer else side
            for k, v in dep.items():
                self.factions[to].resources[k] = self.factions[to].resources.get(k, 0) + v
        if betrayer:
            self.factions[betrayer].betrayals += 1
            lost = t.deposit.get(betrayer)
            if lost:
                self.notify(victim, f"{betrayer}'s deposit {lost} is now yours")
        if self.rel(t.a, t.b) != "war":
            self.set_rel(t.a, t.b, "neutral")

    def sign_treaty(self, a, b, kind, duration, against=None):
        """Returns an error string, or None. Deposits are taken here."""
        dep = C.TREATY_DEPOSIT.get(kind, {})
        for f in (a, b):
            res = self.factions[f].resources
            miss = {k: round(v - res.get(k, 0)) for k, v in dep.items() if res.get(k, 0) < v}
            if miss:
                return f"{f} cannot pay the {kind} deposit {dep} (missing {miss})"
        if kind == "war_pact":
            if against not in self.factions or not self.factions[against].alive \
                    or against in (a, b):
                return f"invalid target for the war pact: {against}"
        old = self.active_treaty_between(a, b)
        if old:
            self.end_treaty(old)                     # replaced, not broken
        # Making peace with the common enemy of a war pact betrays the partner.
        if kind in ("truce", "alliance"):
            for me, other in ((a, b), (b, a)):
                for t in list(self.treaties.values()):
                    if (t.active and t.kind == "war_pact" and me in (t.a, t.b)
                            and t.against == other):
                        partner = t.other(me)
                        self.end_treaty(t, betrayer=me)
                        self.log(f"BETRAYAL: {me} breaks the war pact with {partner} "
                                 f"(separate peace with {other})!")
                        for f in self.factions:
                            self.notify(f, f"{me} betrayed {partner} (war pact broken)")
        for f in (a, b):
            for k, v in dep.items():
                self.factions[f].resources[k] -= v
        tid = self._nid("t")
        self.treaties[tid] = Treaty(id=tid, kind=kind, a=a, b=b, start=self.tick,
                                    expires=self.tick + duration, against=against,
                                    deposit={a: dict(dep), b: dict(dep)} if dep else {})
        self.set_rel(a, b, "alliance" if kind == "war_pact" else kind)
        if kind == "war_pact":
            self.log(f"{a} and {b} sign a war pact against {against} for {duration} ticks")
            for f in (a, b):
                self.declare_war(f, against)
        else:
            art = "an" if kind[0] in "aeiou" else "a"
            self.log(f"{a} and {b} sign {art} {kind} pact for {duration} ticks")
        return None

    # ---------------- pathfinding ----------------
    def find_path(self, start, goal, adjacent_ok=False):
        """A* with Chebyshev heuristic. On an open map it explores a fraction
        of the cells compared to BFS, and on 48x48 that is the difference
        between playable and not."""
        blocked = self._occupied
        W, H = C.MAP_W, C.MAP_H
        grid = self.grid
        if isinstance(goal, Building):
            # Any tile touching ANY cell of the footprint will do. Aiming at a
            # single cell was a real bug: in the first LLM game green's drones
            # sat full of materials for 500 ticks because the hub cell nearest
            # to them was boxed in by solar plants, while the other side of
            # the hub was wide open.
            bld = goal

            def done(p):
                return bld.dist(p[0], p[1]) <= 1

            def heur(x, y):
                return max(bld.dist(x, y) - 1, 0)
            goal = None
        else:
            if start == goal and not adjacent_ok:
                return []
            gx, gy = goal

            def done(p):
                return p == goal or (adjacent_ok and max(abs(p[0] - gx), abs(p[1] - gy)) <= 1)

            def heur(x, y):
                return max(abs(x - gx), abs(y - gy))

        h0 = heur(*start)
        open_heap = [(h0, 0, start)]
        g_cost = {start: 0}
        prev = {start: None}
        end = None
        while open_heap:
            f, gc, cur = heapq.heappop(open_heap)
            if gc > g_cost.get(cur, 1e9):
                continue
            if done(cur):
                end = cur
                break
            x, y = cur
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nx, ny = x + dx, y + dy
                if nx < 0 or ny < 0 or nx >= W or ny >= H:
                    continue
                if grid[ny][nx].terrain in BLOCKING_TERRAIN:
                    continue
                if (nx, ny) in blocked and (nx, ny) != goal:
                    continue
                ng = gc + 1
                if ng < g_cost.get((nx, ny), 1e9):
                    g_cost[(nx, ny)] = ng
                    prev[(nx, ny)] = cur
                    h = heur(nx, ny)
                    heapq.heappush(open_heap, (ng + h, ng, (nx, ny)))
        if end is None:
            return []
        path = []
        cur = end
        while prev[cur] is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    # ---------------- lookup ----------------
    def dropoff_for(self, unit):
        best, bd = None, 1e9
        for b in self.buildings.values():
            if b.faction != unit.faction or not b.complete:
                continue
            if not b.stats.get("dropoff"):
                continue
            d = b.dist(unit.x, unit.y)
            if d < bd:
                best, bd = b, d
        return best

    def entity_at(self, x, y):
        for b in self.buildings.values():
            if b.dist(x, y) == 0:
                return b
        for u in self.units.values():
            if (u.x, u.y) == (x, y):
                return u
        return None

    def enemies_near(self, unit, radius=1, defensive=False):
        """Enemies within radius. defensive=True also counts the enemies of
        one's allies (used by idle units returning fire, not by attacks)."""
        is_enemy = self.defends_against if defensive else self.hostile
        out = []
        for u in self.units.values():
            if is_enemy(unit.faction, u.faction) and \
               max(abs(u.x - unit.x), abs(u.y - unit.y)) <= radius:
                out.append(u)
        for b in self.buildings.values():
            if is_enemy(unit.faction, b.faction) and b.dist(unit.x, unit.y) <= radius:
                out.append(b)
        return out

    # ---------------- visibility ----------------
    def visible_tiles(self, faction):
        vis = set()
        allies = {faction} | {o for o in self.factions if self.allied(faction, o)}
        for u in self.units.values():
            if u.faction in allies:
                r = u.stats["vision"]
                for dx in range(-r, r + 1):
                    for dy in range(-r, r + 1):
                        if dx * dx + dy * dy <= r * r and self.in_bounds(u.x + dx, u.y + dy):
                            vis.add((u.x + dx, u.y + dy))
        for b in self.buildings.values():
            if b.faction in allies:
                r = b.stats["vision"]
                for (cx, cy) in b.cells():
                    for dx in range(-r, r + 1):
                        for dy in range(-r, r + 1):
                            if dx * dx + dy * dy <= r * r and self.in_bounds(cx + dx, cy + dy):
                                vis.add((cx + dx, cy + dy))
        return vis

    # ======================================================================
    # TICK
    # ======================================================================
    def step(self):
        self.tick += 1
        self._step_treaties()
        self._step_buildings()
        self._step_units()
        self._cleanup()
        self._update_vision_memory()
        self._check_end()

    def _step_treaties(self):
        for t in list(self.treaties.values()):
            if t.active and self.tick >= t.expires:
                self.end_treaty(t)
                self.log(f"The {self._kind_name(t.kind)} pact between {t.a} and {t.b} has expired")
        for pid, p in list(self.proposals.items()):
            if self.tick > p["expires_tick"]:
                del self.proposals[pid]

    def _step_buildings(self):
        for b in list(self.buildings.values()):
            if not b.complete:
                continue
            st = b.stats
            # passive yield (solar plants)
            if "yield" in st:
                res, amt = st["yield"]
                f = self.factions[b.faction]
                f.resources[res] += amt
                f.gathered += amt
            # training
            if b.queue:
                kind = b.queue[0]
                cost = C.UNIT_STATS[kind]["cost"]
                f = self.factions[b.faction]
                if b.train_progress == 0:
                    if self.population(b.faction) >= self.pop_cap(b.faction):
                        continue
                    if any(f.resources.get(k, 0) < v for k, v in cost.items()):
                        continue
                    for k, v in cost.items():
                        f.resources[k] -= v
                b.train_progress += 1
                if b.train_progress >= C.UNIT_STATS[kind]["train_time"]:
                    b.queue.pop(0)
                    b.train_progress = 0
                    u = self.spawn_unit(b.faction, kind, b.x, b.y)
                    if u and b.rally:
                        u.order = {"type": "move", "to": list(b.rally)}
            # turrets
            if st.get("atk"):
                b.atk_cd = max(0, b.atk_cd - 1)
                if b.atk_cd == 0:
                    tgt = None
                    for u in self.units.values():
                        if self.defends_against(b.faction, u.faction) and b.dist(u.x, u.y) <= st["range"]:
                            tgt = u
                            break
                    if tgt:
                        self._damage(tgt, st["atk"], b.faction)
                        b.atk_cd = st["atk_cd"]

    def _step_units(self):
        for u in list(self.units.values()):
            if u.id not in self.units:
                continue
            u.move_cd = max(0, u.move_cd - 1)
            u.atk_cd = max(0, u.atk_cd - 1)
            o = u.order
            t = o.get("type", "idle")
            if t == "gather":
                self._do_gather(u, o)
            elif t == "build":
                self._do_build(u, o)
            elif t == "attack":
                self._do_attack(u, o)
            elif t == "move":
                self._do_move_order(u, o)
            else:
                self._auto_defend(u)

    # ---- behaviours ----
    def _advance(self, u, goal, adjacent_ok=False):
        """Move one step towards goal. True once arrived."""
        at_goal = (u.x, u.y) == tuple(goal) or (
            adjacent_ok and max(abs(u.x - goal[0]), abs(u.y - goal[1])) <= 1)
        if at_goal:
            u.path = []
            return True
        if u.move_cd > 0:
            return False
        goal = tuple(goal)
        if not u.path or u.path_goal != goal:
            if u.path_goal == goal and u.path_retry > 0:
                u.path_retry -= 1          # unreachable goal: don't hammer the A*
                return False
            u.path = self.find_path((u.x, u.y), goal, adjacent_ok=adjacent_ok)
            u.path_goal = goal
            if not u.path:
                u.path_retry = 15
                return False
        nx, ny = u.path.pop(0)
        u.x, u.y = nx, ny
        u.move_cd = u.stats["ticks_per_move"]
        return (u.x, u.y) == tuple(goal) or (
            adjacent_ok and max(abs(u.x - goal[0]), abs(u.y - goal[1])) <= 1)

    def _advance_to_building(self, u, b):
        """Like _advance, but the goal is 'next to any tile of building b'."""
        if b.dist(u.x, u.y) <= 1:
            u.path = []
            return True
        if u.move_cd > 0:
            return False
        key = ("building", b.id)
        if not u.path or u.path_goal != key:
            if u.path_goal == key and u.path_retry > 0:
                u.path_retry -= 1
                return False
            u.path = self.find_path((u.x, u.y), b)
            u.path_goal = key
            if not u.path:
                u.path_retry = 15
                return False
        nx, ny = u.path.pop(0)
        if (nx, ny) in self._occupied:        # something was built on the way
            u.path = []
            return False
        u.x, u.y = nx, ny
        u.move_cd = u.stats["ticks_per_move"]
        return b.dist(u.x, u.y) <= 1

    def _do_gather(self, u, o):
        tx, ty = o["at"]
        tile = self.tile(tx, ty)
        # deposit when full
        if u.carrying >= C.CARRY_CAPACITY:
            dep = self.dropoff_for(u)
            if dep is None:
                return
            if self._advance_to_building(u, dep):
                f = self.factions[u.faction]
                f.resources[u.carry_kind] += u.carrying
                f.gathered += u.carrying
                u.carrying = 0.0
            return
        # resource exhausted: find the nearest one of the same type
        if tile.amount <= 0 or tile.terrain not in C.TILE_YIELD:
            newt = self._nearest_resource(u, o.get("res_hint"))
            if newt:
                o["at"] = list(newt)
                u.path = []
            else:
                u.order = {"type": "idle"}
            return
        if self._advance(u, (tx, ty), adjacent_ok=True):
            info = C.TILE_YIELD[tile.terrain]
            got = min(info["rate"], tile.amount, C.CARRY_CAPACITY - u.carrying)
            tile.amount -= got
            u.carrying += got
            u.carry_kind = info["res"]
            if tile.amount <= 0:
                tile.terrain = "plain"

    def _nearest_resource(self, u, res_hint=None):
        best, bd = None, 1e9
        for y in range(C.MAP_H):
            for x in range(C.MAP_W):
                t = self.grid[y][x]
                if t.terrain not in C.TILE_YIELD or t.amount <= 0:
                    continue
                if res_hint and C.TILE_YIELD[t.terrain]["res"] != res_hint:
                    continue
                d = max(abs(x - u.x), abs(y - u.y))
                if d < bd:
                    best, bd = (x, y), d
        return best

    def _do_build(self, u, o):
        bid = o.get("bid")
        b = self.buildings.get(bid)
        if b is None or b.complete:
            u.order = {"type": "idle"}
            return
        if self._advance_to_building(u, b):
            st = b.stats
            cap = st.get("max_builders")
            if cap:
                if b.work_tick != self.tick:
                    b.work_tick, b.work_done = self.tick, 0
                if b.work_done >= cap:
                    return               # site full: this drone waits its turn
                b.work_done += 1
            b.progress += C.BUILD_RATE
            # Construction ADDS hp; it must not reset it. The old line recomputed hp
            # from progress every tick, so any damage to a site being worked on
            # vanished at once: no attack on an AI Center ever left a mark (found
            # 25/09/2026, a three-faction coalition with 50 mechs failed to it).
            b.hp = min(st["hp"], b.hp + st["hp"] * 0.8 * C.BUILD_RATE / st["build_time"])
            if b.progress >= st["build_time"]:
                b.complete = True
                b.hp = min(st["hp"], b.hp)       # damage taken while building stays
                self.notify(u.faction, f"{T.building(b.kind)} completed at ({b.x},{b.y})")
                u.order = {"type": "idle"}
                if st.get("victory"):
                    self.log(f"{u.faction} has completed the {T.building(b.kind)}: VICTORY")
                    self.over = True
                    self.winner = u.faction

    def _do_attack(self, u, o):
        tgt = self.units.get(o.get("target")) or self.buildings.get(o.get("target"))
        if tgt is None:
            if "at" in o:
                if self._advance(u, tuple(o["at"]), adjacent_ok=False):
                    u.order = {"type": "idle"}
                else:
                    near = self.enemies_near(u, 1)
                    if near:
                        self._try_hit(u, near[0])
                return
            u.order = {"type": "idle"}
            return
        if self.rel(u.faction, tgt.faction) != "war":
            self.declare_war(u.faction, tgt.faction)
        if dist_to(tgt, u.x, u.y) <= 1:
            self._try_hit(u, tgt)
        else:
            if isinstance(tgt, Building):
                self._advance_to_building(u, tgt)
            else:
                self._advance(u, (tgt.x, tgt.y), adjacent_ok=True)

    def _do_move_order(self, u, o):
        if self._advance(u, tuple(o["to"])):
            u.order = {"type": "idle"}
        else:
            self._auto_defend(u)

    def _auto_defend(self, u):
        """Idle units return fire when an enemy (or an ally's enemy) is adjacent."""
        near = self.enemies_near(u, 1, defensive=True)
        if near:
            self._try_hit(u, near[0])

    def _try_hit(self, u, tgt):
        if u.atk_cd > 0:
            return
        self._damage(tgt, u.stats["atk"], u.faction)
        u.atk_cd = u.stats["atk_cd"]

    def _damage(self, tgt, amount, by):
        tgt.hp -= amount
        if tgt.hp <= 0:
            self.factions[by].kills += 1
            if isinstance(tgt, Unit):
                self.units.pop(tgt.id, None)
            else:
                self.buildings.pop(tgt.id, None)
                self._invalidate_occ()
                self.notify(tgt.faction, f"{T.building(tgt.kind)} at ({tgt.x},{tgt.y}) destroyed")
                if tgt.kind == "town_center":
                    self.log(f"{by} destroyed a hub of {tgt.faction}")
                if tgt.kind == "ai_center":
                    self.log(f"{by} destroyed the {T.building('ai_center')} of {tgt.faction}!")

    def _cleanup(self):
        for u in list(self.units.values()):
            if u.hp <= 0:
                self.units.pop(u.id, None)

    def _update_vision_memory(self):
        if self.tick % 2:            # map memory can update on alternate ticks
            return
        for name, f in self.factions.items():
            if not f.alive:
                continue
            vis = self.visible_tiles(name)
            f.explored |= vis
            for (x, y) in vis:
                t = self.grid[y][x]
                if t.terrain in C.TILE_YIELD:
                    f.known_tiles[(x, y)] = t.terrain
                elif (x, y) in f.known_tiles:
                    del f.known_tiles[(x, y)]

    def _check_end(self):
        if self.over:
            return
        for name, f in self.factions.items():
            if not f.alive:
                continue
            has_tc = any(b.faction == name and b.kind == "town_center"
                         for b in self.buildings.values())
            has_units = any(u.faction == name for u in self.units.values())
            if not has_tc and not has_units:
                f.alive = False
                self.log(f"{name} has been eliminated")
        alive = [n for n, f in self.factions.items() if f.alive]
        if len(alive) <= 1:
            self.over = True
            self.winner = alive[0] if alive else None
        elif self.tick >= C.MAX_TICKS:
            self.over = True
            self.winner = max(alive, key=self.score)

    def ai_center_status(self):
        """Who is building the final objective, where, and how far along.
        Public. The location used to appear only in the start announcement,
        which scrolls out of the public log: in the Sonnet games of 26/09/2026
        agents wrote "without knowing their base location, sending our army
        to attack is impossible"."""
        out = {}
        for b in self.buildings.values():
            if b.kind == "ai_center":
                pct = 100 if b.complete else int(100 * b.progress / b.stats["build_time"])
                if pct >= out.get(b.faction, {}).get("percent", -1):
                    out[b.faction] = {"percent": pct, "at": [b.x, b.y], "hp": round(b.hp)}
        return out

    # ---------------- score ----------------
    def score(self, faction):
        f = self.factions[faction]
        w = C.SCORE_WEIGHTS
        s = (f.gathered * w["resources_gathered"]
             + sum(1 for u in self.units.values() if u.faction == faction) * w["units_alive"]
             + sum(1 for b in self.buildings.values()
                   if b.faction == faction and b.complete) * w["buildings_alive"]
             + f.kills * w["kills"]
             + len(f.explored) * w["tiles_explored"]
             - f.betrayals * C.BETRAYAL_SCORE_PENALTY)
        return round(s, 1)
