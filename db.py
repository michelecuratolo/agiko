"""The Agiko database. SQLite, standard library, a single file.

PRINCIPLE: the DECISION is the atomic unit. Every row links a participant,
a game, a tick, the observation they saw and the orders they gave.
Whether the participant is an agent or a human is a column, not a different
schema: mixed mode is added with 'human' rows, not with tables.

Games write JSONL replays (one process each, no contention); this module
imports them into a single database. It is the right pattern for parallel
tournaments too.

    python db.py import replays/*.jsonl      # import (idempotent)
    python db.py stats                       # leaderboard and behaviours
    python db.py sql "SELECT ..."            # free-form query
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "agiko.sqlite"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Who plays. An agent is identified by name + model + persona hash: change
-- the prompt and it is a new participant, so comparisons stay honest.
CREATE TABLE IF NOT EXISTS participants (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('agent', 'human', 'scripted')),
    name        TEXT NOT NULL,
    model       TEXT,
    persona_sha TEXT,
    persona     TEXT,
    provider    TEXT,
    is_local    INTEGER,
    -- for humans (mixed mode): a pseudonym, never identifying data.
    -- Consent and deletion are the operator's responsibility.
    consent_at  TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (kind, name, model, persona_sha)
);

CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY,
    replay_file TEXT UNIQUE NOT NULL,
    -- 'agents' = agents only; 'mixed' = at least one human playing;
    -- 'humans' = humans only. Derived from the participants, not declared.
    mode        TEXT NOT NULL DEFAULT 'agents' CHECK (mode IN ('agents', 'mixed', 'humans')),
    n_humans    INTEGER NOT NULL DEFAULT 0,
    n_agents    INTEGER NOT NULL DEFAULT 0,
    seed        INTEGER,
    map_w       INTEGER, map_h INTEGER,
    symmetric   INTEGER,
    max_ticks   INTEGER,
    started_at  TEXT,
    ended_tick  INTEGER,
    winner      TEXT,
    win_type    TEXT CHECK (win_type IN ('ai_center', 'elimination', 'score', NULL)),
    n_factions  INTEGER
);

-- Who occupied which faction in which game, and how it ended for them.
CREATE TABLE IF NOT EXISTS seats (
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    faction        TEXT NOT NULL,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    seat_index     INTEGER,
    final_score    REAL, gathered REAL, kills INTEGER, betrayals INTEGER, alive INTEGER,
    ai_center_pct  INTEGER,
    PRIMARY KEY (game_id, faction)
);

-- The atomic unit. observation and orders are complete JSON.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    faction     TEXT NOT NULL,
    tick        INTEGER NOT NULL,
    thought_at  INTEGER,
    source      TEXT NOT NULL CHECK (source IN ('llm', 'human', 'scripted')),
    -- Deliberately denormalised: a mixed game also contains decisions taken
    -- among agents only, but in a context where a human is playing. To study
    -- "pure agent vs agent" both filters are needed, without a join on every
    -- query.
    game_mode   TEXT NOT NULL DEFAULT 'agents',
    vs_human    INTEGER NOT NULL DEFAULT 0,   -- at least one human was in the game
    latency_ms  INTEGER,
    observation TEXT,
    orders      TEXT NOT NULL,
    n_orders    INTEGER,
    n_errors    INTEGER,
    errors      TEXT,
    assessment  TEXT,
    plan        TEXT
);
CREATE INDEX IF NOT EXISTS ix_dec_game ON decisions(game_id, tick);
CREATE INDEX IF NOT EXISTS ix_dec_src  ON decisions(source);
CREATE INDEX IF NOT EXISTS ix_dec_mode ON decisions(game_mode, source);

-- Two views so the filter is never wrong.
CREATE VIEW IF NOT EXISTS decisions_pure AS
    SELECT * FROM decisions WHERE game_mode = 'agents' AND source = 'llm';
CREATE VIEW IF NOT EXISTS decisions_mixed AS
    SELECT * FROM decisions WHERE game_mode = 'mixed' AND source = 'llm';

-- Conversations between factions. sender_kind/recipient_kind make it possible
-- to isolate "what an agent writes to a human" from "what it writes to an agent".
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    tick           INTEGER NOT NULL,
    sender         TEXT NOT NULL,
    recipient      TEXT NOT NULL,
    sender_kind    TEXT,
    recipient_kind TEXT,
    game_mode      TEXT NOT NULL DEFAULT 'agents',
    text           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_game ON messages(game_id, tick);

-- Public events: pacts, wars, betrayals, AI Center, eliminations.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    tick    INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    actor   TEXT, target TEXT,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ev_kind ON events(kind);

-- World state every N ticks, for replays and trajectory analysis.
CREATE TABLE IF NOT EXISTS snapshots (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    tick    INTEGER NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (game_id, tick)
);
"""

