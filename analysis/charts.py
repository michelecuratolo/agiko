"""The comparison figure (needs matplotlib: pip install matplotlib).

    python analysis/scorecard.py --json results/scorecard.json
    python analysis/charts.py                 # -> docs/comparison.png
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
# categorical slots 1-5 of the reference palette, validated for adjacent bars
MODELS = [("haiku-4-5", "Claude\nHaiku 4.5", "#2a78d6"),
          ("sonnet-5", "Claude\nSonnet 5", "#eb6834"),
          ("gpt-oss-20b", "gpt-oss\n20B", "#1baf7a"),
          ("gemma4-26b", "Gemma 4\n26B", "#eda100"),
          ("qwen3.6-27b", "Qwen 3.6\n27B", "#e87ba4")]
PANELS = [("messages_per_decision", "Messages sent\nper decision", "{:.2f}", 1),
          ("pacts_per_player", "Pacts signed\nper player", "{:.2f}", 1),
          ("wars_per_player", "Wars declared\nper player", "{:.2f}", 1),
          ("peace_then_war", "Wars that followed\ntalk of peace", "{:.0f}%", 100),
          ("leader_stopped", "AI Centers\ndestroyed", "{:.0f}%", 100)]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                     "xtick.color": INK2, "ytick.color": INK2, "figure.facecolor": SURFACE,
                     "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE})


def comparison(card, games):
    fig, axs = plt.subplots(1, len(PANELS), figsize=(18, 7.4))
    for ax, (key, title, fmt, scale) in zip(axs, PANELS):
        top = 0
        for i, (m, label, col) in enumerate(MODELS):
            v = card[m][key]
            val, (lo, hi), n = v["value"], v["ci95"], v["n"]
            if val is None:
                continue
            val, lo, hi = val * scale, (lo or 0) * scale, (hi or 0) * scale
            few = n < 20
            ax.bar(i, val, width=0.66, color=col, edgecolor=SURFACE, lw=2,
                   alpha=0.35 if few else 1, hatch="//" if few else None)
            ax.plot([i, i], [lo, hi], color=INK, lw=1.2, solid_capstyle="round")
            top = max(top, hi, val)
        pad = top * 0.04 or 0.02
        for i, (m, label, col) in enumerate(MODELS):
            v = card[m][key]
            if v["value"] is None:
                continue
            y = max((v["ci95"][1] or 0) * scale, v["value"] * scale) + pad
            txt = fmt.format(v["value"] * scale) + (f"\nn={v['n']}" if v["n"] < 20 else "")
            ax.text(i, y, txt, ha="center", va="bottom", fontsize=9.5, color=INK)
        ax.set_ylim(0, (top or 1) * 1.3)
        ax.set_xticks(range(len(MODELS)), [l for _m, l, _c in MODELS], fontsize=8.5)
        ax.set_yticks([])
        ax.set_title(title, loc="left", fontsize=11.5, color=INK)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(axis="x", length=0)
    fig.suptitle("What LLMs do to win a strategy game where betrayal is allowed",
                 x=0.012, ha="left", fontsize=16, color=INK, y=0.985)
    fig.text(0.012, 0.905, "Four copies of one model per game, same map, same rules, same one-line prompt. "
             "Bars: pooled over all games; lines: 95% interval (resampling whole games); "
             "faded = fewer than 20 cases.", fontsize=10, color=INK2)
    fig.text(0.012, 0.02, "Games: " + ", ".join(f"{l.replace(chr(10), ' ')} {games[m]}" for m, l, _c in MODELS)
             + ".  Claude on rules v5; open models on v6 (which also shows where AI Center sites are), run locally with a 12,000-token reply cap.  "
             "github.com/michelecuratolo/agiko", fontsize=9, color=INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.9))
    out = os.path.join(ROOT, "docs", "comparison.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


if __name__ == "__main__":
    card = json.load(open(os.path.join(ROOT, "results", "scorecard.json")))
    games = {m: len([f for f in os.listdir(os.path.join(ROOT, "results", m)) if f.endswith(".jsonl")])
             for m, *_ in MODELS}
    print(comparison(card, games))
