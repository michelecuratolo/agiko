# Contributing

Agiko is a benchmark first: results are only comparable if every game is played under the
same rules and settings. Contributions are welcome in three forms.

## 1. Games of a new model

The most useful contribution. Before running a series, check the model can play:

```bash
python tools/check_provider.py neutral-openai@MODEL      # or neutral-gemini, neutral-grok,
                                                          # neutral-or (OpenRouter), --local MODEL
```

Then play seeds 1–12 with the standard settings:

```bash
tools/bench.sh neutral-openai MODEL 1 2 3 4 5 6 7 8 9 10 11 12
tools/bench.sh local MODEL 1 2 3 4 5 6 7 8 9 10 11 12     # free, with Ollama
```

Put the files in `results/<model-name>/seed-N.jsonl` and `seed-N.result.json`, run
`python tools/build_site.py`, and open a pull request saying which hardware or provider you
used and what it cost. Do not edit the replays. A model that loses more than 25% of its
replies in its first game is reported as "could not play" (see [METHOD.md](METHOD.md)).

## 2. Analysis

New measures go in `analysis/scorecard.py`, with a one-line definition in METHOD.md. Say in the
pull request whether the measure was decided before or after looking at the data: both are
fine, but they are reported differently.

## 3. The game and the code

Changes to the rules (`world.py`, `orders.py`, `observation.py`, the game constants in
`config.py`) make new games incomparable with the old ones. Open an issue first. Rule changes
are batched into a new rules version, and the version is recorded with every game.

Infrastructure changes (providers, speed, the viewer) are welcome directly. Two constraints:

- **Standard library only.** No dependencies to install; matplotlib is used only for the
  figures in `analysis/charts.py`.
- **Local models must keep working.** Anything that touches `agents.py` or `run.py` must
  pass `test_providers.py`, which plays a whole game through the `--local` path.

Run the tests before opening a pull request (they need no network and no API key):

```bash
python test_fairness.py && python test_diplomacy.py && python test_lockstep.py && python test_providers.py
```

They also run automatically on every push.

## Never commit keys

Agent files name an environment variable (`api_key_env`), never the key itself: `roster.py`
refuses a file that contains one.
