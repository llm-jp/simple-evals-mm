"""`sem convert`: audit and conversion of v0.4 runs (offline). The fake v0.4 runs are written with the v0.4 code
in the tree (runs.write_config / runs.track / SingleEvalResult.to_dict), in the exact v0.4 file formats."""

import hashlib
import json
import os
import subprocess
from typing import ClassVar

import pytest

from simple_evals_mm import convert, errors, runs, verifiers
from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm.common import SingleEvalResult
from simple_evals_mm.dataset import Dataset
from simple_evals_mm.models import Task, user
from simple_evals_mm.store import JobStore
from tests.fakes import VERDICT, FakeSampler

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK_MODULE = "src/simple_evals_mm/tasks/arith.py"


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                   text=True).strip()


def code_repo(path, task_versions=("v1 = 1\n", "v1 = 2\n"), common=None) -> tuple[str, list[str], str]:
    """A git repo with one commit per task-module version (common.py = this tree's unless given)."""
    path.mkdir()
    git(path, "init", "-q")
    with open(os.path.join(REPO_ROOT, convert.V04_COMMON), encoding="utf-8") as f:
        common = common if common is not None else f.read()
    os.makedirs(path / "src/simple_evals_mm/tasks")
    (path / convert.V04_COMMON).write_text(common, encoding="utf-8")
    commits = []
    for v in task_versions:
        (path / TASK_MODULE).write_text(v, encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", v.strip())
        commits.append(git(path, "rev-parse", "HEAD"))
    return str(path), commits, common


def row(rid, question, answer, response, score, **kw) -> dict:
    return SingleEvalResult(id=rid, question=question, correct_answer=answer, response_text=response,
                            extracted_answer=response, score=score, input_tokens=5, output_tokens=2, finish_reason="stop",
                            **kw).to_dict()


V04_CONFIG = {"model": {"path": "openrouter/fake/model"}, "sampler_class": "OpenRouterSampler", "backend": "openrouter",
              "serving": {"backend": "openrouter", "endpoint": {"provider_name": "Fake", "quantization": "bf16"}},
              "request_params": {"temperature": 1.0, "max_tokens": 1000}, "max_new_tokens": 1000,
              "reasoning_effort": "low", "template_kwargs": {}, "chat_template_sha256": None, "behavior_env": {},
              "cli": {"grader_model": "dummy", "n_repeats": 2, "eval_threads": 4}, "code": {"simple_evals_mm": "abc"}}


def write_v04_run(runs_dir, run_id, tasks: dict, provenance: dict | None, judge="gpt-5.1-2025-11-13",
                  tracked: bool = True) -> str:
    """runs/<run_id>/ as the v0.4 orchestrator writes it: config.json, per task items_r<k>.jsonl, score_r<k>.json,
    summary.json, history.jsonl (runs.track)."""
    runs.RUNS_DIR = str(runs_dir)
    runs.write_config(run_id, {**V04_CONFIG, "run_id": run_id, "created_at": "2026-10-21T00:00:00", "tasks": sorted(tasks)})
    for name, reps in tasks.items():
        td = runs.task_dir(run_id, name)
        os.makedirs(td, exist_ok=True)

        def write(name=name, reps=reps, td=td):
            means = []
            for k, rows in reps.items():
                with open(os.path.join(td, f"items_r{k}.jsonl"), "w") as f:
                    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
                scores = [r["score"] for r in rows if r["score"] is not None]
                means.append(sum(scores) / len(scores))
                score = {"score": means[-1], "model_name": "openrouter/fake/model_effort-low", "eval_name": name,
                         "num_examples": len(rows), "num_model_failed": 0, "num_grader_failed": 0,
                         "sampler_config": {"sampler_class": "OpenRouterSampler", "model_id": "fake/model", "max_new_tokens": 1000},
                         "eval_config": {"eval_class": "X", "grader_model_id": f"<obj at {k}>"}, "run_flags": {"cot": False},
                         "model_usage": {"providers": {"Fake": len(rows)}}, "judge_model_name": judge,
                         "request_params": V04_CONFIG["request_params"]}
                if provenance is not None:
                    score["provenance"] = provenance
                with open(os.path.join(td, f"score_r{k}.json"), "w") as f:
                    f.write(json.dumps(score) + "\n")
            mean = sum(means) / len(means)
            with open(os.path.join(td, "summary.json"), "w") as f:
                f.write(json.dumps({"eval_name": name, "n_repeats": len(means), "scores": means, "mean_score": mean,
                                    "num_examples": len(next(iter(reps.values()))), "total_model_failed": 0,
                                    "total_grader_failed": 0}) + "\n")

        if tracked:
            with runs.track(td, "simple_evals"):
                write()
        else:
            write()
    return runs.run_dir(run_id)


class JArith(Dataset):
    """A judge-verified toy dataset (the v0.4 GRADER_TEMPLATE family)."""

    name = "jarith"
    revision = "test-rev"
    verifier = verifiers.judge()
    needs_judge = True
    questions: ClassVar = ["2+2?", "3+3?"]
    truth: ClassVar = {"2+2?": "4", "3+3?": "6"}

    def load(self):
        return [Task(id=q, answer=self.truth[q]) for q in self.questions]

    def prompt(self, task):
        return [user(task.id)]


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(convert, "_DATASETS", {})
    monkeypatch.setattr(runs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setitem(dataset_mod.DATASETS, "jarith", JArith)


def arith_rows(scores=(1.0, 0.0, 1.0), resp=("4", "7", "Paris")):
    q = ["2+2?", "3+3?", "capital of France?"]
    return [row(x, x, a, r, s) for x, a, r, s in zip(q, ["4", "6", "Paris"], resp, scores)]


def provenance_for(repo_common: str, task_text: str) -> dict:
    return {"eval_class": "X", "task_module": TASK_MODULE, "task_module_sha256": sha16(task_text),
            "common_sha256": sha16(repo_common), "code_git": "deadbee", "code_dirty": False}


# ---------------------------------------------------------------- history
def test_history_ok_and_edited(job_env, tmp_path):
    repo, _, common = code_repo(tmp_path / "code")
    run = write_v04_run(tmp_path / "runs", "r1", {"arith": {1: arith_rows()}}, provenance_for(common, "v1 = 2\n"))
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    assert next(x for x in rr.audit if x["check"] == "history")["status"] == "ok"
    with open(os.path.join(run, "arith", "items_r1.jsonl"), "a") as f:
        f.write(json.dumps(row("extra", "q", "a", "r", 1.0)) + "\n")
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    h = next(x for x in rr.audit if x["check"] == "history")
    assert h["status"] == "problem" and "untracked edits" in h["detail"] and "items_r1.jsonl" in h["detail"]
    untracked = write_v04_run(tmp_path / "runs", "r2", {"arith": {1: arith_rows()}}, None, tracked=False)
    rr = convert.convert_run(untracked, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    assert next(x for x in rr.audit if x["check"] == "history")["detail"] == "no history"


# ---------------------------------------------------------------- commit lookup
def test_commit_lookup(tmp_path):
    repo, (c1, c2), common = code_repo(tmp_path / "code")
    g = convert.Git(repo)
    assert g.find_commit(TASK_MODULE, sha16("v1 = 1\n"), sha16(common))["commit"] == c1
    assert g.find_commit(TASK_MODULE, sha16("v1 = 2\n"), sha16(common))["commit"] == c2
    miss = g.find_commit(TASK_MODULE, sha16("v1 = 3\n"), sha16(common))
    assert miss["commit"] is None and not miss["task_found"] and miss["common_found"]
    assert g.find_commit(TASK_MODULE, sha16("v1 = 1\n"), sha16("other"))["commit"] is None


def test_commit_lookup_reports_dirty_and_missing_provenance(job_env, tmp_path):
    repo, _, common = code_repo(tmp_path / "code")
    run = write_v04_run(tmp_path / "runs", "r1", {"arith": {1: arith_rows()}},
                        {**provenance_for(common, "edited\n"), "code_dirty": True})
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    c = next(x for x in rr.audit if x["check"] == "commit")
    assert c["status"] == "problem" and "code_dirty" in c["detail"]
    run = write_v04_run(tmp_path / "runs", "r2", {"arith": {1: arith_rows()}}, None)
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    assert "no provenance" in next(x for x in rr.audit if x["check"] == "commit")["detail"]


# ---------------------------------------------------------------- id mapping
def test_id_mapping_by_row_position_and_turns():
    tasks = [Task(id=f"c{i}", data={"row": i}) for i in range(3)]
    groups, probs = convert.map_items("realworldqa", [{"id": 2}, {"id": "0"}, {"id": 7}, {"id": 2}], tasks)
    assert [(t.id, i) for t, i in groups] == [("c2", [0]), ("c0", [1])]
    assert any("'7'" in p or "7" in p for p in probs) and any("already mapped" in p for p in probs)

    cx = [Task(id=f"fig{i}", data={"original_id": oid, "row": i}) for i, oid in enumerate(["2004.1", "2004.1", "2101.5"])]
    rows = [{"id": "2004.1#reason"}, {"id": "2004.1#reason"}, {"id": "2101.5#reason"}]
    groups, probs = convert.map_items("charxiv", rows, cx)
    assert [t.id for t, _ in groups] == ["fig0", "fig1", "fig2"] and not probs
    _, probs = convert.map_items("charxiv", [{"id": "2101.5#reason"}, *rows[1:]], cx)
    assert any("original_id" in p for p in probs)

    mv = [Task(id="98"), Task(id="99")]
    rows = [{"id": "98#t1"}, {"id": "98#t2"}, {"id": "99#t1"}, {"id": "5#t1"}]
    groups, probs = convert.map_items("multiverse", rows, mv)
    assert [(t.id, i) for t, i in groups] == [("98", [0, 1]), ("99", [2])]
    assert probs == ["item '5#t1': no v0.5 task"]
    _, probs = convert.map_items("multiverse", [{"id": "98#t2"}, {"id": "98#t1"}], mv)
    assert any("not 1..n" in p for p in probs)

    mr = [Task(id="h1", data={"subject": "anatomy", "row": 3})]
    groups, _ = convert.map_items("mmlu_redux", [{"id": "anatomy/3"}], mr)
    assert groups[0][0].id == "h1"


def test_multiverse_dialogue_becomes_one_trial(tmp_path):
    class MV(Dataset):
        name = "multiverse"

        def prompt(self, task):
            return [user("u1")]

        def next_turn(self, task, history):
            n = sum(1 for m in history if m.role == "assistant")
            return [user(f"u{n + 1}")] if n < 2 else None

    run = convert.V04Run("r", str(tmp_path), {}, {})
    rows = [row("7#t1", "u1", "[]", "a1", 0.5), row("7#t2", "u2", "[]", "a2", 1.0)]
    tr = convert.build_trial(run, MV(), Task(id="7"), rows, 1, "sglang/x", None, "job", convert._HashOnlyImages(str(tmp_path)), "cs")
    steps = tr.trajectory.steps
    assert [(s.source, s.text) for s in steps] == [("user", "u1"), ("agent", "a1"), ("user", "u2"), ("agent", "a2")]
    assert steps[0].extra == {"reconstructed": True} and tr.generation_lock is None
    assert tr.reused_from == {"v04_run": "r", "item_ids": ["7#t1", "7#t2"], "repeat": 1}
    g = convert.legacy_result(Task(id="7"), rows, True, "gpt-5.1-2025-11-13")
    assert g.reward == 0.75 and [t["turn"] for t in g.details["turns"]] == [1, 2]
    failed = [row("7#t1", "u1", "[]", "a1", 0.5), row("7#t2", "u2", "[]", "", None, error="model_failed: APIError: 503 upstream")]
    tr = convert.build_trial(run, MV(), Task(id="7"), failed, 1, "sglang/x", None, "job", convert._HashOnlyImages(str(tmp_path)), "cs")
    assert tr.cause == errors.PROVIDER_FAILED and len(tr.trajectory.agent_steps) == 1


# ---------------------------------------------------------------- judge cache
def jarith_rows(gr=None):
    gr = gr or VERDICT.format("yes")
    return [row("2+2?", "2+2?", "4", "4", 1.0, grader_response=gr), row("3+3?", "3+3?", "6", "7", 0.0,
                                                                          grader_response=VERDICT.format("no"))]


def test_judge_cache_identical_prompt_is_reused(tmp_path):
    repo, _, common = code_repo(tmp_path / "code")
    run = write_v04_run(tmp_path / "runs", "r1", {"jarith": {1: jarith_rows()}}, provenance_for(common, "v1 = 2\n"))
    live = FakeSampler(VERDICT.format("no"))
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo, live_judge=live)
    r = rr.datasets["jarith"]
    assert r.cache["hits"] == 2 and r.cache["misses"] == 0 and not live.calls
    assert [g.reward for g in r.regrade[1]] == [1.0, 0.0] and r.regrade[1][0].judge["cached"]["from"] == "v0.4"
    assert rr.judge_calls_needed == 0


def test_judge_cache_other_template_calls_the_judge(tmp_path):
    with open(os.path.join(REPO_ROOT, convert.V04_COMMON), encoding="utf-8") as f:
        other = f.read().replace("Judge whether the following", "Decide whether the following")
    repo, _, common = code_repo(tmp_path / "code", common=other)
    run = write_v04_run(tmp_path / "runs", "r1", {"jarith": {1: jarith_rows()}}, provenance_for(common, "v1 = 2\n"))
    live = FakeSampler(VERDICT.format("yes"))
    rr = convert.convert_run(run, str(tmp_path / "jobs"), repo=repo, live_judge=live)
    r = rr.datasets["jarith"]
    assert r.cache["hits"] == 0 and r.cache["live_calls"] == 2 and len(live.calls) == 2
    assert "differs" in r.cache["unusable"]
    # the v0.4 verdict 0.0 vs the new judge's yes: one flip of two = 50% > 1%
    assert rr.classification == convert.UNVERIFIED and any("flips" in x for x in rr.reasons)


def test_judge_cache_only_gives_judge_not_cached(tmp_path):
    repo, _, common = code_repo(tmp_path / "code")
    rows = jarith_rows()
    rows[1]["question"] = "a question v0.5 renders differently"  # another prompt: not cached
    run = write_v04_run(tmp_path / "runs", "r1", {"jarith": {1: rows}}, provenance_for(common, "v1 = 2\n"))
    jobs = str(tmp_path / "jobs")
    rr = convert.convert_run(run, jobs, judge_cache_only=True, repo=repo, live_judge=FakeSampler("x"))
    r = rr.datasets["jarith"]
    assert r.cache["hits"] == 1 and r.cache["misses"] == 1 and rr.judge_calls_needed == 1
    g = {x.task_id: x for x in r.regrade[1]}
    assert g["3+3?"].cause == errors.JUDGE_NOT_CACHED and g["3+3?"].reward is None and g["2+2?"].reward == 1.0
    assert rr.classification == convert.UNVERIFIED and any("not cached" in x for x in rr.reasons)
    st = JobStore(jobs, "r1__legacy")
    assert st.current_verification_id("jarith") == "legacy"  # the incomplete regrade does not become current
    assert len(os.listdir(st.verifier_dir("jarith"))) == 3  # legacy, the regrade, current


def test_judge_cache_needs_the_same_judge(tmp_path):
    repo, _, common = code_repo(tmp_path / "code")
    run = write_v04_run(tmp_path / "runs", "r1", {"jarith": {1: jarith_rows()}}, provenance_for(common, "v1 = 2\n"),
                        judge="gpt-4o-2024-11-20")
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    assert rr.datasets["jarith"].cache["hits"] == 0 and rr.judge_calls_needed == 2


# ---------------------------------------------------------------- classification and the round trip
def test_classification_and_round_trip(job_env, tmp_path, capsys):
    repo, (_, c2), common = code_repo(tmp_path / "code")
    run = write_v04_run(tmp_path / "runs", "r1", {"arith": {1: arith_rows(), 2: arith_rows((1.0, 0.0, 0.0), ("4", "7", "Lyon"))}},
                        provenance_for(common, "v1 = 2\n"))
    sp = os.path.join(run, "arith", "summary.json")
    with open(sp) as f:
        s = json.load(f)
    jobs = job_env["jobs"]
    rr = convert.convert_run(run, jobs, repo=repo)
    assert rr.classification == convert.VERIFIED, rr.reasons
    assert rr.datasets["arith"].commit == c2
    st = JobStore(jobs, "r1__legacy")
    lock, cfg = st.read_lock(), st.read_config()
    assert lock["legacy"] is True and lock["classification"] == convert.VERIFIED and lock["code"]["commit"] == c2
    assert lock["code"]["checked"] is False and lock["datasets"]["arith"]["verifier_lock"] is None
    assert cfg["model"] == "openrouter/fake/model" and cfg["n_attempts"] == 2 and cfg["effort"] == "low"
    assert cfg["max_output_tokens"] == 1000
    t = st.read_trials("arith", 1)[0]
    assert t.generation_lock is None and t.reused_from["v04_run"] == "r1"
    assert t.trajectory.steps[0].extra == {"reconstructed": True}
    # sem job check / sem job show
    assert job_env["sem"]("job", "check", st.dir) == 0
    capsys.readouterr()
    assert job_env["sem"]("job", "show", st.dir, "--json") == 0
    shown = json.loads(capsys.readouterr().out)["summary"]["arith"]
    assert shown["rewards"] == pytest.approx([2 / 3, 1 / 3]) and shown["reward"] == pytest.approx(0.5)
    assert shown["rewards"] == pytest.approx(s["scores"])
    # sem run --from: legacy trials are RERUN
    assert job_env["run"]("--from", st.dir, "--dry-run", name="new") == 0
    plan = json.loads(capsys.readouterr().out)
    fr = plan["from"]["datasets"]["arith"]
    assert fr["counts"] == {"reuse": 0, "regrade": 0, "rerun": 3}
    assert all("legacy" in r for r in fr["reasons"])
    # a legacy job is never resumed
    assert job_env["sem"]("run", "-m", "sglang/fake", "-d", "arith", "--job-name", "r1__legacy", "-o", jobs) == 2


def test_classification_reasons(job_env, tmp_path):
    repo, _, common = code_repo(tmp_path / "code")
    good = provenance_for(common, "v1 = 2\n")
    # stale stored aggregates alone keep legacy-verified
    run = write_v04_run(tmp_path / "runs", "ok", {"arith": {1: arith_rows()}}, good)
    sp = os.path.join(run, "arith", "summary.json")
    with open(sp) as f:
        s = json.load(f)
    s["mean_score"] = 0.9
    with runs.track(os.path.join(run, "arith"), "rescore"), open(sp, "w") as f:
        f.write(json.dumps(s) + "\n")
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    agg = next(x for x in rr.audit if x["check"] == "aggregates")
    assert agg["status"] == "stale" and agg["differences"][0]["field"] == "mean_score"
    assert rr.classification == convert.VERIFIED
    # a v0.4 score that the deterministic verifier does not reproduce: unverified
    bad = arith_rows((1.0, 1.0, 1.0))
    run = write_v04_run(tmp_path / "runs", "flip", {"arith": {1: bad}}, good)
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo)
    assert rr.classification == convert.UNVERIFIED and any("deterministic" in x for x in rr.reasons)
    # an unmapped item, and --no-regrade
    rows = [*arith_rows(), row("5+5?", "5+5?", "10", "10", 1.0)]
    run = write_v04_run(tmp_path / "runs", "ids", {"arith": {1: rows}}, good)
    rr = convert.convert_run(run, str(tmp_path / "jobs"), dry_run=True, repo=repo, no_regrade=True)
    assert any(x.startswith("arith: ids:") for x in rr.reasons) and any("--no-regrade" in x for x in rr.reasons)


def test_convert_all_cli(job_env, tmp_path, capsys):
    repo, _, common = code_repo(tmp_path / "code")
    write_v04_run(tmp_path / "runs", "a", {"arith": {1: arith_rows()}}, provenance_for(common, "v1 = 2\n"))
    write_v04_run(tmp_path / "runs", "b", {"jarith": {1: jarith_rows()}}, None)
    sel = tmp_path / "sel.txt"
    sel.write_text("a,b,missing\n")
    code = job_env["sem"]("convert", "--all", str(tmp_path / "runs"), "--select", str(sel), "--dry-run", "--repo", repo)
    out = capsys.readouterr().out
    assert code == 1  # a missing run
    assert "legacy-verified" in out and "legacy-unverified" in out and "missing" in out
    assert "total judge calls needed beyond the cache: 0" in out
    assert not os.path.exists(os.path.join(job_env["jobs"], "a__legacy"))


def test_convert_json_is_clean(job_env, tmp_path, capsys):
    repo, _, common = code_repo(tmp_path / "code")
    run = write_v04_run(tmp_path / "runs", "a", {"arith": {1: arith_rows()}}, provenance_for(common, "v1 = 2\n"))
    capsys.readouterr()
    assert job_env["sem"]("convert", run, "--dry-run", "--json", "--repo", repo) == 0
    doc = json.loads(capsys.readouterr().out)  # stdout is the JSON document only
    assert doc["classification"] == convert.VERIFIED and doc["job_dir"] is None
