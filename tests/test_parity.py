"""`sem job parity`: model matching, setting mismatch, tolerance from the card's binomial SE (cap), the variance flag,
non-binary rewards, n/a and --strict."""

import json
import math

import pytest
import yaml

from simple_evals_mm import cli, parity
from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm.dataset import Dataset
from simple_evals_mm.models import VerifierResult
from simple_evals_mm.store import JobStore


class Toy(Dataset):
    name = "toy"
    split = "testmini"
    accepted_args = {"split": str}  # noqa: RUF012


def _register(monkeypatch, *names):
    for n in names:
        monkeypatch.setitem(dataset_mod.DATASETS, n, type(f"T_{n}", (Toy,), {"name": n}))


def _rewards(mean: float, n: int) -> list[float]:
    k = round(mean * n)
    return [1.0] * k + [0.0] * (n - k)


def make_job(tmp_path, datasets: dict, model="sglang/Org/Model-9B", dataset_args=None, n_tasks=None) -> str:
    """datasets: {name: [rewards of attempt 1, attempt 2, ...]}."""
    st = JobStore(str(tmp_path / "jobs"), "j")
    st.write_config({"model": model, "datasets": list(datasets), "n_attempts": len(next(iter(datasets.values()))),
                     "dataset_args": dataset_args or {}})
    st.write_lock({"job_id": "u", "sampler": {"config": {"settings": {
        "temperature": {"value": 1.0, "source": "preset:x"}, "effort": {"value": None, "source": "n/a"}}}},
        "datasets": {n: {"n_tasks": (n_tasks or {}).get(n, len(atts[0])), "revision": "r",
                         "config": {"args": (dataset_args or {}).get(n, {})}} for n, atts in datasets.items()}})
    for name, atts in datasets.items():
        for k in range(1, len(atts) + 1):
            p = st.trials_path(name, k)
            import os

            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        st.write_verification(name, "v1", {k: [VerifierResult(f"t{i}", r) for i, r in enumerate(rs)]
                                           for k, rs in enumerate(atts, 1)}, {})
    return st.dir


REFS = {"model-9b": {"slugs": ["openrouter/org/model-9b"], "hf_id": "Org/Model-9B", "source": "https://x", "scores": {
    "toy_a": {"value": 50.0},
    "toy_split": {"value": 50.0, "setting": {"split": "test"}},
    "toy_na": {"value": 90.0, "comparable": False, "reason": "open-ended vs MCQ"},
    "toy_frac": {"value": 50.0},
    "toy_unknown": {"value": 50.0, "setting": {"start": "unknown"}},
}}}


def test_model_matching():
    assert parity.find_model(REFS, "openrouter/org/model-9b")[0] == "model-9b"
    assert parity.find_model(REFS, "vllm/Org/Model-9B")[0] == "model-9b"  # served: hf_id under any engine prefix
    assert parity.find_model(REFS, "sglang/Org/Model-9B")[0] == "model-9b"
    assert parity.find_model(REFS, "openrouter/Org/Model-9B")[0] is None  # not a served engine, not listed
    assert parity.find_model(REFS, "sglang/Org/Model-90B") == (None, None)


def test_tolerance_from_n_and_cap():
    tol, se = parity.tolerance(50.0, 1600, True)
    assert se == pytest.approx(1.25) and tol == pytest.approx(2.5)
    assert parity.tolerance(50.0, 10000, True) == (2.0, pytest.approx(0.5))  # floor 2 points
    assert parity.tolerance(50.0, 100, True)[0] == 5.0  # 2 x SE = 10 -> cap 5
    assert parity.tolerance(97.2, 491, True)[0] == 2.0
    assert parity.tolerance(50.0, 100, False) == (2.0, None)  # non-binary: fixed


def test_variance_flag_uses_expected_noise():
    exp = 100 * math.sqrt(0.5 * 0.5 / 100)  # 5 points
    assert parity.variance_flag(50.0, 2 * exp + 0.1, 100, True) == (True, pytest.approx(exp))
    assert parity.variance_flag(50.0, 2 * exp - 0.1, 100, True)[0] is False
    assert parity.variance_flag(50.0, 30.0, 100, False) == (False, None)  # non-binary: skipped
    assert parity.variance_flag(50.0, None, 100, True) == (False, None)  # one attempt


