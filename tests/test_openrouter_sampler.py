"""Offline tests for the OpenRouter sampler: routing, request composition, effort validation, image policy."""
import base64
from io import BytesIO

import pytest
from PIL import Image

from simple_evals_mm.sampler.sampler import get_sampler


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_OFFLINE", "1")  # no endpoint-catalog request
    for k in ("OPENROUTER_PROVIDER", "OPENROUTER_QUANT", "OPENROUTER_SAMPLING", "OPENROUTER_TEMP", "OPENROUTER_TOP_P", "OPENROUTER_TOP_K"):
        monkeypatch.delenv(k, raising=False)
    from simple_evals_mm.sampler.openrouter_sampler import OpenRouterSampler
    return lambda mid: OpenRouterSampler(mid)


def test_routing():
    from simple_evals_mm.sampler.openrouter_sampler import OpenRouterSampler
    assert get_sampler("openrouter/qwen/qwen3.8-27b") is OpenRouterSampler


def test_open_weight_request_params(make):
    s = make("openrouter/qwen/qwen3.8-27b")
    s.set_reasoning_effort("low")
    p = s.request_params()
    assert p["temperature"] == 1.0 and p["top_p"] == 0.95
    assert p["extra_body"]["top_k"] == 20  # non-OpenAI params go through extra_body
    assert p["extra_body"]["provider"] == {"order": ["alibaba"], "allow_fallbacks": False}
    assert p["extra_body"]["reasoning"] == {"effort": "low"}
    assert p["extra_body"]["usage"] == {"include": True}
    assert p["max_tokens"] == 131072


def test_bf16_host_and_thinking_toggle(make):
    s = make("openrouter/google/gemma-4-31b-it")
    assert s.request_params()["extra_body"]["provider"]["quantizations"] == ["bf16"]
    s.set_reasoning_effort("none")
    assert s.request_params()["extra_body"]["reasoning"] == {"enabled": False}
    s.set_reasoning_effort("high")
    assert s.request_params()["extra_body"]["reasoning"] == {"enabled": True}


def test_effort_validation(make):
    with pytest.raises(ValueError, match="must be one of"):
        make("openrouter/z-ai/glm-5.3-flash").set_reasoning_effort("medium")
    with pytest.raises(ValueError, match="no reasoning effort control"):
        make("openrouter/qwen/qwen3.8-flash").set_reasoning_effort("low")


def test_api_model_uses_provider_default_sampling(make):
    p = make("openrouter/anthropic/claude-fable-5").request_params()
    assert "temperature" not in p and "top_p" not in p


def test_env_overrides_and_endpoint_cap(make, monkeypatch):
    monkeypatch.setenv("OPENROUTER_SAMPLING", "default")
    monkeypatch.setenv("OPENROUTER_PROVIDER", "together")
    s = make("openrouter/qwen/qwen3.8-27b")
    s.max_completion_cap = 32768
    p = s.request_params()
    assert "temperature" not in p and p["extra_body"]["provider"]["order"] == ["together"]
    assert p["max_tokens"] == 32768


def test_large_images_are_downscaled_lossless(make):
    s = make("openrouter/qwen/qwen3.8-27b")
    part = s._handle_image(Image.new("RGB", (8000, 2000), "white"))
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    img = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert img.size == (4096, 1024)
