"""Cost and time estimate for `sem run --dry-run`, from earlier jobs on the same datasets.

For each dataset to run, the basis is the complete attempts (their meta.json usage totals) of earlier jobs found in the
estimate directories (default: the jobs dir), best first:

  same     the same model slug and effort: its cost and tokens per trial; time = trials x seconds per trial (a sample
           of its trial rows) / concurrent trials
  model    the same model at another effort: the same, flagged (efforts change the output length a lot)
  others   other models on the dataset: input tokens per trial (the prompt) and the range of their output tokens per
           trial, priced at this endpoint's catalog price (OpenRouter); no time (speeds differ by model)
  none     nothing to go by

Model cost only (the judge's is not included); time is a lower bound (retries, rate limits and a slow tail add to it).
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime

from simple_evals_mm import store as store_mod

SAMPLE_ROWS = 200  # trial rows read per job for the seconds per trial


def _duration_sample(path: str, n: int = SAMPLE_ROWS) -> list[float]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= n:
                    break
                r = json.loads(line)
                a, b = r.get("started_at"), r.get("finished_at")
                if a and b and not (r.get("exception_info") or {}).get("cause"):
                    out.append((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (OSError, ValueError):
        pass
    return out


def collect(dirs: list[str], datasets: list[str]) -> dict[str, list[dict]]:
    """{dataset: [one record per earlier job: model, effort, trials, tokens and cost per trial, seconds per trial]}
    from the complete attempts of the jobs in `dirs`."""
    out: dict[str, list[dict]] = {d: [] for d in datasets}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            st = store_mod.JobStore(d, name)
            if not st.exists():
                continue
            try:
                cfg = st.read_config()
            except (OSError, ValueError):
                continue
            for ds in datasets:
                metas = [m for k in st.attempts(ds) if (m := st.read_meta(ds, k)) and m.get("complete")]
                n = sum(m.get("n_tasks") or 0 for m in metas)
                if not n:
                    continue
                u = [m.get("usage") or {} for m in metas]
                cost = [x.get("cost_usd") for x in u]
                durs = _duration_sample(st.trials_path(ds, st.attempts(ds)[0]))
                out[ds].append({
                    "job": name, "model": cfg.get("model"), "effort": cfg.get("effort"), "trials": n,
                    "prompt_tokens": sum(x.get("prompt_tokens") or 0 for x in u) / n,
                    "completion_tokens": sum(x.get("completion_tokens") or 0 for x in u) / n,
                    "cost_usd": sum(cost) / n if cost and all(c is not None for c in cost) else None,
                    "seconds": statistics.median(durs) if durs else None})
    return out


def _price(plan_model: dict) -> dict | None:
    ep = (plan_model.get("config") or {}).get("endpoint") or {}
    p = ep.get("price_per_mtok")
    return p if p and (p.get("prompt") or p.get("completion")) else None


def estimate(plan: dict, cfg: dict, records: dict[str, list[dict]]) -> dict:
    """{datasets: {name: {basis, cost_usd, cost_range_usd, hours, from}}, cost_usd, hours} for the trials to run."""
    model, effort = cfg.get("model"), cfg.get("effort")
    conc = max(1, int(cfg.get("n_concurrent") or 1))
    price = _price(plan.get("model") or {})
    out: dict = {"datasets": {}, "cost_usd": 0.0, "hours": 0.0, "price_per_mtok": price, "n_concurrent": conc}
    unknown_cost = unknown_time = False
    for name, d in (plan.get("datasets") or {}).items():
        todo = d.get("trials_to_run") or 0
        recs = records.get(name) or []
        same = [r for r in recs if r["model"] == model and r["effort"] == effort]
        other_effort = [r for r in recs if r["model"] == model and r["effort"] != effort]
        e: dict = {"trials": todo}
        pick = same or other_effort
        if not todo:
            e["basis"] = "nothing to run"
        elif pick:
            r = max(pick, key=lambda x: x["trials"])
            e |= {"basis": "same model and effort" if same else f"same model, effort {r['effort']}", "from": r["job"],
                  "completion_tokens_per_trial": round(r["completion_tokens"])}
            if r["cost_usd"] is not None:
                e["cost_usd"] = round(r["cost_usd"] * todo, 2)
            elif price:
                e["cost_usd"] = round(todo * (r["prompt_tokens"] * price.get("prompt", 0)
                                              + r["completion_tokens"] * price.get("completion", 0)) / 1e6, 2)
            if r["seconds"] is not None:
                e["hours"] = round(todo * r["seconds"] / conc / 3600, 2)
        elif recs:
            pt = statistics.median(r["prompt_tokens"] for r in recs)
            ct = sorted(r["completion_tokens"] for r in recs)
            e |= {"basis": f"other models ({len(recs)})", "completion_tokens_range": [round(ct[0]), round(ct[-1])]}
            if price:
                lo, mid, hi = (todo * (pt * price.get("prompt", 0) + c * price.get("completion", 0)) / 1e6
                               for c in (ct[0], statistics.median(ct), ct[-1]))
                e["cost_range_usd"] = [round(lo, 2), round(hi, 2)]
                e["cost_usd"] = round(mid, 2)
        else:
            e["basis"] = "none"
        unknown_cost |= bool(todo) and "cost_usd" not in e
        unknown_time |= bool(todo) and "hours" not in e
        out["cost_usd"] += e.get("cost_usd") or 0
        out["datasets"][name] = e
    out["cost_usd"] = round(out["cost_usd"], 2)
    # one thread pool for every dataset: the times add up
    out["hours"] = round(sum(e.get("hours") or 0 for e in out["datasets"].values()), 2)
    out["incomplete"] = {"cost": unknown_cost, "time": unknown_time}
    return out


def format_lines(est: dict) -> list[str]:
    lines = []
    for name, e in est["datasets"].items():
        if not e["trials"]:
            continue
        cost = f"${e['cost_usd']:.2f}" if "cost_usd" in e else "–"
        if "cost_range_usd" in e:
            cost += f" (${e['cost_range_usd'][0]:.2f}–{e['cost_range_usd'][1]:.2f})"
        hours = f"{e['hours']:.1f} h" if "hours" in e else "–"
        lines.append(f"  - {name:18s} {e['trials']:>6d} trials  {cost:>24s}  {hours:>8s}  basis: {e['basis']}"
                     + (f" ({e['from']})" if e.get("from") else ""))
    tot_c = f"${est['cost_usd']:.2f}" + (" + unknown" if est["incomplete"]["cost"] else "")
    tot_h = f"{est['hours']:.1f} h" + (" + unknown" if est["incomplete"]["time"] else "")
    lines.append(f"  total: model cost ≈ {tot_c} (judge not included), time ≥ {tot_h} at {est['n_concurrent']} "
                 "concurrent trials (retries, rate limits and a slow tail add to it)")
    return lines
