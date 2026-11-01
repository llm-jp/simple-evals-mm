"""Revision history of run result files (runs/<id>/<task>/history.jsonl, written by runs.track()).

    uv run python scripts/runs_history.py show --run <run_id> --task <task>      # every recorded change
    uv run python scripts/runs_history.py verify --run <id>[,<id>...]            # files vs their last recorded hash
    uv run python scripts/runs_history.py baseline --run <id>[,<id>...]          # start histories for older runs
    uv run python scripts/runs_history.py accept --run <id> --task <task> --reason "..."  # accept a deliberate edit

verify exits 1 if any task has a result file that differs from its history ('untracked'): it was edited outside the
recording tools (orchestrator, rescore.py, apply_cutoff_rule.py, retry_failed.py, merge_runs.py). baseline records the
current files of tasks that have no history yet, once, marked "legacy baseline"; from then on every change is checked.
The recording tools refuse to write on top of an untracked edit; accept records it (with a reason) so they continue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from simple_evals_mm import runs


def _tasks(run_id: str, only: str | None):
    rd = runs.run_dir(run_id)
    for t in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
        td = os.path.join(rd, t)
        if os.path.isdir(td) and ".stale" not in t and (not only or t == only):
            yield t, td


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=("show", "verify", "baseline", "accept"))
    p.add_argument("--run", required=True, help="comma-separated run ids")
    p.add_argument("--task", help="restrict to one task")
    p.add_argument("--reason", help="accept: why the edit is fine (recorded)")
    a = p.parse_args()
    if a.action == "accept" and not (a.task and a.reason):
        p.error("accept needs --task and --reason")
    bad = 0
    for run_id in [r.strip() for r in a.run.split(",") if r.strip()]:
        for task, td in _tasks(run_id, a.task):
            if a.action == "show":
                hp = os.path.join(td, runs.HISTORY)
                print(f"== {run_id}/{task}")
                lines = []
                if os.path.exists(hp):
                    with open(hp) as f:
                        lines = f.readlines()
                for line in lines:
                    e = json.loads(line)
                    print(f"  {e['at']} {e['tool']:18s} {e.get('status', '')} git {e.get('code_git')} "
                          f"{', '.join(e.get('files', {}))} {json.dumps(e.get('details', {}), ensure_ascii=False)[:120]}")
            elif a.action == "accept":
                files = runs.accept_edits(td, a.reason)
                print(f"accepted {run_id}/{task}: {', '.join(files) or 'nothing to accept'}")
            elif a.action == "baseline":
                if runs.baseline_history(td):
                    print(f"baselined {run_id}/{task}")
            else:
                v = runs.verify_history(td)
                if v["status"] != "ok":
                    bad += v["status"] == "untracked"
                    print(f"{v['status']:10s} {run_id}/{task} " + ", ".join(f"{k}={x}" for k, x in v.items() if k != "status" and x))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
