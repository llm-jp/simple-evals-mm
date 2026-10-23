"""Every model in KNOWN_MODELS routes to a sampler (offline), and answers text-only, one-image and two-image
requests (live). Generated from KNOWN_MODELS, so a new entry is covered automatically.

  -m api          API models (OpenAI, Gemini, OpenRouter): need keys and network
  -m local_model  in-process HF models (need a GPU), and sglang-served models (need SGLANG_BASE_URL)
"""
import os

import pytest
from PIL import Image

from simple_evals_mm.sampler.sampler import get_sampler
from simple_evals_mm.simple_evals import KNOWN_MODELS

MODELS = [m for m, _ in KNOWN_MODELS if m != "dummy"]
API_PREFIXES = ("gpt-", "gemini-", "openrouter/")
SERVED_ONLY = ("Qwen/Qwen3.5", "Qwen/Qwen3.8", "google/gemma-4", "zai-org/GLM-5", "deepseek-ai/DeepSeek-V4")


@pytest.mark.parametrize("model", MODELS)
def test_known_model_routes_to_a_sampler(model):
    cls = get_sampler(model)
    assert callable(getattr(cls, "pack_message", None)) and callable(getattr(cls, "__call__", None))


def _marks(model):
    if model.startswith(API_PREFIXES):
        return [pytest.mark.api]
    marks = [pytest.mark.local_model]
    if model.startswith(SERVED_ONLY):
        marks.append(pytest.mark.skipif(not os.environ.get("SGLANG_BASE_URL"), reason="needs an sglang server (SGLANG_BASE_URL)"))
    return marks


_cache: dict[str, object] = {}


def _sampler(model):
    if model not in _cache:
        s = get_sampler(model)(model_id=model)
        s.max_new_tokens = 256  # keep smoke requests short (thinking models may still use part of it)
        _cache[model] = s
    return _cache[model]


@pytest.mark.parametrize("model,n_images", [pytest.param(m, n, marks=_marks(m), id=f"{m}-{n}img") for m in MODELS for n in (0, 1, 2)])
def test_sampler_answers(model, n_images):
    s = _sampler(model)
    images = [Image.new("RGB", (64, 64), c) for c in ("red", "blue")[:n_images]]
    r = s([s.pack_message(images=images or None, instruction="Name the colours you see, or say hello.")])
    assert isinstance(r.response_text, str) and (r.response_text or r.finish_reason == "length")
