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
    ("vllm/m", {"base_url": "http://cli/v1", "preset": "none"}, {"VLLM_BASE_URL": "http://env/v1"}, "VLLM_API_KEY", "--base-url", "http://cli/v1"),
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


def test_presets_and_errors(monkeypatch, endpoints):
    monkeypatch.delenv("SGLANG_BASE_URL", raising=False)
    sampler = registry.make_sampler(registry.resolve("sglang/Qwen/Qwen3.8-27B", effort="low"))
    sampler.prepare()
    body = sampler.request_body()
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "preserve_thinking": False, "reasoning_effort": "low"}
    assert body["max_tokens"] == 262144 and body["top_k"] == 20  # the server's context length (fake /v1/models)
    assert registry.resolve("sglang/other", preset="none").sampling == {}  # nothing model-specific
    judge = registry.make_sampler(registry.resolve(registry.DEFAULT_JUDGE))
    judge.prepare()
    jb = judge.request_body()
    assert jb["reasoning"] == {"effort": "none"} and jb["max_tokens"] == 128000  # the endpoint maximum, no small cap
    assert jb["provider"] == {"order": ["openai"], "allow_fallbacks": False}
    for bad, kw in (("noprovider", {}), ("bogus/x", {}), ("openrouter/qwen/qwen3.8-27b", {"effort": "max"}),
                    ("azure/x", {"preset": "none"}), ("sglang/other", {}), ("sglang/m", {"preset": "nope"})):
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
        self.models = NS(generate_content=self._rec, get=lambda model: self.model_info)
        self.model_info = NS(name="models/x", version="3", temperature=None, top_p=None, top_k=None, output_token_limit=65536)

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


ENDPOINT_ROW = {"model_id": "m", "matched": True, "provider_name": "OpenAI", "tag": "openai", "quantization": "unknown",
                "context_length": 400000, "max_completion_tokens": 128000, "price_per_mtok": {"prompt": 1.25}}


@pytest.fixture
def endpoints(monkeypatch):
    """Fake catalogs for prepare(): OpenRouter /models (default_parameters) and /models/{id}/endpoints (the pinned
    row), a served engine's /v1/models (+ version) and its generation_config.json. Tests edit the dicts."""
    from simple_evals_mm.samplers import openai_chat

    fake = {"defaults": {}, "row": dict(ENDPOINT_ROW), "server": {"model_id": "m", "root": "m", "max_model_len": 262144,
                                                                   "version": "0.5.23"}, "gen": {},
            "real_read_generation_config": openai_chat.read_generation_config}
    monkeypatch.setattr(openai_chat, "fetch_model_info", lambda model_id, **kw: {
        "default_parameters": dict(fake["defaults"]), "canonical_slug": model_id + "-2026", "hugging_face_id": None,
        "catalog_context_length": 400000})
    monkeypatch.setattr(openai_chat, "fetch_endpoint_info", lambda model_id, providers, quant=None, **kw: {
        **fake["row"], "model_id": model_id})
    monkeypatch.setattr(openai_chat, "fetch_server_info", lambda base_url, engine, key=None, **kw: {
        "engine": engine, **fake["server"]})
    monkeypatch.setattr(openai_chat, "read_generation_config", lambda model: (dict(fake["gen"]), {"model": model, "found": bool(fake["gen"])}))
    return fake


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


# (v0.4 sampler, v0.4 model id, env, effort, v0.5 slug, the v0.5-only body fields); V04_MAX_TOKENS: the v0.4 default
# budget, passed to v0.5 explicitly (v0.5's default is the endpoint maximum)
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


V04_MAX_TOKENS = {"gpt-4o (OpenAI chat)": 8192, "gpt-4o (OpenRouter)": 8192, "qwen3.8-27b (OpenRouter pin)": 131072,
                  "gemma-4-31b (OpenRouter bf16 pin, toggle)": 131072, "Qwen3.8 sglang": 65536, "Qwen3.5 sglang": 16384,
                  "gemma-4 sglang": 8192, "GLM-5.3 sglang": 65536, "DeepSeek-V4.1 sglang": 65536,
                  "llm-jp-4-vl Harmony": 8192, "Qwen3-VL served": 8192}


def _cls(ref):
    import importlib

    mod, name = ref.split(":")
    return getattr(importlib.import_module(mod), name)


