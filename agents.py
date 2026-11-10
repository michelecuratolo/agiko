"""Agents. Two kinds:
  - LLMAgent: calls the Anthropic API or any server that speaks the OpenAI
    chat-completions dialect: OpenAI, Google Gemini, xAI Grok, OpenRouter,
    and local servers (Ollama, LM Studio, vLLM, llama.cpp). Stdlib only.
  - ScriptedAgent: deterministic bot, useful as a reference opponent and for
    testing the engine without spending tokens.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request

import config as C
import observation as OBS

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
# Ollama, LM Studio, vLLM and llama.cpp all speak the OpenAI dialect:
#   provider = "openai", api_base = "http://localhost:11434/v1/chat/completions"


class BaseAgent:
    def __init__(self, faction: str):
        self.faction = faction
        self.label = "scripted"
        self.max_tokens = C.AGENT_MAX_TOKENS
        self.usage = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
        self.last_errors: list[str] = []
        self.last_plan = ""
        self.thinking = False
        self.calls = 0

    async def decide(self, obs: dict) -> list:
        raise NotImplementedError


# --------------------------------------------------------------------------
# LLM agent
# --------------------------------------------------------------------------
class LLMAgent(BaseAgent):
    def __init__(self, faction, model=None, persona="", api_key=None,
                 verbose=True, api_key_env="ANTHROPIC_API_KEY", api_base=None,
                 provider="anthropic"):
        super().__init__(faction)
        if not model and provider != "anthropic":
            raise RuntimeError("no model given: pick one per seat, e.g. "
                               "red=neutral-openai@<model-id>")
        self.model = model or C.DEFAULT_MODEL
        self.persona = persona
        self.verbose = verbose
        self.provider = provider
        self.api_key_env = api_key_env
        self.url = api_base or (ANTHROPIC_URL if provider == "anthropic" else OPENAI_URL)
        self.local = bool(re.search(r"://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])", self.url))
        self.api_key = api_key or os.environ.get(api_key_env) or ("local" if self.local else None)
        if not self.api_key:
            raise RuntimeError(f"environment variable {api_key_env} is not set")
        self.system = OBS.system_prompt(faction, persona)
        self.history: list[dict] = []
        # request fields a server rejected once (HTTP 400): learned at run
        # time, so one agent file works with servers that differ in details
        self._param_fix: dict[str, str | None] = {}

    def _headers(self):
        if self.provider == "anthropic":
            return {"content-type": "application/json", "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01"}
        return {"content-type": "application/json",
                "Authorization": f"Bearer {self.api_key}"}

    def _payload(self, user_text: str) -> dict:
        if self.provider == "anthropic":
            body = {
                "model": self.model, "max_tokens": self.max_tokens,
                "system": [{"type": "text", "text": self.system,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user_text}],
            }
            # NB: not self.thinking — that is the "busy right now" flag used by
            # --lockstep. Sharing the name froze every lockstep game.
            mode = getattr(self, "thinking_mode", C.THINKING)
            if mode == "off":
                body["thinking"] = {"type": "disabled"}
            elif mode == "auto" and self.model in C.THINKS_BY_DEFAULT:
                # it thinks anyway: ask for the summary instead of an empty field
                body["thinking"] = {"type": "adaptive", "display": "summarized"}
            return body
        # OpenAI dialect: response_format pushes local models towards valid
        # JSON, which is their main weakness on this task
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system},
                         {"role": "user", "content": user_text}],
            "response_format": {"type": "json_object"},
        }
        # OpenAI's own API wants max_completion_tokens (its reasoning models
        # reject max_tokens); local servers and the other providers take
        # max_tokens. Local models are unaffected by everything below.
        token_field = "max_completion_tokens" if "api.openai.com" in self.url else "max_tokens"
        body[token_field] = self.max_tokens
        if "openrouter.ai" in self.url:
            # return the model's reasoning with the reply (recording only: it
            # does not switch reasoning on or change how much the model thinks)
            body["include_reasoning"] = True
            body["usage"] = {"include": True}        # report the cost of each call
        for field, replacement in self._param_fix.items():
            if field in body:
                value = body.pop(field)
                if replacement:
                    body[replacement] = value
        return body

    def _learn_from_400(self, body_text: str, payload: dict) -> bool:
        """A server rejected a field: drop or rename it and say whether to retry."""
        if self.provider == "anthropic":
            return False
        swaps = {"max_tokens": "max_completion_tokens", "max_completion_tokens": "max_tokens"}
        for field in ("response_format", "max_tokens", "max_completion_tokens",
                      "include_reasoning", "usage"):
            if field in payload and field not in self._param_fix and field in body_text:
                self._param_fix[field] = swaps.get(field)
                return True
        return False

    def _call_sync(self, payload: dict) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=self._headers(), method="POST")
        # reasoning models can write 12,000 tokens locally at 13-50 tokens/s:
        # up to about 15 minutes for one reply (config.LOCAL_TIMEOUT is 30)
        with urllib.request.urlopen(req, timeout=C.LOCAL_TIMEOUT if self.local else 300) as r:
            return json.loads(r.read().decode())

    def _extract_reply(self, data: dict) -> str:
        if self.provider == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
        choices = data.get("choices") or []
        return (choices[0].get("message", {}) or {}).get("content", "") if choices else ""

    def _record_usage(self, data: dict):
        u = data.get("usage", {}) or {}
        if self.provider == "anthropic":
            self.usage["in"] += u.get("input_tokens", 0)
            self.usage["out"] += u.get("output_tokens", 0)
            self.usage["cache_write"] += u.get("cache_creation_input_tokens", 0)
            self.usage["cache_read"] += u.get("cache_read_input_tokens", 0)
        else:
            self.usage["in"] += u.get("prompt_tokens", 0)
            self.usage["out"] += u.get("completion_tokens", 0)
            if isinstance(u.get("cost"), (int, float)):      # OpenRouter reports it
                self.usage["cost_reported"] = self.usage.get("cost_reported", 0) + u["cost"]

    async def decide(self, obs: dict):
        obs = dict(obs)
        obs["last_errors"] = self.last_errors
        obs["your_previous_plan"] = self.last_plan
        # The errors have now been shown to the agent once: clear them, or
        # they pile up turn after turn (bloating the prompt) and old order
        # rejections get mistaken for API failures.
        self.last_errors = []

        payload = self._payload(
            "Current state:\n" + json.dumps(obs, ensure_ascii=False, separators=(",", ":"))
            + "\n\nReply only with the JSON of your orders.")
        self.thinking = True
        try:
            for _attempt in range(3):
                try:
                    data = await asyncio.to_thread(self._call_sync, payload)
                    break
                except urllib.error.HTTPError as e:
                    body = e.read().decode()[:600]
                    if e.code == 400 and self._learn_from_400(body, payload):
                        payload = self._payload(payload["messages"][-1]["content"])
                        continue
                    self.last_errors = [f"HTTP {e.code}: {body[:300]}"]
                    return []
            else:
                self.last_errors = ["HTTP 400: the server rejected the request format"]
                return []
            self.calls += 1
        except urllib.error.URLError as e:
            hint = (" — is the local server up? try: ollama serve"
                    if self.local else "")
            self.last_errors = [f"network: {e.reason}{hint}"]
            return []
        except Exception as e:                       # noqa: BLE001
            self.last_errors = [f"API error: {type(e).__name__}: {e}"]
            return []
        finally:
            self.thinking = False

        self._record_usage(data)
        text = self._extract_reply(data)
        # Diagnostics: output tokens billed vs characters actually written.
        # A large gap means the model spent tokens on something not in the
        # reply (e.g. hidden reasoning), which the replay would otherwise hide.
        u = data.get("usage", {}) or {}
        self.last_out_tokens = u.get("output_tokens", u.get("completion_tokens", 0))
        self.last_reply_chars = len(text)
        self.last_block_types = sorted({b.get("type", "?") for b in data.get("content", [])
                                        if isinstance(b, dict)})
        # Some models (Sonnet 5 among them) reason in "thinking" blocks before
        # answering: in the first games that was ~90% of their output tokens.
        # Keep it: it is the best record of WHY a decision was taken.
        self.last_reasoning = "".join(
            b.get("thinking", "") for b in data.get("content", [])
            if isinstance(b, dict) and b.get("type") == "thinking")[:C.REASONING_CHARS]
        if not self.last_reasoning and self.provider != "anthropic":
            # OpenAI-style servers put it beside the reply: Ollama (gpt-oss)
            # in "reasoning", vLLM / LM Studio / DeepSeek in
            # "reasoning_content"; others inline it as <think>...</think>.
            # Only recorded, never sent back: behaviour does not change.
            ch = data.get("choices") or [{}]
            msg = ch[0].get("message", {}) or {}
            r = msg.get("reasoning") or msg.get("reasoning_content") or ""
            if not r:
                m = re.search(r"<think>(.*?)(?:</think>|$)", text, flags=re.S | re.I)
                r = m.group(1) if m else ""
            self.last_reasoning = str(r).strip()[:C.REASONING_CHARS]
        parsed = _extract_json(text)
        if not isinstance(parsed, dict):
            cut = (data.get("stop_reason") == "max_tokens" or any(
                c.get("finish_reason") == "length" for c in data.get("choices") or []))
            self.truncated = getattr(self, "truncated", 0) + cut
            self.last_errors = [
                f"your previous reply was cut off at {self.max_tokens} tokens and could "
                f"not be read: write a much shorter assessment and plan, fewer orders"
                if cut else "your previous reply was not valid JSON"]
            return []
        self.last_plan = str(parsed.get("plan", ""))[:400]
        self.last_assessment = str(parsed.get("assessment", ""))[:300]
        if self.verbose:
            print(f"  [{self.faction}] {str(parsed.get('assessment',''))[:110]}")
        return parsed.get("orders", [])


def _extract_json(text: str):
    # Local reasoning models emit <think>...</think> before the JSON: it must
    # be stripped, otherwise parsing always fails.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S | re.I)
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:                 # empty or truncated reply: no JSON at all
        return None
    depth, instr, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
        elif ch == '"':
            instr = not instr
        elif not instr:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


# --------------------------------------------------------------------------
# Scripted bot (reference opponent / testing without an API)
# --------------------------------------------------------------------------
class ScriptedAgent(BaseAgent):
    """Simple economy, then army. Accepts truces, refuses alliances if the
    other side has already betrayed."""

    def __init__(self, faction, aggression=0.5):
        super().__init__(faction)
        self.aggression = aggression

    async def decide(self, obs: dict):
        self.last_errors = []     # the bot does not read them: just start clean
        o = []
        tcs = [b for b in obs["my_buildings"] if b["kind"] == "town_center"]
        if not tcs:
            return []
        tc = tcs[0]
        res = obs["resources"]
        workers = [u for u in obs["my_units"] if u["kind"] == "worker"]
        soldiers = [u for u in obs["my_units"] if u["kind"] == "soldier"]
        barracks = [b for b in obs["my_buildings"] if b["kind"] == "barracks"]
        pop, cap = obs["population"]["current"], obs["population"]["cap"]

        # diplomacy: accept truces, alliances only with those who never betrayed
        for p in obs["incoming_proposals"]:
            ok = p["treaty"] == "truce" or obs["betrayals_by_faction"].get(p["from"], 0) == 0
            o.append({"type": "respond", "id": p["id"], "accept": ok})

        # economy
        tiles = [t for t in obs["known_resource_tiles"] if t["left"] > 0]
        idle = [u for u in workers if u["order"] in ("idle", "move")]
        for i, u in enumerate(idle):
            if not tiles:
                break
            need = min(res, key=lambda k: res[k])
            pool = [t for t in tiles if t["type"] == need] or tiles
            t = pool[i % len(pool)]
            o.append({"type": "gather", "unit": u["id"], "at": t["pos"]})

        # construction: if nobody is idle, pull a gatherer off
        free = [u for u in workers if u["order"] == "idle"] or workers[-1:]
        spot = (obs.get("buildable_spots") or [None])[0]
        big = (obs.get("buildable_spots_large") or [None])[0]
        farms = [b for b in obs["my_buildings"] if b["kind"] == "farm"]
        houses = [b for b in obs["my_buildings"] if b["kind"] == "house"]
        ai = [b for b in obs["my_buildings"] if b["kind"] == "ai_center"]
        need = C.BUILDING_STATS["ai_center"]["cost"]
        if (not ai and big and free and barracks and len(farms) >= 2 and len(houses) >= 3
                and all(res[k] >= v for k, v in need.items())):
            o.append({"type": "build", "unit": free[0]["id"], "building": "ai_center", "at": big,
                      "helpers": [u["id"] for u in workers[:5]]})
        elif ai and "building_progress" in ai[0]:
            # keep at least five drones on the objective's construction site
            on_site = [u for u in workers if u["order"] == "build"]
            for u in [u for u in workers if u["order"] != "build"][:max(0, 5 - len(on_site))]:
                o.append({"type": "assist", "unit": u["id"], "building": ai[0]["id"]})
        if pop >= cap - 1 and res["wood"] >= 45 and free and spot:
            o.append({"type": "build", "unit": free[0]["id"], "building": "house", "at": spot})
        elif not barracks and res["wood"] >= 180 and len(workers) >= 7 and free and big:
            o.append({"type": "build", "unit": free[0]["id"], "building": "barracks", "at": big})
        elif len(farms) < 3 and res["wood"] >= 300 and free and spot:
            o.append({"type": "build", "unit": free[0]["id"], "building": "farm", "at": spot})

        # training
        if len(workers) < 14 and pop < cap and not tc.get("training_queue"):
            o.append({"type": "train", "building": tc["id"], "unit": "worker", "count": 2})
        # after ten mechs stop recruiting and save for the AI Center
        for b in barracks:
            if (pop < cap and len(soldiers) < 10 and not b.get("training_queue")
                    and "building_progress" not in b):
                o.append({"type": "train", "building": b["id"], "unit": "soldier", "count": 2})

        # military
        enemies = [e for e in obs["visible_enemy_units"] if e["relation"] == "war"]
        if enemies:
            for s in soldiers:
                o.append({"type": "attack", "unit": s["id"], "target": enemies[0]["id"]})
        elif len(soldiers) >= 6 and self.aggression > 0.4:
            targets = [e for e in obs["visible_enemy_buildings"]
                       if e["relation"] in ("neutral", "war")]
            if targets:
                for s in soldiers:
                    o.append({"type": "attack", "unit": s["id"], "target": targets[0]["id"]})
        return o
