"""`sem run --from JOB`: a derived job reuses an earlier job's trials where they are still valid.

For each (dataset, attempt, task) of the new job's plan, the source job's trial is classified:

  REUSE    same task_checksum and an identical generation_lock (rendered request, settings, endpoint / server
           manifest, seed, commit), and the source's current verification ran with the verifier lock this job would
           use: the trial and its verifier result are copied
  REGRADE  the generation is reusable, but the verifier differs (another judge / verifier lock, the dataset's
           verification files changed between the commits, or the source was not verified): the trial is copied and
           verified in the new job
  RERUN    everything else (no source trial, a transient source failure, changed task content, other generation
           settings, or the dataset's own / generation files changed between the commits): generated anew

Across commits, provenance.changed_datasets(generation commit, new commit) decides instead of requiring equal commits:
the generation_lock is recomputed with the commit that generated the trial (its `generation_commit`: a job resumed
across commits holds trials of several; everything except the commit must match) and the dataset must not have changed
dataset-own, generation-side or unknown files since that commit; verification-side changes (since the commit of the
source verification) only force a REGRADE.

`sem job resume` across commits runs plan() of the job against itself as its backstop: every stored non-transient
trial must come out REUSE or REGRADE.

Copies are real copies (trial files stay append-only per job, no hard links) and carry
`reused_from: {job_id, job, trial_id, trial_name, decision, reason, verification_id, verifier_lock}`; the images their
trajectories reference are copied into the new job's images/.
"""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from types import SimpleNamespace

from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm import errors
from simple_evals_mm import store as store_mod
from simple_evals_mm.models import TrialResult

REUSE, REGRADE, RERUN = "reuse", "regrade", "rerun"
_IMAGE_RE = re.compile(r"images/[0-9a-f]{64}\.[A-Za-z0-9]+")


@dataclass
class Decision:
    kind: str
    reason: str
    trial: TrialResult | None = None
    commit: str | None = None  # the source trial's generation commit (REUSE / REGRADE)


@dataclass
class Context:
    """What the new job would generate and verify with (start_job / dry_run fill it)."""

    datasets: dict  # name -> Dataset
    tasks: dict  # name -> list[Task]
    checksums: dict  # name -> {task id: task_checksum}
    sampler_config: dict | None  # lock_config of the resolved sampler config
    server_digest: str | None
    commit: str | None
    verifier_locks: dict  # name -> the new job's verifier lock (dataset_mod.verifier_lock)
    judge_config: dict | None
    n_attempts: dict | int  # {dataset: attempts} (an int: the same for every dataset)
    generate_only: bool = False
    sampler_modules: list | None = None  # provenance.sampler_modules of the new job (None: every sampler counts)


def is_transient(cause: str | None) -> bool:
    """A failure that a resume regenerates (not a stored outcome): retried, or not counted in the score."""
    pol = errors.POLICY.get(cause or "")
    return bool(cause) and (pol is None or pol.retry or not pol.counts_in_score)


_transient = is_transient


def first_commit(lock: dict) -> str | None:
    """The commit of a job's first session (lock.json code.commits[0]; a job written before `commits` was recorded
    ran under one commit only)."""
    code = lock.get("code") or {}
    return (code.get("commits") or [code.get("commit")])[0]


def generation_commit(trial, lock: dict, cache: dict | None = None, _depth: int = 0) -> str | None:
    """The commit that generated `trial` (of the job whose lock.json is `lock`): its generation_commit; for a trial
    written before that field existed, a copy (`reused_from`) resolves through its source job, else the job's first
    commit (resuming across commits was refused until generation_commit was recorded, so such a job had one commit).
    `cache`: {source job dir: (lock, {trial id: (generation_commit, reused_from)})} shared across calls."""
    if trial.generation_commit:
        return trial.generation_commit
    r = trial.reused_from or {}
    if r.get("job") and _depth < 8:
        cache = {} if cache is None else cache
        key = os.path.abspath(r["job"])
        if key not in cache:
            src = store_mod.JobStore.from_path(key)
            try:
                src_lock = src.read_lock()
            except (OSError, ValueError):
                cache[key] = None
            else:
                cache[key] = (src_lock, {t.id: (t.generation_commit, t.reused_from)
                                         for n in src.datasets() for k in src.attempts(n) for t in src.read_trials(n, k)})
        if cache[key] is not None:
            src_lock, index = cache[key]
            hit = index.get(r.get("trial_id"))
            if hit is None:
                return first_commit(src_lock)
            return hit[0] or generation_commit(SimpleNamespace(generation_commit=None, reused_from=hit[1]), src_lock,
                                               cache, _depth + 1)
    return first_commit(lock)