@pytest.mark.parametrize("case", sorted(CHAT_CASES))
def test_chat_request_parity_with_v04(clean_env, endpoints, png, case):
    ref, model_id, env, effort, slug, v05_only = CHAT_CASES[case]
    for k, v in env.items():
        clean_env.setenv(k, v)
    endpoints["row"]["max_completion_tokens"] = 262144  # above every v0.4 budget: nothing clamped
    content = HARMONY_RAW if "Harmony" in case else " 42 "
    old, old_kw = _v04(_cls(ref), model_id, _chat_reply(content), png, "How many?", effort)
    new, new_kw, _ = _v05(slug, _chat_reply(content), [user(Image(path=png), "How many?")], effort=effort,
                          max_output_tokens=V04_MAX_TOKENS[case])
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
        v04_max = 16384 if effort == "high" else 8192  # v0.4: 8192, the effort floor 16384 at "high"
        new, new_kw, s = _v05("openai/gpt-5.1-2025-11-13", reply, [user(Image(path=png), "How many?")],
                              effort=None if effort == "none" else effort, max_output_tokens=v04_max)
        assert s.api_format == "openai_responses" and new_kw == old_kw
        assert new_kw["max_output_tokens"] == v04_max
        assert (new.text, new.finish_reason, new.reasoning_tokens) == (old.response_text, old.finish_reason, old.reasoning_tokens)
    clean_env.delenv("OPENAI_API_KEY")
    clean_env.setenv("AZURE_OPENAI_KEY_GPT5", "k")
    clean_env.setenv("AZURE_OPENAI_ENDPOINT_GPT5", "https://az.example/")
    old, old_kw = _v04(ResponsesSampler, "gpt-5.1-2025-11-13", reply, png, "How many?")
    spec = registry.resolve("azure/gpt-5.1-2025-11-13")
    assert (spec.api_key_name, spec.base_url_source, spec.api_version) == ("AZURE_OPENAI_KEY_GPT5", "AZURE_OPENAI_ENDPOINT_GPT5", "2025-04-01-preview")
    new, new_kw, _ = _v05("azure/gpt-5.1-2025-11-13", reply, [user(Image(path=png), "How many?")], max_output_tokens=8192)
    assert new_kw == old_kw and new_kw["model"] == "gpt-5-1-2025-11-13"
    _, new_kw, s = _v05("azure/gpt-5.1-2025-11-13", reply, [user(Image(path=png), "How many?")])
    assert new_kw["max_output_tokens"] == 128000 == s.config()["settings"]["max_output_tokens"]["value"]  # output_limit


def test_judge_request_parity_with_v04(clean_env, endpoints, png):
    """The v0.4 judge (ResponsesSampler with an OpenRouter key) vs DEFAULT_JUDGE: v0.5 pins OpenAI's endpoint, asks
    for the billed cost and sends the endpoint maximum (128000) instead of v0.4's 8192 (at effort medium a small cap
    cuts verdicts: judge_failed); the rest is equal."""
    from simple_evals_mm.sampler.responses_sampler import ResponsesSampler

    clean_env.setenv("OPENROUTER_API_KEY", "k")
    old, old_kw = _v04(ResponsesSampler, "gpt-5.1-2025-11-13", _chat_reply(), png, "Grade this.")
    assert _flat(old_kw)["max_tokens"] == 8192
    new, new_kw, _ = _v05(registry.DEFAULT_JUDGE, _chat_reply(), [user(Image(path=png), "Grade this.")])
    assert _flat(new_kw) == {**_flat(old_kw), "provider": {"order": ["openai"], "allow_fallbacks": False},
                             "usage": {"include": True}, "max_tokens": 128000}
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
        new, new_kw, sampler = _v05("gemini/gemini-3.1-pro-preview", reply, [user(Image(path=png), "How many?")], effort=effort,
                                    max_output_tokens=16384 if effort == "high" else 8192)  # v0.4: 8192, 16384 at high
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
    assert s.request_body() == {"max_output_tokens": 128000, "reasoning": {"effort": "low", "summary": "auto"}}
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
    ("vertex/gemini-3.1-pro-preview", "google_generate_content"), ("openrouter/openai/gpt-5.1", "openai_chat_completions"),
    ("sglang/Qwen/Qwen3.8-27B", "openai_chat_completions"), ("hf/sbintuitions/sarashina2.2-vision-3b", "openai_chat_completions"),
])
def test_default_api_format(clean_env, slug, fmt):
    spec = registry.resolve(slug, preset="none" if slug == "openai/gpt-5.2-x" else None)
    sampler = registry.make_sampler(spec)
    assert spec.api_format == fmt == sampler.api_format
    json.dumps(sampler.config())


