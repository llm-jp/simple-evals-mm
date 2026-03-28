"""Plot accuracy curves during training for each eval task.

Shows how model performance changes across training steps for different
training dataset variants, with optional baseline horizontal lines.
"""

import argparse
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from simple_evals_mm.visualize import EVAL_DISPLAY_NAMES, load_summaries, deduplicate_summaries

# Publication-quality defaults
plt.rcParams["font.size"] = 12
plt.rcParams["axes.labelsize"] = 13
plt.rcParams["axes.titlesize"] = 13.5
plt.rcParams["legend.fontsize"] = 13
plt.rcParams["figure.dpi"] = 300

# Distinct line styles/markers for training dataset variants
VARIANT_STYLES = [
    {"marker": "o", "linestyle": "-"},
    {"marker": "s", "linestyle": "-"},
    {"marker": "^", "linestyle": "-"},
    {"marker": "D", "linestyle": "-"},
    {"marker": "v", "linestyle": "-"},
]

# Color cycle for variants (Okabe-Ito colorblind-safe)
VARIANT_COLORS = [
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#56B4E9",  # sky blue
]

# Colors + line styles for baselines (distinct from variants)
BASELINE_COLORS = [
    "#D55E00",  # vermillion
    "#000000",  # black
    "#F0E442",  # yellow
    "#999999",  # gray
    "#882255",  # wine
]

BASELINE_LINESTYLES = [
    "--",
    ":",
    "-.",
    (0, (5, 1)),    # densely dashed
    (0, (3, 1, 1, 1)),  # densely dashdotted
]

# Human-friendly names for dataset variant suffixes
VARIANT_DISPLAY_NAMES: dict[str, str] = {
    "bcdfghijklmnopqt": "Jagle",
    "a": "FineVision",
    "abcdfghijklmnopqt": "Jagle + FineVision",
}

BASELINE_DISPLAY_NAMES: dict[str, str] = {
    "Qwen/Qwen3-VL-2B-Instruct": "Qwen3-VL-2B-Instruct",
    "OpenGVLab/InternVL3_5-2B": "InternVL3.5-2B",
    "sbintuitions/sarashina2.2-vision-3b": "Sarashina2.2-Vision-3B",
    "llm-jp/llm-jp-3-vila-14b": "LLM-jp-3-VILA-14B",
}

# Japanese eval tasks
JA_EVALS = {
    "heronbench", "javlmbench", "jdocqa", "jgraphqa", "ccocrjavqa",
    "cvqaja", "jamultiimage", "jmmmu", "mechaja", "waonbenchvqapro",
    "businessslidevqa",
    # old variants
    "heronbench_old", "javlmbench_old", "jdocqa_old", "jgraphqa_old",
    "ccocrjavqa_old", "cvqaja_old", "jamultiimage_old",
}

_STEP_RE = re.compile(r"-steps-(\d+)$")


def parse_model_variant_and_step(model_name: str) -> tuple[str, int] | None:
    """Extract (variant_prefix, step) from a model name like ...-{variant}-steps-{N}."""
    m = _STEP_RE.search(model_name)
    if not m:
        return None
    step = int(m.group(1))
    prefix = model_name[: m.start()]
    return prefix, step


