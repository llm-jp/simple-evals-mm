"""v0.5 provider/model slugs, keys, base urls."""

import json

import pytest

from simple_evals_mm import errors
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
