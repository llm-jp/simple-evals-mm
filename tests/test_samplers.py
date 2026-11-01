"""v0.5 provider/model slugs, keys, base urls."""

import json
from types import SimpleNamespace as NS

import pytest

from simple_evals_mm import errors
from simple_evals_mm.models import Image, user
from simple_evals_mm.samplers import registry


@pytest.mark.parametrize("slug,kw,env,key,url_from,url", [
    ("openrouter/qwen/qwen3.8-27b", {}, {}, "OPENROUTER_API_KEY", "default", "https://openrouter.ai/api/v1"),
    ("openai/gpt-5.1-2025-11-13", {"api_key_name": "MY_KEY"}, {}, "MY_KEY", "default", "https://api.openai.com/v1"),
    ("sglang/Qwen/Qwen3.8-27B", {}, {"SGLANG_BASE_URL": "http://h:1/v1"}, "SGLANG_API_KEY", "SGLANG_BASE_URL", "http://h:1/v1"),
    ("vllm/m", {"base_url": "http://cli/v1"}, {"VLLM_BASE_URL": "http://env/v1"}, "VLLM_API_KEY", "--base-url", "http://cli/v1"),
])
def test_resolution(monkeypatch, slug, kw, env, key, url_from, url):
    for k in ("SGLANG_BASE_URL", "VLLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = registry.resolve(slug, **kw)
    assert (spec.api_key_name, spec.base_url_source, spec.base_url(kw.get("base_url"))) == (key, url_from, url)
    res = json.dumps(spec.resolution())
    assert all(v not in res for v in env.values())  # names only, never values


def test_presets_and_errors(monkeypatch):
    monkeypatch.delenv("SGLANG_BASE_URL", raising=False)
    s = registry.resolve("sglang/Qwen/Qwen3.8-27B", effort="low")
    body = registry.make_sampler(s).request_body()
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "preserve_thinking": False, "reasoning_effort": "low"}
    assert body["max_tokens"] == 65536 and body["top_k"] == 20
    assert registry.resolve("sglang/other").sampling == {"temperature": 0.0}  # served: always explicit
    judge = registry.make_sampler(registry.resolve(registry.DEFAULT_JUDGE)).request_body()
    assert judge["reasoning"] == {"effort": "none"} and judge["max_tokens"] == 8192
    assert judge["provider"] == {"order": ["openai"], "allow_fallbacks": False}
    for bad, kw in (("noprovider", {}), ("bogus/x", {}), ("openrouter/qwen/qwen3.8-27b", {"effort": "max"}), ("azure/x", {})):
        with pytest.raises(ValueError):
            registry.resolve(bad, **kw)


def test_pinned_provider_mismatch_is_a_failure():
    from types import SimpleNamespace as NS

    sampler = registry.make_sampler(registry.resolve("openrouter/qwen/qwen3.8-27b"))
    msg = NS(content="hi", tool_calls=[NS(id="c1", function=NS(name="f", arguments='{"a": 1}'))], model_extra={})
    resp = NS(choices=[NS(message=msg, finish_reason="tool_calls")], usage=None, model="m", system_fingerprint=None,
              model_extra={"provider": "Alibaba"})
    r = sampler._parse(resp)
    assert r.provider == "Alibaba" and r.tool_calls[0].arguments == {"a": 1}
    resp.model_extra = {"provider": "DeepInfra"}
    with pytest.raises(errors.SamplerError) as e:
        sampler._parse(resp)
    assert e.value.cause == errors.PROVIDER_FAILED


# ---------------------------------------------------------------- request parity with the v0.4 samplers
# The same prompt (one image + text) through a v0.4 sampler and the v0.5 sampler of the mapped slug; the captured
# request (the chat body with extra_body merged, the Responses kwargs, the Gemini contents / config) must be equal up
# to the intended differences listed per case. Offline: the SDK clients are replaced by recorders.

CHAT_REPLY = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
              "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": " 42 "}}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
HARMONY_RAW = ("<|channel|>analysis<|message|>think hard<|end|><|start|>assistant<|channel|>final<|message|>"
               "The answer is 42<|return|>")
RESPONSES_REPLY = {"id": "r", "created_at": 0, "model": "gpt-5.1-2025-11-13", "object": "response", "status": "completed",
                   "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                   "output": [{"type": "message", "id": "m", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": " 42 ", "annotations": []}]}],
                   "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
                             "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                             "output_tokens_details": {"reasoning_tokens": 4}}}