def _sampler_diff(src_lock: dict, ctx: Context) -> list[str]:
    from simple_evals_mm.runner import diff_dicts
    from simple_evals_mm.samplers.base import lock_config

    old = lock_config(((src_lock.get("sampler") or {}).get("config")) or {}) or {}
    return [line.strip().split(":", 1)[0] for line in diff_dicts(old, ctx.sampler_config or {})]


def plan(src: store_mod.JobStore, ctx: Context, changed_datasets=None) -> dict:
    """{name: {"decisions": {(k, task_id): Decision}, "counts": {...}, "reasons": {reason: n}}}. changed_datasets:
    provenance.changed_datasets (injected by tests)."""
    from simple_evals_mm import provenance

    if changed_datasets is None:
        changed_datasets = provenance.changed_datasets
    src_lock = src.read_lock()
    src_server = (src_lock.get("server") or {}).get("digest")
    changes: dict = {}  # commit -> changed_datasets(commit, ctx.commit) or the error text

    def changed(commit: str | None) -> dict | str:
        if commit == ctx.commit:
            return {}
        if commit not in changes:
            try:
                changes[commit] = changed_datasets(commit, ctx.commit, sorted(ctx.datasets),
                                                   sampler_modules=ctx.sampler_modules)
            except Exception as e:  # noqa: BLE001 - an unknown commit: nothing can be compared
                changes[commit] = (f"cannot compare commits {str(commit)[:12]}..{str(ctx.commit)[:12]} "
                                   f"({type(e).__name__})")
        return changes[commit]

    sampler_diff = _sampler_diff(src_lock, ctx)
    gc_cache: dict = {}
    out = {}
    for name, ds in ctx.datasets.items():
        decisions: dict = {}
        vid = src.current_verification_id(name)
        vmeta = src.read_verification_meta(name, vid) if vid else None
        _, vresults = src.read_verification(name, vid) if vid else (None, {})
        verified = {k: {r.task_id for r in rs} for k, rs in vresults.items()}
        # the verifier lock this job would use, computed at the source verification's commit when the commits differ
        vcommit = (vmeta or {}).get("commit")
        vch = changed(vcommit) if vmeta is not None else {}
        ver_changed = [] if isinstance(vch, str) else sorted(set((vch.get(name) or {}).get("verification", [])) |
                                                             set((vch.get(name) or {}).get("dataset", [])))
        if vmeta is None:
            ver_reason = "the source job has no verification of this dataset"
        elif ctx.generate_only and ds.needs_judge:
            ver_reason = "this job verifies later (--generate-only): its judge is not resolved yet"
        elif isinstance(vch, str):
            ver_reason = vch
        elif ver_changed:
            ver_reason = f"verification files changed between the commits: {', '.join(ver_changed[:5])}"
        else:
            want = ctx.verifier_locks.get(name) if vcommit == ctx.commit else \
                dataset_mod.verifier_lock(ds, ctx.judge_config, vcommit)
            ver_reason = None if vmeta.get("verifier_lock") == want else \
                "another verifier lock (judge identity / settings, dataset args or verifier dependencies)"
        src_trials = {k: {t.task_name: t for t in src.read_trials(name, k)} for k in src.attempts(name)}
        n = ctx.n_attempts.get(name, 1) if isinstance(ctx.n_attempts, dict) else ctx.n_attempts
        for k in range(1, n + 1):
            for task in ctx.tasks[name]:
                t = src_trials.get(k, {}).get(task.id)
                gc = generation_commit(t, src_lock, gc_cache) if t is not None else None
                ch = changed(gc) if t is not None else {}
                gen_changed = [] if isinstance(ch, str) else provenance.generation_files(ch.get(name) or {})
                if t is None:
                    d = Decision(RERUN, "no trial in the source job")
                elif _transient(t.cause):
                    d = Decision(RERUN, f"the source trial failed ({t.cause})")
                elif t.task_checksum != ctx.checksums[name].get(task.id):
                    d = Decision(RERUN, "task content changed (task_checksum)")
                elif isinstance(ch, str):
                    d = Decision(RERUN, ch)
                elif gen_changed:
                    d = Decision(RERUN, f"dataset / generation files changed between the commits: {', '.join(gen_changed[:5])}"
                                 + (" ..." if len(gen_changed) > 5 else ""))
                elif dataset_mod.generation_lock(t.trajectory, ctx.sampler_config, ctx.server_digest,
                                                 commit=gc) != t.generation_lock:
                    why = []
                    if sampler_diff:
                        why.append("sampler " + ", ".join(sampler_diff[:6]))
                    if src_server != ctx.server_digest:
                        why.append("server manifest")
                    d = Decision(RERUN, "generation_lock differs (" + ("; ".join(why) or "rendered request") + ")")
                elif ver_reason is None and task.id in verified.get(k, set()):
                    d = Decision(REUSE, "identical generation and verifier", t, gc)
                else:
                    d = Decision(REGRADE, ver_reason or "the source trial was not verified", t, gc)
                decisions[(k, task.id)] = d
        counts = Counter(d.kind for d in decisions.values())
        out[name] = {"decisions": decisions,
                     "counts": {REUSE: counts[REUSE], REGRADE: counts[REGRADE], RERUN: counts[RERUN]},
                     "reasons": dict(Counter(f"{d.kind}: {d.reason}" for d in decisions.values())),
                     "source_verification": vid}
    return out



