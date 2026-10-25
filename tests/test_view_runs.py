"""scripts/view_runs.py: the JSON behind the run viewer (listing, filters, compare, path safety)."""
import importlib.util
import json
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _viewer(runs_dir):
    spec = importlib.util.spec_from_file_location("view_runs", os.path.join(ROOT, "scripts", "view_runs.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RUNS_DIR = str(runs_dir)
    return m


def _run(root, run, rows):
    d = root / run / "mmmu"
    d.mkdir(parents=True)
    (root / run / "config.json").write_text(json.dumps({"run_id": run, "model": "m", "created_at": "2026-10-25T00:00:00"}))
    (d / "items_r1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "score_r1.json").write_text(json.dumps({"score": 0.5}) + "\n")
    (d / "summary.json").write_text(json.dumps({"mean_score": 0.5}))


def test_list_filter_compare_and_safety(tmp_path):
    pre = "Answer the multiple choice question.\n"
    _run(tmp_path, "a", [{"id": "1", "question": pre + "q one", "correct_answer": "A", "response_text": "A", "score": 1.0},
                         {"id": "2", "question": pre + "q two", "correct_answer": "B", "response_text": "C", "score": 0.0},
                         {"id": "3", "question": pre + "q three", "correct_answer": "C", "response_text": "", "score": None,
                          "error": "model_failed: timeout"}])
    _run(tmp_path, "b", [{"id": "1", "question": pre + "q one", "correct_answer": "A", "response_text": "D", "score": 0.0},
                         {"id": "2", "question": pre + "q two", "correct_answer": "B", "response_text": "C", "score": 0.0}])
    v = _viewer(tmp_path)
    assert [r["run"] for r in v.list_runs()] == ["a", "b"]
    assert v.run_detail("a")["tasks"][0]["scores"] == [0.5]
    d = v.item_list({"run": "a", "task": "mmmu"})
    assert d["counts"] == {"correct": 1, "wrong": 1, "failed": 1}
    assert d["items"][0]["question"] == "q one"  # the shared prompt prefix is dropped from the preview
    assert [i["id"] for i in v.item_list({"run": "a", "task": "mmmu", "filter": "wrong"})["items"]] == ["2"]
    assert [i["id"] for i in v.item_list({"run": "a", "task": "mmmu", "q": "three"})["items"]] == ["3"]
    c = v.item_list({"run": "a", "task": "mmmu", "run2": "b", "filter": "changed"})
    assert [i["id"] for i in c["items"]] == ["1"] and c["items"][0]["status2"] == "wrong"
    det = v.item_detail({"run": "a", "task": "mmmu", "run2": "b", "i": "2"})
    assert det["status"] == "failed" and det["item2"] is None and det["status2"] == "absent"
    assert "error" in v.item_list({"run": "../a", "task": "mmmu"}) and "error" in v.item_detail({"run": "a", "task": "..", "i": "0"})
