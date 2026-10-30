"""Per-example checkpoint of map_examples (set_checkpoint): finished examples are reused after an interruption."""
import pytest

from simple_evals_mm.common import SingleEvalResult, map_examples, set_checkpoint


def _row(i, error=None):
    return SingleEvalResult(id=str(i), question=f"q{i}", correct_answer="a", response_text=f"r{i}",
                            extracted_answer="", score=1.0, error=error)


@pytest.fixture
def ckpt(tmp_path):
    path = str(tmp_path / "partial_r1.jsonl")
    yield path
    set_checkpoint(None)


@pytest.mark.parametrize("threads", [1, 4])
def test_interrupted_map_resumes_from_the_checkpoint(ckpt, threads):
    calls = []

    def fn(i):
        if i == 3 and len(calls) < 10:
            raise KeyboardInterrupt  # the job dies on item 3
        calls.append(i)
        return _row(i)

    set_checkpoint(ckpt)
    with pytest.raises(KeyboardInterrupt):
        map_examples(fn, range(6), 1)
    assert calls == [0, 1, 2]
    calls.extend([None] * 10)  # let item 3 through now
    set_checkpoint(ckpt)
    rows = map_examples(fn, range(6), threads)
    assert [r.id for r in rows] == ["0", "1", "2", "3", "4", "5"]
    assert sorted(c for c in calls if c is not None) == [0, 1, 2, 3, 4, 5]  # 0-2 not generated again


def test_failed_model_calls_are_retried_and_a_different_item_list_is_ignored(ckpt):
    set_checkpoint(ckpt)
    map_examples(lambda i: _row(i, error="model_failed: 429" if i == 1 else None), range(3))
    calls = []
    set_checkpoint(ckpt)
    map_examples(lambda i: calls.append(i) or _row(i), range(3))
    assert calls == [1]
    calls.clear()
    set_checkpoint(ckpt)
    map_examples(lambda i: calls.append(i) or _row(i), range(4))  # e.g. another --examples
    assert calls == [0, 1, 2, 3]


def test_nested_calls_are_not_checkpointed(ckpt):
    def fn(i):
        inner = map_examples(lambda j: _row(100 + j), range(2))  # e.g. per-turn grading inside an example
        return _row(i) if len(inner) == 2 else None

    set_checkpoint(ckpt)
    map_examples(fn, range(3))
    with open(ckpt) as f:
        lines = f.read().splitlines()
    assert len(lines) == 3 and all('"call": 0' in line for line in lines)


def test_no_checkpoint_by_default(tmp_path):
    set_checkpoint(None)
    assert [r.id for r in map_examples(_row, range(2))] == ["0", "1"]
    assert list(tmp_path.iterdir()) == []
