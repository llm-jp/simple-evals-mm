"""v0.5 jobs: run, resume, regrade, check, locks, preflight, retry."""

import json
import os

import pytest

from simple_evals_mm import checks, errors, runner
from simple_evals_mm.models import Response
from simple_evals_mm.samplers import registry
from tests.fakes import ANSWERS, Arith, FakeSampler


def test_run_resume_after_interruption_and_refusals(job_env, capsys):
    s = job_env
    s["sampler"].fail = {"capital": KeyboardInterrupt()}  # the process dies on the third task
    with pytest.raises(KeyboardInterrupt):
        s["run"]()
    st = s["store"]()
    assert [t.task_name for t in st.read_trials("arith", 1)] == ["2+2?", "3+3?"] and not st.is_complete("arith", 1)
    with open(st.trials_path("arith", 1), "a") as f:
        f.write('{"torn": ')  # killed mid-write
    job_id = st.read_lock()["job_id"]

    s["sampler"].fail, s["sampler"].calls = {}, []
    assert s["run"]() == 0
    assert [m[-1].text for m in s["sampler"].calls] == ["capital of France?"]  # only the missing task
    assert st.is_complete("arith", 1) and st.read_lock()["job_id"] == job_id
    summary = runner.summarize_job(st)["arith"]
    assert summary["reward"] == pytest.approx(2 / 3)
    assert summary["attempts"]["a1"]["categories"]["math"]["reward"] == 0.5
    trial = st.read_trials("arith", 1)[0]
    assert trial.trial_name == "2+2?__a1" and trial.trajectory.session_id == job_id and trial.task_checksum

    capsys.readouterr()
    assert s["run"]("--temperature", "0.5") == 2  # different config under the same job name
    assert runner.RESUME_REFUSED in capsys.readouterr().err
    s["commit"] = "c2"
    assert s["run"]() == 2
    assert "commit" in capsys.readouterr().err


def test_generate_only_then_regrade(job_env):
    s = job_env
    assert s["run"]("--generate-only") == 0
    st = s["store"]()
    assert st.current_verification_id("arith") is None and st.is_complete("arith", 1)
    assert s["sem"]("job", "regrade", st.dir) == 0
    first = st.current_verification_id("arith")
    assert first and runner.summarize_job(st)["arith"]["reward"] == pytest.approx(2 / 3)
    assert s["sem"]("job", "regrade", "-p", st.dir) == 0
    second = st.current_verification_id("arith")
    assert second != first and sorted(os.listdir(st.verifier_dir("arith"))) == sorted([first, second, "current"])


@pytest.mark.parametrize("zero", [True, False])
def test_aggregation_follows_policy(job_env, monkeypatch, zero):
    s = job_env
    monkeypatch.setattr(Arith, "questions", ["2+2?", "cut", "refuse", "flaky"])
    monkeypatch.setattr(Arith, "counts_unanswerable_as_zero", zero)
    s["sampler"].reply = lambda q: Response(text="partial", finish_reason="length") if q == "cut" else Response(text=ANSWERS[q])
    s["sampler"].fail = {"refuse": errors.SamplerError("data_inspection_failed"), "flaky": errors.SamplerError("503 overloaded")}
    assert s["run"]("-r", "1") == 0
    st = s["store"]()
    assert sum(1 for m in s["sampler"].calls if m[-1].text == "flaky") == 2  # retried once (transient), refusal not
    a1 = runner.summarize_job(st)["arith"]["attempts"]["a1"]
    assert a1["causes"] == {"budget_exhausted": 1, "provider_failed": 1, "refused": 1}
    assert a1["n"] == 4 and a1["n_counted"] == (3 if zero else 1)
    assert a1["reward"] == pytest.approx(1 / 3 if zero else 1.0)
    failed = {t.task_name: t.exception_info for t in st.read_trials("arith", 1)}
    assert failed["flaky"]["cause"] == "provider_failed" and failed["2+2?"] is None

    s["sampler"].fail = {}
    assert s["sem"]("job", "resume", "-p", st.dir, "-f", "provider_failed") == 0  # = retry of the transient failures
    assert runner.summarize_job(st)["arith"]["attempts"]["a1"]["causes"] == {"budget_exhausted": 1, "refused": 1}


def test_job_check_detects_edits(job_env, capsys):
    s = job_env
    assert s["run"]("-k", "2") == 0
    st = s["store"]()
    assert s["sem"]("job", "check", st.dir) == 0
    path = st.trials_path("arith", 2)
    with open(path) as f:
        lines = f.read().splitlines()
    lines[0] = lines[0].replace('"4"', '"5"')
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    capsys.readouterr()
    assert s["sem"]("job", "check", st.dir) == 1
    out = capsys.readouterr().out
    assert "trials_a2.jsonl: sha256 differs" in out and "verified a2 trials differ" in out and "trials_a1" not in out


