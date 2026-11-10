"""The 'roster': agents are FILES, not code.

An agent is a .toml file in roster/ with a model and a personality.
Classes only change the prompt, never the game rules: so when an agent wins
you know the model won, not a faction bonus.

Usage:
    python run.py --list-agents          list the presets
    python run.py --new-agent rebel      create roster/rebel.toml from a template
    python run.py --pick                 interactive selection screen
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import config as C

ROSTER_DIR = Path(__file__).parent / "roster"
FACTION_SLOTS = ["red", "blue", "green", "yellow", "purple", "cyan"]

try:
    import tomllib
except ModuleNotFoundError:                          # Python < 3.11
    tomllib = None

TEMPLATE = '''# An AGIKO agent.
# It changes ONLY behaviour: rules, costs and stats stay identical for
# everyone, so comparisons stay honest.

tagline = "describe it in half a line"
model = "{model}"

# How many game ticks between one decision and the next.
# Lower = more reactive but more expensive.
think_every = 12

# Which environment variable to read the API key from.
# This way every participant pays for their own calls, and the file stays
# shareable without exposing secrets.
api_key_env = "ANTHROPIC_API_KEY"

persona = """
Describe the agent's character here, in the second person.

The things that really make a difference:
 - the priority between economy and army
 - how willing it is to trust, and to betray
 - what it does when it is behind
 - when it decides the time has come to attack
"""
'''


class AgentDef:
    """Agent definition loaded from a file."""

    def __init__(self, name, persona, model=None, think_every=None,
                 tagline="", max_tokens=None, api_key_env=None, api_base=None,
                 provider=None):
        self.name = name
        self.provider = (provider or "anthropic").lower()
        self.api_key_env = api_key_env or "ANTHROPIC_API_KEY"
        self.api_base = api_base
        self.persona = persona.strip()
        self.model = model or C.DEFAULT_MODEL
        self.think_every = int(think_every or C.AGENT_THINK_TICKS)
        self.tagline = tagline or persona.strip().splitlines()[0][:60]
        self.max_tokens = int(max_tokens or C.AGENT_MAX_TOKENS)

    @property
    def api_key(self):
        """The key ALWAYS comes from an environment variable: agent files are
        shared, keys are not."""
        if self.is_local:
            return "local"        # a local server needs no authentication
        return os.environ.get(self.api_key_env)

    @property
    def is_local(self):
        import re as _re
        return bool(self.api_base and _re.search(
            r"://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])", self.api_base))

    def __repr__(self):
        return f"<AgentDef {self.name} model={self.model}>"


def _require_toml():
    if tomllib is None:
        sys.exit("Agent files are TOML: use Python 3.11 or newer "
                 f"(you are on {sys.version_info.major}.{sys.version_info.minor}).")


def load(name: str) -> AgentDef:
    """Load roster/<name>.toml (or an explicit path)."""
    _require_toml()
    path = Path(name) if name.endswith(".toml") else ROSTER_DIR / f"{name}.toml"
    if not path.exists():
        avail = ", ".join(list_names()) or "(none)"
        sys.exit(f"Agent '{name}' not found in {ROSTER_DIR}.\nAvailable: {avail}")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    if "persona" not in data:
        sys.exit(f"{path}: the required field 'persona' is missing")
    if "api_key" in data:
        sys.exit(f"{path}: do NOT put the API key in the agent file — files "
                 f"get shared and committed.\nUse instead:\n"
                 f'  api_key_env = "ANTHROPIC_API_KEY_{path.stem.upper()}"\n'
                 f"and set that environment variable.")
    return AgentDef(name=path.stem, persona=data["persona"],
                    model=data.get("model"), think_every=data.get("think_every"),
                    tagline=data.get("tagline", ""),
                    max_tokens=data.get("max_tokens"),
                    api_key_env=data.get("api_key_env"),
                    api_base=data.get("api_base"),
                    provider=data.get("provider"))


def list_names() -> list[str]:
    if not ROSTER_DIR.exists():
        return []
    return sorted(p.stem for p in ROSTER_DIR.glob("*.toml"))


def list_all() -> list[AgentDef]:
    return [load(n) for n in list_names()]


def print_list():
    defs = list_all()
    if not defs:
        print(f"No agents in {ROSTER_DIR}.")
        return
    print(f"\nAvailable agents ({ROSTER_DIR}):\n")
    for d in defs:
        print(f"  {d.name:<16} {d.tagline}")
    print(f"\n  {'scripted':<16} deterministic bot, no API needed\n")
    print("Usage:  python run.py --agents red=aggressor,blue=merchant,green=scripted\n")


def scaffold(name: str, model: str = None) -> Path:
    ROSTER_DIR.mkdir(exist_ok=True)
    path = ROSTER_DIR / f"{name}.toml"
    if path.exists():
        sys.exit(f"{path} already exists.")
    path.write_text(TEMPLATE.format(model=model or C.DEFAULT_MODEL), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Selection screen
# --------------------------------------------------------------------------
def _ask(prompt, valid, default=None):
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        if raw in valid:
            return raw
        print(f"  Allowed values: {', '.join(map(str, valid))}")


def pick_interactive() -> list[tuple[str, str]]:
    """'Choose your character' screen. Returns [(faction, agent), ...]."""
    defs = list_all()
    options = [d.name for d in defs] + ["scripted"]

    print("\n" + "=" * 58)
    print("  AGIKO — agent selection")
    print("=" * 58 + "\n")
    for i, d in enumerate(defs, 1):
        mark = "  " if d.api_key else "!!"
        print(f"{mark}{i:>2}. {d.name:<16} {d.tagline}")
        where = "local, free" if d.is_local else f"key ${d.api_key_env}"
        print(f"      model {d.model}, every {d.think_every} ticks, {where}")
    print(f"  {len(defs) + 1:>2}. {'scripted':<16} deterministic bot (no API needed)")

    ready = {d.name: bool(d.api_key) for d in defs}
    missing = [d for d in defs if not ready[d.name]]
    if missing:
        print("\n  Agents without a configured key (only 'scripted' is usable):")
        for d in missing:
            print(f"    {d.name:<16} missing ${d.api_key_env}")
    has_key = any(ready.values())

    n = int(_ask(f"\nHow many factions? [2-{len(FACTION_SLOTS)}] (2): ",
                 [str(i) for i in range(2, len(FACTION_SLOTS) + 1)], default="2"))

    spec = []
    for slot in FACTION_SLOTS[:n]:
        choice = _ask(f"  {slot:<7} -> number or name: ",
                      options + [str(i) for i in range(1, len(options) + 1)])
        if choice.isdigit():
            choice = options[int(choice) - 1]
        if choice != "scripted" and not ready.get(choice, False):
            env = next(d.api_key_env for d in defs if d.name == choice)
            print(f"    missing ${env}, falling back to 'scripted'")
            choice = "scripted"
        spec.append((slot, choice))

    print("\nLine-up:")
    for slot, kind in spec:
        print(f"  {slot:<7} {kind}")
    print()
    return spec