EVENT_PATTERNS = [
    ("separate_peace", r"BETRAYAL: (\w+) breaks the war pact with (\w+) \(separate peace"),
    ("betrayal",       r"BETRAYAL: (\w+) breaks .* with (\w+)"),
    ("treaty_broken",  r"(\w+) unilaterally breaks .* with (\w+)"),
    ("war",            r"(\w+) declares war on (\w+)"),
    ("war_pact",       r"(\w+) and (\w+) sign a war pact against (\w+)"),
    ("treaty",         r"(\w+) and (\w+) sign an? "),
    ("treaty_expired", r"pact between (\w+) and (\w+) has expired"),
    ("ai_center_start", r"(\w+) has started building the AI Center"),
    ("ai_center_done", r"(\w+) has completed the AI Center"),
    ("ai_center_destroyed", r"(\w+) destroyed the AI Center of (\w+)"),
    ("hub_destroyed",  r"(\w+) destroyed a hub of (\w+)"),
    ("eliminated",     r"(\w+) has been eliminated"),
]


def connect(path=DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _participant(con, p: dict, persona_text=None) -> int:
    kind = p.get("kind", "scripted")
    row = (kind, p.get("name", "scripted"), p.get("model"), p.get("persona_sha"))
    cur = con.execute("SELECT id FROM participants WHERE kind=? AND name=? AND model IS ? AND persona_sha IS ?", row)
    hit = cur.fetchone()
    if hit:
        return hit[0]
    cur = con.execute(
        "INSERT INTO participants (kind, name, model, persona_sha, persona, provider, is_local) VALUES (?,?,?,?,?,?,?)",
        (*row, persona_text, p.get("provider"), int(bool(p.get("local"))) if p.get("local") is not None else None))
    return cur.lastrowid


def _classify(text: str):
    for kind, pat in EVENT_PATTERNS:
        m = re.search(pat, text)
        if m:
            g = m.groups()
            return kind, (g[0] if g else None), (g[1] if len(g) > 1 else None)
    return "other", None, None


def import_replay(con, path: str, persona_lookup=None) -> bool:
    """Import a replay. Returns False if it was already present."""
    name = Path(path).name
    if con.execute("SELECT 1 FROM games WHERE replay_file=?", (name,)).fetchone():
        return False
    meta, decs, snaps = None, [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "meta" in d:
                meta = d["meta"]
            elif "snapshot" in d:
                snaps.append(d["snapshot"])
            elif "faction" in d:
                decs.append(d)
    if not meta or not snaps:
        print(f"  {name}: incomplete replay, skipped")
        return False

    last = snaps[-1]
    factions = list(meta["agents"].keys())
    winner, win_type = None, None
    for s in snaps:
        for l in s.get("log", []):
            if "has completed the AI Center" in l:
                winner, win_type = _classify(l)[1], "ai_center"
    if winner is None:
        alive = [f for f, v in last["factions"].items() if v["alive"]]
        if len(alive) == 1:
            winner, win_type = alive[0], "elimination"
        elif alive:
            winner = max(alive, key=lambda f: last["factions"][f]["score"])
            win_type = "score"

    parts = meta.get("participants") or {f: {"kind": "scripted" if a == "scripted" else "agent", "name": a}
                                          for f, a in meta["agents"].items()}
    kinds = {f: parts.get(f, {}).get("kind", "scripted") for f in factions}
    n_humans = sum(1 for k in kinds.values() if k == "human")
    n_agents = sum(1 for k in kinds.values() if k in ("agent", "scripted"))
    mode = "humans" if n_agents == 0 and n_humans else "mixed" if n_humans else "agents"

    cur = con.execute(
        "INSERT INTO games (replay_file, mode, n_humans, n_agents, seed, map_w, map_h, symmetric, max_ticks, started_at, ended_tick, winner, win_type, n_factions)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, mode, n_humans, n_agents, meta.get("seed"), meta["map"][0], meta["map"][1],
         int(bool(meta.get("symmetric", True))), meta.get("max_ticks"), meta.get("started_at"),
         last["t"], winner, win_type, len(factions)))
    gid = cur.lastrowid
    for i, f in enumerate(factions):
        p = parts.get(f, {"kind": "scripted", "name": "scripted"})
        persona = (persona_lookup or {}).get(p.get("name"))
        pid = _participant(con, p, persona)
        v = last["factions"][f]
        con.execute(
            "INSERT INTO seats (game_id, faction, participant_id, seat_index, final_score, gathered, kills, betrayals, alive, ai_center_pct)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (gid, f, pid, i, v.get("score"), None, None, v.get("betrayals"), int(bool(v.get("alive"))), v.get("ai")))

    for d in decs:
        orders = d.get("orders") or []
        con.execute(
            "INSERT INTO decisions (game_id, faction, tick, thought_at, source, game_mode, vs_human, latency_ms, observation, orders, n_orders, n_errors, errors, assessment, plan)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, d["faction"], d["t"], d.get("thought_at"), d.get("source", "llm" if d.get("plan") else "scripted"),
             mode, int(n_humans > 0),
             d.get("latency_ms"), json.dumps(d["observation"], ensure_ascii=False) if d.get("observation") else None,
             json.dumps(orders, ensure_ascii=False), len(orders), len(d.get("errors") or []),
             json.dumps(d.get("errors") or [], ensure_ascii=False), d.get("assessment"), d.get("plan")))
        for o in orders:
            if isinstance(o, dict) and o.get("type") == "message" and o.get("text"):
                to = o.get("to", "all")
                con.execute("INSERT INTO messages (game_id, tick, sender, recipient, sender_kind, recipient_kind, game_mode, text)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (gid, d["t"], d["faction"], to, kinds.get(d["faction"]),
                             kinds.get(to) if to != "all" else "all", mode, str(o["text"])))

    for s in snaps:
        for l in s.get("log", []):
            kind, actor, target = _classify(l)
            m = re.match(r"\[t(\d+)\]", l)
            con.execute("INSERT INTO events (game_id, tick, kind, actor, target, text) VALUES (?,?,?,?,?,?)",
                        (gid, int(m.group(1)) if m else s["t"], kind, actor, target, l))
        con.execute("INSERT OR IGNORE INTO snapshots (game_id, tick, data) VALUES (?,?,?)",
                    (gid, s["t"], json.dumps(s, ensure_ascii=False)))
    con.commit()
    return True


