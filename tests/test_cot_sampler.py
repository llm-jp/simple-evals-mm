"""Unit tests for CoTSampler: reasoning/answer splitting (no extraction),
budget raising, and delegation."""

from simple_evals_mm.common import SamplerResponse
from simple_evals_mm.sampler.cot_sampler import CoTSampler


class FakeSampler:
    """Minimal stub that records pack_message calls and returns canned responses."""

    max_new_tokens = 1024
    temperature = 0.0

    def __init__(self, response=""):
        if isinstance(response, str):
            response = SamplerResponse(response_text=response, raw=response)
        self.response = response
        self.last_pack_args = None
        self.last_message_list = None

    def pack_message(self, images=None, instruction="", role="user"):
        self.last_pack_args = {"images": images, "instruction": instruction, "role": role}
        return {"role": role, "content": instruction}

    def __call__(self, message_list):
        self.last_message_list = message_list
        return self.response


# ---------------------------------------------------------------------------
# pack_message delegation tests
# ---------------------------------------------------------------------------


class TestPackMessage:
    def test_delegates_instruction(self):
        fake = FakeSampler()
        cot = CoTSampler(fake)
        cot.pack_message(instruction="What is 2+2?")
        assert fake.last_pack_args["instruction"] == "What is 2+2?"

    def test_preserves_images(self):
        fake = FakeSampler()
        cot = CoTSampler(fake)
        sentinel = ["img1", "img2"]
        cot.pack_message(images=sentinel, instruction="Describe.")
        assert fake.last_pack_args["images"] is sentinel

    def test_preserves_role(self):
        fake = FakeSampler()
        cot = CoTSampler(fake)
        cot.pack_message(instruction="Hi", role="developer")
        assert fake.last_pack_args["role"] == "developer"


# ---------------------------------------------------------------------------
# Reasoning/answer splitting (NOT extraction: the Answer: line is kept)
# ---------------------------------------------------------------------------


class TestReasoningSplit:
    def test_splits_at_last_answer_line(self):
        fake = FakeSampler(response="Let me think...\nAnswer: B\nAnswer: C")
        out = CoTSampler(fake)([])
        assert out.response_text == "Answer: C"
        assert out.reasoning == "Let me think...\nAnswer: B"
        assert out.raw == "Let me think...\nAnswer: B\nAnswer: C"

    def test_answer_line_kept_for_task_extraction(self):
        fake = FakeSampler(response="Step 1... Step 2...\nAnswer: 42")
        out = CoTSampler(fake)([])
        assert out.response_text == "Answer: 42"
        assert out.reasoning == "Step 1... Step 2..."

    def test_fullwidth_colon(self):
        fake = FakeSampler(response="考えます...\nAnswer： A")
        out = CoTSampler(fake)([])
        assert out.response_text == "Answer： A"
        assert out.reasoning == "考えます..."

    def test_no_answer_line_passthrough(self):
        fake = FakeSampler(response="I don't know the answer to this.")
        out = CoTSampler(fake)([])
        assert out.response_text == "I don't know the answer to this."
        assert out.reasoning == ""

    def test_empty_response_passthrough(self):
        fake = FakeSampler(response="")
        out = CoTSampler(fake)([])
        assert out.response_text == ""

    def test_thinking_model_passthrough(self):
        # Inner sampler already separated the reasoning: do not re-split.
        inner = SamplerResponse(
            response_text="short answer with Answer: X inside",
            reasoning="native thinking trace",
            raw="<think>native thinking trace</think>short answer",
            input_tokens=100,
            output_tokens=50,
            reasoning_tokens=30,
            finish_reason="stop",
        )
        out = CoTSampler(FakeSampler(response=inner))([])
        assert out is inner

    def test_metadata_preserved_on_split(self):
        inner = SamplerResponse(
            response_text="thinking...\nAnswer: X",
            raw="thinking...\nAnswer: X",
            input_tokens=100,
            output_tokens=50,
            finish_reason="stop",
        )
        out = CoTSampler(FakeSampler(response=inner))([])
        assert out.input_tokens == 100 and out.output_tokens == 50
        assert out.finish_reason == "stop"


# ---------------------------------------------------------------------------
# max_new_tokens budget raising (applied to the inner sampler at wrap time)
# ---------------------------------------------------------------------------


class TestBudgetRaise:
    def test_raises_small_budget(self):
        fake = FakeSampler(response="Answer: ok")
        fake.max_new_tokens = 512
        cot = CoTSampler(fake)
        assert fake.max_new_tokens == 8192
        assert cot.max_new_tokens == 8192

    def test_keeps_large_budget(self):
        fake = FakeSampler(response="Answer: ok")
        fake.max_new_tokens = 16384
        cot = CoTSampler(fake)
        assert fake.max_new_tokens == 16384
        assert cot.max_new_tokens == 16384


# ---------------------------------------------------------------------------
# Delegation tests
# ---------------------------------------------------------------------------


class TestDelegation:
    def test_is_local_delegates(self):
        fake = FakeSampler()
        fake.is_local = True
        cot = CoTSampler(fake)
        assert cot.is_local is True

    def test_is_local_default_false(self):
        fake = FakeSampler()
        cot = CoTSampler(fake)
        assert cot.is_local is False

    def test_temperature_delegates(self):
        fake = FakeSampler()
        fake.temperature = 1.0
        cot = CoTSampler(fake)
        assert cot.temperature == 1.0
