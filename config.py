"""Every balance parameter in one place. Change them freely."""

# ---------- World ----------
MAP_W = 48
MAP_H = 48
TICK_SECONDS = 0.5          # real-time duration of one tick
MAX_TICKS = 1500            # game ends by timeout (the AI Center usually decides it earlier)
# Rotationally symmetric map: every faction sees the same landscape,
# rotated. Removes the starting-position advantage.
SYMMETRIC_MAP = True

# ---------- Resources ----------
RESOURCES = ("food", "wood", "gold")
START_RESOURCES = {"food": 200, "wood": 250, "gold": 100}

# How much a resource tile holds and how much is extracted per tick
TILE_YIELD = {
    "forest": {"res": "wood", "amount": 400, "rate": 0.9},
    "gold":   {"res": "gold", "amount": 300, "rate": 0.6},
    "berry":  {"res": "food", "amount": 350, "rate": 0.9},
}
CARRY_CAPACITY = 12          # how much a worker carries before returning to a depot

# ---------- Units ----------
# ticks_per_move: lower = faster. atk_cd: ticks between one hit and the next.
UNIT_STATS = {
    "worker":  {"hp": 45,  "atk": 3,  "ticks_per_move": 2, "atk_cd": 3, "vision": 4,
                "cost": {"food": 50},              "train_time": 16, "pop": 1},
    "soldier": {"hp": 110, "atk": 12, "ticks_per_move": 2, "atk_cd": 2, "vision": 5,
                "cost": {"food": 60, "gold": 25},  "train_time": 26, "pop": 1},
    "scout":   {"hp": 55,  "atk": 4,  "ticks_per_move": 1, "atk_cd": 3, "vision": 8,
                "cost": {"food": 45},              "train_time": 14, "pop": 1},
}

# ---------- Buildings ----------
# "size": (width, height) in tiles. The anchor is the top-left corner.
BUILDING_STATS = {
    "town_center": {"hp": 900, "cost": {"wood": 350}, "build_time": 80, "vision": 7, "size": (2, 2),
                    "trains": ["worker", "scout"], "pop_cap": 10, "dropoff": True},
    "house":      {"hp": 130, "cost": {"wood": 45},  "build_time": 14, "vision": 3, "size": (1, 1),
                   "trains": [], "pop_cap": 5},
    "farm":       {"hp": 110, "cost": {"wood": 70},  "build_time": 22, "vision": 2, "size": (1, 1),
                   "trains": [], "pop_cap": 0, "yield": ("food", 0.45)},
    "barracks":   {"hp": 450, "cost": {"wood": 180}, "build_time": 50, "vision": 5, "size": (2, 2),
                   "trains": ["soldier"], "pop_cap": 0},
    "tower":      {"hp": 320, "cost": {"wood": 110, "gold": 60}, "build_time": 34, "size": (1, 1),
                   "vision": 8, "trains": [], "pop_cap": 0, "atk": 14, "range": 3, "atk_cd": 3},
    # The final objective. Expensive, slow to build, and everyone sees who is
    # building it: deliberately designed to force coalitions.
    # Calibrated on 24/09/2026 after the first LLM games: at 1200/700/500 with
    # the old gather rates nobody reached it within 1500 ticks. Now the
    # reference bots start it around tick 720-950.
    "ai_center":  {"hp": 1200, "cost": {"wood": 600, "gold": 400, "food": 300}, "build_time": 1800,
                   "vision": 6, "size": (3, 3), "trains": [], "pop_cap": 0, "victory": True,
                   "requires": {"barracks": 1, "farm": 2, "house": 3},
                   # At most this many drones work at once: 300 ticks at best.
                   # Without a cap, 20 drones would finish it in 30 ticks and
                   # the others would get no time to react to the announcement.
                   # 100 ticks proved too short: in the first neutral benchmark
                   # green reacted after 17 ticks, but its mechs needed ~80 to
                   # cross the map and arrived as the site was finishing.
                   # 150 was still too short (25/09/2026): stopping a rival
                   # takes ~200 ticks (notice ~25, raise an army ~50-100,
                   # cross the map ~80), so racing was always the rational
                   # choice and a coalition could never work. At 300 ticks a
                   # coalition is possible but not forced.
                   "max_builders": 6},
}
BUILD_RATE = 1.0             # construction progress per worker per tick
POP_HARD_CAP = 60

# ---------- Diplomacy ----------
# Possible relations between two factions
RELATIONS = ("war", "neutral", "truce", "alliance")
DEFAULT_RELATION = "neutral"
TREATY_KINDS = ("truce", "alliance", "war_pact")
# Chips each side puts down when signing. Returned when the pact expires;
# whoever breaks the pact loses theirs to the other side. Makes trust a number.
# War pacts have no deposit: they are signed under time pressure, when chips
# have just gone into an AI Center. In the Sonnet game of 25/09/2026 a war
# pact against the leader failed at signing because blue was 24 chips short.
TREATY_DEPOSIT = {"alliance": {"gold": 100}}
MIN_TREATY_DURATION = 60     # ticks
MAX_TREATY_DURATION = 600
BETRAYAL_SCORE_PENALTY = 150 # breaking an active treaty costs points and reputation
MESSAGE_MAX_CHARS = 400
MESSAGE_LOG_KEEP = 12        # how many recent messages an agent sees

# ---------- Score ----------
SCORE_WEIGHTS = {
    "resources_gathered": 0.25,
    "units_alive": 8,
    "buildings_alive": 25,
    "kills": 12,
    "tiles_explored": 0.4,
}

# ---------- Agents ----------
DEFAULT_MODEL = "claude-sonnet-5"
# Short names usable on the command line: --agents red=neutral@haiku,...
MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "opus55": "claude-opus-5-5",
}
# USD per million tokens (input, output, cache-read multiplier), used only to
# print an estimate. From platform.claude.com/docs/en/about-claude/pricing,
# checked 26/09/2026 (Haiku also matches a real bill). Update when they change.
PRICES = {
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.10),
    "claude-opus-5": (5.0, 25.0, 0.10),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
}
AGENT_THINK_TICKS = 12       # game ticks between one LLM decision and the next
# Reasoning models spend most of their output on hidden "thinking" before
# the JSON (Sonnet 5: ~1,600 tokens per decision, of which ~175 visible); a
# low limit cut some replies before the JSON was written.
AGENT_MAX_TOKENS = 8000
LOCAL_MAX_TOKENS = 12000       # --local models (see run.py); was 4000 until 30 Sep 2026
LOCAL_TIMEOUT = 1800           # seconds per reply for local models: a dense 27B model writes ~13 tokens/s
REASONING_CHARS = 60000        # reasoning saved per decision; was 8,000 until 11 Oct 2026 (cut 50-67%
                               # of the local models' reasoning in the October games)

# Models that reason ("think") by default before answering. Their reasoning is
# billed but, by default, returned EMPTY ("display": "omitted"). With
# thinking="auto" Agiko asks for a readable summary instead, at no extra cost,
# and saves it in the replay. "off" disables thinking where the model allows
# it (Sonnet 5 does), "none" sends no thinking parameter at all.
THINKS_BY_DEFAULT = ("claude-sonnet-5", "claude-opus-5", "claude-opus-5-5",
                     "claude-fable-5", "claude-fable-5-1", "claude-mythos-5",
                     "claude-mythos-5-1")
THINKING = "auto"
