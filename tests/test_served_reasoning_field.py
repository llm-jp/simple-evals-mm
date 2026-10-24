"""The served sampler reads the separated reasoning under either name: sglang / older vLLM use `reasoning_content`,
vLLM >= 0.32 renamed it to `reasoning` (without this, its startup check rejects a correctly configured vLLM server)."""
from types import SimpleNamespace

from openai.types.chat import ChatCompletionMessage

from simple_evals_mm.sampler.sglang_sampler import _reasoning_of


def test_reasoning_field_names():
    vllm = ChatCompletionMessage.model_validate({"role": "assistant", "content": "4", "reasoning": "2+2 is 4"})
    sglang = ChatCompletionMessage.model_validate({"role": "assistant", "content": "4", "reasoning_content": "2+2 is 4"})
    assert _reasoning_of(vllm) == _reasoning_of(sglang) == "2+2 is 4"
    assert _reasoning_of(SimpleNamespace(content="4")) == ""
