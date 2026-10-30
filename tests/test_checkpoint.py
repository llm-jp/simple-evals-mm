"""Per-example checkpoint of map_examples (set_checkpoint): finished examples are reused after an interruption.
Also the shared pool of concurrent repeats (set_shared_pool) and SamplerBase.fork."""
import concurrent.futures
import threading
import time

import pytest

from simple_evals_mm.common import (
    SingleEvalResult,
    checkpoint_prior_usage,
    map_examples,
    merge_usage,
    set_checkpoint,
    set_shared_pool,
)


def _row(i, error=None, id=...):
    return SingleEvalResult(id=str(i) if id is ... else id, question=f"q{i}", correct_answer="a", response_text=f"r{i}",
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


def _interrupt_after(n):
    def fn(item):
        if fn.calls >= n:
            raise KeyboardInterrupt
        fn.calls += 1
        return _row(item)
    fn.calls = 0
    return fn


def test_a_changed_item_order_is_refused(ckpt):
    set_checkpoint(ckpt)
    with pytest.raises(KeyboardInterrupt):
        map_examples(_interrupt_after(2), ["a", "b", "c", "d"])  # a, b checkpointed at positions 0, 1
    set_checkpoint(ckpt)
    with pytest.raises(RuntimeError, match="item order"):
        map_examples(_row, ["c", "d", "a", "b"])  # positions 0, 1 now hold c, d: a and b would be generated twice


def test_rows_without_id_and_duplicates_within_new_rows_are_not_flagged(tmp_path):
    for i, (items, make) in enumerate([
        (["x", "x", "x"], lambda it: _row(it, id=None)),  # ScienceQA-style rows without an id
        (["y", "z", "z"], _row),  # the same id twice among the new rows (CharXiv-style)
    ]):
        path = str(tmp_path / f"partial_{i}.jsonl")
        set_checkpoint(path)
        with pytest.raises(KeyboardInterrupt):
            map_examples(_interrupt_after(1), items)
        set_checkpoint(path)
        assert len(map_examples(make, items)) == 3
    set_checkpoint(None)


def test_usage_of_interrupted_attempts_is_kept(ckpt):
    usage = {"call_count": 0, "cost_usd": 0.0}

    def fn(i):
        if i == 2 and usage["call_count"] == 2:
            raise KeyboardInterrupt
        usage["call_count"] += 1
        usage["cost_usd"] += 0.5
        return _row(i)

    set_checkpoint(ckpt, lambda: {"model": dict(usage), "judge": None})
    with pytest.raises(KeyboardInterrupt):
        map_examples(fn, range(4))
    usage.update(call_count=0, cost_usd=0.0)  # a new job: fresh counters
    set_checkpoint(ckpt, lambda: {"model": dict(usage), "judge": None})
    assert checkpoint_prior_usage() == [{"model": {"call_count": 2, "cost_usd": 1.0}, "judge": None}]
    map_examples(fn, range(4))
    total = merge_usage(dict(usage), checkpoint_prior_usage()[0]["model"])
    assert total == {"call_count": 4, "cost_usd": 2.0}


def test_merge_usage_sums_numbers_and_provider_counts():
    a = {"input_tokens": 10, "call_count": 1, "cost_usd": 0.1, "providers": {"X": 1}}
    b = {"input_tokens": 5, "call_count": 2, "cost_usd": None, "providers": {"X": 1, "Y": 1}}
    assert merge_usage(a, b) == {"input_tokens": 15, "call_count": 3, "cost_usd": 0.1, "providers": {"X": 2, "Y": 1}}
    assert merge_usage(None, b) == b


def test_concurrent_repeats_share_the_pool_and_keep_their_own_checkpoints(tmp_path):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    lock, active, peak = threading.Lock(), [0], [0]

    def fn(i):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return _row(i)

    def repeat(k):
        set_checkpoint(str(tmp_path / f"partial_r{k}.jsonl"))
        set_shared_pool(pool)
        try:
            return [r.id for r in map_examples(fn, range(8), 3)]
        finally:
            set_shared_pool(None)
            set_checkpoint(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        out = list(ex.map(repeat, (1, 2, 3)))
    pool.shutdown()
    assert out == [[str(i) for i in range(8)]] * 3
    assert peak[0] <= 3  # 3 repeats x 3 threads requested, but the shared pool caps the total
    for k in (1, 2, 3):  # every repeat checkpointed its own examples into its own file
        with open(tmp_path / f"partial_r{k}.jsonl") as f:
            assert len(f.read().splitlines()) == 8


def test_fork_has_its_own_usage_counters():
    from simple_evals_mm.sampler.cot_sampler import CoTSampler
    from tests.helpers import EchoSampler

    base = EchoSampler(reply="x")
    wrapped = CoTSampler(base)
    a, b = wrapped.fork(), wrapped.fork()
    a._sampler([a._sampler.pack_message(instruction="q")])
    assert a.get_usage()["call_count"] == 1 and b.get_usage()["call_count"] == 0
    assert base.get_usage()["call_count"] == 0
