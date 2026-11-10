# Setup on Mac — and free tournaments with local models

## 1. Python

macOS ships with Python, but it is often the system 3.9, which is too old.
Check in the Terminal:

```bash
python3 --version
```

If it is below 3.11, install Homebrew from <https://brew.sh> and then:

```bash
brew install python@3.12
```

## 2. The folder

Put the files in `~/agiko`, with the `.toml` files in a `roster` subfolder:

```
agiko/
├── config.py, world.py, orders.py, observation.py, agents.py,
│   roster.py, run.py, tournament.py, serve.py, db.py, theme.py, ui.html,
│   test_diplomacy.py, test_fairness.py, README.md
├── roster/
│   └── the 7 .toml files
└── replays/
    └── the demo .jsonl files
```

## 3. Free trial run

```bash
cd ~/agiko
python3 run.py --agents red=scripted,blue=scripted,green=scripted
```

The macOS Terminal supports ANSI colours natively: you will see the map right
away. `Ctrl-C` to stop.

## 4. With an API key

```bash
export ANTHROPIC_API_KEY="sk-ant-your-key"
python3 serve.py
```

To make it permanent, add it to `~/.zshrc`:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc
source ~/.zshrc
```

---

# Local models: zero-cost tournaments

With 32GB of unified memory you can run everything locally without spending
anything. But there are three real constraints to know about first.

## Constraint 1 — you do not have 32GB, you have ~21

macOS limits how much unified memory the GPU can use: roughly **66% below
36GB**, so **~21GB on a 32GB machine**. The rest goes to the system, the
runtime and the context cache. Beyond that limit the work spills over to the
CPU, which works but is much slower.

A rule of thumb: a model takes about **0.6 GB per billion parameters** at Q4
quantisation.

## Constraint 2 — the default context is too small

This would block you on the very first attempt. Ollama uses a **4K context
below 24GiB of VRAM**, and you are below it. The Agiko prompt is about
**1,900 tokens** and grows during the game. At 4K it works at the start and
begins truncating later.

Set the context before starting Ollama:

```bash
export OLLAMA_CONTEXT_LENGTH=16384
ollama serve
```

## Constraint 3 — there is one GPU, the agents queue up

This changes how you play. Ollama processes one request at a time: six local
agents do not think in parallel, they wait in line.

Doing the maths: a 26B model in this class generates about **17 tokens per
second**. A 600-token reply is **~35 seconds**. With six agents, a full round
of decisions takes **over three minutes**.

It is not a fatal flaw — orders are standing orders, so the units keep working
while the agents think. But keep it in mind:

- use `--speed 2` or more, so game time runs more slowly
- leave `--interval` high (with `--local` the default is already 30 ticks)
- with six agents prefer an **MoE** model (the `A3B`/`A4B` suffixes), much
  faster at the same quality
- in tournaments, `--local` automatically forces `--parallel 1`

Note: the **Neural Engine is not used** for this. Ollama runs on the GPU via
Metal, and speed depends on the GPU cores and the memory bandwidth.

## Which models on 32GB

**Prefer MoE models to dense ones.** You recognise them by the `A3B` / `A4B`
suffix: they have many total parameters but activate few per token. Memory
follows the total size, speed follows the active parameters.

The textbook case: `gpt-oss:20b` has 21 billion parameters but at generation
time behaves like a 3.6-billion model. It runs **faster than a dense 9B** and
scores better at the same time.

| model | memory | when |
|---|---|---|
| `gpt-oss:20b` | ~13 GB | **the default choice**, even with six agents; ~4 h per benchmark game |
| `gemma4:26b` | ~19 GB | MoE, a solid alternative; ~3 h per benchmark game |
| `qwen3.6:27b` | ~19 GB | dense, so slower (~13 tokens/s, ~8 h per benchmark game), but the most diplomatic |
| `qwen3.5:9b` | ~6 GB | only if the others give you trouble |

Benchmark settings for these: context 16,384 (`OLLAMA_CONTEXT_LENGTH`), GPU memory limit
raised with `sudo sysctl iogpu.wired_limit_mb=24576` (it resets at every restart). Avoid
`qwen3.6:35b-a3b` with the 12,000-token reply cap: with that much to keep in memory it froze a 32 GB Mac.

## A note on M6

M6 has 170 GB/s of memory bandwidth (153 on the 16GB configuration), against
the M4's 120. The memory ceiling is 32GB: it cannot be configured higher, you
would need an M5 Pro (up to 64GB and 307 GB/s).

The interesting part for Agiko is where M6 is strong. With a Neural
Accelerator in every GPU core and a doubled Neural Engine, Apple claims prompt
processing in LM Studio up to **4.8x faster than M4**.

And Agiko's profile is exactly that: **~1,900 prompt tokens in, ~500 out**.
Most of the work is in the reading, not the generation — which is precisely
the half where M6 made the leap. A normal chat, with short prompts and long
replies, makes far less use of this chip.

## Launch

```bash
brew install ollama
export OLLAMA_CONTEXT_LENGTH=16384
ollama serve                      # leave it open in one Terminal

# in another Terminal
ollama pull qwen3.5:9b
cd ~/agiko
python3 run.py --agents red=aggressor,blue=merchant,green=fortress \
               --local qwen3.5:9b --speed 2 --ticks 600
```

`--local` routes **all** agents to Ollama without touching the files: the
personalities stay the same, only who runs them changes. No key required, no
cost.

A full tournament for free:

```bash
python3 tournament.py --agents aggressor,merchant,fortress \
                      --local qwen3.5:9b --rounds 2 --ticks 600
```

## If the local model plays badly

Normal, and recoverable. In order of effectiveness:

1. **Shorten the personalities.** The paid presets are nuanced; a 9B model
   follows a numbered list of rules in priority order much better. The `local`
   preset is written that way on purpose — use it as a template.
2. **Go up in size** if you only have two or three agents in the game.
3. **Check the diary** in the Replay tab: if you see
   `your previous reply was not valid JSON` repeating, the model is too small
   for the schema. Change the model, not the prompt.
