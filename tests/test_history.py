"""Revision history of result files: runs.track records every write, verify_history finds edits made outside the
tools, and the tools refuse to write on top of one until it is accepted."""
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
    with pytest.raises(runs.UntrackedEditError), runs.track(d, "tool_b"):  # no recording on top of it
        _write(d, "score_r1.json", '{"score": 0.25}\n')
    runs.accept_edits(d, reason="test: hand-fixed score")  # an accepted edit is recorded, writes continue
    assert runs.verify_history(d)["status"] == "ok"
    with open(os.path.join(d, runs.HISTORY)) as f:
        assert [json.loads(line)["tool"] for line in f] == ["tool_a", "accept"]
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


def test_the_first_tracked_write_of_an_older_run_baselines_its_files(runs_dir, tmp_path):
    d = str(tmp_path / "legacy")
    os.makedirs(d)
    _write(d, "items_r1.jsonl", '{"id": "1"}\n')
    _write(d, "summary.json", "{}\n")
    with runs.track(d, "rescore"):  # touches only the score file
        _write(d, "score_r1.json", '{"score": 1.0}\n')
    assert runs.verify_history(d)["status"] == "ok"  # the unchanged files were recorded by the baseline
