#!/bin/bash
# Benchmark games, four seats of the same model, standard settings (see METHOD.md).
#
#   tools/bench.sh local gemma4:26b 1 2 3              # Ollama on this machine, free
#   tools/bench.sh api deepseek/deepseek-v4-pro 1 2    # OpenRouter (needs OPENROUTER_API_KEY)
#   tools/bench.sh neutral-openai MODEL 1 2            # any agent file in roster/ (here OpenAI)
#
# One game per seed, one after the other. Replays land in replays/.
cd "$(dirname "$0")/.." || exit 1
where=$1; model=$2; shift 2
if [ -z "$where" ] || [ -z "$model" ] || [ $# -eq 0 ]; then
  echo "usage: tools/bench.sh local|api|AGENT-FILE MODEL SEED [SEED...]"; exit 1
fi
PY=$(command -v python3.12 || command -v python3)
CAF=""; command -v caffeinate >/dev/null && CAF="caffeinate -i"     # macOS: don't sleep
for s in "$@"; do
  if [ "$where" = local ]; then
    seats="red=neutral,blue=neutral,green=neutral,yellow=neutral"
    $CAF "$PY" run.py --agents "$seats" --local "$model" --no-render --seed "$s" --lockstep --interval 25
  else
    agent="$where"; [ "$where" = api ] && agent="neutral-or"
    a="$agent@$model"
    $CAF "$PY" run.py --agents "red=$a,blue=$a,green=$a,yellow=$a" --no-render --seed "$s" --lockstep --interval 25
  fi
done
