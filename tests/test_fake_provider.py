"""The scripted test-only provider `fake/` (samplers/fake.py): absent unless enabled, deterministic, and its odd answers
(cut off, refused, tricky unicode, one transient failure) go through the real runner, store, verifiers and summary."""

from typing import ClassVar

import pytest

from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm import errors, verifiers
from simple_evals_mm.dataset import Dataset
from simple_evals_mm.models import Message, Task, Text, user
from simple_evals_mm.samplers import fake, registry


class FakeMCQ(Dataset):
    name = "fmcq"
    revision = "test-rev"
    verifier = verifiers.mcq()
    needs_judge = True

    def load(self):
        return [Task(id=f"q{i}", answer="ABCD"[i % 4]) for i in range(46)]

    def prompt(self, task):
        return [user(f"Question {task.id}?\nA. one\nB. two\nC. three\nD. four\n"
                     "The last line of your response should be 'Answer: $LETTER'.")]


class FakeOpen(Dataset):
    name = "fopen"
    revision = "test-rev"
    verifier = verifiers.judge()
    needs_judge = True
    answers: ClassVar = ["Paris", "4", "blue"]

    def load(self):
        return [Task(id=f"o{i}", answer=self.answers[i % 3]) for i in range(46)]

    def prompt(self, task):
        return [user(f"Open question {task.id}?")]


def test_absent_unless_enabled():
    if "fake" in registry.PROVIDERS:
        pytest.skip("SEM_FAKE_PROVIDER=1 in this environment")
    with pytest.raises(errors.ConfigError, match="unknown provider 'fake'"):
        registry.resolve("fake/scripted")


def test_answers_are_deterministic(fake_provider):
    s = registry.make_sampler(registry.resolve("fake/scripted"))
    assert isinstance(s, fake.ScriptedSampler)
    msgs = [Message("user", [Text("Which?\nA. x\nB. y\nC. z\nAnswer: $LETTER")])]
    seeds = [s0 for s0 in range(200) if (s0 + 1) % 23 > 3]  # normal answers
    outs = [s(msgs, seed=k).text for k in seeds[:5]]
    assert outs == [s(msgs, seed=k).text for k in seeds[:5]]
    assert all(o.splitlines()[-1] in ("Answer: A", "Answer: B", "Answer: C") for o in outs)
    assert s(msgs, seed=22).finish_reason == "length" and s(msgs, seed=0).finish_reason == "content_filter"
    assert " " in s(msgs, seed=1).text
    with pytest.raises(errors.SamplerError) as e:
        s(msgs, seed=2)
    assert e.value.cause == errors.PROVIDER_FAILED
    assert s(msgs, seed=2).finish_reason == "stop"  # the retry succeeds
    boxed = s([Message("user", [Text("Put the answer in \\boxed{}.")])], seed=5)
    assert "\\boxed{" in boxed.text


def test_judge_formats(fake_provider):
    j = registry.make_sampler(registry.resolve("fake/judge"))

    def ask(prompt):
        return j([user(prompt)]).text

    assert verifiers.parse_correct_verdict(ask(verifiers.GRADER_TEMPLATE.format(question="q", response="r", correct_answer="a"))) \
        in ("yes", "no", None)
    assert ask('Grade it.\nJust return the letters "A", "B", or "C", with no text around it. #1') in ("A", "B", "I cannot decide.")
    assert ask("Expression 1: 1\n    Expression 2: 1\n") in ("Yes", "No", "I cannot decide.")
    undecided = "I cannot decide."
    pb = ask("Answer with [reason] ... [judge] True/False. x")
    assert pb == undecided or pb.startswith("[reason]") and "[judge]" in pb
    cl = ask("[CHECKLIST]\n1. a?\n2. b?")
    assert cl == undecided or set(__import__("json").loads(cl)) == {"1", "2"}
    from simple_evals_mm.tasks import mmdu

    sc = ask("Rate it. Overall Score")
    assert sc == undecided or mmdu.parse_judge(sc) is not None


def test_full_job_with_real_runner(job_env, fake_provider, monkeypatch):
    monkeypatch.setitem(dataset_mod.DATASETS, "fmcq", FakeMCQ)
    monkeypatch.setitem(dataset_mod.DATASETS, "fopen", FakeOpen)
    sem, jobs = job_env["sem"], job_env["jobs"]
    assert sem("run", "-m", "fake/scripted", "-d", "fmcq,fopen", "--judge-model", "fake/judge", "--job-name", "f",
               "-o", jobs, "-n", "4") == 0
    st = job_env["store"]("f")
    from simple_evals_mm import runner

    s = runner.summarize_job(st)
    for name in ("fmcq", "fopen"):
        a = s[name]["attempts"]["a1"]
        assert a["n"] == 46 and s[name]["reward"] is not None
        assert {"budget_exhausted", "refused"} <= set(a["causes"]), a["causes"]
        assert len(st.read_trials(name, 1)) == 46
    raw = ""
    for n in ("fmcq", "fopen"):
        with open(st.trials_path(n, 1), "rb") as f:
            raw += f.read().decode("utf-8")
    assert " " in raw and "\x85" in raw  # raw in the JSON lines, read back line by line
    retried = [t for n in ("fmcq", "fopen") for t in st.read_trials(n, 1) if t.trajectory.extra.get("retries")]
    assert retried and all(t.exception_info is None for t in retried)
    assert sem("job", "check", st.dir) == 0
    # a derived job on the same commit reuses every trial
    assert sem("run", "-m", "fake/scripted", "-d", "fmcq,fopen", "--judge-model", "fake/judge", "--job-name", "g",
               "-o", jobs, "-n", "4", "--from", st.dir) == 0
    lock = job_env["store"]("g").read_lock()
    assert all(c["rerun"] == 0 and c["reuse"] == 46 for c in lock["derived_from"]["counts"].values())
