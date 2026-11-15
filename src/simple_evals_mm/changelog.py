"""`sem site changelog`: a draft changelog entry from what changed on the leaderboard since a git revision.

Compared: the leaderboard data (site/data.js) at the revision with the current build, and the commits since it.

  Models     settings added / removed; models newly left out of Overall (or back in)
  Tasks      benchmarks added / removed; what Overall averages
  Scoring    scores that moved by more than their 95% interval over questions (or 2 points without one); the
             subjects of the commits since the revision that touch code changing outputs (tasks, verifiers, samplers,
             the error policy)
  Reporting  the parity count (comparable results within the tolerance)

The entry is a draft (`draft: true`: changelog.html shows it only with #drafts): the facts are listed, the reasons
behind them are for a person to write before publishing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from simple_evals_mm import store as store_mod

OUTPUT_PATHS = ("src/simple_evals_mm/tasks", "src/simple_evals_mm/verifiers.py", "src/simple_evals_mm/samplers",
                "src/simple_evals_mm/errors.py", "src/simple_evals_mm/agent.py", "src/simple_evals_mm/dataset.py")
MOVE_MIN = 2.0  # points: a move smaller than this is never reported


def _load(text: str) -> dict:
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def data_at(rev: str, path: str = "site/data.js", repo: str = ".") -> dict | None:
    try:
        out = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        return None
    return _load(out.stdout)


def _settings(d: dict) -> dict[str, dict]:
    """{"<model name> (<label>)": level} for every shown setting."""
    out = {}
    for m in d.get("models") or []:
        for lv, lab in ((m.get("max"), m.get("maxLabel")), (m.get("low"), m.get("lowLabel"))):
            if lv:
                out[f"{m['name']} ({lab})"] = lv
    return out


def _overall(lv: dict) -> float | None:
    return ((lv.get("benchmarks") or {}).get("Overall") or {}).get("score")


def _labels(d: dict) -> dict[str, str]:
    return {t["key"]: t.get("label", t["key"]) for t in d.get("tasks") or []}


def _parity(d: dict) -> tuple[int, int]:
    rs = [c["parity"]["result"] for m in d.get("models") or [] for c in (m.get("max") or {}).get("tasks", {}).values()
          if c.get("parity")]
    return sum(1 for r in rs if r == "✓"), sum(1 for r in rs if r in ("✓", "⚠"))


def diff(old: dict | None, new: dict) -> list[dict]:
    """[{category, text}] of the leaderboard changes from `old` (None: everything is new) to `new`."""
    old = old or {"models": [], "tasks": [], "benchmarks": []}
    items: list[dict] = []
    add = lambda cat, text: items.append({"category": cat, "text": text})
    so, sn = _settings(old), _settings(new)
    if added := [k for k in sn if k not in so]:
        add("Models", "Added: " + ", ".join(added) + ".")
    if removed := [k for k in so if k not in sn]:
        add("Models", "No longer on the leaderboard: " + ", ".join(removed) + ".")
    out_now = [k for k in sn if k in so and _overall(so[k]) is not None and _overall(sn[k]) is None]
    back_in = [k for k in sn if k in so and _overall(so[k]) is None and _overall(sn[k]) is not None]
    if out_now:
        add("Models", "Not in Overall (a benchmark missing): " + ", ".join(out_now) + ".")
    if back_in:
        add("Models", "Back in Overall: " + ", ".join(back_in) + ".")
    lo, ln = _labels(old), _labels(new)
    if t_added := [ln[k] for k in ln if k not in lo]:
        add("Tasks", "Added: " + ", ".join(t_added) + ".")
    if t_removed := [lo[k] for k in lo if k not in ln]:
        add("Tasks", "No longer run: " + ", ".join(t_removed) + ".")
    ov_old = next((b for b in old.get("benchmarks") or [] if b.get("overall")), {})
    ov_new = next((b for b in new.get("benchmarks") or [] if b.get("overall")), {})
    if ov_old and set(ov_old.get("tasks") or []) != set(ov_new.get("tasks") or []):
        add("Tasks", f"Overall is the mean of {len(ov_new.get('tasks') or [])} benchmarks "
                     f"(was {len(ov_old.get('tasks') or [])}).")
    for k in (x for x in sn if x in so):
        for t, c in (sn[k].get("tasks") or {}).items():
            c0 = (so[k].get("tasks") or {}).get(t)
            if not c0 or c.get("score") is None or c0.get("score") is None:
                continue
            ci = c.get("ci95")
            half = (ci[1] - ci[0]) / 2 if ci else 0.0
            moved = c["score"] - c0["score"]
            if abs(moved) > max(MOVE_MIN, half):
                add("Scoring", f"{k}, {ln.get(t, t)}: {c0['score']:.1f} → {c['score']:.1f}.")
    po, pn = _parity(old), _parity(new)
    if pn != po and pn[1]:
        add("Reporting", f"Parity with model cards: {pn[0]} of {pn[1]} comparable results within the tolerance "
                         f"(was {po[0]} of {po[1]}).")
    return items


def commits(rev: str, repo: str = ".") -> list[str]:
    """Subjects of the commits since `rev` that touch code changing outputs (merges left out)."""
    try:
        out = subprocess.run(["git", "-C", repo, "log", "--no-merges", "--format=%h %s", f"{rev}..HEAD", "--",
                              *OUTPUT_PATHS], capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return []
    return [line for line in out.splitlines() if line.strip()]


def entry(items: list[dict], commit_lines: list[str], when: str | None = None, version: str | None = None) -> str:
    """The JS object of a draft entry (to go first in CHANGELOG)."""
    body = [{"category": i["category"], "text": i["text"]} for i in items]
    body += [{"category": "Scoring", "text": f"Code change (review the wording): {c}"} for c in commit_lines]
    e = {"date": when or store_mod._local_now().date().isoformat(), **({"version": version} if version else {}), "draft": True,
         "items": body or [{"category": "Reporting", "text": "(no leaderboard change found: describe it)"}]}
    lines = ["  {", "    // DRAFT from `sem site changelog`: check the facts, add the reasons, then remove draft: true",
             f"    date: {json.dumps(e['date'])},"]
    if version:
        lines.append(f"    version: {json.dumps(version)},")
    lines += ["    draft: true,", "    items: ["]
    lines += [f"      {{ category: {json.dumps(i['category'])}, text: {json.dumps(i['text'], ensure_ascii=False)} }},"
              for i in e["items"]]
    lines += ["    ],", "  },"]
    return "\n".join(lines) + "\n"


def insert(changelog_js: str, entry_js: str) -> None:
    """Put the entry first in the CHANGELOG array of changelog.js."""
    with open(changelog_js, encoding="utf-8") as f:
        s = f.read()
    m = re.search(r"const CHANGELOG = \[\n", s)
    if not m:
        raise ValueError(f"{changelog_js}: no `const CHANGELOG = [` line")
    with open(changelog_js, "w", encoding="utf-8") as f:
        f.write(s[:m.end()] + entry_js + s[m.end():])


def build(since: str, data_js: str = "site/data.js", repo: str = ".") -> tuple[list[dict], list[str]]:
    with open(data_js, encoding="utf-8") as f:
        new = _load(f.read())
    rel = os.path.relpath(os.path.abspath(data_js), os.path.abspath(repo))
    return diff(data_at(since, rel, repo), new), commits(since, repo)
