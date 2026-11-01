"""Model slugs, presets and key / base-url resolution.

A model is named `PROVIDER/MODEL` (`openrouter/qwen/qwen3.8-27b`, `openai/gpt-5.1-2025-11-13`,
`sglang/Qwen/Qwen3.8-27B`). Resolution (docs/design/v0.5.md):

  API key   --api-key-name VAR  >  <PROVIDER>_API_KEY            (served engines: optional)
  base URL  --base-url URL      >  <PROVIDER>_BASE_URL  >  the provider's default

Presets (recommended sampling, provider pin, effort levels, token budgets) are data, not classes. CLI options
override them. Only variable NAMES are ever reported (dry run, job config); values stay in the environment.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

API_FORMATS = ("openai_chat_completions",)

PROVIDERS: dict[str, dict] = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_required": True, "max_tries": 60, "image_detail": "high"},
    "azure": {"base_url": None, "key_required": True, "max_tries": 60, "image_detail": "high"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_required": True, "max_tries": 60},
    "sglang": {"base_url": "http://localhost:30000/v1", "key_required": False, "max_tries": 120, "served": True},
    "vllm": {"base_url": "http://localhost:8000/v1", "key_required": False, "max_tries": 120, "served": True},
}

# ---------------------------------------------------------------- OpenRouter registry (moved from
# sampler/openrouter_sampler.py, which re-exports it): one pinned provider per model.
#   provider  - the endpoint we pin (`provider.order` + `allow_fallbacks: false`)
#   quant     - `provider.quantizations` filter for open-weight models without a first-party endpoint
#   sampling  - the model card's recommended decoding (open-weight models); None = provider default
#   efforts   - `reasoning.effort` values the endpoint accepts (None = unverified, () = no effort control)
QWEN38 = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
GEMMA4 = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
QWEN35 = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}  # Qwen3.5 card: thinking, general tasks
GLM53 = {"temperature": 1.0, "top_p": 0.95}
DSV41 = {"temperature": 1.0, "top_p": 0.95}

OPENROUTER_MODELS: dict[str, dict] = {
    # ---- API models: first-party endpoint, provider-default decoding -------------------------
    "anthropic/claude-fable-5": {"provider": ["anthropic"], "efforts": ("low", "medium", "high", "max"), "open_weight": False},
    "openai/gpt-6-astra": {"provider": ["openai"], "efforts": ("low", "medium", "high", "xhigh", "max"), "open_weight": False},  # OpenAI docs; reasoning mandatory, no none/minimal (2026-10-24)
    # README usage example (model and judge); OpenRouter supported_efforts, reasoning optional (2026-10-30)
    "openai/gpt-6.1-terra": {"provider": ["openai"], "efforts": ("none", "low", "medium", "high", "xhigh", "max"), "open_weight": False},
    "openai/gpt-6-luna": {"provider": ["openai"], "efforts": ("none", "low", "medium", "high", "xhigh", "max"), "open_weight": False},
    "google/gemini-3.8-flash": {"provider": ["google-ai-studio"], "efforts": ("low", "medium", "high"), "open_weight": False},  # Gemini docs: minimal unavailable on 3.8 (OpenRouter maps it to low)
    # muse-wonder: "none" rejected (reasoning mandatory); minimal/low/medium/high accepted (smoke 2026-10-21)
    "meta/muse-wonder": {"provider": ["meta"], "efforts": ("minimal", "low", "medium", "high", "xhigh", "max"), "open_weight": False},  # OpenRouter supported_efforts / Cursor docs (2026-10-24)
    # Alibaba's endpoint exposes no effort control for Qwen3.8 Flash (thinking on, default budget).
    "qwen/qwen3.8-flash": {"provider": ["alibaba"], "sampling": QWEN38, "efforts": (), "open_weight": False},
    # ---- open-weight models: first-party endpoint where it exists --------------------------
    "moonshotai/kimi-k3": {"provider": ["moonshotai"], "efforts": ("none", "low", "high", "max"), "open_weight": True},  # OpenRouter supported_efforts; others are mapped (2026-10-24)
    # Kimi K3.1 Pro: only Moonshot's endpoints (mxfp4 = the released weight format); no sampling params accepted (2026-10-24).
    "moonshotai/kimi-k3.1-pro": {"provider": ["moonshotai"], "efforts": ("none", "low", "high", "max"), "open_weight": True},  # OpenRouter page lists these 4; minimal/medium/xhigh are accepted but not distinct (probe 2026-10-24)
    # DeepSeek's own endpoint trains on prompts ("Paid model training violation" guardrail: OpenRouter's
    # account privacy setting blocks it) and benchmark questions must not leak into training data,
    # so DeepSeek-V4.1-Flash is pinned to Alibaba's non-training host (smoke 2026-10-21).
    "deepseek/deepseek-v4.1-flash-1017": {"provider": ["alibaba"], "sampling": DSV41, "efforts": ("low", "high", "max"), "open_weight": True},
    "z-ai/glm-5.3-flash": {"provider": ["z-ai"], "sampling": GLM53, "efforts": ("low", "high", "max"), "open_weight": True},
    "qwen/qwen3.8-27b": {"provider": ["alibaba"], "sampling": QWEN38, "efforts": ("low", "medium", "xhigh"), "open_weight": True},
    # ---- open-weight models WITHOUT a first-party endpoint: pinned bf16 host ---------------
    # gemma-4 has thinking on/off only: "none" -> reasoning.enabled=false, "high" -> enabled.
    "google/gemma-4-31b-it": {"provider": ["crusoe"], "quant": ["bf16"], "sampling": GEMMA4, "efforts": ("none", "high"), "toggle": True, "open_weight": True},
    "meta/muse-glimmer-30b": {"provider": ["together"], "efforts": ("low", "medium", "high", "xhigh"), "open_weight": True},  # model card
    # Qwen3.5-9B: no first-party endpoint; Parasail serves bf16 with a 235k-token limit (thinking on by default).
    "qwen/qwen3.5-9b": {"provider": ["parasail"], "quant": ["bf16"], "sampling": QWEN35, "efforts": (), "open_weight": True},
    # ---- reference models of the OmniDocBench leaderboard (parity check of the omnidocbench task, 2026-10-28) ----
    "google/gemini-3-flash-preview": {"provider": ["google-ai-studio"], "efforts": (), "open_weight": False},
    "qwen/qwen3-vl-235b-a22b-instruct": {"provider": ["deepinfra"], "efforts": (), "open_weight": True},  # fp8 hosts only
    # ---- judge candidates ----------------------------------------------------------------------
    "openai/gpt-5.1": {"provider": ["openai"], "efforts": ("none", "low", "medium", "high"), "open_weight": False},
    "openai/gpt-6-sol": {"provider": ["openai"], "efforts": None, "open_weight": False},
    "anthropic/claude-sonnet-5.5": {"provider": ["anthropic"], "efforts": None, "open_weight": False},
    "anthropic/claude-opus-5.5": {"provider": ["anthropic"], "efforts": None, "open_weight": False},
    "google/gemini-3.1-pro-preview": {"provider": ["google-ai-studio"], "efforts": None, "open_weight": False},
}

OPENAI_SYSTEM_MESSAGE = "You are a helpful assistant."  # v0.4 OpenAISampler / ResponsesSampler (role developer)

# The v0.4 judge (DEFAULT_GRADER_MODEL gpt-5.1-2025-11-13): ResponsesSampler with effort "none", max 8192 tokens,
# the developer system message; with an OpenRouter key it went to OpenRouter's openai/gpt-5.1 (the 2025-11-13
# snapshot; OpenRouter has no dated id).
_GPT51_JUDGE = {"effort": "none", "efforts": ("none", "low", "medium", "high"), "max_tokens": 8192,
                    "system_message": OPENAI_SYSTEM_MESSAGE, "system_role": "developer"}

# Explicit presets (`provider/model`); "*" suffix = prefix match on the model (served HF ids).
PRESETS: dict[str, dict] = {
    **{f"openrouter/{m}": {"provider_pin": list(s["provider"]), "quant": list(s["quant"]) if s.get("quant") else None,
                               "sampling": dict(s.get("sampling") or {}), "efforts": s.get("efforts"), "toggle": bool(s.get("toggle")),
                               "max_tokens": 131072}  # ceiling: clamped to the endpoint's max_completion_tokens
       for m, s in OPENROUTER_MODELS.items()},
    # v0.4 sent reasoning {"effort": "none"} (not {"enabled": false}, the OpenRouterSampler mapping of "none")
    "openrouter/openai/gpt-5.1": dict(provider_pin=["openai"], **{**_GPT51_JUDGE, "effort": None},
                                      model_args={"reasoning": {"effort": "none"}}),
    "openai/gpt-5.1-2025-11-13": dict(max_tokens_field="max_completion_tokens", effort_field="reasoning_effort", **_GPT51_JUDGE),
    # v0.4 OpenAISampler: temperature 0, 8192 tokens, developer system message
    "openai/gpt-4o-2024-11-20": {"sampling": {"temperature": 0.0}, "max_tokens": 8192, "system_message": OPENAI_SYSTEM_MESSAGE,
                                     "system_role": "developer", "efforts": ()},
    "openrouter/openai/gpt-4o-2024-11-20": {"provider_pin": ["openai"], "sampling": {"temperature": 0.0}, "max_tokens": 8192,
                                                "system_message": OPENAI_SYSTEM_MESSAGE, "system_role": "developer", "efforts": ()},
    # Served models (sglang / vllm): v0.4 sglang_sampler.py subclasses. Serve thinking models with --reasoning-parser.
    "*/Qwen/Qwen3.8*": {"sampling": {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 0.0, "top_k": 20, "min_p": 0.0, "repetition_penalty": 1.0},
                            "template_kwargs": {"enable_thinking": True, "preserve_thinking": False},
                            "efforts": ("low", "medium", "xhigh"), "max_tokens": 65536, "expects_reasoning": True},
    "*/Qwen/Qwen3.5*": {"sampling": {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 1.5, "top_k": 20, "min_p": 0.0, "repetition_penalty": 1.0},
                            "max_tokens": 16384, "expects_reasoning": True},
    "*/zai-org/GLM-5*": {"sampling": {"temperature": 1.0, "top_p": 0.95}, "template_kwargs": {"clear_thinking": True},
                             "efforts": ("low", "high", "max"), "max_tokens": 65536, "expects_reasoning": True},
    "*/deepseek-ai/DeepSeek-V4*": {"sampling": {"temperature": 1.0, "top_p": 0.95}, "template_kwargs": {"thinking": True},
                                       "efforts": ("low", "high", "max"), "effort_field": "reasoning_effort", "max_tokens": 65536,
                                       "expects_reasoning": True},
    "*/google/gemma-4*": {"sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 64}, "max_tokens": 8192},
}

DEFAULT_JUDGE = "openrouter/openai/gpt-5.1"

# Image policy for OpenRouter (v0.4 OpenRouterSampler): downscale only when the longest side exceeds 4096 px or the
# base64 payload 9 MB; shrink the whole request under 19 MB (Google AI Studio rejects bodies over 20 MB).
OPENROUTER_IMAGE_POLICY = {"max_image_px": 4096, "max_image_b64_mb": 9.0, "max_request_mb": 19.0}


@dataclass
class SamplerSpec:
    provider: str
    model: str  # the id the API expects (slug without the provider)
    api_format: str = "openai_chat_completions"
    sampling: dict = field(default_factory=dict)  # temperature / top_p / top_k / ... sent as given
    max_tokens: int = 8192
    max_tokens_field: str = "max_tokens"
    effort: str | None = None
    efforts: tuple | None = None  # accepted levels (None = unverified, () = none)
    effort_field: str = ""  # "reasoning" (OpenRouter object) / "reasoning_effort" (top level) / "chat_template_kwargs"
    toggle: bool = False  # effort is on/off only (reasoning.enabled)
    template_kwargs: dict = field(default_factory=dict)
    system_message: str | None = None
    system_role: str = "system"
    provider_pin: list[str] | None = None
    quant: list[str] | None = None
    model_args: dict = field(default_factory=dict)  # --model-arg k=v: extra request body fields
    expects_reasoning: bool = False
    image_detail: str | None = None
    max_image_px: int | None = None
    max_image_b64_mb: float | None = None
    max_request_mb: float | None = None
    max_tries: int = 60
    max_rate_limit_tries: int = 600
    api_version: str = "2023-05-15"  # azure
    # machine-local / secret resolution: names only (never values)
    base_url_source: str = "default"  # "--base-url" / "<PROVIDER>_BASE_URL" / "default"
    api_key_name: str | None = None
    api_key_required: bool = True

    _NOT_PUBLIC = ("base_url_source", "api_key_name", "api_key_required", "max_tries", "max_rate_limit_tries")

    def public(self) -> dict:
        """The frozen part (everything that changes outputs)."""
        return {k: v for k, v in asdict(self).items() if k not in self._NOT_PUBLIC}

    def base_url(self, cli_base_url: str | None = None) -> str | None:
        if self.base_url_source == "--base-url":
            return cli_base_url
        if self.base_url_source != "default":
            return os.environ.get(self.base_url_source)
        return PROVIDERS[self.provider]["base_url"]

    def api_key(self) -> str | None:
        v = os.environ.get(self.api_key_name or "")
        if not v and self.api_key_required:
            raise RuntimeError(f"{self.api_key_name} is not set (needed for provider {self.provider})")
        return v or "EMPTY"

    def resolution(self) -> dict:
        """Dry-run report: variable names and whether they are set; never the values."""
        return {
            "provider": self.provider, "model": self.model, "api_format": self.api_format,
            "api_key_var": self.api_key_name, "api_key_set": bool(os.environ.get(self.api_key_name or "")),
            "api_key_required": self.api_key_required,
            "base_url_from": self.base_url_source,
            "base_url_set": self.base_url_source in ("default", "--base-url") or bool(os.environ.get(self.base_url_source)),
        }


def parse_slug(slug: str) -> tuple[str, str]:
    provider, sep, model = slug.partition("/")
    if not sep or not model:
        raise ValueError(f"model must be PROVIDER/MODEL, got {slug!r} (providers: {', '.join(PROVIDERS)})")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} in {slug!r} (providers: {', '.join(PROVIDERS)})")
    return provider, model


def find_preset(provider: str, model: str) -> dict:
    exact = PRESETS.get(f"{provider}/{model}")
    if exact is not None:
        return dict(exact)
    if PROVIDERS[provider].get("served"):
        for key, p in PRESETS.items():
            if key.startswith("*/") and key.endswith("*") and model.startswith(key[2:-1]):
                return dict(p)
    return {}


def resolve(slug: str, *, effort: str | None = None, max_tokens: int | None = None, temperature: float | None = None,
            top_p: float | None = None, top_k: int | None = None, provider_pin: str | None = None,
            quantization: str | None = None, model_args: dict | None = None, base_url: str | None = None,
            api_key_name: str | None = None, api_format: str | None = None) -> SamplerSpec:
    """The full sampler spec of a slug: preset < CLI options; key / base-url variable names resolved."""
    provider, model = parse_slug(slug)
    if api_format and api_format not in API_FORMATS:
        raise ValueError(f"--api-format {api_format!r} is not supported yet (supported: {', '.join(API_FORMATS)})")
    pinfo = PROVIDERS[provider]
    preset = find_preset(provider, model)
    spec = SamplerSpec(provider=provider, model=model, max_tries=pinfo["max_tries"], image_detail=pinfo.get("image_detail"),
                       api_key_required=pinfo["key_required"])
    for k, v in preset.items():
        setattr(spec, k, v)
    if not spec.effort_field:
        spec.effort_field = {"openrouter": "reasoning", "openai": "reasoning_effort", "azure": "reasoning_effort"}.get(
            provider, "chat_template_kwargs")
    if provider == "openrouter":
        for k, v in OPENROUTER_IMAGE_POLICY.items():
            setattr(spec, k, v)
    if pinfo.get("served") and "temperature" not in spec.sampling:
        spec.sampling = {"temperature": 0.0, **spec.sampling}  # always explicit: the server default is 1.0
    for k, v in (("temperature", temperature), ("top_p", top_p), ("top_k", top_k)):
        if v is not None:
            spec.sampling = {**spec.sampling, k: v}
    if max_tokens:
        spec.max_tokens = int(max_tokens)
    if effort is not None:
        if spec.efforts == ():
            raise ValueError(f"{slug}: the model has no reasoning effort control")
        if spec.efforts is not None and effort not in spec.efforts:
            raise ValueError(f"{slug}: --effort must be one of {spec.efforts}, got {effort!r}")
        spec.effort = effort
    if provider_pin:
        if provider != "openrouter":
            raise ValueError("--provider pins an OpenRouter provider; it needs an openrouter/... model")
        spec.provider_pin = provider_pin.split(",")
    if quantization:
        spec.quant = quantization.split(",")
    if model_args:
        spec.model_args = dict(model_args)
    if api_format:
        spec.api_format = api_format
    spec.api_key_name = api_key_name or f"{provider.upper()}_API_KEY"
    env_url = f"{provider.upper()}_BASE_URL"
    if base_url:
        spec.base_url_source = "--base-url"
    elif os.environ.get(env_url):
        spec.base_url_source = env_url
    elif pinfo["base_url"] is None:
        raise ValueError(f"provider {provider!r} has no default base URL: pass --base-url or set {env_url}")
    else:
        spec.base_url_source = "default"
    return spec


def make_sampler(spec: SamplerSpec, base_url: str | None = None):
    from simple_evals_mm.samplers.openai_chat import OpenAIChatSampler

    if spec.api_format == "openai_chat_completions":
        return OpenAIChatSampler(spec, base_url=base_url)
    raise ValueError(f"api format {spec.api_format!r} is not implemented")