def test_api_format_override(clean_env):
    assert registry.resolve("openai/gpt-5.1-2025-11-13", api_format="openai_chat_completions").api_format == "openai_chat_completions"
    with pytest.raises(ValueError, match="not available"):
        registry.resolve("sglang/m", preset="none", api_format="openai_responses")


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
    assert registry.resolve("sglang/m", preset="none", top_k=0).sampling["top_k"] == -1  # v0.4 SGLANG_TOP_K=0
    e = registry.make_sampler(registry.resolve("sglang/m", preset="none", model_args={"extra_body": {"seed": 1}})).request_body()
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
    slugs = {r["slug"] for r in rows}
    assert all(json.dumps(r) for r in rows) and {f"--preset {f}" for f in presets.FAMILIES} <= slugs
    assert "{sglang,vllm,hf}/Qwen/Qwen3.8-27B" in slugs and "openrouter/qwen/qwen3.8-27b" in slugs


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


def test_planned_judge_preset(endpoints):
    """openrouter/openai/gpt-6-luna (planned judge): effort medium, pinned to openai, max_output_tokens = the endpoint
    maximum."""
    s = registry.make_sampler(registry.resolve("openrouter/openai/gpt-6-luna"))
    s.prepare()
    cfg = s.config()
    assert cfg["effort"] == "medium" and cfg["max_output_tokens"] is None
    assert cfg["settings"]["max_output_tokens"] == {"value": 128000, "source": "openrouter:endpoint max_completion_tokens",
                                                   "requested": None, "endpoint_max": 128000}
    assert registry.resolve("openrouter/openai/gpt-6-luna").resolution()["model"] == "openai/gpt-6-luna"


# ---------------------------------------------------------------- exact presets, --preset
def test_presets_are_exact_slugs(clean_env):
    """No pattern matching: an unknown slug is refused; --preset none / --preset FAMILY are explicit and recorded."""
    from simple_evals_mm.samplers import presets

    assert not [k for k in presets.PRESETS if "*" in k]
    for slug in ("openai/gpt-5.9-2027-01-01", "gemini/gemini-3.9-pro", "sglang/google/gemma-4-99B-it", "openrouter/x/y"):
        with pytest.raises(ValueError, match="--preset none"):
            registry.resolve(slug)
    none = registry.resolve("sglang/google/gemma-4-99B-it", preset="none")
    assert (none.preset, none.family, none.sampling, none.efforts, none.max_output_tokens) == ("none", None, {}, None, None)
    assert none.public()["preset"] == "none"
    path = "models/LLM-jp-VL-llmjp4_harmony-steps-30000"
    fam = registry.resolve(f"sglang/{path}", preset="llm-jp-vl", effort="high")
    assert (fam.preset, fam.family, fam.response_parser, fam.model) == ("llm-jp-vl", "llm-jp-vl", "harmony", path)
    assert fam.sampling_source == {"temperature": "preset:llm-jp-vl", "skip_special_tokens": "preset:llm-jp-vl"}
    exact = registry.resolve("vllm/Qwen/Qwen3.5-9B")  # every engine prefix of a listed HF id: same family
    assert (exact.preset, exact.family) == ("vllm/Qwen/Qwen3.5-9B", "qwen3.5")


# ---------------------------------------------------------------- max_output_tokens = the endpoint maximum
def test_max_output_tokens_default_and_clamp(clean_env, endpoints):
    """Without --max-output-tokens: the pinned endpoint's max_completion_tokens (context_length when null), the served
    engine's max_model_len, the preset's output_limit; a larger request is clamped, requested + effective recorded."""
    endpoints["row"]["max_completion_tokens"] = 65536
    s = registry.make_sampler(registry.resolve("openrouter/qwen/qwen3.8-27b"))
    assert s.config()["settings"]["max_output_tokens"]["value"] is None  # before prepare: unresolved
    s.prepare()
    assert s.request_body()["max_tokens"] == 65536
    endpoints["row"]["max_completion_tokens"] = None
    s = registry.make_sampler(registry.resolve("openrouter/qwen/qwen3.8-27b", max_output_tokens=10 ** 6))
    s.prepare()
    assert s.request_body()["max_tokens"] == 400000
    assert s.config()["settings"]["max_output_tokens"] == {
        "value": 400000, "requested": 10 ** 6, "endpoint_max": 400000,
        "source": "cli, clamped to the endpoint maximum (openrouter:endpoint context_length)"}
    small = registry.make_sampler(registry.resolve("openrouter/qwen/qwen3.8-27b", max_output_tokens=4096))
    small.prepare()
    assert small.config()["settings"]["max_output_tokens"]["source"] == "cli" and small.request_body()["max_tokens"] == 4096

    endpoints["server"]["max_model_len"] = 32768
    v = registry.make_sampler(registry.resolve("vllm/Qwen/Qwen3.5-9B"))
    v.prepare()
    assert v.request_body()["max_tokens"] == 32768
    assert v.config()["settings"]["max_output_tokens"]["source"] == "vllm:max_model_len"
    assert v.config()["endpoint"]["version"] == "0.5.23"
    endpoints["server"]["max_model_len"] = None
    with pytest.raises(RuntimeError, match="--max-output-tokens"):
        registry.make_sampler(registry.resolve("vllm/Qwen/Qwen3.5-9B")).prepare()
    with pytest.raises(RuntimeError, match="give --provider"):  # unpinned OpenRouter: no single endpoint maximum
        registry.make_sampler(registry.resolve("openrouter/x/y", preset="none")).prepare()
    with pytest.raises(RuntimeError, match="output_limit"):
        registry.make_sampler(registry.resolve("openai/gpt-5.9", preset="none")).prepare()
    g = registry.make_sampler(registry.resolve("openai/gpt-5.9", preset="none", max_output_tokens=2048))
    g.prepare()
    assert g.request_body()["max_output_tokens"] == 2048


