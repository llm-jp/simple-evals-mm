"""`sem jobs compare`: the per-dataset comparability of jobs from different commits (compare.classify)."""

import pytest

from simple_evals_mm import compare


class FakeGolden:
    def __init__(self, rewards=None, bodies=()):
        self.rewards, self.bodies = rewards or {}, list(bodies)

    def reward_part(self, commit, name):
        return self.rewards.get(commit)

    def bodies_changed(self, a, b, models):
        return self.bodies


def facts(job, gen="c1", ver="c1", prompts=None, tasks="t", args=None, judge=None, model="m"):
    return {"job": f"/jobs/{job}", "api_model": model, "generation_commits": [gen], "verification_commit": ver,
            "judge": judge, "tasks_sha256": tasks, "n_tasks": 2, "args": args or {}, "sampler_modules": ["samplers.x"],
            "prompts": prompts if prompts is not None else {"a": "p1", "b": "p2"}, "reward": 0.5, "std": None,
            "causes": {}}


def changed_with(rec):
    def changed(a, b, datasets, **kw):
        return {n: {"dataset": [], "generation": [], "verification": [], "unknown": [], **rec} for n in datasets}
    return changed


@pytest.fixture(autouse=True)
def order(monkeypatch):  # c1 is older than c2
    monkeypatch.setattr(compare, "_commit_time", lambda repo, c: int(c[1:]))


def test_same_commit_is_comparable():
    r = compare.classify("ds", [facts("A"), facts("B")], ".", FakeGolden(), changed=changed_with({}))
    assert r["status"] == "ok" and r["notes"] == []


def test_framework_change_with_identical_prompts_is_a_note():
    rec = {"generation": ["src/simple_evals_mm/dataset.py"]}
    r = compare.classify("ds", [facts("A", gen="c1"), facts("B", gen="c2")], ".", FakeGolden(), changed=changed_with(rec))
    assert r["status"] == "ok" and "dataset.py changed; the prompts sent are identical" in r["notes"][0]


def test_a_task_sent_differently_is_not_comparable():
    r = compare.classify("ds", [facts("A"), facts("B", prompts={"a": "p1", "b": "other"})], ".", FakeGolden(),
                         changed=changed_with({}))
    assert r["status"] == "different" and "1 of 2 tasks were sent different prompts (e.g. b)" in r["notes"][0]
    # tasks only one job has are not compared
    r = compare.classify("ds", [facts("A"), facts("B", prompts={"a": "p1"})], ".", FakeGolden(), changed=changed_with({}))
    assert r["status"] == "ok"


def test_other_tasks_args_are_not_comparable():
    assert compare.classify("ds", [facts("A"), facts("B", tasks="u")], ".", FakeGolden(),
                            changed=changed_with({}))["status"] == "different"
    assert compare.classify("ds", [facts("A"), facts("B", args={"split": "test"})], ".", FakeGolden(),
                            changed=changed_with({}))["status"] == "different"


def test_a_request_body_change_of_a_compared_model_needs_review():
    rec = {"generation": ["src/simple_evals_mm/samplers/openai_chat.py"]}
    seen = []

    class Golden(FakeGolden):
        def bodies_changed(self, a, b, models):
            seen.append(models)
            return ["chat: m"] if "old" in models else []

    # the changed body is the model of the job generated at the older commit: review
    r = compare.classify("ds", [facts("A", gen="c1", model="old"), facts("B", gen="c2", model="new")], ".", Golden(),
                         changed=changed_with(rec))
    assert r["status"] == "review" and "chat: m" in r["notes"][0] and "a model of A" in r["notes"][0]
    # only the models of the jobs at the older commit are looked up
    r = compare.classify("ds", [facts("A", gen="c1", model="x"), facts("B", gen="c2", model="old")], ".", Golden(),
                         changed=changed_with(rec))
    assert r["status"] == "ok" and seen[-1] == {"x"}


def test_verification_changes_by_the_golden_rewards():
    rec = {"verification": ["src/simple_evals_mm/verifiers.py"]}
    jobs = [facts("A", ver="c1"), facts("B", ver="c2")]
    same = compare.classify("ds", jobs, ".", FakeGolden({"c1": {"r": 1}, "c2": {"r": 1}}), changed=changed_with(rec))
    assert same["status"] == "ok" and "the golden rewards are equal" in same["notes"][0]
    other = compare.classify("ds", jobs, ".", FakeGolden({"c1": {"r": 1}, "c2": {"r": 0}}), changed=changed_with(rec))
    assert other["status"] == "regrade" and "regrade A at c2" in other["notes"][0]
    none = compare.classify("ds", jobs, ".", FakeGolden({"c2": {"r": 0}}), changed=changed_with(rec))
    assert none["status"] == "review"


