"""`sem jobs compare JOB JOB ...`: the jobs' scores side by side, and per dataset whether they can be compared.

Jobs made at different commits (versions) can be compared on a dataset when nothing that decides what the model was
asked or how its answers were scored differs between them. Per dataset:

  data          the tasks (datasets.lock tasks_sha256 recorded in each job's lock.json) and the dataset's resolved
                settings (its config after defaults, without the allow_exec / unsafe_sandbox permissions; a default
                and the same value given explicitly are equal): any difference -> different
  prompts       what the jobs actually sent, task by task: a fingerprint of every non-agent step of the trajectory
                (system / user texts and the sha256 of each image; attempt 1) over the tasks the jobs share. A task
                sent differently -> different. This is the evidence for the generation side whatever the commits:
                file diffs (provenance.changed_datasets between the trials' generation commits) only explain it and
                are listed as notes
  requests      a change of the sampler code between the generation commits is checked with the golden request
                bodies (tests/golden/samplers.json) of the models whose jobs were generated at the older commit: a
                body that changed -> review (e.g. a chat-template default made explicit: same meaning, check it)
  verification  the commits of the current verifications: changed verification-side files are judged by the golden
                snapshot's reward part of the dataset (tests/golden/: rewards, judge prompts, cases, reviewed on every
                intended change) at both commits: differ -> regrade (`sem job regrade` the older job at the newer
                commit makes it comparable); a commit without a snapshot of the dataset -> review
  judge         the judge's identity (model, settings, served endpoint) of the verifications: differ -> regrade
                (with one --judge-model)

Status per dataset, the worst wins: different > review > regrade > ok. Files outside the package (tests, docs) never
count. The error policy (errors.py) is a note: the failure counts per cause are printed next to the scores.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from simple_evals_mm import provenance
from simple_evals_mm import store as store_mod

GOLDEN_FILES = ("tests/golden/datasets.json", "tests/golden/datasets_mcq.json", "tests/golden/datasets_judge.json",
                "tests/golden/datasets_rules.json")
SAMPLERS_GOLDEN = "tests/golden/samplers.json"
# keys of a golden dataset entry that the generation side decides (the rest: rewards, judge prompts, cases, variants)
GENERATION_KEYS = {"ids", "requests", "prompt", "tasks", "id", "images", "category"}
RANK = {"ok": 0, "regrade": 1, "review": 2, "different": 3}
MARK = {"ok": "✓", "regrade": "↻", "review": "?", "different": "✗"}


class Golden:
    """The golden snapshot entries of the datasets and the sampler request bodies at a commit (cached per commit)."""

    def __init__(self, repo: str):
        self.repo, self.cache = repo, {}

    def _load(self, commit: str) -> tuple[dict, dict]:
        if commit not in self.cache:
            entries = {}
            for f in GOLDEN_FILES:
                src = provenance._show(self.repo, commit, f)
                if src:
                    entries.update(json.loads(src))
            src = provenance._show(self.repo, commit, SAMPLERS_GOLDEN)
            self.cache[commit] = (entries, json.loads(src) if src else {})
        return self.cache[commit]

    def reward_part(self, commit: str, name: str) -> dict | None:
        """The verification side of the dataset's snapshot entry; None when the commit has none."""
        entries, _ = self._load(commit)
        e = entries.get(name, entries.get(name.split("[")[0]))
        if not isinstance(e, dict):
            return None
        ver = {k: v for k, v in e.items() if k not in GENERATION_KEYS}
        if "judge_prompt" in e or "judge_template_sha256" in e or "judge_correct_answer" in e:
            ver["GRADER_TEMPLATE"] = entries.get("GRADER_TEMPLATE")
        return ver

    def bodies_changed(self, a: str, b: str, models: set[str]) -> list[str]:
        """Golden request bodies of these models (body "model", case-insensitive, with or without the org prefix)
        present at both commits that differ."""
        sa, sb = self._load(a)[1], self._load(b)[1]
        names = {m.lower() for m in models} | {m.lower().split("/")[-1] for m in models}
        out = []
        for k in sorted(set(sa) & set(sb)):
            body = (sa[k] or {}).get("body") or {}
            model = str(body.get("model") or "").lower()
            if model and (model in names or model.split("/")[-1] in names) and sa[k] != sb[k]:
                out.append(k)
        return out


def _commit_time(repo: str, commit: str) -> int:
    try:
        return int(provenance._git(repo, "show", "-s", "--format=%ct", commit).strip())
    except Exception:  # noqa: BLE001 - an unknown commit sorts first
        return -1


_COMMIT_RE = re.compile(rb'"generation_commit": *"([0-9a-f]{7,40})"')


CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "simple-evals-mm", "compare")


