import json

from simple_evals_mm import estimate


def _past_job(root, name, model, effort, ds="arith", n_tasks=10, prompt=1000, completion=500, cost=0.02, seconds=6.0):
    d = root / name
    (d / ds).mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"model": model, "effort": effort, "datasets": [ds]}))
    (d / ds / "trials_a1.meta.json").write_text(json.dumps({"complete": True, "n_tasks": n_tasks, "usage": {
        "prompt_tokens": prompt * n_tasks, "completion_tokens": completion * n_tasks,
        "cost_usd": None if cost is None else cost * n_tasks}}))
    (d / ds / "trials_a1.jsonl").write_text("".join(json.dumps({
        "task_name": f"t{i}", "started_at": "2026-11-14T00:00:00+00:00",
        "finished_at": f"2026-11-14T00:00:{seconds:02.0f}+00:00"}) + "\n" for i in range(n_tasks)))


def _plan(todo, price=None):
    ep = {"price_per_mtok": price} if price else {}
    return {"model": {"config": {"endpoint": ep}}, "datasets": {"arith": {"trials_to_run": todo}}}


def test_same_model_and_effort_gives_cost_and_time(tmp_path):
    _past_job(tmp_path, "a", "openrouter/x/m", "high")
    _past_job(tmp_path, "b", "openrouter/x/m", "low", cost=0.001)
    rec = estimate.collect([str(tmp_path)], ["arith"])
    est = estimate.estimate(_plan(100), {"model": "openrouter/x/m", "effort": "high", "n_concurrent": 10}, rec)
    e = est["datasets"]["arith"]
    assert e["basis"] == "same model and effort" and e["from"] == "a"
    assert e["cost_usd"] == 2.0 and e["hours"] == round(100 * 6 / 10 / 3600, 2)
    assert not est["incomplete"]["cost"] and not est["incomplete"]["time"]


def test_other_effort_is_flagged(tmp_path):
    _past_job(tmp_path, "b", "openrouter/x/m", "low")
    est = estimate.estimate(_plan(10), {"model": "openrouter/x/m", "effort": "max"}, estimate.collect([str(tmp_path)], ["arith"]))
    assert est["datasets"]["arith"]["basis"] == "same model, effort low"


def test_other_models_priced_at_this_endpoint_with_a_range(tmp_path):
    _past_job(tmp_path, "a", "openrouter/y/one", "high", completion=100)
    _past_job(tmp_path, "b", "openrouter/y/two", "high", completion=900)
    rec = estimate.collect([str(tmp_path)], ["arith"])
    est = estimate.estimate(_plan(1000, {"prompt": 1.0, "completion": 10.0}), {"model": "openrouter/x/new", "effort": "high"}, rec)
    e = est["datasets"]["arith"]
    assert e["basis"] == "other models (2)" and "hours" not in e
    assert e["cost_range_usd"] == [1000 * (1000 * 1 + 100 * 10) / 1e6, 1000 * (1000 * 1 + 900 * 10) / 1e6]
    assert est["incomplete"]["time"]


def test_no_basis_and_nothing_to_run(tmp_path):
    est = estimate.estimate(_plan(5), {"model": "m"}, estimate.collect([str(tmp_path)], ["arith"]))
    assert est["datasets"]["arith"]["basis"] == "none" and est["incomplete"]["cost"]
    assert estimate.estimate(_plan(0), {"model": "m"}, {})["datasets"]["arith"]["basis"] == "nothing to run"


def test_incomplete_attempts_are_not_a_basis(tmp_path):
    _past_job(tmp_path, "a", "openrouter/x/m", "high")
    meta = tmp_path / "a" / "arith" / "trials_a1.meta.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "complete": False}))
    assert estimate.collect([str(tmp_path)], ["arith"])["arith"] == []
