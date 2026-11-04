"""Golden REAL trials (tests/golden/real_trials/, built by its build.py from the Qwen3.5-9B job): what the fake-based
tests cannot produce. Trials with U+2028 inside a JSON string, multi-image prompts, cut-off answers, judge fallbacks and
1 MB lines are read back, classified by `sem run --from`'s plan, and verified again with a judge that replays the
stored judge replies: the stored rewards must come back."""

import json
import os
import shutil

import pytest

from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm import derive
from simple_evals_mm.models import Response, Task
from simple_evals_mm.store import JobStore, iter_trials

HERE = os.path.join(os.path.dirname(__file__), "golden", "real_trials")
JOB = os.path.join(HERE, "job")
with open(os.path.join(HERE, "tasks.json"), encoding="utf-8") as _f:
    GOLD = json.load(_f)
DATASETS = sorted(GOLD["tasks"])


def _lock():
    with open(os.path.join(JOB, "lock.json"), encoding="utf-8") as f:
        return json.load(f)


def _results(st: JobStore, name: str) -> dict:
    _, res = st.read_verification(name)
    return {g.task_id: g for g in res[1]}


def test_trials_read_back_line_by_line():
    n_lines = 0
    for name in DATASETS:
        with open(os.path.join(JOB, name, "trials_a1.jsonl"), "rb") as f:
            data = f.read().decode("utf-8")
        trials = list(iter_trials(JOB, name, 1))
        assert len(trials) == data.count("\n") == len(GOLD["tasks"][name])
        assert [t.task_name for t in trials] == list(GOLD["tasks"][name])
        if " " in data:  # the case str.splitlines() broke on
            assert len(data.splitlines()) > data.count("\n")
        n_lines += len(trials)
    assert n_lines >= 20
    st = JobStore.from_path(JOB)
    gpqa = {t.task_name: t for t in st.read_trials("gpqa", 1)}
    assert " " in gpqa["rec1zl5LvaatzGhFt"].trajectory.steps[0].to_dict()["message"]
    multi = {t.task_name: t for t in st.read_trials("jamultiimage", 1)}["35"]
    images = [p for p in multi.trajectory.steps[0].to_dict()["message"] if p["type"] == "image"]
    assert len(images) >= 2 and all(os.path.exists(os.path.join(JOB, p["source"]["path"])) for p in images)


def test_nothing_secret_or_machine_local():
    import re

    # absolute paths of the machine the job ran on, API keys / tokens (OpenAI / OpenRouter sk-..., HF hf_..., bearer)
    bad = re.compile(r"/groups/|/home/|/tmp/|/scratch/|\bsk-[A-Za-z0-9_-]{16,}|\bhf_[A-Za-z0-9]{30,}|Bearer ")
    for root, _, files in os.walk(HERE):
        for fn in files:
            if fn.endswith((".json", ".jsonl")):
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    hits = bad.findall(f.read())
                assert not hits, (fn, hits)


class ReplayJudge:
    """The judge whose reply is the stored one of the trial being verified (by its seed: judge_call sends the trial's)."""

    def __init__(self, replies):
        self.replies, self.slug = replies, "replay/judge"

    def __call__(self, messages, tools=None, seed=None):
        rec = self.replies[seed]
        return Response(text=rec.get("response") or "", finish_reason=rec.get("finish_reason") or "stop",
                        model=rec.get("served_model"))


@pytest.mark.parametrize("name", DATASETS)
def test_reverify_reproduces_the_stored_rewards(name):
    from simple_evals_mm.verifiers import trial_seed

    st = JobStore.from_path(JOB)
    ds = dataset_mod.get_dataset(name)
    stored = _results(st, name)
    replies = {trial_seed(f"{tid}__a1"): (g.judge or {}) for tid, g in stored.items()}
    for t in st.read_trials(name, 1):
        rec = GOLD["tasks"][name][t.task_name]
        task = Task(id=rec["id"], data=rec["data"], answer=rec["answer"], category=rec["category"])
        g = ds.verify_trial(task, t.trajectory, ReplayJudge(replies))
        want = stored[t.task_name]
        assert (g.reward, g.cause, g.method) == (want.reward, want.cause, want.method), t.task_name
        if want.judge:
            assert g.judge["response"] == want.judge["response"]


def _context(st: JobStore, **over) -> derive.Context:
    lock = _lock()
    tasks = {n: [Task(id=t.task_name) for t in st.read_trials(n, 1)] for n in DATASETS}
    checksums = {n: {t.task_name: t.task_checksum for t in st.read_trials(n, 1)} for n in DATASETS}
    kw = {"datasets": {n: dataset_mod.get_dataset(n) for n in DATASETS}, "tasks": tasks, "checksums": checksums,
          "sampler_config": lock["sampler"]["config"], "server_digest": lock["server"]["digest"],
          "commit": lock["code"]["commit"],
          "verifier_locks": {n: d["verifier_lock"] for n, d in lock["datasets"].items()},
          "judge_config": None, "n_attempts": 1}
    return derive.Context(**(kw | over))


def test_derive_plan_classifies_real_trials(tmp_path):
    shutil.copytree(JOB, tmp_path / "job")
    st = JobStore.from_path(str(tmp_path / "job"))
    same = derive.plan(st, _context(st))
    for name, v in same.items():  # identical generation and verifier: every (non-transient) trial is reused
        assert v["counts"] == {"reuse": len(GOLD["tasks"][name]), "regrade": 0, "rerun": 0}, (name, v["reasons"])
    other_judge = derive.plan(st, _context(st, verifier_locks={n: "another" for n in DATASETS}))
    assert all(v["counts"]["regrade"] == len(GOLD["tasks"][n]) for n, v in other_judge.items())
    lock = _lock()
    cfg = dict(lock["sampler"]["config"])
    cfg["settings"] = {**cfg["settings"], "temperature": {"value": 0.0, "source": "cli"}}
    rerun = derive.plan(st, _context(st, sampler_config=cfg))
    assert all(v["counts"]["rerun"] == len(GOLD["tasks"][n]) for n, v in rerun.items())
    assert any("sampler settings.temperature" in r for v in rerun.values() for r in v["reasons"])