def _cache_get(kind: str, key: str):
    try:
        with open(os.path.join(CACHE_DIR, kind, f"{key}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cache_put(kind: str, key: str, value) -> None:
    try:
        d = os.path.join(CACHE_DIR, kind)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{key}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f)
        os.replace(tmp, os.path.join(d, f"{key}.json"))
    except OSError:
        pass  # a cache that cannot be written only costs time


def _key(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def prompt_fingerprints(st: store_mod.JobStore, name: str, attempt: int = 1) -> tuple[dict[str, str], set[str]]:
    """({task id: sha256 of what was sent} over attempt `attempt`, the generation commits of every trial). What was
    sent: every non-agent step of the trajectory (role, texts, the image files, named by their sha256). Only the
    fingerprinted attempt is parsed; the other attempts' commits are read from the raw lines. Cached
    (~/.cache/simple-evals-mm/compare) by the sha256 of the trial files when every attempt is complete (frozen)."""
    metas = [st.read_meta(name, k) for k in st.attempts(name)]
    key = _key([attempt, [m.get("sha256") for m in metas]]) if metas and all(m and m.get("sha256") for m in metas) else None
    if key and (hit := _cache_get("prompts", key)) is not None:
        return hit["fps"], set(hit["commits"])
    fps, commits = _prompt_fingerprints(st, name, attempt)
    if key:
        _cache_put("prompts", key, {"fps": fps, "commits": sorted(commits)})
    return fps, commits


def _prompt_fingerprints(st: store_mod.JobStore, name: str, attempt: int) -> tuple[dict[str, str], set[str]]:
    fps, commits = {}, set()
    for k in st.attempts(name):
        if k != attempt:
            with open(st.trials_path(name, k), "rb") as f:
                commits |= {m.decode() for m in _COMMIT_RE.findall(f.read())}
            continue
        for t in st.read_trials(name, k):
            if t.generation_commit:
                commits.add(t.generation_commit)
            if t.trajectory is None:
                continue
            sent = [[s.source, s.message if isinstance(s.message, str) else [p.to_dict() for p in s.message]]
                    for s in t.trajectory.steps if s.source != "agent"]
            blob = json.dumps(sent, sort_keys=True, ensure_ascii=False, default=str)
            fps[t.task_name] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return fps, commits


def job_facts(st: store_mod.JobStore, name: str) -> dict:
    """What decides the comparison of one dataset of one job."""
    from simple_evals_mm import leaderboard, runner

    cfg, lock = st.read_config(), st.read_lock()
    vid = st.current_verification_id(name)
    meta = {}
    if vid:
        with open(os.path.join(st.verifier_dir(name), vid, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    samp = lock.get("sampler") or {}
    entry = (lock.get("datasets") or {}).get(name) or {}
    summary = runner.summarize_dataset(st, name, cfg=cfg, lock_ds=lock.get("datasets") or {})
    causes: dict = {}
    for a in summary.get("attempts", {}).values():
        for c, n in (a.get("causes") or {}).items():
            causes[c] = causes.get(c, 0) + n
    fps, commits = prompt_fingerprints(st, name)
    if not commits:  # trials without a generation_commit: the job's sessions
        code = lock.get("code") or {}
        commits = set(code.get("commits") or ([code["commit"]] if code.get("commit") else []))
    api_model = ((samp.get("config") or {}).get("api_model") or samp.get("model") or "")
    return {"job": st.dir, "model": cfg.get("model"), "api_model": api_model, "effort": cfg.get("effort"),
            "generation_commits": sorted(commits), "verification_commit": meta.get("commit"),
            "judge": leaderboard.judge_identity(meta.get("judge")),
            "tasks_sha256": entry.get("tasks_sha256"), "n_tasks": entry.get("n_tasks"),
            "args": resolved_config(entry, (cfg.get("dataset_args") or {}).get(name) or {}),
            "sampler_modules": provenance.sampler_modules(samp.get("provider"), samp.get("api_format"),
                                                          samp.get("provider")),
            "prompts": fps, "reward": summary.get("reward"), "std": summary.get("std"), "causes": causes}


# config keys that only permit running (not what is run) or restate the given args
PERMISSION_KEYS = {"args", "allow_exec", "unsafe_sandbox", "verifier"}


def resolved_config(entry: dict, given: dict) -> dict:
    """The dataset's settings after its defaults (lock.json datasets.<name>.config: e.g. LiveCodeBench's resolved
    start / end), so a job that relied on a default and one that gave the same value explicitly compare equal; the
    permission flags (allow_exec, unsafe_sandbox) are left out. Older locks without a config: the given args."""
    conf = entry.get("config")
    if not isinstance(conf, dict):
        return {k: v for k, v in given.items() if k not in PERMISSION_KEYS}
    return {k: v for k, v in conf.items() if k not in PERMISSION_KEYS}


def _pkg(repo: str) -> str:
    """The package directory inside `repo` (provenance sorts the files relative to it)."""
    return os.path.join(repo, "src", provenance.PACKAGE)


def _short(job_dir: str) -> str:
    return os.path.basename(str(job_dir).rstrip(os.sep))


def _rel(path: str) -> str:
    return path.split("src/simple_evals_mm/", 1)[-1]


def _files(files: list[str]) -> str:
    return ", ".join(_rel(f) for f in files[:6]) + (" ..." if len(files) > 6 else "")


def classify(name: str, facts: list[dict], repo: str, golden: Golden, changed=None) -> dict:
    """{"status", "notes": [...], "reference": {"generation", "verification"}} for one dataset over the jobs' facts.
    `changed`: provenance.changed_datasets (injected by tests)."""
    changed = changed or provenance.changed_datasets
    status, notes = "ok", []

    def worse(s: str, note: str):
        nonlocal status
        if RANK[s] > RANK[status]:
            status = s
        notes.append(f"{MARK[s]} {note}")

    if len({f["tasks_sha256"] for f in facts}) > 1:
        worse("different", "the tasks differ (datasets.lock tasks_sha256): " +
              ", ".join(f"{_short(f['job'])} {str(f['tasks_sha256'])[:8]} ({f['n_tasks']})" for f in facts))
    if len({json.dumps(f["args"], sort_keys=True) for f in facts}) > 1:
        worse("different", "the dataset settings differ: " + ", ".join(f"{_short(f['job'])} {f['args']}" for f in facts))

    # prompts: what was sent, over the tasks every job has
    shared = set.intersection(*(set(f["prompts"]) for f in facts))
    differ = sorted(t for t in shared if len({f["prompts"][t] for f in facts}) > 1)
    if differ:
        worse("different", f"{len(differ)} of {len(shared)} tasks were sent different prompts (e.g. {differ[0]})")

    gen_commits = sorted({c for f in facts for c in f["generation_commits"]}, key=lambda c: _commit_time(repo, c))
    ver_commits = sorted({f["verification_commit"] for f in facts if f["verification_commit"]},
                         key=lambda c: _commit_time(repo, c))
    gref = gen_commits[-1] if gen_commits else None
    vref = ver_commits[-1] if ver_commits else None
    modules = sorted({m for f in facts for m in f["sampler_modules"]}) if all(f["sampler_modules"] for f in facts) else None

    for c in gen_commits[:-1]:
        try:
            rec = changed(c, gref, [name], repo=repo, package_dir=_pkg(repo), sampler_modules=modules)[name]
        except Exception as e:  # noqa: BLE001 - an unknown commit cannot be diffed (the prompts still decide)
            notes.append(f"  generation {c[:8]}: cannot diff against {gref[:8]} ({type(e).__name__})")
            continue
        files = rec.get("dataset", []) + rec.get("generation", []) + rec.get("unknown", [])
        if not files:
            continue
        # a model's request body changed: only that model's jobs generated at c differ from what gref would send
        at_c = [f for f in facts if c in f["generation_commits"]]
        if rec.get("generation") and (bodies := golden.bodies_changed(c, gref, {f["api_model"] for f in at_c if f["api_model"]})):
            jobs_c = [_short(f["job"]) for f in at_c
                      if golden.bodies_changed(c, gref, {f["api_model"]})] or [_short(f["job"]) for f in at_c]
            worse("review", f"generation {c[:8]} -> {gref[:8]}: the golden request body changed for "
                            f"{', '.join(bodies[:3])}, a model of {', '.join(jobs_c[:3])}{' ...' if len(jobs_c) > 3 else ''}: "
                            "check that its requests still mean the same (e.g. a template default made explicit)")
        else:
            notes.append(f"  generation {c[:8]} -> {gref[:8]}: {_files(files)} changed"
                         + ("; the prompts sent are identical" if not differ else ""))

    for v in ver_commits[:-1]:
        try:
            rec = changed(v, vref, [name], repo=repo, package_dir=_pkg(repo))[name]
        except Exception as e:  # noqa: BLE001
            worse("review", f"verification {v[:8]}: cannot diff against {vref[:8]} ({type(e).__name__})")
            continue
        files = rec.get("dataset", []) + rec.get("verification", []) + rec.get("unknown", [])
        if not files:
            continue
        a, b = golden.reward_part(v, name), golden.reward_part(vref, name)
        older = [_short(f["job"]) for f in facts if f["verification_commit"] == v]
        if a is None or b is None:
            worse("review", f"verification {v[:8]} -> {vref[:8]}: {_files(files)} changed, and no golden snapshot of "
                            f"the dataset at both commits tells whether its rewards changed (or regrade "
                            f"{', '.join(older)} at {vref[:8]})")
        elif a != b:
            worse("regrade", f"verification {v[:8]} -> {vref[:8]}: the rewards changed (golden snapshot); regrade "
                             f"{', '.join(older)} at {vref[:8]} or later")
        else:
            notes.append(f"  verification {v[:8]} -> {vref[:8]}: {_files(files)} changed; the golden rewards are equal")

    if len({json.dumps(f["judge"], sort_keys=True) for f in facts}) > 1:
        worse("regrade", "the judges differ: " + ", ".join(
            f"{_short(f['job'])} {(f['judge'] or {}).get('model')}" for f in facts) + " (regrade with one --judge-model)")
    if any("errors.py" in n for n in notes):
        notes.append("  the error policy (errors.py) changed: compare the failure counts per cause")
    return {"status": status, "notes": notes, "reference": {"generation": gref, "verification": vref}}


def compare(job_dirs: list[str], datasets: list[str] | None = None, repo: str | None = None, changed=None) -> dict:
    """{"jobs": [...], "datasets": {name: {"status", "notes", "reference", "jobs": {job: facts}}}} over the datasets
    every job has (or `datasets`)."""
    stores = []
    for d in job_dirs:
        st = store_mod.JobStore.from_path(d)
        if not st.exists():
            raise FileNotFoundError(f"{d}: not a job directory (no config.json)")
        stores.append(st)
    repo = repo or provenance._git(provenance.PACKAGE_DIR, "rev-parse", "--show-toplevel").strip()
    shared = set.intersection(*(set(st.datasets()) for st in stores)) if stores else set()
    golden = Golden(repo)
    names = list(datasets or sorted(shared))
    changed = changed or _all_at_once(names)
    out = {"jobs": [st.dir for st in stores], "datasets": {}}
    for name in names:
        facts = [job_facts(st, name) for st in stores if name in st.datasets()]
        rec = classify(name, facts, repo, golden, changed=changed) if len(facts) > 1 else \
            {"status": "ok", "notes": ["  one job only"], "reference": {}}
        out["datasets"][name] = {**rec, "jobs": {f["job"]: {k: v for k, v in f.items() if k != "prompts"} for f in facts}}
    return out


def _rules_version(names: list[str]) -> str:
    """What else decides changed_datasets' answer for a commit pair: provenance's own rules (its source) and the
    datasets' modules (the registry)."""
    from simple_evals_mm import dataset as dataset_mod

    with open(provenance.__file__, "rb") as f:
        src = hashlib.sha256(f.read()).hexdigest()
    return _key([src, {n: str(dataset_mod.DATASETS.get(n.split("[")[0])) for n in names}])


def _all_at_once(names: list[str]):
    """provenance.changed_datasets for every compared dataset in one call per commit pair (a call reads both trees,
    seconds each), cached."""
    cache: dict = {}

    def changed(a, b, datasets, **kw):
        key = (a, b, tuple(kw.get("sampler_modules") or ()), kw.get("sampler_modules") is None)
        if key not in cache:
            disk = _key([a, b, sorted(names), key[2], key[3], _rules_version(names)])
            cache[key] = _cache_get("changed", disk)
            if cache[key] is None:  # commits are immutable: the result for this pair and these datasets never changes
                cache[key] = provenance.changed_datasets(a, b, names, **kw)
                _cache_put("changed", disk, cache[key])
        return {n: cache[key][n] for n in datasets}

    return changed


def _cell(f: dict | None) -> str:
    if not f or f.get("reward") is None:
        return "—"
    return f"{100 * f['reward']:.1f}" + (f" ± {100 * f['std']:.1f}" if f.get("std") is not None else "")


def markdown(rep: dict) -> str:
    jobs = rep["jobs"]
    lines = ["| dataset | | " + " | ".join(_short(j) for j in jobs) + " |", "|" + "---|" * (len(jobs) + 2)]
    for name, d in rep["datasets"].items():
        lines.append(f"| {name} | {MARK[d['status']]} | " + " | ".join(_cell(d["jobs"].get(j)) for j in jobs) + " |")
    lines += ["", "✓ comparable · ↻ comparable after `sem job regrade` · ? review · ✗ not comparable", ""]
    for name, d in rep["datasets"].items():
        causes = {_short(j): f["causes"] for j, f in d["jobs"].items() if f["causes"]}
        if d["notes"] or causes:
            lines.append(f"- **{name}** ({MARK[d['status']]} {d['status']})")
            lines += [f"  - {n.strip()}" for n in d["notes"]]
            if causes:
                lines.append("  - failures per cause: " + "; ".join(
                    f"{j} " + ", ".join(f"{c}={n}" for c, n in sorted(cs.items())) for j, cs in causes.items()))
    return "\n".join(lines) + "\n"