GEMINI_REPLY = {"candidates": [{"content": {"role": "model", "parts": [{"text": "42"}]}, "finish_reason": "STOP"}],
                "usage_metadata": {"prompt_token_count": 4, "candidates_token_count": 1, "thoughts_token_count": 9}}
API_ENV = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY_GPT5",
           "AZURE_OPENAI_ENDPOINT_GPT5", "GEMINI_API_KEY", "SGLANG_BASE_URL", "HARMONY_DATE", "HARMONY_IDENTITY",
           "OPENROUTER_PROVIDER", "OPENROUTER_QUANT", "OPENROUTER_SAMPLING", "OPENROUTER_TEMP", "OPENROUTER_TOP_P",
           "OPENROUTER_TOP_K", "SGLANG_TEMP", "SGLANG_TOP_P", "SGLANG_TOP_K", "SGLANG_PRESENCE", "SGLANG_REPETITION")


class Recorder:
    """Stands in for an SDK client: records the kwargs of chat.completions.create / responses.create /
    models.generate_content and answers with `reply`."""

    def __init__(self, reply):
        self.calls, self.reply = [], reply
        self.chat = NS(completions=NS(create=self._rec))
        self.responses = NS(create=self._rec)
        self.models = NS(generate_content=self._rec)

    def _rec(self, **kw):
        self.calls.append(kw)
        return self.reply


def _chat_reply(content=" 42 "):
    from openai.types.chat import ChatCompletion

    d = json.loads(json.dumps(CHAT_REPLY))
    d["choices"][0]["message"]["content"] = content
    return ChatCompletion.model_validate(d)


def _flat(kw: dict) -> dict:
    """A chat.completions.create call as the JSON body sent (extra_body merged into the top level)."""
    out = {k: v for k, v in kw.items() if k != "extra_body"}
    out.update(kw.get("extra_body") or {})
    return out


@pytest.fixture
def png(tmp_path):
    from PIL import Image as PILImage

    p = tmp_path / "img.png"
    PILImage.new("RGBA", (12, 7), (10, 200, 30, 128)).save(p)
    return str(p)


@pytest.fixture
def clean_env(monkeypatch):
    for k in API_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EVAL_HANDSHAKE", "off")
    monkeypatch.setenv("OPENROUTER_OFFLINE", "1")
    return monkeypatch


def _v05(slug, reply, msgs, **kw):
    s = registry.make_sampler(registry.resolve(slug, **kw))
    rec = Recorder(reply)
    s._client = rec
    return s(msgs), rec.calls[-1], s


def _v04(cls, model_id, reply, png, text, effort=None, attr="client"):
    s = cls(model_id)
    if effort:
        s.set_reasoning_effort(effort)
    rec = Recorder(reply)
    setattr(s, attr, rec)
    return s([s.pack_message(images=[png], instruction=text)]), rec.calls[-1]


