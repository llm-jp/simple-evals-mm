"""`sem jobs watch`: progress of running jobs, read from their trial files, with early warnings.

For every job x dataset: trials written / planned (attempts x tasks), the rate over the last `window` minutes and the
time left at that rate, the model cost so far and projected to the end, and the failure causes. Warnings:

  stalled      no new trial for `stale` minutes while the dataset is incomplete (a dead process, or every slot held)
  slowdown     the recent rate is under a quarter of the dataset's rate so far (e.g. requests retried for an hour each
               hold most of the concurrent slots)
  persistent   the same task failed in two or more attempts (a request the provider rejects every time)
  failing      more than 2% of the recent trials failed

Only the trial rows are read (incrementally: each refresh parses the lines appended since the last one), never the
dataset classes or the verification, so it is cheap on a login node and works on a job another process is writing.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from simple_evals_mm import runner
from simple_evals_mm import store as store_mod

FAILING_SHARE = 0.02
SLOWDOWN = 0.25


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


@dataclass
class _Attempt:
    """One trials_a<k>.jsonl, read incrementally."""
    path: str
    pos: int = 0
    rows: list = field(default_factory=list)  # (finished_ts, task, cause, cost)

    def read(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return  # only a line being written
        for line in data[:end + 1].splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = (r.get("agent_result") or {}).get("usage") or {}
            self.rows.append((_ts(r.get("finished_at")), r.get("task_name"),
                              (r.get("exception_info") or {}).get("cause"), usage.get("cost_usd")))
        self.pos += end + 1


class Watcher:
    def __init__(self, job_dirs: list[str], window_min: float = 30, stale_min: float = 15):
        self.stores = [store_mod.JobStore.from_path(d) for d in job_dirs]
        self.window, self.stale = window_min * 60, stale_min * 60
        self.files: dict[tuple, _Attempt] = {}

    def _plan(self, st: store_mod.JobStore) -> dict[str, tuple[int | None, int | None]]:
        """{dataset: (attempts planned, tasks)} from config.json / lock.json (None when not recorded yet)."""
        cfg = st.read_config()
        try:
            lock_ds = st.read_lock().get("datasets") or {}
        except OSError:
            lock_ds = {}
        return {name: (runner.planned_attempts(cfg, name), (lock_ds.get(name) or {}).get("n_tasks"))
                for name in runner.dataset_entries(cfg)}

    def snapshot(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        for st in self.stores:
            if not st.exists():
                continue
            cfg = st.read_config()
            for name, (n_att, n_tasks) in self._plan(st).items():
                rows = []
                for k in sorted(set(st.attempts(name)) | set(range(1, (n_att or 1) + 1))):
                    key = (st.dir, name, k)
                    if key not in self.files:
                        self.files[key] = _Attempt(st.trials_path(name, k))
                    self.files[key].read()
                    rows += [(k, *r) for r in self.files[key].rows]
                out.append(self._row(st, cfg, name, n_att, n_tasks, rows, now))
        return out

    def _row(self, st, cfg, name, n_att, n_tasks, rows, now) -> dict:
        total = n_att * n_tasks if n_att and n_tasks else None
        done = len(rows)
        ts = sorted(t for _, t, *_ in rows if t)
        recent = [r for r in rows if r[1] and r[1] >= now - self.window]
        rate_recent = len(recent) / (self.window / 3600)
        span = (ts[-1] - ts[0]) if len(ts) > 1 else 0
        rate_all = (len(ts) - 1) / (span / 3600) if span > 0 else None
        causes = Counter(c for *_, c, _ in rows if c)
        cost = sum(c for *_, c in rows if c)
        left = (total - done) if total is not None else None
        eta_h = left / rate_recent if left and rate_recent > 0 else None
        failed_tasks = defaultdict(set)
        for k, _, task, cause, _ in rows:
            if cause:
                failed_tasks[task].add(k)
        persistent = sorted(t for t, ks in failed_tasks.items() if len(ks) >= 2)
        warnings = []
        complete = total is not None and done >= total
        if not complete:
            last = ts[-1] if ts else None
            if last is not None and now - last >= self.stale:
                warnings.append(f"stalled: no new trial for {(now - last) / 60:.0f} min")
            if rate_all and len(ts) >= 20 and now - ts[0] > self.window and rate_recent < SLOWDOWN * rate_all:
                warnings.append(f"slowdown: {rate_recent:.0f} trials/h in the last {self.window / 60:.0f} min "
                                f"vs {rate_all:.0f} so far")
        if persistent:
            ex = ", ".join(persistent[:3]) + (" ..." if len(persistent) > 3 else "")
            warnings.append(f"persistent: {len(persistent)} task(s) failed in 2+ attempts ({ex})")
        rf = sum(1 for r in recent if r[3])
        if len(recent) >= 20 and rf / len(recent) > FAILING_SHARE:
            warnings.append(f"failing: {rf} of the last {len(recent)} trials failed")
        return {"job": st.job_name, "model": cfg.get("model"), "effort": cfg.get("effort"), "dataset": name,
                "done": done, "total": total, "complete": complete,
                "rate_per_h": round(rate_recent, 1), "eta_h": round(eta_h, 2) if eta_h is not None else None,
                "cost_usd": round(cost, 4), "cost_projected_usd": round(cost * total / done, 2) if total and done else None,
                "causes": dict(causes), "persistent_tasks": persistent, "warnings": warnings}


def _fmt_h(h: float | None) -> str:
    if h is None:
        return "–"
    return f"{h * 60:.0f}m" if h < 1 else f"{h:.1f}h"


def format_table(rows: list[dict], show_complete: bool = False) -> str:
    shown = [r for r in rows if show_complete or not r["complete"]]
    lines = [f"{'job':34s} {'dataset':16s} {'done/total':>13s} {'rate/h':>7s} {'left':>6s} {'cost':>9s} {'→ end':>9s}  failures"]
    for r in shown:
        tot = f"{r['done']}/{r['total']}" if r["total"] else f"{r['done']}/?"
        fails = " ".join(f"{c}={n}" for c, n in sorted(r["causes"].items(), key=lambda x: -x[1])) or "–"
        proj = f"${r['cost_projected_usd']:.2f}" if r["cost_projected_usd"] is not None else "–"
        lines.append(f"{r['job'][:34]:34s} {r['dataset'][:16]:16s} {tot:>13s} {r['rate_per_h']:>7.0f} "
                     f"{_fmt_h(r['eta_h']):>6s} {'$' + format(r['cost_usd'], '.2f'):>9s} {proj:>9s}  {fails}")
    n_done = sum(1 for r in rows if r["complete"])
    lines.append(f"{len(shown)} running / incomplete, {n_done} complete"
                 f"{' (not shown; --all shows them)' if n_done and not show_complete else ''}; "
                 f"cost so far ${sum(r['cost_usd'] for r in rows):.2f}")
    warns = [(r, w) for r in rows for w in r["warnings"]]
    if warns:
        lines.append("")
        lines += [f"! {r['job']} / {r['dataset']}: {w}" for r, w in warns]
    return "\n".join(lines)


def job_dirs(names: list[str], jobs_dir: str) -> list[str]:
    """Job directories: the given names / paths, else every job of the jobs dir."""
    if names:
        return [n if os.sep in n or os.path.isdir(n) else os.path.join(jobs_dir, n) for n in names]
    if not os.path.isdir(jobs_dir):
        return []
    return [os.path.join(jobs_dir, n) for n in sorted(os.listdir(jobs_dir))
            if os.path.exists(os.path.join(jobs_dir, n, "config.json"))]
