"""Revision history of result files (runs.track / verify_history / baseline_history), end to end through
simple_evals.py and scripts/rescore.py."""
import json
import os

import pytest

from simple_evals_mm import runs


def _write(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def test_track_records_changes_and_verify_catches_untracked_edits(runs_dir, tmp_path):
    d = str(tmp_path / "task")
    with runs.track(d, "tool_a", note="first"):
        _write(d, "items_r1.jsonl", '{"id": "1"}\n')
        _write(d, "score_r1.json", '{"score": 1.0}\n')
    with open(os.path.join(d, runs.HISTORY)) as f:
        entry = json.loads(f.readline())
    assert entry["tool"] == "tool_a" and set(entry["files"]) == {"items_r1.jsonl", "score_r1.json"}
    assert entry["files"]["score_r1.json"]["before"] is None and entry["details"] == {"note": "first"}
    assert runs.verify_history(d)["status"] == "ok"
    _write(d, "score_r1.json", '{"score": 0.5}\n')  # edited by hand
    v = runs.verify_history(d)
    assert v["status"] == "untracked" and v["mismatched"] == ["score_r1.json"]
    with runs.track(d, "tool_b"):  # a recorded rewrite makes it consistent again
        _write(d, "score_r1.json", '{"score": 0.25}\n')
    assert runs.verify_history(d)["status"] == "ok"
    _write(d, "summary.json", "{}\n")  # a result file the history never saw
    assert runs.verify_history(d)["unrecorded"] == ["summary.json"]
    os.remove(os.path.join(d, "summary.json"))
    os.remove(os.path.join(d, "items_r1.jsonl"))  # removed outside the tools
    assert runs.verify_history(d)["missing"] == ["items_r1.jsonl"]


def test_track_records_a_failed_block(runs_dir, tmp_path):
    d = str(tmp_path / "task")
    with pytest.raises(ValueError), runs.track(d, "crashing_tool"):
        _write(d, "items_r1.jsonl", "partial\n")
        raise ValueError("boom")
    with open(os.path.join(d, runs.HISTORY)) as f:
        assert json.loads(f.readline())["status"] == "failed: ValueError"
    assert runs.verify_history(d)["status"] == "ok"


def test_baseline_starts_a_history_once(runs_dir, tmp_path):
    d = str(tmp_path / "legacy")
    os.makedirs(d)
    _write(d, "summary.json", "{}\n")
    assert runs.verify_history(d)["status"] == "no_history"
    assert runs.baseline_history(d) and not runs.baseline_history(d)
    assert runs.verify_history(d)["status"] == "ok"