# (v0.4 sampler, v0.4 model id, env, effort, v0.5 slug, the v0.5-only body fields)
CHAT_CASES = {
    "gpt-4o (OpenAI chat)": ("simple_evals_mm.sampler.openai_sampler:OpenAISampler", "gpt-4o-2024-11-20",
                             {"OPENAI_API_KEY": "k"}, None, "openai/gpt-4o-2024-11-20", {}),
    # v0.4 sent OpenAISampler over OpenRouter unpinned; v0.5 pins OpenAI's endpoint and asks for the billed cost
    "gpt-4o (OpenRouter)": ("simple_evals_mm.sampler.openai_sampler:OpenAISampler", "gpt-4o-2024-11-20",
                            {"OPENROUTER_API_KEY": "k"}, None, "openrouter/openai/gpt-4o-2024-11-20",
                            {"provider": {"order": ["openai"], "allow_fallbacks": False}, "usage": {"include": True}}),
    "qwen3.8-27b (OpenRouter pin)": ("simple_evals_mm.sampler.openrouter_sampler:OpenRouterSampler", "openrouter/qwen/qwen3.8-27b",
                                     {"OPENROUTER_API_KEY": "k"}, "low", "openrouter/qwen/qwen3.8-27b", {}),
    "gemma-4-31b (OpenRouter bf16 pin, toggle)": ("simple_evals_mm.sampler.openrouter_sampler:OpenRouterSampler",
                                                  "openrouter/google/gemma-4-31b-it", {"OPENROUTER_API_KEY": "k"}, "none",
                                                  "openrouter/google/gemma-4-31b-it", {}),
    "Qwen3.8 sglang": ("simple_evals_mm.sampler.sglang_sampler:Qwen3_8SGLangSampler", "Qwen/Qwen3.8-27B",
                       {"SGLANG_BASE_URL": "http://h:1/v1"}, "low", "sglang/Qwen/Qwen3.8-27B", {}),
    "Qwen3.5 sglang": ("simple_evals_mm.sampler.sglang_sampler:Qwen3_5SGLangSampler", "Qwen/Qwen3.5-9B",
                       {"SGLANG_BASE_URL": "http://h:1/v1"}, None, "sglang/Qwen/Qwen3.5-9B", {}),
    "gemma-4 sglang": ("simple_evals_mm.sampler.sglang_sampler:Gemma4SGLangSampler", "google/gemma-4-E4B-it",
                       {"SGLANG_BASE_URL": "http://h:1/v1"}, None, "sglang/google/gemma-4-E4B-it", {}),
    "GLM-5.3 sglang": ("simple_evals_mm.sampler.sglang_sampler:GLM53SGLangSampler", "zai-org/GLM-5.3-Flash",
                       {"SGLANG_BASE_URL": "http://h:1/v1"}, "max", "sglang/zai-org/GLM-5.3-Flash", {}),
    "DeepSeek-V4.1 sglang": ("simple_evals_mm.sampler.sglang_sampler:DeepSeekV41SGLangSampler", "deepseek-ai/DeepSeek-V4.1-Flash",
                             {"SGLANG_BASE_URL": "http://h:1/v1"}, "high", "sglang/deepseek-ai/DeepSeek-V4.1-Flash", {}),
    "llm-jp-4-vl Harmony": ("simple_evals_mm.sampler.sglang_sampler:HarmonyServedSampler", "llm-jp/llm-jp-4-vl-9b",
                            {"SGLANG_BASE_URL": "http://h:1/v1"}, "medium", "hf/llm-jp/llm-jp-4-vl-9b", {}),
    "Qwen3-VL served": ("simple_evals_mm.sampler.sglang_sampler:SGLangSampler", "Qwen/Qwen3-VL-2B-Instruct",
                        {"SGLANG_BASE_URL": "http://h:1/v1"}, None, "sglang/Qwen/Qwen3-VL-2B-Instruct", {}),
}


def _cls(ref):
    import importlib

    mod, name = ref.split(":")
    return getattr(importlib.import_module(mod), name)


@pytest.mark.parametrize("case", sorted(CHAT_CASES))
def test_chat_request_parity_with_v04(clean_env, png, case):
    ref, model_id, env, effort, slug, v05_only = CHAT_CASES[case]
    for k, v in env.items():
        clean_env.setenv(k, v)
    content = HARMONY_RAW if "Harmony" in case else " 42 "
    old, old_kw = _v04(_cls(ref), model_id, _chat_reply(content), png, "How many?", effort)
    new, new_kw, _ = _v05(slug, _chat_reply(content), [user(Image(path=png), "How many?")], effort=effort)
    expected = _flat(old_kw)
    expected.update(v05_only)
    assert _flat(new_kw) == expected
    assert (new.text, new.reasoning, new.finish_reason) == (old.response_text, old.reasoning, old.finish_reason)


def test_responses_request_parity_with_v04(clean_env, png):
    """gpt-5.1 through the Responses API (OpenAI; Azure: deployment name with dashes, the GPT-5 variables)."""
    from openai.types.responses import Response as OAIResponse

    from simple_evals_mm.sampler.responses_sampler import ResponsesSampler

    reply = OAIResponse.model_validate(RESPONSES_REPLY)
    clean_env.setenv("OPENAI_API_KEY", "k")
    for effort in ("none", "high"):
        old, old_kw = _v04(ResponsesSampler, "gpt-5.1-2025-11-13", reply, png, "How many?", effort)
        new, new_kw, s = _v05("openai/gpt-5.1-2025-11-13", reply, [user(Image(path=png), "How many?")],
                              effort=None if effort == "none" else effort)
        assert s.api_format == "openai_responses" and new_kw == old_kw
        assert new_kw["max_output_tokens"] == (16384 if effort == "high" else 8192)
        assert (new.text, new.finish_reason, new.reasoning_tokens) == (old.response_text, old.finish_reason, old.reasoning_tokens)
    clean_env.delenv("OPENAI_API_KEY")
    clean_env.setenv("AZURE_OPENAI_KEY_GPT5", "k")
    clean_env.setenv("AZURE_OPENAI_ENDPOINT_GPT5", "https://az.example/")
    old, old_kw = _v04(ResponsesSampler, "gpt-5.1-2025-11-13", reply, png, "How many?")
    spec = registry.resolve("azure/gpt-5.1-2025-11-13")
    assert (spec.api_key_name, spec.base_url_source, spec.api_version) == ("AZURE_OPENAI_KEY_GPT5", "AZURE_OPENAI_ENDPOINT_GPT5", "2025-04-01-preview")
    new, new_kw, _ = _v05("azure/gpt-5.1-2025-11-13", reply, [user(Image(path=png), "How many?")])
    assert new_kw == old_kw and new_kw["model"] == "gpt-5-1-2025-11-13"


