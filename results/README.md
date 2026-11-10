# Benchmark games

One folder per model, one file per map seed. All games: four seats running the same model,
agent `roster/neutral.toml`, `--lockstep --interval 25`, 1,500-tick limit.

| folder | model | games | rules | run on |
|---|---|---|---|---|
| `haiku-4-5/` | claude-haiku-4-5-20251001 (no extended thinking) | seeds 1–6 | v5 | 25 Sep 2026 |
| `sonnet-5/` | claude-sonnet-5 (adaptive thinking, summarized) | seeds 1–6 | v5 | 26 Sep 2026 |
| `gpt-oss-20b/` | gpt-oss:20b via Ollama, 12,000-token reply cap, reasoning recorded | seeds 1–12 | v6 | 7–9 Oct 2026 |
| `gemma4-26b/` | gemma4:26b via Ollama, 12,000-token reply cap, reasoning recorded | seeds 1–12 | v6 | 9–10 Oct 2026 |
| `qwen3.6-27b/` | qwen3.6:27b via Ollama, 12,000-token reply cap, reasoning recorded | seeds 1–12 | v6 | 1–7 Oct 2026 |
| `../results-batch1/gpt-oss-20b/` | first batch: 4,000-token reply cap (reasoning not recorded) | seeds 1–11 | v6 | 26–28 Sep 2026 |
| `../results-batch1/gemma4-26b/` | first batch: 4,000-token reply cap, reasoning recorded | seeds 1–12 | v6 | 28–29 Sep 2026 |
| `../results-excluded/qwen3.6-35b/` | qwen3.6:35b-a3b via Ollama: excluded, 68% of replies over the cap | seed 1 | v6 | 30 Sep 2026 |

**v5 vs v6.** The only difference is in what agents see: v6 lists the position and hit
points of every AI Center site in `ai_center_progress`; v5 gave the percentage there and
announced the position once, in the public log. The code in this repository is v6.

**Files.** `seed-N.jsonl` is the full replay: a `meta` line, world `snapshot` lines, and one
line per decision with the observation the agent received, its assessment, plan, orders,
errors, reasoning (Sonnet: the thinking summary returned by the API; the open models of `results/`: their reasoning, saved up to 8,000 characters), tokens and
latency. `seed-N.result.json` is the final score sheet.

**Costs.** `cost_usd` in the Sonnet result files was computed at an outdated $3/$15 per
million tokens. At the list price when the games ran ($2 input / $10 output) the six Sonnet
games cost $12.02; the six Haiku games $7.99 (at $1/$5).

`summary.json`, `metrics.json`, `scorecard.json` and `tests.json` are generated:

```bash
python analysis/summary.py --json results/summary.json
python metrics.py results/*/seed-*.jsonl --json results/metrics.json
python analysis/scorecard.py --json results/scorecard.json
python analysis/tests.py
python analysis/tests.py --vs results-batch1      # batch 1 vs batch 2, same model
```
