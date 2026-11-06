"""`sem run --from JOB` (derive.py): per (dataset, attempt, task) REUSE / REGRADE / RERUN, copies with reused_from,
reused verifier results, `--dry-run` counts, lock.json derived_from."""

import functools
import json
import subprocess

import pytest

from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm import derive, provenance, runner
from simple_evals_mm.models import VerifierResult
from simple_evals_mm.samplers import registry
from tests.fakes import ANSWERS, VERDICT, Arith, FakeSampler


class JudgedArith(Arith):
    """Arith verified by the judge (its verifier lock includes the judge's identity)."""

    name = "jarith"
    needs_judge = True

    @staticmethod
    def verifier(task, traj, judge):
        from simple_evals_mm.models import user

        r = judge([user(task.id)])  # FakeSampler: the reply text is the verdict
        return VerifierResult(task.id, float("correct: yes" in r.text and traj.final_text == task.answer), method="judge")


class SettingsSampler(FakeSampler):
    """A fake whose config carries the resolved sampling (as the real samplers' config does)."""

    def config(self):
        return {**super().config(), "sampling": dict(self.spec.sampling)}


@pytest.fixture
def env(job_env, monkeypatch):
    s = job_env
    monkeypatch.setitem(dataset_mod.DATASETS, "jarith", JudgedArith)
    s["sampler"] = SettingsSampler(lambda q: ANSWERS[q])

    def make(spec, base_url=None):
        if "judge" in spec.model:
            return s["judge"]
        s["sampler"].spec = spec
        return s["sampler"]

    monkeypatch.setattr(registry, "make_sampler", make)

    def run(*extra, name="j", ds="arith"):
        return s["sem"]("run", "-m", "sglang/fake", "-d", ds, "--job-name", name, "-o", s["jobs"], "-n", "1",
                        "--judge-model", "sglang/judge", *extra)

    s["run"] = run
    return s


def _reused(st, ds="arith"):
    return {t.task_name: (t.reused_from or {}).get("decision") for t in st.read_trials(ds, 1)}


def test_identical_config_reuses_everything(env, capsys):
    s = env
    assert s["run"](name="a") == 0
    src = s["store"]("a")
    s["sampler"].calls = []
    capsys.readouterr()
    assert s["run"]("--from", src.dir, "--dry-run", "--json", name="b") == 0
    plan = json.loads(capsys.readouterr().out)["from"]["datasets"]["arith"]
    assert plan["counts"] == {"reuse": 3, "regrade": 0, "rerun": 0}
    assert s["run"]("--from", src.dir, name="b") == 0
    st = s["store"]("b")
    assert not s["sampler"].calls  # nothing generated
    assert _reused(st) == {"2+2?": "reuse", "3+3?": "reuse", "capital of France?": "reuse"}
    t = st.read_trials("arith", 1)[0]
    src_t = src.read_trials("arith", 1)[0]
    assert t.reused_from["job_id"] == src.read_lock()["job_id"] and t.reused_from["trial_id"] == src_t.id
    assert t.id != src_t.id and t.generation_lock == src_t.generation_lock
    lock = st.read_lock()
    assert lock["derived_from"]["counts"]["arith"] == {"reuse": 3, "regrade": 0, "rerun": 0}
    assert st.read_verification_meta("arith")["reused_results"] == 3
    assert runner.summarize_job(st)["arith"]["reward"] == runner.summarize_job(src)["arith"]["reward"]
    assert st.check() == []
    # resuming the derived job copies nothing twice
    assert s["run"]("--from", src.dir, name="b") == 0
    assert len(st.read_trials("arith", 1)) == 3


def test_judge_changed_regrades(env):
    s = env
    assert s["run"](name="a", ds="jarith") == 0
    s["judge"] = FakeSampler(VERDICT.format("yes"), slug="sglang/judge", endpoint={"model_id": "judge-2"})
    s["sampler"].calls = []
    assert s["run"]("--from", s["store"]("a").dir, name="b", ds="jarith") == 0
    st = s["store"]("b")
    assert not s["sampler"].calls and set(_reused(st, "jarith").values()) == {"regrade"}
    assert "another verifier lock" in st.read_trials("jarith", 1)[0].reused_from["reason"]
    assert "reused_results" not in st.read_verification_meta("jarith")  # verified anew with the new judge
    assert st.read_lock()["derived_from"]["counts"]["jarith"] == {"reuse": 0, "regrade": 3, "rerun": 0}