def test_served_overflow_refit():
    """sglang and vLLM name the input length in their overflow error: max_tokens is refitted to the remaining context."""
    from simple_evals_mm.samplers.openai_chat import _fit_max_tokens

    sg = ("Requested token count exceeds the model's maximum context length of 32768 tokens. You requested a total of "
          "33768 tokens: 1000 tokens from the input messages and 32768 tokens for the completion.")
    vl = ("This model's maximum context length is 32768 tokens. However, you requested 32768 output tokens and your "
          "prompt contains 1000 input tokens, for a total of 33768 tokens.")
    assert _fit_max_tokens(sg, 32768) == _fit_max_tokens(vl, 32768) == 32768 - 1000 - 16
    assert _fit_max_tokens(vl.replace("1000 input", "32700 input"), 32768) is None  # no room: context_overflow


# ---------------------------------------------------------------- resolved settings: value + source per field
def test_settings_precedence_cli_preset_provider(clean_env, endpoints):
    """CLI beats the preset beats the provider's declared defaults (OpenRouter default_parameters, sent explicitly);
    a field nobody declares is not sent and recorded as not reported."""
    endpoints["defaults"] = {"temperature": 0.3, "top_p": 0.5, "top_k": 7, "frequency_penalty": 0.1, "repetition_penalty": None}
    s = registry.make_sampler(registry.resolve("openrouter/qwen/qwen3.8-27b", temperature=0.6))
    s.prepare()
    body, st = s.request_body(), s.config()["settings"]
    preset = "preset:openrouter/qwen/qwen3.8-27b"
    assert (body["temperature"], body["top_p"], body["top_k"], body["frequency_penalty"]) == (0.6, 0.95, 20, 0.1)
    assert (st["temperature"]["source"], st["top_p"]["source"], st["frequency_penalty"]["source"]) == (
        "cli", preset, "openrouter:default_parameters")
    assert "repetition_penalty" not in body and st["repetition_penalty"] == {"value": None, "source": "provider default (not reported)"}
    assert st["effort"] == {"value": None, "source": "provider default (not reported)"}
    assert s.config()["endpoint"]["canonical_slug"] == "qwen/qwen3.8-27b-2026"

    luna = registry.make_sampler(registry.resolve("openrouter/openai/gpt-6-luna"))  # no preset sampling
    endpoints["defaults"] = {"temperature": 1, "top_p": None}
    luna.prepare()
    assert luna.request_body()["temperature"] == 1 and "top_p" not in luna.request_body()
    assert luna.config()["settings"]["temperature"] == {"value": 1, "source": "openrouter:default_parameters"}
    assert luna.config()["settings"]["effort"] == {"value": "medium", "source": "preset:openrouter/openai/gpt-6-luna"}


