"""Unit tests for the per-example metrics plumbing: count_images,
map_examples duration recording, SingleEvalResult serialization, and the
llm-jp-4-vl token-level reasoning counter. All hermetic (no models, no
network)."""

from types import SimpleNamespace

from simple_evals_mm.common import (
    SingleEvalResult,
    count_images,
    map_examples,
)
from simple_evals_mm.sampler.llmjpvl_sampler import LLMjpVLSampler, _ANALYSIS_RE


# ---------------------------------------------------------------------------
# count_images
# ---------------------------------------------------------------------------


class TestCountImages:
    def test_chat_completions_formats(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "image", "image": "x.png"},
                    {"type": "input_image", "image_url": "data:..."},
                    {"type": "text", "text": "q"},
                ],
            }
        ]
        assert count_images(msgs) == 3

    def test_string_content(self):
        assert count_images([{"role": "user", "content": "hello"}]) == 0

    def test_none_content(self):
        assert count_images([{"role": "user", "content": None}]) == 0

    def test_gemini_style_raw_parts(self):
        # Gemini pack_message returns a bare list of parts; a part without a
        # .text attribute counts as an image.
        class Part:
            def __init__(self, text=None):
                self.text = text

        assert count_images([[Part("question"), Part(), Part()]]) == 2

    def test_multiple_messages(self):
        msg = {"role": "user", "content": [{"type": "image", "image": "a"}]}
        assert count_images([msg, msg]) == 2


# ---------------------------------------------------------------------------
# map_examples: duration_seconds
# ---------------------------------------------------------------------------


def _result():
    return SingleEvalResult(
        id="1", question="q", correct_answer="a",
        response_text="r", extracted_answer="a", score=1.0,
    )


class TestMapExamplesDuration:
    def test_sets_duration(self):
        results = map_examples(lambda x: _result(), [1, 2])
        assert all(r.duration_seconds >= 0 for r in results)

    def test_threaded_preserves_order_and_duration(self):
        results = map_examples(
            lambda x: SingleEvalResult(
                id=str(x), question="q", correct_answer="a",
                response_text="r", extracted_answer="a", score=1.0,
            ),
            [1, 2, 3],
            num_threads=3,
        )
        assert [r.id for r in results] == ["1", "2", "3"]
        assert all(r.duration_seconds >= 0 for r in results)

    def test_non_result_passthrough(self):
        assert map_examples(lambda x: x * 2, [1, 2]) == [2, 4]


# ---------------------------------------------------------------------------
# SingleEvalResult.to_dict emission rules
# ---------------------------------------------------------------------------


class TestToDict:
    def test_omits_unreported_metrics(self):
        d = _result().to_dict()
        assert "input_tokens" not in d
        assert "reasoning_tokens" not in d
        assert "finish_reason" not in d
        assert "duration_seconds" not in d
        assert d["num_images"] == 0  # always emitted

    def test_emits_reported_metrics(self):
        r = _result()
        r.input_tokens, r.output_tokens, r.reasoning_tokens = 10, 5, 3
        r.finish_reason, r.num_images, r.duration_seconds = "stop", 2, 1.5
        d = r.to_dict()
        assert d["input_tokens"] == 10 and d["output_tokens"] == 5
        assert d["reasoning_tokens"] == 3 and d["finish_reason"] == "stop"
        assert d["num_images"] == 2 and d["duration_seconds"] == 1.5


# ---------------------------------------------------------------------------
# llm-jp-4-vl token-level reasoning counter (harmony format)
# ---------------------------------------------------------------------------

# Synthetic harmony marker ids (values arbitrary; only identity matters).
CH, ANA, MSG, END, START, RET, FIN = 9, 2202, 12, 11, 10, 2, 2520


class _StubTokenizer:
    def encode(self, text, add_special_tokens=False):
        return [1] * len(text.split())


def _counter(analysis_id=ANA):
    stub = SimpleNamespace(
        _id_channel=CH,
        _id_analysis=analysis_id,
        _id_message=MSG,
        _id_end=END,
        processor=SimpleNamespace(tokenizer=_StubTokenizer()),
    )
    return lambda ids, text: LLMjpVLSampler._count_reasoning_tokens(
        stub, ids, text
    )


COT = list(range(100, 121))  # 21 analysis-content tokens
ANS = [200, 201]


class TestCountReasoningTokens:
    def test_analysis_then_final(self):
        count = _counter()
        ids = [CH, ANA, MSG] + COT + [END, START, CH, FIN, MSG] + ANS + [RET]
        assert count(ids, "some analysis text") == len(COT)

    def test_truncated_mid_analysis(self):
        # No <|end|>: count to the end of generation.
        count = _counter()
        ids = [CH, ANA, MSG] + COT
        assert count(ids, "partial analysis") == len(COT)

    def test_direct_answer_no_analysis(self):
        count = _counter()
        ids = [CH, FIN, MSG] + ANS + [RET]
        assert count(ids, "") == 0

    def test_multiple_analysis_segments(self):
        count = _counter()
        ids = (
            [CH, ANA, MSG] + COT + [END]
            + [CH, ANA, MSG] + COT[:5] + [END]
            + [CH, FIN, MSG] + ANS
        )
        assert count(ids, "x") == len(COT) + 5

    def test_reencode_fallback_when_scan_misses(self):
        # The model emitted a tokenization variant of "analysis" that the
        # scan does not recognize -> fall back to re-encoding the text.
        count = _counter(analysis_id=None)
        ids = [CH, 9999, MSG] + COT + [END]
        assert count(ids, "three word reasoning") == 3

    def test_fallback_when_scan_zero_but_text_present(self):
        count = _counter()
        ids = [CH, 9999, MSG] + COT + [END]  # variant id, scan finds nothing
        assert count(ids, "three word reasoning") == 3


class TestAnalysisRegex:
    def test_normal(self):
        raw = "<|channel|> analysis<|message|> think hard<|end|><|start|> assistant<|channel|> final<|message|> Answer: A<|return|>"
        m = _ANALYSIS_RE.search(raw)
        assert m and m.group(1).strip() == "think hard"

    def test_truncated_analysis_without_end(self):
        raw = "<|channel|> analysis<|message|> partial thought that hit max tokens"
        m = _ANALYSIS_RE.search(raw)
        assert m and m.group(1).strip() == "partial thought that hit max tokens"

    def test_no_analysis(self):
        raw = "<|channel|> final<|message|> Answer: A<|return|>"
        assert _ANALYSIS_RE.search(raw) is None
