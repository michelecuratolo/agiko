"""Provider handling, without network: every call goes to a fake server.

Checks the request each kind of server receives (local Ollama, OpenAI,
OpenRouter, Anthropic), that a server rejecting a field gets a corrected
request, that reasoning and reported costs are recorded, and that a whole game
with --local still runs exactly as before.

    python test_providers.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import urllib.error

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "test")
import agents  # noqa: E402
from agents import LLMAgent  # noqa: E402

REPLY = json.dumps({"assessment": "ok", "plan": "p", "orders": []})
sent = []


def ok(extra_msg=None, usage=None):
    msg = {"content": REPLY}
    msg.update(extra_msg or {})
    return {"choices": [{"message": msg, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    assert cond, msg


def run(agent):
    return asyncio.run(agent.decide({"tick": 1}))


def fake(responder):
    def call(self, payload):
        sent.append(payload)
        return responder(self, payload)
    LLMAgent._call_sync = call


# 1. local server: exactly what the benchmark games used
fake(lambda s, p: ok({"reasoning": "think"}))
a = LLMAgent("red", model="gemma4:26b", provider="openai",
             api_base="http://localhost:11434/v1/chat/completions", verbose=False)
a.max_tokens = 12000
run(a)
p = sent[-1]
check(a.local, "localhost is recognised as local (no key needed)")
check(p.get("max_tokens") == 12000 and "max_completion_tokens" not in p, "local: max_tokens")
check(p.get("response_format") == {"type": "json_object"}, "local: asks for JSON")
check("include_reasoning" not in p and "usage" not in p, "local: no provider-specific fields")
check(a.last_reasoning == "think", "local: reasoning recorded")

# 2. OpenAI's own API
a = LLMAgent("red", model="some-model", provider="openai",
             api_base="https://api.openai.com/v1/chat/completions", api_key_env="OPENAI_API_KEY",
             verbose=False)
a.max_tokens = 8000
run(a)
p = sent[-1]
check(p.get("max_completion_tokens") == 8000 and "max_tokens" not in p, "OpenAI: max_completion_tokens")

# 3. OpenRouter: reasoning and cost come back with the reply
fake(lambda s, p: ok({"reasoning": "plan the betrayal"},
                     {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.0012}))
a = LLMAgent("red", model="x/y", provider="openai",
             api_base="https://openrouter.ai/api/v1/chat/completions",
             api_key_env="OPENROUTER_API_KEY", verbose=False)
a.max_tokens = 8000
run(a)
run(a)
p = sent[-1]
check(p.get("include_reasoning") is True and p.get("usage") == {"include": True}, "OpenRouter: asks for reasoning and cost")
check(abs(a.usage["cost_reported"] - 0.0024) < 1e-9, "OpenRouter: reported cost summed")
check(a.last_reasoning == "plan the betrayal", "OpenRouter: reasoning recorded")
import run as RUN  # noqa: E402
check(RUN._cost(a) == 0.0024, "run.py uses the provider's own cost")

# 4. a server that rejects a field: corrected once, remembered afterwards
def picky(self, payload):
    if "response_format" in payload:
        raise urllib.error.HTTPError(self.url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error": "unknown field response_format"}'))
    return ok()
fake(picky)
a = LLMAgent("red", model="m", provider="openai", api_base="https://example.org/v1/chat/completions",
             api_key_env="OPENAI_API_KEY", verbose=False)
a.max_tokens = 8000
n0 = len(sent)
run(a)
check(not a.last_errors and len(sent) - n0 == 2, "rejected field: retried without it")
run(a)
check("response_format" not in sent[-1] and len(sent) - n0 == 3, "rejected field: not sent again")

# 5. max_tokens refused by an OpenAI-style server: renamed
def wants_new_name(self, payload):
    if "max_tokens" in payload:
        raise urllib.error.HTTPError(self.url, 400, "Bad Request", {},
                                     io.BytesIO(b"Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."))
    return ok()
fake(wants_new_name)
a = LLMAgent("red", model="m", provider="openai", api_base="https://example.org/v1/chat/completions",
             api_key_env="OPENAI_API_KEY", verbose=False)
a.max_tokens = 8000
run(a)
check(not a.last_errors and sent[-1].get("max_completion_tokens") == 8000, "max_tokens renamed when refused")

# 6. a paid OpenAI-style provider without a model: clear error
try:
    LLMAgent("red", model=None, provider="openai", api_base="https://api.openai.com/v1/chat/completions",
             api_key_env="OPENAI_API_KEY", verbose=False)
    check(False, "missing model should fail")
except RuntimeError as e:
    check("no model" in str(e), "missing model: clear error")

# 7. Anthropic request unchanged
LLMAgent._call_sync = lambda self, payload: (sent.append(payload) or
                                             {"content": [{"type": "text", "text": REPLY}],
                                              "usage": {"input_tokens": 1, "output_tokens": 1}})
a = LLMAgent("red", model="claude-haiku-4-5-20251001", verbose=False)
a.max_tokens = 8000
run(a)
p = sent[-1]
check(p.get("max_tokens") == 8000 and isinstance(p.get("system"), list), "Anthropic: request unchanged")

# 8. a whole --local game, through run.py
fake(lambda s, p: ok({"reasoning": "r"}))
out = tempfile.mktemp(suffix=".jsonl")
sys.argv = ["run.py", "--agents", "red=neutral,blue=neutral", "--local", "gpt-oss:20b",
            "--no-render", "--seed", "1", "--ticks", "60", "--speed", "0.002", "--lockstep",
            "--interval", "10", "--replay", out, "--result-json", out + ".res"]
asyncio.run(asyncio.wait_for(RUN.main(), timeout=60))
res = json.load(open(out + ".res"))
decs = [json.loads(l) for l in open(out) if '"faction"' in l]
check(res["ticks"] == 60 and decs and all(d["reasoning"] == "r" for d in decs), "--local game runs and records reasoning")
check(all(p.get("max_tokens") for p in sent[-len(decs):]), "--local game used max_tokens")
print("\nAll provider tests passed.")
