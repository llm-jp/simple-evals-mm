"""Flag run tasks whose recorded task code differs from the current tree (or that lack provenance).
  python scripts/runs_check.py [--task charxiv] [--all-runs]
For each runs/<id>/<task>/summary.json: compare provenance.task_module_sha256 (and common_sha256) with the
current files. 'STALE' = task code changed since generation -> regenerate that task;
'NO-PROV' = no provenance recorded."""
import argparse
import glob
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from simple_evals_mm import runs  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--task", default="")
ap.add_argument("--quiet-ok", action="store_true", help="only print problems")
a = ap.parse_args()
common_now = runs.current_task_sha("src/simple_evals_mm/common.py")
n_ok = 0
rows = []
for sp in sorted(glob.glob("runs/*/*/summary.json")):
    rid, task = sp.split(os.sep)[1:3]
    if a.task and task != a.task:
        continue
    s = json.load(open(sp))
    prov = s.get("provenance")
    if not prov:
        rows.append(("NO-PROV", rid, task, s.get("timestamp", "")[:16], ""))
        continue
    now = runs.current_task_sha(prov.get("task_module") or "")
    why = []
    if now and prov.get("task_module_sha256") and now != prov["task_module_sha256"]:
        why.append("task module changed")
    if common_now and prov.get("common_sha256") and common_now != prov["common_sha256"]:
        why.append("common.py changed")
    if why:
        rows.append(("STALE", rid, task, prov.get("started_at", "")[:16], "; ".join(why) + f" (generated at git {prov.get('code_git')}{'+dirty' if prov.get('code_dirty') else ''})"))
    else:
        n_ok += 1
for st, rid, task, ts, why in rows:
    print(f"{st:8s} {task:16s} {rid:55s} {ts} {why}")
print(f"\nok: {n_ok}  stale: {sum(r[0] == 'STALE' for r in rows)}  no-provenance: {sum(r[0] == 'NO-PROV' for r in rows)}")
