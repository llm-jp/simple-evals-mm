import json
import os
from datetime import UTC, datetime

from simple_evals_mm import watch

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC).timestamp()


def _iso(minutes_ago: float) -> str:
    return datetime.fromtimestamp(NOW - minutes_ago * 60, UTC).isoformat()


def _job(tmp_path, rows_by_attempt, n_tasks=10, attempts=2):
    d = tmp_path / "jobs" / "j"
    (d / "arith").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"model": "openrouter/fake/model", "datasets": ["arith"],
                                               "n_attempts": {"arith": attempts}}))
    (d / "lock.json").write_text(json.dumps({"datasets": {"arith": {"n_tasks": n_tasks}}}))
    for k, rows in rows_by_attempt.items():
        (d / "arith" / f"trials_a{k}.jsonl").write_text("".join(
            json.dumps({"task_name": task, "finished_at": _iso(ago), "exception_info": {"cause": cause} if cause else None,
                        "agent_result": {"usage": {"cost_usd": cost}}}) + "\n" for task, ago, cause, cost in rows))
    return str(d)


def test_progress_cost_and_time_left(tmp_path):
    d = _job(tmp_path, {1: [(f"t{i}", 25 - i, None, 0.5) for i in range(10)], 2: [("t0", 5, None, 0.5)]})
    (row,) = watch.Watcher([d]).snapshot(NOW)
    assert row["done"] == 11 and row["total"] == 20 and not row["complete"]
    assert row["cost_usd"] == 5.5 and row["cost_projected_usd"] == 10.0
    assert row["rate_per_h"] == 2 * 11  # 11 trials in the last 30 min
    assert row["eta_h"] == round(9 / 22, 2)


def test_reads_only_appended_lines_and_ignores_a_torn_line(tmp_path):
    d = _job(tmp_path, {1: [("t0", 5, None, 0.1)]}, attempts=1)
    w = watch.Watcher([d])
    assert w.snapshot(NOW)[0]["done"] == 1
    with open(os.path.join(d, "arith", "trials_a1.jsonl"), "a") as f:
        f.write(json.dumps({"task_name": "t1", "finished_at": _iso(1)}) + "\n" + '{"task_name": "t2", "fini')
    assert w.snapshot(NOW)[0]["done"] == 2  # the half-written line waits for its newline


def test_warnings_stalled_persistent_failing(tmp_path):
    ok = [(f"t{i}", 120 - i, None, 0.0) for i in range(2, 10)]
    d = _job(tmp_path, {1: ok + [("t0", 100, "provider_failed", 0.0)], 2: [("t0", 90, "request_rejected", 0.0)]})
    (row,) = watch.Watcher([d], stale_min=15).snapshot(NOW)
    assert any(w.startswith("stalled") for w in row["warnings"])
    assert row["persistent_tasks"] == ["t0"]
    assert row["causes"] == {"provider_failed": 1, "request_rejected": 1}


def test_slowdown_when_the_recent_rate_collapses(tmp_path):
    early = [(f"t{i}", 120 - i * 0.5, None, 0.0) for i in range(60)]  # 60 trials in 30 min, an hour ago
    late = [("u0", 10, None, 0.0)]
    d = _job(tmp_path, {1: early + late}, n_tasks=200, attempts=1)
    (row,) = watch.Watcher([d]).snapshot(NOW)
    assert any(w.startswith("slowdown") for w in row["warnings"])


def test_table_hides_complete_datasets_unless_asked(tmp_path):
    d = _job(tmp_path, {1: [(f"t{i}", 5, None, 0.0) for i in range(10)]}, attempts=1)
    rows = watch.Watcher([d]).snapshot(NOW)
    assert rows[0]["complete"]
    assert "arith" not in watch.format_table(rows).splitlines()[1]
    assert "arith" in watch.format_table(rows, show_complete=True)