def test_sampler_setting_changed_reruns(env, capsys):
    s = env
    assert s["run"](name="a") == 0
    s["sampler"].calls = []
    capsys.readouterr()
    assert s["run"]("--from", s["store"]("a").dir, "--temperature", "0.3", "--dry-run", "--json", name="b") == 0
    plan = json.loads(capsys.readouterr().out)["from"]["datasets"]["arith"]
    assert plan["counts"] == {"reuse": 0, "regrade": 0, "rerun": 3}
    assert any("generation_lock differs (sampler sampling.temperature" in r for r in plan["reasons"])
    assert s["run"]("--from", s["store"]("a").dir, "--temperature", "0.3", name="b") == 0
    assert len(s["sampler"].calls) == 3 and set(_reused(s["store"]("b")).values()) == {None}


def test_task_content_changed_reruns_that_task(env, monkeypatch):
    s = env
    assert s["run"](name="a") == 0
    monkeypatch.setattr(Arith, "truth", {**Arith.truth, "3+3?": "6 (fixed)"})
    s["sampler"].calls = []
    assert s["run"]("--from", s["store"]("a").dir, name="b") == 0
    assert [m[-1].text for m in s["sampler"].calls] == ["3+3?"]
    assert _reused(s["store"]("b")) == {"2+2?": "reuse", "capital of France?": "reuse", "3+3?": None}


def test_transient_source_failure_reruns(env):
    from simple_evals_mm import errors

    s = env
    s["sampler"].fail = {"capital": errors.SamplerError("503 overloaded")}
    assert s["run"]("-r", "0", name="a") == 0
    s["sampler"].fail, s["sampler"].calls = {}, []
    assert s["run"]("--from", s["store"]("a").dir, name="b") == 0
    assert [m[-1].text for m in s["sampler"].calls] == ["capital of France?"]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, files: dict) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("changed_file,expect", [("verifiers.py", "regrade"), ("tasks/arith.py", "rerun"),
                                                 ("agent.py", "rerun"), ("samplers/openai_chat.py", "rerun"),
                                                 ("samplers/gemini.py", "reuse"), ("store.py", "reuse"),
                                                 ("newmod.py", "rerun")])
def test_commits_differ(env, tmp_path, monkeypatch, changed_file, expect):
    """Different commits: provenance.changed_datasets decides (a verifier-side change regrades; a change of the
    dataset's own, the generation-side files, this job's sampler or an unknown package file reruns; another API
    format's sampler and NEUTRAL files reuse), in a temporary git repository."""
    s = env
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    pkg = "src/simple_evals_mm"
    c1 = _commit(repo, {f"{pkg}/__init__.py": "", f"{pkg}/verifiers.py": "V = 1\n", f"{pkg}/agent.py": "A = 1\n",
                        f"{pkg}/store.py": "S = 1\n", f"{pkg}/samplers/openai_chat.py": "C = 1\n",
                        f"{pkg}/samplers/gemini.py": "G = 1\n",
                        f"{pkg}/tasks/arith.py": "from simple_evals_mm import verifiers\n"})
    c2 = _commit(repo, {f"{pkg}/{changed_file}": "# changed\n"})
    monkeypatch.setattr(provenance, "changed_datasets", functools.partial(
        provenance.changed_datasets, repo=str(repo), package_dir=str(repo / pkg),
        modules={"arith": "simple_evals_mm.tasks.arith"}))
    s["commit"] = c1
    assert s["run"](name="a") == 0
    s["commit"] = c2
    s["sampler"].calls = []
    assert s["run"]("--from", s["store"]("a").dir, name="b") == 0
    st = s["store"]("b")
    assert st.read_lock()["derived_from"]["counts"]["arith"][expect] == 3
    if expect == "reuse":
        assert not s["sampler"].calls
    elif expect == "regrade":
        assert not s["sampler"].calls and "verification files changed" in st.read_trials("arith", 1)[0].reused_from["reason"]
        assert "reused_results" not in st.read_verification_meta("arith")
    else:
        assert len(s["sampler"].calls) == 3


