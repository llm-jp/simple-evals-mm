"""The task and sampler templates in CONTRIBUTING.md run as written (placeholders filled: a fake dataset and a
fake backend client), so the guide cannot drift from the code again."""
import os
import re
import types

import pytest
from PIL import Image

DOC = os.path.join(os.path.dirname(__file__), "..", "CONTRIBUTING.md")


def _module(name: str, marker: str):
    blocks = re.findall(r"```python\n(.*?)```", open(DOC).read(), re.S)
    src = next(b for b in blocks if marker in b)
    mod = types.ModuleType(name)
    exec(compile(src, f"CONTRIBUTING.md:{name}", "exec"), mod.__dict__)
    return mod


class _Out:
    def __init__(self, text):
        self.text, self.reasoning, self.input_tokens, self.output_tokens, self.finish_reason = text, "", 3, 1, "stop"


class _Client:
    """Answers "cat" correctly, "dog" wrongly, and fails on "fox"."""

    def generate(self, messages, max_tokens, temperature):
        q = messages[-1]["content"][-1]["text"]
        if "fox" in q:
            raise RuntimeError("backend down")
        return _Out("cat" if "cat" in q else "wolf")


@pytest.fixture
def templates(monkeypatch):
    rows = [{"id": i, "question": f"Which animal? {w}", "answer": w, "image": Image.new("RGB", (8, 8))} for i, w in enumerate(["cat", "dog", "fox"])]
    monkeypatch.setattr("datasets.load_dataset", lambda *a, **k: rows)
    task_mod = _module("mytask", "class MyTaskEval")
    sampler_mod = _module("mybackend_sampler", "class MyBackendSampler")
    sampler = sampler_mod.MyBackendSampler("myorg/MyModel-7B")
    sampler.client = _Client()
    return task_mod.MyTaskEval(), sampler


def test_task_and_sampler_templates(templates):
    task, sampler = templates
    result = task(sampler)
    assert [r.score for r in result.single_eval_results] == [1.0, 0.0, None]
    assert result.single_eval_results[2].error.startswith("model_failed")
    assert result.score == pytest.approx(0.5)
    usage = sampler.get_usage()
    assert usage["error_count"] == 1 and usage["input_tokens"] == 6
    assert [r.score for r in task.rescore(result.single_eval_results).single_eval_results] == [1.0, 0.0, None]