def test_another_judge_needs_a_regrade():
    r = compare.classify("ds", [facts("A", judge={"model": "j1"}), facts("B", judge={"model": "j2"})], ".", FakeGolden(),
                         changed=changed_with({}))
    assert r["status"] == "regrade" and "j1" in r["notes"][0] and "j2" in r["notes"][0]


def test_settings_compare_after_defaults():
    # a default and the same value given explicitly are equal; permissions do not count; an older lock: given args
    conf = {"revision": "r", "start": "2025-02-01", "end": "2025-05-01"}
    explicit = {"config": {**conf, "args": {"start": "2025-02-01", "end": "2025-05-01", "allow_exec": True},
                           "allow_exec": True, "unsafe_sandbox": False, "verifier": "v(True)"}}
    default = {"config": {**conf, "args": {"allow_exec": True}, "allow_exec": True, "unsafe_sandbox": True}}
    assert compare.resolved_config(explicit, {}) == compare.resolved_config(default, {}) == conf
    assert compare.resolved_config({}, {"split": "test", "allow_exec": True}) == {"split": "test"}


def test_worst_status_wins_and_markdown():
    r = compare.classify("ds", [facts("A", judge={"model": "j1"}), facts("B", tasks="u", judge={"model": "j2"})], ".",
                         FakeGolden(), changed=changed_with({}))
    assert r["status"] == "different"
    rep = {"jobs": ["/jobs/A", "/jobs/B"], "datasets": {"ds": {**r, "jobs": {"/jobs/A": facts("A"), "/jobs/B": facts("B")}}}}
    md = compare.markdown(rep)
    assert md.splitlines()[0] == "| dataset | | A | B |" and "| ds | ✗ | 50.0 | 50.0 |" in md


def test_dataset_changes_for_release_notes(monkeypatch):
    """`sem datasets diff A B`: new module, tasks changed, revision only, prompts / rewards by the golden snapshot,
    code only; unchanged datasets left out."""
    from simple_evals_mm import dataset as dataset_mod
    from simple_evals_mm import provenance

    names = ["new", "data", "rev", "prm", "rwd", "code", "same"]
    monkeypatch.setattr(dataset_mod, "DATASETS", {n: f"simple_evals_mm.tasks.{n}:X" for n in names})
    own = {"data": ["L:datasets.data"], "rev": ["L:datasets.rev"], "prm": ["tasks/prm.py"], "rwd": ["tasks/rwd.py"],
           "code": ["tasks/code.py"], "new": ["tasks/new.py"]}
    monkeypatch.setattr(provenance, "changed_datasets", lambda a, b, ns, **kw: {
        n: {"dataset": own.get(n, []), "generation": [], "verification": [], "unknown": []} for n in ns})
    locks = {"A": {"datasets": {"data": {"tasks_sha256": 1}, "rev": {"tasks_sha256": 1, "revision": "x"}}},
             "B": {"datasets": {"data": {"tasks_sha256": 2}, "rev": {"tasks_sha256": 1, "revision": "y"}}}}

    def show(repo, commit, path):
        if path.endswith("datasets.lock.json"):
            return __import__("json").dumps(locks[commit])
        return None if (commit == "A" and path.endswith("/new.py")) else "src"

    monkeypatch.setattr(provenance, "_show", show)

    class G:
        def __init__(self, repo):
            pass

        def generation_part(self, c, n):
            return {"p": c if n == "prm" else 0}

        def reward_part(self, c, n):
            return {"r": c if n == "rwd" else 0}

        def _load(self, c):
            return {}, {}

    monkeypatch.setattr(compare, "Golden", G)
    rep = compare.dataset_changes("A", "B", repo=".")
    assert {n: d["change"] for n, d in rep["datasets"].items()} == {
        "new": "added", "data": "data", "rev": "revision", "prm": "prompts", "rwd": "rewards", "code": "code"}
    md = compare.changes_markdown(rep)
    assert md.index("| new |") < md.index("| data |") < md.index("| code |") and "same" not in md
