"""Local interface: create agents, launch games, watch replays.

    python serve.py            # opens http://127.0.0.1:8765 in the browser

Standard library only. Listens ONLY on localhost: API keys stay in the
process environment variables and never pass through the browser.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import roster

HERE = Path(__file__).parent
REPLAYS = HERE / "replays"
REPLAYS.mkdir(exist_ok=True)
roster.ROSTER_DIR.mkdir(exist_ok=True)
UI = HERE / "ui.html"
PORT = 8765
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# one game at a time from the interface: simple and sufficient
_game = {"proc": None, "replay": None, "started": None, "tail": []}
_lock = threading.Lock()


# --------------------------------------------------------------------------
def agent_to_dict(d: roster.AgentDef):
    return {"name": d.name, "tagline": d.tagline, "model": d.model,
            "think_every": d.think_every, "api_key_env": d.api_key_env,
            "persona": d.persona, "has_key": bool(d.api_key),
            "provider": d.provider, "api_base": d.api_base or "",
            "is_local": d.is_local}


def write_agent(data: dict) -> str:
    name = str(data.get("name", "")).strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("The name may only contain lowercase letters, digits, "
                         "hyphens and underscores (max 32).")
    if name == "scripted":
        raise ValueError("'scripted' is reserved for the reference bot.")
    persona = str(data.get("persona", "")).strip()
    if len(persona) < 40:
        raise ValueError("The personality is too short: describe at least the "
                         "economy/army priority and how it behaves with pacts.")
    if '"""' in persona:
        raise ValueError('The personality cannot contain """.')
    env = str(data.get("api_key_env", "ANTHROPIC_API_KEY")).strip() or "ANTHROPIC_API_KEY"
    if not re.match(r"^[A-Z0-9_]+$", env):
        raise ValueError("The environment variable name should look like "
                         "ANTHROPIC_API_KEY_ALICE.")
    if env.startswith("sk-") or len(env) > 64:
        raise ValueError("This field takes the NAME of the environment variable, not the key.")
    provider = str(data.get("provider", "anthropic")).strip().lower()
    if provider not in ("anthropic", "openai"):
        raise ValueError("The protocol must be 'anthropic' or 'openai'.")
    base = str(data.get("api_base", "")).strip()
    if base and not re.match(r"^https?://[A-Za-z0-9.\-:/\[\]]+$", base):
        raise ValueError("Invalid server address.")
    think = int(data.get("think_every", 12))
    think = max(3, min(60, think))
    tagline = str(data.get("tagline", "")).strip().replace('"', "'")[:80]
    model = str(data.get("model", "")).strip() or roster.C.DEFAULT_MODEL
    if not re.match(r"^[A-Za-z0-9.:_\\-]+$", model):
        raise ValueError("Invalid model name.")

    toml = (f'tagline = "{tagline}"\n'
            f'model = "{model}"\n'
            f'think_every = {think}\n'
            f'provider = "{provider}"\n'
            + (f'api_base = "{base}"\n' if base else "")
            + f'api_key_env = "{env}"\n\n'
            f'persona = """\n{persona}\n"""\n')
    (roster.ROSTER_DIR / f"{name}.toml").write_text(toml, encoding="utf-8")
    return name


def start_game(data: dict) -> dict:
    with _lock:
        if _game["proc"] and _game["proc"].poll() is None:
            raise ValueError("A game is already in progress.")
        seats = data.get("seats") or {}
        seats = {k: v for k, v in seats.items() if v}
        if not 2 <= len(seats) <= len(roster.FACTION_SLOTS):
            raise ValueError(f"Between 2 and {len(roster.FACTION_SLOTS)} factions are required.")
        names = set(roster.list_names()) | {"scripted"}
        for slot, agent in seats.items():
            if slot not in roster.FACTION_SLOTS or agent not in names:
                raise ValueError(f"Invalid faction or agent: {slot}={agent}")
            if agent != "scripted" and not roster.load(agent).api_key:
                raise ValueError(f"Agent '{agent}' has no key: "
                                 f"${roster.load(agent).api_key_env} is missing from the "
                                 f"environment serve.py was started in.")
        ticks = max(100, min(5000, int(data.get("ticks", 1500))))
        seed = int(data.get("seed", int(time.time()) % 100000))
        speed = float(data.get("speed", 0.5))
        speed = max(0.002, min(2.0, speed))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        replay = REPLAYS / f"game-{stamp}.jsonl"
        spec = ",".join(f"{s}={a}" for s, a in seats.items())
        cmd = [sys.executable, str(HERE / "run.py"), "--agents", spec,
               "--ticks", str(ticks), "--seed", str(seed), "--speed", str(speed),
               "--no-render", "--replay", str(replay),
               "--result-json", str(replay.with_suffix(".result.json"))]
        # explicit encoding: on Windows the default is cp1252 and games with
        # accented characters in the plans would crash the read
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=str(HERE))
        _game.update({"proc": proc, "replay": replay.name,
                      "started": time.time(), "tail": []})
        threading.Thread(target=_pump, args=(proc,), daemon=True).start()
        return {"replay": replay.name, "seed": seed}


def _pump(proc):
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _game["tail"] = (_game["tail"] + [line])[-30:]


def status() -> dict:
    p = _game["proc"]
    running = bool(p and p.poll() is None)
    return {"running": running, "replay": _game["replay"],
            "returncode": None if running or p is None else p.returncode,
            "tail": _game["tail"][-12:],
            "elapsed": round(time.time() - _game["started"]) if _game["started"] else 0}


def list_replays():
    out = []
    for p in sorted(REPLAYS.glob("*.jsonl"), reverse=True):
        item = {"name": p.name, "size_kb": round(p.stat().st_size / 1024),
                "when": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m %H:%M")}
        res = p.with_suffix(".result.json")
        if res.exists():
            try:
                r = json.loads(res.read_text(encoding="utf-8"))
                item["winner"] = r.get("winner")
                item["seats"] = r.get("seats")
                item["ticks"] = r.get("ticks")
            except json.JSONDecodeError:
                pass
        out.append(item)
    return out


# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                body = UI.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/agents":
                self._json(200, [agent_to_dict(d) for d in roster.list_all()])
            elif path == "/api/replays":
                self._json(200, list_replays())
            elif path == "/api/status":
                self._json(200, status())
            elif path == "/logo.svg":
                body = (HERE / "logo.svg").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/replays/"):
                name = unquote(path.split("/", 2)[2])
                f = REPLAYS / name
                if not f.is_file() or f.suffix != ".jsonl" or "/" in name:
                    return self._json(404, {"error": "replay non trovato"})
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(404, {"error": "non trovato"})
        except Exception as e:                        # noqa: BLE001
            self._json(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._read()
            if path == "/api/agents":
                name = write_agent(data)
                self._json(200, {"ok": True, "name": name})
            elif path == "/api/run":
                self._json(200, start_game(data))
            elif path == "/api/stop":
                p = _game["proc"]
                if p and p.poll() is None:
                    p.terminate()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "non trovato"})
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:                        # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/agents/"):
            name = unquote(path.rsplit("/", 1)[1])
            f = roster.ROSTER_DIR / f"{name}.toml"
            if NAME_RE.match(name) and f.exists():
                f.unlink()
                return self._json(200, {"ok": True})
        self._json(404, {"error": "non trovato"})


def main():
    if not UI.exists():
        sys.exit(f"Missing {UI}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Agiko: {url}   (Ctrl-C to quit)")
    keys = [k for k in os.environ if "API_KEY" in k]
    print("Keys visible in this environment:", ", ".join(keys) or "none")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nclosed")


if __name__ == "__main__":
    main()
