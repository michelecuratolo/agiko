"""Check that a model and provider can play, with ONE real decision.

Builds the opening position of a real game, sends it to the model exactly as a
game would, and reports whether the reply could be read, how long it took,
how many tokens it used, whether reasoning came back, and the cost when known.
It costs one call: a few cents at most with a paid API, nothing locally.

    python tools/check_provider.py neutral@haiku
    python tools/check_provider.py neutral-openai@MODEL         # needs OPENAI_API_KEY
    python tools/check_provider.py neutral-gemini@MODEL         # needs GEMINI_API_KEY
    python tools/check_provider.py neutral-grok@MODEL           # needs XAI_API_KEY
    python tools/check_provider.py neutral-or@deepseek/deepseek-v4-pro   # needs OPENROUTER_API_KEY
    python tools/check_provider.py --local gemma4:26b           # Ollama, free
"""
import argparse
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import config as C              # noqa: E402
import observation as OBS       # noqa: E402
import roster                   # noqa: E402
from agents import LLMAgent     # noqa: E402
from world import World         # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agent", nargs="?", default="neutral", help="agent file and model: neutral-openai@MODEL")
    ap.add_argument("--local", metavar="MODEL", help="use this model on the local Ollama server")
    ap.add_argument("--local-url", default="http://localhost:11434/v1/chat/completions")
    args = ap.parse_args()

    name, _, seat_model = args.agent.partition("@")
    d = roster.load(name)
    model = args.local or C.MODEL_ALIASES.get(seat_model, seat_model) or d.model
    try:
        agent = LLMAgent("red", model=model, persona=d.persona, verbose=False,
                         api_key_env=d.api_key_env,
                         api_base=args.local_url if args.local else d.api_base,
                         provider="openai" if args.local else d.provider)
    except RuntimeError as e:
        sys.exit(f"cannot create the agent: {e}\n(set {d.api_key_env}, or pick a model with @MODEL)")
    agent.max_tokens = C.LOCAL_MAX_TOKENS if args.local else d.max_tokens
    agent.thinking_mode = C.THINKING

    world = World(["red", "blue", "green", "yellow"], seed=1)
    obs = OBS.compact(OBS.build_observation(world, "red"))
    print(f"model    {agent.model}\nendpoint {agent.url}\nmax      {agent.max_tokens} tokens"
          f"{'  (local)' if agent.local else ''}\nasking for one decision…")
    t0 = time.monotonic()
    orders = asyncio.run(agent.decide(obs))
    dt = time.monotonic() - t0

    ok = not agent.last_errors
    print(f"\ntime     {dt:.1f} s")
    print(f"tokens   in {agent.usage['in']}  out {agent.usage['out']}")
    if agent.usage.get("cost_reported") is not None:
        print(f"cost     ${agent.usage['cost_reported']:.5f} (reported by the provider)")
    elif not agent.local and model in C.PRICES:
        p = C.PRICES[model]
        print(f"cost     ~${(agent.usage['in'] * p[0] + agent.usage['out'] * p[1]) / 1e6:.5f} (config.PRICES)")
    print(f"reasoning {'yes, ' + str(len(agent.last_reasoning)) + ' characters' if getattr(agent, 'last_reasoning', '') else 'not returned'}")
    if getattr(agent, "_param_fix", None):
        print(f"adapted  the server rejected {sorted(agent._param_fix)}: adjusted automatically")
    if ok:
        print(f"orders   {len(orders)} valid-looking orders, plan: {agent.last_plan[:120]!r}")
        print("\nOK: this model can play. Full game:\n"
              f"  python run.py --agents red={args.agent},blue={args.agent},green={args.agent},yellow={args.agent} "
              f"--no-render --seed 1 --lockstep --interval 25" + (f" --local {args.local}" if args.local else ""))
    else:
        print(f"\nPROBLEM: {agent.last_errors[0][:400]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
