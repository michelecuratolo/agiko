"""A full game loop with LLM agents, without network or API key.

The API call is replaced by a fake that answers after a short delay. Checks
that real-time and --lockstep games reach the end (a name clash once froze
every lockstep game at tick 0), that lockstep gives every agent the same
number of decisions whatever its latency, and that each decision is saved.

    python test_lockstep.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("ANTHROPIC_API_KEY", "test-no-network")
import agents  # noqa: E402


def fake_call(self, payload):
    time.sleep(0.15 if "sonnet" in self.model else 0.02)     # slow vs fast model
    reply = {"assessment": "ok", "plan": "test", "orders": []}
    return {"content": [{"type": "text", "text": json.dumps(reply)}],
            "usage": {"input_tokens": 10, "output_tokens": 5}, "stop_reason": "end_turn"}


agents.LLMAgent._call_sync = fake_call
import run  # noqa: E402


def game(extra, ticks=150):
    out = tempfile.mktemp(suffix=".jsonl")
    sys.argv = ["run.py", "--agents", "red=neutral@haiku,blue=neutral@sonnet",
                "--no-render", "--seed", "1", "--ticks", str(ticks), "--speed", "0.005",
                "--interval", "10", "--replay", out, "--result-json", out + ".res"] + extra
    t0 = time.time()
    asyncio.run(asyncio.wait_for(run.main(), timeout=120))
    res = json.load(open(out + ".res"))
    decs = [json.loads(l) for l in open(out) if '"faction"' in l]
    return res, decs, time.time() - t0


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    assert cond, msg


res, decs, el = game([])
check(res["ticks"] == 150, f"real-time game reaches the end ({el:.1f}s)")
res, decs, el = game(["--lockstep"])
check(res["ticks"] == 150, f"lockstep game reaches the end ({el:.1f}s)")
n = {f: v["decisions"] for f, v in res["factions"].items()}
check(abs(n["red"] - n["blue"]) <= 1, f"lockstep: same number of decisions for fast and slow model {n}")
check(all("out_tokens" in d and "reasoning" in d for d in decs), "every decision records tokens and reasoning")
print("\nAll lockstep tests passed.")