def summary(p: dict) -> dict:
    """The printable part of a plan: counts and reasons per dataset."""
    return {name: {"counts": v["counts"], "reasons": v["reasons"]} for name, v in p.items()}


def copy_trials(src: store_mod.JobStore, dst: store_mod.JobStore, p: dict, src_job_id: str,
                verifier_locks: dict) -> dict:
    """Copy the REUSE / REGRADE trials into the new job's trial files (append; trials already there or attempts
    already complete are left alone) with reused_from, and the images they reference. Returns {name: n copied}."""
    out = {}
    for name, v in p.items():
        n = 0
        by_attempt: dict = {}
        for (k, _tid), d in v["decisions"].items():
            if d.kind in (REUSE, REGRADE):
                by_attempt.setdefault(k, []).append(d)
        for k, ds in sorted(by_attempt.items()):
            if dst.is_complete(name, k):
                continue
            have = {t.task_name for t in dst.read_trials(name, k)}
            for d in ds:
                t = d.trial
                if t.task_name in have:
                    continue
                c = TrialResult.from_dict(t.to_dict())
                c.id = TrialResult.__dataclass_fields__["id"].default_factory()  # type: ignore[misc]
                c.reused_from = {"job_id": src_job_id, "job": os.path.abspath(src.dir), "trial_id": t.id,
                                 "trial_name": t.trial_name, "decision": d.kind, "reason": d.reason,
                                 "verification_id": v["source_verification"] if d.kind == REUSE else None,
                                 "verifier_lock": verifier_locks.get(name)}
                c.verifier_lock = verifier_locks.get(name) or c.verifier_lock
                c.generation_commit = d.commit or c.generation_commit
                _copy_images(src.dir, dst.dir, t)
                dst.append_trial(name, c)
                n += 1
        out[name] = n
    return out


def _copy_images(src_dir: str, dst_dir: str, trial: TrialResult) -> None:
    import json

    for rel in set(_IMAGE_RE.findall(json.dumps(trial.to_dict()))):
        a, b = os.path.join(src_dir, rel), os.path.join(dst_dir, rel)
        if os.path.exists(a) and not os.path.exists(b):
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copyfile(a, b + ".part")
            os.replace(b + ".part", b)


def reused_results(trials: list[TrialResult], name: str, k: int, vlock: str) -> dict:
    """{task_name: VerifierResult} copied from the source verification for the REUSE trials of one attempt whose
    planned verifier lock equals the lock of this verification."""
    out, cache = {}, {}
    for t in trials:
        r = t.reused_from or {}
        if r.get("decision") != REUSE or r.get("verifier_lock") != vlock or not r.get("verification_id"):
            continue
        key = (r["job"], r["verification_id"])
        if key not in cache:
            st = store_mod.JobStore.from_path(r["job"])
            try:
                _, res = st.read_verification(name, r["verification_id"])
            except OSError:
                res = {}
            cache[key] = {kk: {g.task_id: g for g in gs} for kk, gs in res.items()}
        g = cache[key].get(k, {}).get(t.task_name)
        if g is not None:
            out[t.task_name] = g
    return out