def test_compare_rows(tmp_path, monkeypatch):
    _register(monkeypatch, "toy_a", "toy_split", "toy_na", "toy_frac", "toy_unknown", "toy_noref")
    n = 1600
    job = make_job(tmp_path, {
        "toy_a": [_rewards(0.53, n), _rewards(0.53, n)],  # +3.0 > tol 2.5
        "toy_split": [_rewards(0.5, n), _rewards(0.5, n)],
        "toy_na": [_rewards(0.8, n), _rewards(0.8, n)],
        "toy_frac": [[0.5] * n, [0.52] * n],  # non-binary rewards
        "toy_unknown": [_rewards(0.5, n), _rewards(0.5, n)],
        "toy_noref": [_rewards(0.5, n), _rewards(0.5, n)],
    })
    rep = parity.compare(JobStore.from_path(job), REFS)
    rows = {r["dataset"]: r for r in rep["rows"]}
    assert set(rows) == {"toy_a", "toy_split", "toy_na", "toy_frac", "toy_unknown"}  # no reference: not listed
    a = rows["toy_a"]
    assert a["result"] == "⚠" and a["diff"] == pytest.approx(3.0) and a["tol"] == pytest.approx(2.5)
    assert a["n"] == n and a["se_card"] == pytest.approx(1.25) and a["binary"] and not a["variance_flag"]
    assert rows["toy_split"]["result"] == "setting differs" and "split: card test, job testmini" in rows["toy_split"]["reason"]
    assert rows["toy_unknown"]["result"] == "setting differs"  # `unknown` never matches
    assert rows["toy_na"]["result"] == "n/a" and rows["toy_na"]["reason"] == "open-ended vs MCQ"
    f = rows["toy_frac"]
    assert not f["binary"] and f["tol"] == 2.0 and f["se_card"] is None and not f["variance_flag"]
    assert f["result"] == "✓"  # 51.0 vs 50.0
    assert parity.strict_failures(rep) == ["toy_a"]


def test_setting_matches_dataset_arg(tmp_path, monkeypatch):
    _register(monkeypatch, "toy_split")
    job = make_job(tmp_path, {"toy_split": [_rewards(0.51, 400), _rewards(0.51, 400)]},
                   dataset_args={"toy_split": {"split": "test"}})
    row = parity.compare(JobStore.from_path(job), REFS)["rows"][0]
    assert row["result"] == "✓" and row["tol"] == 5.0  # SE 2.5 -> 2 x SE capped at 5


def test_variance_flag_in_report(tmp_path, monkeypatch, capsys):
    _register(monkeypatch, "toy_a")
    job = make_job(tmp_path, {"toy_a": [_rewards(0.3, 100), _rewards(0.7, 100)]})  # std 28 points, expected 5
    row = parity.compare(JobStore.from_path(job), REFS)["rows"][0]
    assert row["result"] == "✓" and row["variance_flag"]  # parity holds (50 vs 50), the variance does not
    parity.print_report(parity.compare(JobStore.from_path(job), REFS))
    out = capsys.readouterr().out
    assert "⚠ variance" in out and "flaky judge" in out


def test_cli_parity_and_strict(tmp_path, monkeypatch, capsys):
    _register(monkeypatch, "toy_a", "toy_na")
    job = make_job(tmp_path, {"toy_a": [_rewards(0.6, 1600)] * 2, "toy_na": [_rewards(0.8, 10)] * 2})
    ref = tmp_path / "refs.yaml"
    ref.write_text(yaml.safe_dump({"format": "sem-model-cards/1", "models": REFS}))
    assert cli.main(["job", "parity", job, "--references", str(ref)]) == 0
    out = capsys.readouterr().out
    assert "⚠" in out and "n/a" in out and "[ ] split / subset" in out and "temperature: 1.0 (preset:x)" in out
    assert cli.main(["job", "parity", job, "--references", str(ref), "--strict"]) == 1
    capsys.readouterr()
    assert cli.main(["job", "parity", job, "--references", str(ref), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert {r["dataset"]: r["result"] for r in rep["rows"]} == {"toy_a": "⚠", "toy_na": "n/a"}
    explained = json.loads(json.dumps(REFS))
    explained["model-9b"]["scores"]["toy_a"] |= {"status": "explained", "link": "#1"}
    ref.write_text(yaml.safe_dump({"models": explained}))
    assert cli.main(["job", "parity", job, "--references", str(ref), "--strict"]) == 0


def test_no_model_card(tmp_path, monkeypatch, capsys):
    _register(monkeypatch, "toy_a")
    job = make_job(tmp_path, {"toy_a": [_rewards(0.5, 10)]}, model="sglang/Other/X")
    ref = tmp_path / "refs.yaml"
    ref.write_text(yaml.safe_dump({"models": REFS}))
    assert cli.main(["job", "parity", job, "--references", str(ref), "--strict"]) == 0
    assert "no model card" in capsys.readouterr().out


def test_committed_references_parse():
    refs = parity.load_references()
    assert parity.find_model(refs, "sglang/Qwen/Qwen3.5-9B")[0] == "qwen3.5-9b"
    for key, e in refs.items():
        assert e.get("source") and e.get("retrieved") and e.get("scores"), key
        for ds, s in e["scores"].items():
            assert ds in dataset_mod.DATASETS, (key, ds)
            assert isinstance(s["value"], (int, float)) and s.get("status", "open") in ("ok", "open", "explained")
            if s.get("comparable", True) is False:
                assert s.get("reason"), (key, ds)