# --------------------------------------------------------------------------
STATS = {
    "Games by mode": """
        SELECT mode, COUNT(*) AS games, SUM(n_humans) AS total_humans,
               ROUND(AVG(ended_tick)) AS avg_length FROM games GROUP BY mode""",
    "Agents: against agents only vs with humans playing": """
        SELECT p.name,
               SUM(d.game_mode = 'agents') AS pure_decisions,
               SUM(d.game_mode = 'mixed')  AS mixed_decisions,
               ROUND(AVG(CASE WHEN d.game_mode='agents' THEN d.n_orders END), 1) AS orders_pure,
               ROUND(AVG(CASE WHEN d.game_mode='mixed'  THEN d.n_orders END), 1) AS orders_mixed
        FROM decisions d JOIN seats s ON s.game_id = d.game_id AND s.faction = d.faction
        JOIN participants p ON p.id = s.participant_id
        WHERE d.source = 'llm' GROUP BY p.id""",
    "Leaderboard by agent (games, wins, average score)": """
        SELECT p.name, p.model, COUNT(*) AS games,
               SUM(g.winner = s.faction) AS wins,
               ROUND(AVG(s.final_score)) AS avg_score,
               SUM(s.betrayals) AS betrayals
        FROM seats s JOIN participants p ON p.id = s.participant_id JOIN games g ON g.id = s.game_id
        GROUP BY p.id ORDER BY wins DESC, avg_score DESC""",
    "Betrayals: against agents or against humans": """
        SELECT e.kind, m.recipient_kind AS against, COUNT(*) AS n FROM events e
        LEFT JOIN messages m ON m.game_id = e.game_id AND m.sender = e.actor
        WHERE e.kind = 'betrayal' GROUP BY e.kind, m.recipient_kind""",
    "How games end": """
        SELECT win_type, COUNT(*) AS n, ROUND(AVG(ended_tick)) AS avg_tick FROM games GROUP BY win_type""",
    "Diplomatic events": """
        SELECT kind, COUNT(*) AS n, ROUND(AVG(tick)) AS avg_tick FROM events
        WHERE kind != 'other' GROUP BY kind ORDER BY n DESC""",
    "Who betrays, and how often they win when they do": """
        SELECT p.name, COUNT(DISTINCT e.game_id) AS games_with_betrayal,
               SUM(g.winner = e.actor) AS won
        FROM events e JOIN games g ON g.id = e.game_id
        JOIN seats s ON s.game_id = e.game_id AND s.faction = e.actor
        JOIN participants p ON p.id = s.participant_id
        WHERE e.kind = 'betrayal' GROUP BY p.id""",
    "Decision quality (rejected orders) by agent": """
        SELECT p.name, COUNT(*) AS decisions, ROUND(100.0 * SUM(d.n_errors > 0) / COUNT(*), 1) AS pct_with_errors,
               ROUND(AVG(d.latency_ms)) AS latency_ms
        FROM decisions d JOIN seats s ON s.game_id = d.game_id AND s.faction = d.faction
        JOIN participants p ON p.id = s.participant_id
        WHERE d.source = 'llm' GROUP BY p.id""",
    "Messages: who writes to whom": """
        SELECT game_mode, sender_kind, recipient_kind, COUNT(*) AS n,
               ROUND(AVG(LENGTH(text))) AS avg_length
        FROM messages GROUP BY game_mode, sender_kind, recipient_kind ORDER BY n DESC""",
    "Messages exchanged per game": """
        SELECT g.replay_file, COUNT(m.id) AS messages FROM games g LEFT JOIN messages m ON m.game_id = g.id
        GROUP BY g.id ORDER BY messages DESC LIMIT 10""",
}


def _print(cur):
    rows = cur.fetchall()
    if not rows:
        print("  (no rows)")
        return
    cols = [c[0] for c in cur.description]
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  " + "  ".join(str(v if v is not None else "-").ljust(w) for v, w in zip(r, widths)))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    con = connect()
    if cmd == "import":
        try:
            import roster
            lookup = {n: roster.load(n).persona for n in roster.list_names()}
        except Exception:                            # noqa: BLE001
            lookup = {}
        files = [f for pat in sys.argv[2:] for f in glob.glob(pat)]
        n = sum(1 for f in files if import_replay(con, f, lookup))
        print(f"Imported {n} new replays out of {len(files)}. Database: {DB_PATH}")
    elif cmd == "stats":
        for title, sql in STATS.items():
            print(f"\n{title}")
            _print(con.execute(sql))
        print()
    elif cmd == "sql":
        _print(con.execute(" ".join(sys.argv[2:])))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