def test_preflight(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SGLANG_BASE_URL", "http://127.0.0.1:9/v1")  # discard port: nothing listens
    problems = runner.preflight(registry.make_sampler(registry.resolve("sglang/m")),
                                registry.make_sampler(registry.resolve("openrouter/openai/gpt-5.1")))
    assert any(p.startswith("model: base URL from SGLANG_BASE_URL is unreachable") for p in problems)
    assert any(p.startswith("judge: OPENROUTER_API_KEY is not set") for p in problems)
    assert "127.0.0.1" not in " ".join(problems)
    assert runner.preflight(FakeSampler(), FakeSampler()) == []  # fakes have no preflight


def test_dataset_preflight(job_env, monkeypatch, capsys):
    """Dataset.preflight(verify) runs in the job preflight with verify = not --generate-only; --skip-preflight skips it."""
    s, seen = job_env, []

    def preflight(self, verify):
        seen.append(verify)
        return ["arith: the evaluator image is missing"] if verify else []

    monkeypatch.setattr(Arith, "preflight", preflight, raising=False)
    assert s["run"](name="v") == 2
    assert "dataset arith: the evaluator image is missing" in capsys.readouterr().err and seen == [True]
    assert not os.path.exists(os.path.join(s["jobs"], "v", "arith"))  # failed before any trial
    assert s["run"]("--generate-only", name="g") == 0 and seen[-1] is False
    n = len(seen)
    assert s["run"]("--skip-preflight", name="k") == 0 and len(seen) == n


@pytest.mark.parametrize("retry,calls", [({}, 3), ({"exclude_causes": ["provider_failed"]}, 1), ({"include_causes": []}, 1)])
def test_retry_config(job_env, monkeypatch, tmp_path, retry, calls):
    s = job_env
    monkeypatch.setattr(Arith, "questions", ["flaky"])
    s["sampler"].fail = {"flaky": errors.SamplerError("503")}
    cfg = tmp_path / "job.yaml"
    cfg.write_text(json.dumps({"retry": retry}))  # JSON is YAML
    assert s["run"]("-c", str(cfg)) == 0
    assert len(s["sampler"].calls) == calls  # default max_retries 2


def test_trial_locks_and_dry_run(job_env, capsys):
    s = job_env
    assert s["run"]("-k", "2") == 0
    st = s["store"]()
    a1, a2 = st.read_trials("arith", 1), st.read_trials("arith", 2)
    assert [t.generation_lock for t in a1] == [t.generation_lock for t in a2]  # same request in both attempts
    assert len({t.generation_lock for t in a1}) == 3 and len({t.verifier_lock for t in a1 + a2}) == 1
    assert st.read_lock()["datasets"]["arith"]["verifier_lock"] == a1[0].verifier_lock
    capsys.readouterr()
    assert s["run"]("-k", "3", "--dry-run") == 0
    plan = json.loads(capsys.readouterr().out)
    assert {k: plan["datasets"]["arith"][k] for k in ("n_tasks", "trials_done", "trials_to_run")} == {"n_tasks": 3, "trials_done": 6, "trials_to_run": 3}
    assert plan["model"]["api_key_var"] == "SGLANG_API_KEY" and plan["model"]["api_format"] == "openai_chat_completions"


def test_job_lock_and_dirty_regrade(job_env, monkeypatch, capsys):
    s = job_env
    assert s["run"]("--generate-only") == 0
    st = s["store"]()
    with st.lock():  # another process holds the job: run / regrade stop without writing
        assert s["run"]() == 2 and s["sem"]("job", "regrade", st.dir) == 2
    assert "in use by another sem process" in capsys.readouterr().err
    assert st.current_verification_id("arith") is None

    def dirty(allow_dirty=False, **kw):
        if not allow_dirty:
            raise checks.CheckError("start checks failed")
        return {"commit": "c1", "dirty": True, "lock_check": {"ok": True}}

    monkeypatch.setattr(checks, "start_checks", dirty)
    assert s["sem"]("job", "regrade", st.dir) == 2 and st.current_verification_id("arith") is None
    assert s["sem"]("job", "regrade", st.dir, "--allow-dirty") == 0
    vid = st.current_verification_id("arith")
    with open(os.path.join(st.verifier_dir("arith"), vid, "meta.json")) as f:
        assert json.load(f)["dirty"] is True