def test_judge_request_parity_with_v04(clean_env, png):
    """The v0.4 judge (ResponsesSampler with an OpenRouter key) vs DEFAULT_JUDGE: v0.5 pins OpenAI's endpoint and asks
    for the billed cost; the rest is equal."""
    from simple_evals_mm.sampler.responses_sampler import ResponsesSampler

    clean_env.setenv("OPENROUTER_API_KEY", "k")
    old, old_kw = _v04(ResponsesSampler, "gpt-5.1-2025-11-13", _chat_reply(), png, "Grade this.")
    new, new_kw, _ = _v05(registry.DEFAULT_JUDGE, _chat_reply(), [user(Image(path=png), "Grade this.")])
    assert _flat(new_kw) == {**_flat(old_kw), "provider": {"order": ["openai"], "allow_fallbacks": False}, "usage": {"include": True}}
    assert new.text == old.response_text


def test_gemini_request_parity_with_v04(clean_env, png):
    """Gemini 3.1 Pro: the same parts and GenerateContentConfig; v0.5 sets the role "user" explicitly (v0.4 sent a
    role-less Content, which the API reads as user). The answer is stripped in v0.5."""
    from google.genai import types

    from simple_evals_mm.sampler.gemini_sampler import GeminiSampler as V04

    reply = types.GenerateContentResponse.model_validate(GEMINI_REPLY)
    clean_env.setenv("GEMINI_API_KEY", "k")
    for effort in (None, "high"):
        s = V04("gemini-3.1-pro-preview")
        if effort:
            s.set_reasoning_effort(effort)
        rec = Recorder(reply)
        s.client = rec
        old = s([s.pack_message(images=[png], instruction="How many?")])
        old_kw = rec.calls[-1]
        new, new_kw, sampler = _v05("gemini/gemini-3.1-pro-preview", reply, [user(Image(path=png), "How many?")], effort=effort)
        assert sampler.api_format == "google_generate_content" and new_kw["model"] == old_kw["model"]
        assert new_kw["config"].model_dump(exclude_none=True) == old_kw["config"].model_dump(exclude_none=True)
        [old_c], [new_c] = old_kw["contents"], new_kw["contents"]
        assert old_c.role is None and new_c.role == "user" and new_c.parts == old_c.parts
        assert (new.text, new.finish_reason, new.output_tokens, new.reasoning_tokens) == \
            (old.response_text, old.finish_reason, old.output_tokens, old.reasoning_tokens)


def test_gemini_parse_and_tools(clean_env):
    from google.genai import types

    clean_env.setenv("GEMINI_API_KEY", "k")
    s = registry.make_sampler(registry.resolve("gemini/gemini-3.1-pro-preview", model_args={"include_thoughts": "true"}))
    assert s.request_body()["thinking_config"] == {"thinking_level": "low", "include_thoughts": True}
    r = s._parse(types.GenerateContentResponse.model_validate({
        "candidates": [{"content": {"role": "model", "parts": [{"text": "plan", "thought": True}, {"text": "ok"},
                                                              {"function_call": {"name": "f", "args": {"a": 1}}}]},
                        "finish_reason": "STOP"}]}))
    assert (r.text, r.reasoning, r.tool_calls[0].name, r.tool_calls[0].arguments, r.finish_reason) == ("ok", "plan", "f", {"a": 1}, "tool_calls")
    blocked = s._parse(types.GenerateContentResponse.model_validate({"candidates": [{"finish_reason": "SAFETY"}]}))
    assert blocked.finish_reason == "content_filter" and errors.classify_finish(blocked.finish_reason) == errors.REFUSED
    cfg = s.build_config("be brief", [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}])
    assert cfg.system_instruction == "be brief" and cfg.tools[0].function_declarations[0].name == "f"