def test_settings_generation_config_under_preset_none(clean_env, endpoints, monkeypatch, tmp_path):
    """--preset none on a served model: the sampling defaults come from the model's generation_config.json (the
    directory the server reports as root) and are sent explicitly."""
    from simple_evals_mm.samplers import openai_chat

    monkeypatch.setattr(openai_chat, "read_generation_config", endpoints["real_read_generation_config"])
    model_dir = tmp_path / "ckpt"
    model_dir.mkdir()
    (model_dir / "generation_config.json").write_text(json.dumps({"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                                                                  "repetition_penalty": 1.05, "do_sample": True}))
    endpoints["server"]["root"] = str(model_dir)
    s = registry.make_sampler(registry.resolve("sglang/ckpt", preset="none", top_k=5))
    s.prepare()
    body, st = s.request_body(), s.config()["settings"]
    assert {k: body[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty")} == {
        "temperature": 0.7, "top_p": 0.8, "top_k": 5, "repetition_penalty": 1.05}
    assert st["temperature"]["source"] == "model:generation_config.json" and st["top_k"]["source"] == "cli"
    assert "min_p" not in body and st["min_p"]["source"] == "provider default (not reported)"
    gen = s.config()["endpoint"]["generation_config"]
    assert gen["found"] and gen["model"] == "ckpt" and len(gen["sha256"]) == 64  # no absolute path recorded


def test_settings_unknown_on_openai_and_gemini_defaults(clean_env):
    """OpenAI does not report defaults: a field not given is not sent, recorded as not reported. Gemini models.get
    declares temperature / top_p / top_k: sent unless the preset / CLI sets them."""
    clean_env.setenv("OPENAI_API_KEY", "k")
    o = registry.make_sampler(registry.resolve("openai/gpt-5.1-2025-11-13"))
    o.prepare()
    assert "temperature" not in o.request_body()
    assert o.config()["settings"]["temperature"] == {"value": None, "source": "provider default (not reported)"}
    clean_env.setenv("GEMINI_API_KEY", "k")
    g = registry.make_sampler(registry.resolve("gemini/gemini-3.1-pro-preview"))
    g._client = Recorder(None)
    g._client.model_info = NS(name="models/gemini-3.1-pro-preview", version="3.1", temperature=1.0, top_p=0.95, top_k=64,
                              output_token_limit=65536)
    g.prepare()
    body, st = g.request_body(), g.config()["settings"]
    assert (body["temperature"], body["top_p"], body["top_k"]) == (0.0, 0.95, 64)
    assert (st["temperature"]["source"], st["top_k"]["source"]) == ("preset:gemini-3", "gemini:models.get")
    assert st["max_output_tokens"]["source"] == "preset:gemini-3 output_limit"


def test_responses_sampling_echo(clean_env):
    """The Responses API echoes temperature / top_p: stored per step with the fields that differ from what was sent."""
    from openai.types.responses import Response as OAIResponse

    clean_env.setenv("OPENAI_API_KEY", "k")
    s = registry.make_sampler(registry.resolve("openai/gpt-5.1-2025-11-13", temperature=0.5))
    r = s._parse(OAIResponse.model_validate({**RESPONSES_REPLY, "temperature": 1.0, "top_p": 0.98}))
    assert r.sampling_echo == {"echo": {"temperature": 1.0, "top_p": 0.98}, "differs": ["temperature"]}
    assert s._parse(OAIResponse.model_validate(RESPONSES_REPLY)).sampling_echo is None


# ---------------------------------------------------------------- endpoint unavailable
def test_endpoint_unavailable_classification(clean_env, endpoints, monkeypatch, png):
    """404 / "No endpoints found" / an upstream model-not-found: endpoint_unavailable at once; a served engine that
    refuses connections after every retry: endpoint_unavailable; a timeout stays provider_failed."""
    import httpx
    import openai

    from simple_evals_mm.samplers import openai_chat

    monkeypatch.setattr(openai_chat.time, "sleep", lambda s: None)
    req = httpx.Request("POST", "http://x/v1/chat/completions")

    def raising(exc):
        rec = Recorder(None)
        rec.chat = NS(completions=NS(create=lambda **kw: (_ for _ in ()).throw(exc)))
        return rec

    clean_env.setenv("OPENROUTER_API_KEY", "k")
    s = registry.make_sampler(registry.resolve("openrouter/qwen/qwen3.8-27b"))
    s._client = raising(openai.NotFoundError("Error code: 404 - {'error': {'message': 'No endpoints found for qwen/qwen3.8-27b.'}}",
                                             response=httpx.Response(404, request=req), body=None))
    with pytest.raises(errors.SamplerError) as e:
        s([user("q")])
    assert e.value.cause == errors.ENDPOINT_UNAVAILABLE

    served = registry.make_sampler(registry.resolve("sglang/Qwen/Qwen3.5-9B"))
    served.spec.max_tries = 2
    served._client = raising(openai.APIConnectionError(request=req))
    with pytest.raises(errors.SamplerError) as e:
        served([user("q")])
    assert e.value.cause == errors.ENDPOINT_UNAVAILABLE
    served._client = raising(openai.APITimeoutError(request=req))
    with pytest.raises(errors.SamplerError) as e:
        served([user("q")])
    assert e.value.cause == errors.PROVIDER_FAILED