def test_same_settings_other_commit_without_changes_reuses(env, tmp_path, monkeypatch):
    s = env
    monkeypatch.setattr(derive, "plan", functools.partial(derive.plan, changed_datasets=lambda a, b, names, **kw: {
        n: {"changed": False, "dataset": [], "generation": [], "verification": [], "unknown": []} for n in names}))
    s["commit"] = "c1"
    assert s["run"](name="a") == 0
    s["commit"] = "c2"
    s["sampler"].calls = []
    assert s["run"]("--from", s["store"]("a").dir, name="b") == 0
    st = s["store"]("b")
    # everything but the commit matches: the generation lock is recomputed with the source commit, the verifier lock
    # at the source verification's commit
    assert not s["sampler"].calls
    assert st.read_lock()["derived_from"]["counts"]["arith"] == {"reuse": 3, "regrade": 0, "rerun": 0}
    assert st.read_verification_meta("arith")["reused_results"] == 3


def _none_changed(a, b, names, **kw):
    return {n: {"changed": False, "generation_relevant": False, "dataset": [], "generation": [], "verification": [],
                "unknown": []} for n in names}


def test_mixed_commit_trials_classified_per_generation_commit(env, monkeypatch):
    """A job resumed across commits holds trials of two commits: each is classified with its own generation_commit
    (the generation_lock recomputed with it, provenance from it), not with the job's latest commit."""
    s = env
    monkeypatch.setattr(provenance, "is_ancestor", lambda a, b, **kw: True)
    monkeypatch.setattr(provenance, "changed_datasets", _none_changed)
    s["commit"] = "c1"
    s["sampler"].fail = {"capital": KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        s["run"](name="a")
    s["commit"], s["sampler"].fail = "c2", {}
    assert s["run"](name="a") == 0  # resumed in place at c2
    src = s["store"]("a")
    assert {t.task_name: t.generation_commit for t in src.read_trials("arith", 1)} == {
        "2+2?": "c1", "3+3?": "c1", "capital of France?": "c2"}

    # c3: the generation side changed since c1, not since c2
    def changed(a, b, names, **kw):
        out = _none_changed(a, b, names)
        if a == "c1":
            for n in names:
                out[n].update(changed=True, generation_relevant=True, generation=["src/simple_evals_mm/agent.py"])
        return out

    monkeypatch.setattr(provenance, "changed_datasets", changed)
    s["commit"], s["sampler"].calls = "c3", []
    assert s["run"]("--from", src.dir, name="b") == 0
    st = s["store"]("b")
    assert sorted(m[-1].text for m in s["sampler"].calls) == ["2+2?", "3+3?"]
    assert _reused(st) == {"2+2?": None, "3+3?": None, "capital of France?": "reuse"}
    by = {t.task_name: t for t in st.read_trials("arith", 1)}
    assert by["capital of France?"].generation_commit == "c2" and by["2+2?"].generation_commit == "c3"


def test_generation_commit_of_trials_written_before_it_was_recorded(env, monkeypatch):
    """Trials without generation_commit: a copy resolves through its source job, else the job's first commit."""
    s = env
    monkeypatch.setattr(derive, "plan", functools.partial(derive.plan, changed_datasets=_none_changed))
    s["commit"] = "c1"
    assert s["run"](name="a") == 0
    s["commit"] = "c2"
    assert s["run"]("--from", s["store"]("a").dir, name="b") == 0
    for name in ("a", "b"):  # strip the field, as in a job written before it existed
        path = s["store"](name).trials_path("arith", 1)
        with open(path) as f:
            rows = [json.loads(line) for line in f]
        with open(path, "w") as f:
            f.write("".join(json.dumps({k: v for k, v in r.items() if k != "generation_commit"}) + "\n" for r in rows))
    b = s["store"]("b")
    lock = b.read_lock()
    assert lock["code"]["commit"] == "c2"
    assert {derive.generation_commit(t, lock) for t in b.read_trials("arith", 1)} == {"c1"}  # copies of a's trials
    a = s["store"]("a")
    assert {derive.generation_commit(t, a.read_lock()) for t in a.read_trials("arith", 1)} == {"c1"}