def test_responses_parse_reasoning_refusal_tools(clean_env):
    from openai.types.responses import Response as OAIResponse

    clean_env.setenv("OPENAI_API_KEY", "k")
    s = registry.make_sampler(registry.resolve("openai/gpt-5.1-2025-11-13", effort="low", model_args={"reasoning_summary": "auto"}))
    assert s.request_body() == {"max_output_tokens": 8192, "reasoning": {"effort": "low", "summary": "auto"}}
    d = json.loads(json.dumps(RESPONSES_REPLY))
    d["output"] = [{"type": "reasoning", "id": "rs", "summary": [{"type": "summary_text", "text": "thought"}]},
                   {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a": 1}'}]
    r = s._parse(OAIResponse.model_validate(d))
    assert (r.reasoning, r.tool_calls[0].id, r.tool_calls[0].arguments, r.finish_reason, r.reasoning_tokens) == ("thought", "c1", {"a": 1}, "tool_calls", 4)
    d["output"] = [{"type": "message", "id": "m", "role": "assistant", "status": "completed",
                    "content": [{"type": "refusal", "refusal": "no"}]}]
    assert s._parse(OAIResponse.model_validate(d)).finish_reason == "content_filter"
    d.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    d["output"] = []
    assert s._parse(OAIResponse.model_validate(d)).finish_reason == "length"
    tools = s.to_api_messages([user("q")])
    assert tools[0] == {"role": "developer", "content": [{"type": "input_text", "text": "You are a helpful assistant."}]}


def test_retry_policy():
    from simple_evals_mm.samplers.base import retry_call

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return "ok"

    assert retry_call(flaky, label="t", max_tries=5, max_rate_limit_tries=5, classify=lambda e: "retry", sleep=lambda s: None) == "ok"
    with pytest.raises(errors.SamplerError) as e:
        retry_call(lambda: (_ for _ in ()).throw(ConnectionError("down")), label="t", max_tries=2, max_rate_limit_tries=2,
                   classify=lambda e: "retry", sleep=lambda s: None)
    assert e.value.cause == errors.PROVIDER_FAILED
    with pytest.raises(errors.SamplerError) as e:
        retry_call(lambda: (_ for _ in ()).throw(ValueError("prompt is too long")), label="t", max_tries=2,
                   max_rate_limit_tries=2, classify=lambda e: "fail", sleep=lambda s: None)
    assert e.value.cause == errors.CONTEXT_OVERFLOW


# ---------------------------------------------------------------- presets, formats, model args
@pytest.mark.parametrize("slug,fmt", [
    ("openai/gpt-5.1-2025-11-13", "openai_responses"), ("openai/gpt-5.2-x", "openai_responses"),
    ("openai/gpt-4o-2024-11-20", "openai_chat_completions"), ("gemini/gemini-3.1-pro-preview", "google_generate_content"),
    ("vertex/gemini-3-pro-preview", "google_generate_content"), ("openrouter/openai/gpt-5.1", "openai_chat_completions"),
    ("sglang/Qwen/Qwen3.8-27B", "openai_chat_completions"), ("hf/sbintuitions/sarashina2.2-vision-3b", "openai_chat_completions"),
])
def test_default_api_format(clean_env, slug, fmt):
    spec = registry.resolve(slug)
    sampler = registry.make_sampler(spec)
    assert spec.api_format == fmt == sampler.api_format
    json.dumps(sampler.config())


def test_api_format_override(clean_env):
    assert registry.resolve("openai/gpt-5.1-2025-11-13", api_format="openai_chat_completions").api_format == "openai_chat_completions"
    with pytest.raises(ValueError, match="not available"):
        registry.resolve("sglang/m", api_format="openai_responses")


def test_model_args(clean_env):
    with pytest.raises(ValueError, match="unknown --model-arg max_soft_tokens"):
        registry.resolve("sglang/Qwen/Qwen3.8-27B", model_args={"max_soft_tokens": 1120})
    g = registry.resolve("sglang/google/gemma-4-12B-it", effort="high", model_args={"max_soft_tokens": "1120", "repetition_penalty": 1.05})
    body = registry.make_sampler(g).request_body()
    assert g.server_args == {"max_soft_tokens": 1120} and "max_soft_tokens" not in json.dumps(body)  # server side
    assert body["chat_template_kwargs"] == {"enable_thinking": True} and body["repetition_penalty"] == 1.05
    assert g.public()["model_args"] == {"max_soft_tokens": "1120", "repetition_penalty": 1.05}  # recorded as given
    h = registry.resolve("hf/llm-jp/llm-jp-4-vl-9b-beta", model_args={"date": "none", "identity": "You are X.", "decode": "qwen"})
    hb = registry.make_sampler(h).request_body()
    assert hb["chat_template_kwargs"] == {"conversation_start_date": "", "model_identity": "You are X."}
    assert {k: hb[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty")} == {"temperature": 1.0, "top_p": 1.0, "top_k": 40, "repetition_penalty": 1.3}
    assert registry.resolve("sglang/llm-jp/llm-jp-4.1-8b-thinking").template_kwargs == {"conversation_start_date": "2026-10-01"}
    o = registry.resolve("openrouter/qwen/qwen3.8-27b", temperature=0.6, model_args={"sampling": "default", "max_image_px": 2048})
    assert o.sampling == {"temperature": 0.6} and o.max_image_px == 2048
    with pytest.raises(ValueError, match="must be one of"):
        registry.resolve("openrouter/qwen/qwen3.8-27b", model_args={"sampling": "greedy"})
    assert registry.resolve("sglang/m", top_k=0).sampling["top_k"] == -1  # v0.4 SGLANG_TOP_K=0
    e = registry.make_sampler(registry.resolve("sglang/m", model_args={"extra_body": {"seed": 1}})).request_body()
    assert e["seed"] == 1


def test_presets_cover_v04_models(clean_env):
    """Every KNOWN_MODELS entry and every leaderboard model has a preset (docs/design/v0.5.md "Model names")."""
    from simple_evals_mm.common import EVAL_FIXED_DATE
    from simple_evals_mm.samplers import presets

    assert presets.EVAL_FIXED_DATE == EVAL_FIXED_DATE
    v05 = ["openrouter/qwen/qwen3.8-27b", "openai/gpt-4o-2024-11-20", "openai/gpt-5.1-2025-11-13", "gemini/gemini-3.1-pro-preview",
           "sglang/google/gemma-4-E4B-it", "sglang/Qwen/Qwen3-VL-2B-Instruct", "sglang/Qwen/Qwen3.5-4B", "sglang/Qwen/Qwen3.8-27B",
           "sglang/zai-org/GLM-5.3-Flash", "sglang/deepseek-ai/DeepSeek-V4.1-Flash", "sglang/OpenGVLab/InternVL3_5-2B",
           "hf/sbintuitions/sarashina2.2-vision-3b", "hf/llm-jp/llm-jp-4-vl-9b", "hf/llm-jp/llm-jp-4-vl-9b-beta",
           *[f"openrouter/{m}" for m in presets.OPENROUTER_MODELS]]
    for slug in v05:
        key, _ = registry.find_preset(*registry.parse_slug(slug))
        assert key is not None, slug
    rows = registry.preset_rows()
    assert len(rows) == len(presets.PRESETS) and all(json.dumps(r) for r in rows)


def test_preflight_new_formats(clean_env):
    """Key / project variables named (never values); the base URL probed (discard port: unreachable)."""
    clean_env.setenv("GEMINI_BASE_URL", "http://127.0.0.1:9")
    p = registry.make_sampler(registry.resolve("gemini/gemini-3.1-pro-preview")).preflight(timeout=2)
    assert p[0] == "GEMINI_API_KEY is not set (provider gemini)" and "unreachable" in p[1] and "127.0.0.1" not in " ".join(p)
    clean_env.setenv("GEMINI_PROJECT_ID", "secret-project")
    p = registry.make_sampler(registry.resolve("vertex/gemini-3.1-pro-preview")).preflight()
    assert p == ["GEMINI_LOCATION is not set (provider vertex)"]
    assert registry.resolve("vertex/gemini-3.1-pro-preview").resolution()["vars_set"] == {"GEMINI_PROJECT_ID": True, "GEMINI_LOCATION": False}
    clean_env.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    p = registry.make_sampler(registry.resolve("openai/gpt-5.1-2025-11-13")).preflight(timeout=2)
    assert p[0] == "OPENAI_API_KEY is not set (provider openai)" and "OPENAI_BASE_URL is unreachable" in p[1]