def main():
    parser = argparse.ArgumentParser(
        description="Plot accuracy curves during training."
    )
    parser.add_argument(
        "--results-dir",
        default="results",
        help="Directory containing result subdirectories (default: results)",
    )
    parser.add_argument(
        "--evals",
        required=True,
        help="Comma-separated list of eval names to plot",
    )
    parser.add_argument(
        "--model-prefix",
        required=True,
        help="Common model name prefix to match training checkpoints "
        "(e.g. 'LLM-jp-VL-llmjp4_harmony-Qwen3-1.7B-siglip2-so400m-patch16-512')",
    )
    parser.add_argument(
        "--baselines",
        default=None,
        help="Comma-separated list of baseline model names to show as horizontal lines",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output file path (default: results/training_curve.png)",
    )
    parser.add_argument(
        "--variant-labels",
        default=None,
        help="Comma-separated display labels for each variant, in discovery order. "
        "E.g. 'Dataset A,Dataset AB,Dataset B'",
    )
    parser.add_argument(
        "--no-subtitle",
        action="store_true",
        help="Hide subtitles under task titles",
    )
    parser.add_argument(
        "--show-std",
        action="store_true",
        help="Show standard deviation as a shaded band around each line",
    )
    args = parser.parse_args()

    evals = [e.strip() for e in args.evals.split(",")]
    baselines = (
        [b.strip() for b in args.baselines.split(",")]
        if args.baselines
        else []
    )
    variant_labels_input = (
        [l.strip() for l in args.variant_labels.split(",")]
        if args.variant_labels
        else None
    )

    summaries = load_summaries(args.results_dir)
    summaries = deduplicate_summaries(summaries)

    # Build lookup: (eval_name, model_name) -> summary
    lookup: dict[tuple[str, str], dict] = {}
    for s in summaries:
        lookup[(s["eval_name"], s["model_name"])] = s

    # Find all training checkpoint models matching the prefix
    # Group by variant: variant_suffix -> [(step, model_name), ...]
    variants: dict[str, list[tuple[int, str]]] = {}
    for s in summaries:
        parsed = parse_model_variant_and_step(s["model_name"])
        if parsed is None:
            continue
        prefix, step = parsed
        if not prefix.startswith(args.model_prefix):
            continue
        # variant suffix is what comes after the common prefix
        suffix = prefix[len(args.model_prefix) :]
        # strip leading '-' if present
        suffix = suffix.lstrip("-")
        variants.setdefault(suffix, [])
        if (step, s["model_name"]) not in variants[suffix]:
            variants[suffix].append((step, s["model_name"]))

    # Order variants to match VARIANT_DISPLAY_NAMES key order, unknown at end
    _display_order = list(VARIANT_DISPLAY_NAMES.keys())
    variant_names = sorted(
        variants.keys(),
        key=lambda v: _display_order.index(v) if v in _display_order else len(_display_order),
    )
    for v in variant_names:
        variants[v].sort(key=lambda x: x[0])

    if not variant_names:
        print(f"No training checkpoints found matching prefix '{args.model_prefix}'")
        sys.exit(1)

    # Build variant display labels
    if variant_labels_input and len(variant_labels_input) == len(variant_names):
        variant_display = dict(zip(variant_names, variant_labels_input))
    else:
        variant_display = {
            v: VARIANT_DISPLAY_NAMES.get(v, v if v else "default")
            for v in variant_names
        }

    # Collect all steps for shared x-axis limits
    all_steps = set()
    for vname in variant_names:
        for step, _ in variants[vname]:
            all_steps.add(step)
    if all_steps:
        step_min, step_max = min(all_steps), max(all_steps)
        step_padding = (step_max - step_min) * 0.05
        shared_xlim = (step_min - step_padding, step_max + step_padding)
    else:
        shared_xlim = None

    def _plot_single_ax(ax, eval_name, lookup, variant_names, variants,
                        variant_display, baselines, args, shared_xlim,
                        is_bottom, col, nrows, row):
        """Plot a single eval's training curve on the given axis."""
        for vi, vname in enumerate(variant_names):
            style = VARIANT_STYLES[vi % len(VARIANT_STYLES)]
            color = VARIANT_COLORS[vi % len(VARIANT_COLORS)]

            steps_list = []
            scores = []
            stds = []
            for step, model_name in variants[vname]:
                s = lookup.get((eval_name, model_name))
                if s is None:
                    continue
                mean = s.get("mean_score")
                if mean is None:
                    continue
                steps_list.append(step)
                scores.append(mean * 100)
                std = s.get("std_score")
                stds.append(std * 100 if std is not None else 0.0)

            if steps_list:
                ax.plot(
                    steps_list,
                    scores,
                    label=variant_display[vname],
                    color=color,
                    markersize=5,
                    linewidth=2.5,
                    **style,
                )
                if args.show_std and any(s > 0 for s in stds):
                    lower = [sc - sd for sc, sd in zip(scores, stds)]
                    upper = [sc + sd for sc, sd in zip(scores, stds)]
                    ax.fill_between(steps_list, lower, upper, color=color, alpha=0.15)

        # Baseline horizontal lines
        for bi, bmodel in enumerate(baselines):
            bs = lookup.get((eval_name, bmodel))
            if bs is None:
                continue
            mean = (bs.get("mean_score") or 0) * 100
            bcolor = BASELINE_COLORS[bi % len(BASELINE_COLORS)]
            blinestyle = BASELINE_LINESTYLES[bi % len(BASELINE_LINESTYLES)]
            ax.axhline(
                y=mean,
                color=bcolor,
                linestyle=blinestyle,
                linewidth=2,
                alpha=0.8,
                label=BASELINE_DISPLAY_NAMES.get(bmodel, bmodel.split("/")[-1]),
            )

        ax.yaxis.set_major_locator(ticker.MultipleLocator(10))
        # Add y-axis padding so lowest values aren't flush with bottom
        y_lo, y_hi = ax.get_ylim()
        ax.set_ylim(y_lo - 1, y_hi + 1)
        if shared_xlim:
            ax.set_xlim(shared_xlim)
        ax.xaxis.set_major_formatter(
            ticker.FuncFormatter(lambda x, _: f"{int(x/1000)}k" if x >= 1000 else str(int(x)))
        )
        if is_bottom:
            ax.set_xlabel("Steps")
        else:
            ax.set_xlabel("")
            ax.set_xticklabels([])
        if col == 0:
            ax.set_ylabel("Accuracy (%)")
        else:
            ax.set_ylabel("")
        ax.grid(True, alpha=0.3)

    def _add_legends(fig, axes, baselines, legend_gap=0.04):
        """Add two-row legends (variants + baselines) below the figure.

        legend_gap controls vertical spacing between the two legend rows
        (in figure-relative coords). Increase for shorter figures.
        """
        baseline_display_set = set(
            BASELINE_DISPLAY_NAMES.get(b, b.split("/")[-1]) for b in baselines
        )
        seen = set()
        variant_handles, variant_labels = [], []
        baseline_handles, baseline_labels = [], []
        for ax_row in axes:
            for ax in ax_row:
                h, l = ax.get_legend_handles_labels()
                for hi, li in zip(h, l):
                    if li not in seen:
                        seen.add(li)
                        if li in baseline_display_set:
                            baseline_handles.append(hi)
                            baseline_labels.append(li)
                        else:
                            variant_handles.append(hi)
                            variant_labels.append(li)

        n_legend_rows = (1 if variant_handles else 0) + (1 if baseline_handles else 0)
        bottom_margin = 0.025 * n_legend_rows + 0.01

        # Place legends below the figure (y < 0 in figure coords)
        # Both legends anchored below 0; variant sits above baseline by legend_gap
        y_baseline = -0.03 - (legend_gap if baseline_handles and variant_handles else 0)
        y_variant = y_baseline + legend_gap if baseline_handles else y_baseline

        if variant_handles:
            leg1 = fig.legend(
                variant_handles, variant_labels,
                loc="lower center",
                ncol=len(variant_labels),
                frameon=True,
                fontsize=13.5,
                bbox_to_anchor=(0.5, y_variant),
                borderpad=0.3,
            )
            fig.add_artist(leg1)

        if baseline_handles:
            fig.legend(
                baseline_handles, baseline_labels,
                loc="lower center",
                ncol=len(baseline_labels),
                frameon=True,
                fontsize=13.5,
                bbox_to_anchor=(0.5, y_baseline),
                borderpad=0.3,
            )

        return bottom_margin

    def _save_fig(fig, output, bottom_margin):
        """tight_layout + save PNG and PDF."""
        plt.tight_layout(rect=[0, bottom_margin, 1, 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, dpi=300, bbox_inches="tight", facecolor="white")
        pdf_output = output.with_suffix(".pdf")
        fig.savefig(pdf_output, bbox_inches="tight", dpi=300)
        plt.close(fig)
        print(f"Saved: {output}")
        print(f"Saved: {pdf_output}")

    # === Per-task figure ===
    n_evals = len(evals)
    ncols = min(n_evals, 3)
    nrows = max(1, -(-n_evals // ncols))  # ceil division

    fig_width = 3.7 * ncols
    fig_height = 3.6 * nrows
    fig, axes = plt.subplots(nrows, ncols, figsize=(fig_width, fig_height), squeeze=False)
    fig.patch.set_facecolor("white")

    for idx, eval_name in enumerate(evals):
        row, col = divmod(idx, ncols)
        ax = axes[row][col]
        is_bottom = row == nrows - 1 or (idx + ncols >= n_evals)

        _plot_single_ax(ax, eval_name, lookup, variant_names, variants,
                        variant_display, baselines, args, shared_xlim,
                        is_bottom, col, nrows, row)

        display_name, subtitle = EVAL_DISPLAY_NAMES.get(eval_name, (eval_name, ""))
        if subtitle and not args.no_subtitle:
            ax.set_title(display_name, fontsize=13.5, fontweight="bold", loc="left", pad=14)
            ax.text(
                0.0, 1.005, subtitle, transform=ax.transAxes,
                ha="left", va="bottom", fontsize=13.5, color="gray",
            )
        else:
            ax.set_title(display_name, fontsize=13.5, fontweight="bold", loc="left", pad=8)

    # Hide unused axes
    for idx in range(n_evals, nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row][col].set_visible(False)

    bottom_margin = _add_legends(fig, axes, baselines)

    if args.output is None:
        output = Path(args.results_dir) / "training_curve.png"
    else:
        output = args.output
    _save_fig(fig, output, bottom_margin)

    # === Average figure (Avg, JA Avg, EN Avg) ===
    ja_evals = [e for e in evals if e in JA_EVALS]
    en_evals = [e for e in evals if e not in JA_EVALS]

    # Build averaged lookup for each group
    avg_groups = [("Avg", evals), ("JA Avg", ja_evals), ("EN Avg", en_evals)]
    # Only include groups that have evals
    avg_groups = [(name, group) for name, group in avg_groups if group]

    if avg_groups:
        # Create synthetic lookup for averaged scores
        avg_lookup: dict[tuple[str, str], dict] = {}
        for group_name, group_evals in avg_groups:
            # For each model (variant checkpoints + baselines), average across group_evals
            all_model_names = set()
            for s in summaries:
                all_model_names.add(s["model_name"])

            for model_name in all_model_names:
                scores_for_avg = []
                for eval_name in group_evals:
                    s = lookup.get((eval_name, model_name))
                    if s is not None and s.get("mean_score") is not None:
                        scores_for_avg.append(s["mean_score"])
                if scores_for_avg:
                    avg_mean = sum(scores_for_avg) / len(scores_for_avg)
                    avg_lookup[(group_name, model_name)] = {
                        "mean_score": avg_mean,
                        "std_score": None,
                    }

        ncols_avg = len(avg_groups)
        fig_avg, axes_avg = plt.subplots(1, ncols_avg, figsize=(3.7 * ncols_avg, 3.6), squeeze=False)
        fig_avg.patch.set_facecolor("white")

        for idx, (group_name, group_evals) in enumerate(avg_groups):
            ax = axes_avg[0][idx]
            _plot_single_ax(ax, group_name, avg_lookup, variant_names, variants,
                            variant_display, baselines, args, shared_xlim,
                            is_bottom=True, col=idx, nrows=1, row=0)
            n_tasks = len(group_evals)
            if not args.no_subtitle:
                ax.set_title(group_name, fontsize=13.5, fontweight="bold", loc="left", pad=14)
                ax.text(
                    0.0, 1.005, f"Average of {n_tasks} tasks", transform=ax.transAxes,
                    ha="left", va="bottom", fontsize=13.5, color="gray",
                )
            else:
                ax.set_title(group_name, fontsize=13.5, fontweight="bold", loc="left", pad=8)

        # Larger legend gap for short single-row figure
        bottom_margin_avg = _add_legends(fig_avg, axes_avg, baselines,
                                         legend_gap=0.12)

        avg_output = output.with_name(output.stem + "_avg" + output.suffix)
        _save_fig(fig_avg, avg_output, bottom_margin_avg)


if __name__ == "__main__":
    main()
