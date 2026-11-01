"""Model slugs, presets and key / base-url resolution.

A model is named `PROVIDER/MODEL` (`openrouter/qwen/qwen3.8-27b`, `openai/gpt-5.1-2025-11-13`,
`sglang/Qwen/Qwen3.8-27B`). Resolution (docs/design/v0.5.md):

  API key   --api-key-name VAR  >  the preset's variable  >  the provider's (<PROVIDER>_API_KEY; Azure
            AZURE_OPENAI_KEY, the GPT-5 deployments AZURE_OPENAI_KEY_GPT5)   (served engines: optional)
  base URL  --base-url URL      >  the preset's / provider's variable (<PROVIDER>_BASE_URL; Azure AZURE_OPENAI_ENDPOINT
            / AZURE_OPENAI_ENDPOINT_GPT5) when set  >  the provider's default. For openrouter / openai / gemini a set
            <PROVIDER>_BASE_URL is refused unless --base-url is given (a stray variable must not redirect a public API);
            the URL's identity (scheme + host + path) is recorded in the sampler config
  format    --api-format F      >  the preset's  >  the provider's (openai / azure: openai_responses for gpt-5*, else
            openai_chat_completions; gemini / vertex: google_generate_content)

Presets (recommended sampling, provider pin, effort levels; samplers/presets.py) are data, not classes, keyed by
exact slugs: an unknown slug is refused unless `--preset FAMILY` or `--preset none` is given (recorded as `preset`). `--model-arg` keys are declared per preset / provider / format; CLI options override both. Only variable NAMES are ever reported (dry run, job config); values stay in the environment.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from simple_evals_mm.samplers.presets import (  # noqa: F401 - re-exported (v0.4 openrouter_sampler imports them here)
    DEFAULT_JUDGE,
    DSV41,
    FAMILIES,
    FORMAT_ARGS,
    GEMMA4,
    GLM53,
    MODEL_ARGS,
    OPENAI_SYSTEM_MESSAGE,
    OPENROUTER_IMAGE_POLICY,
    OPENROUTER_MODELS,
    PRESETS,
    PROVIDER_ARGS,
    QWEN35,
    QWEN38,
    SERVED_ENGINES,
    SERVED_MODELS,
    SPECIAL_SAMPLING,
)

CHAT, RESPONSES, GOOGLE = "openai_chat_completions", "openai_responses", "google_generate_content"
API_FORMATS = (CHAT, RESPONSES, GOOGLE)

# Per provider: default base URL (None = must be given), the key / base-url variable NAMES (default
# <PROVIDER>_API_KEY / <PROVIDER>_BASE_URL), retry budget, and the API formats it speaks (the first is the default;
# openai / azure use the Responses API for gpt-5*, as v0.4 did).
PROVIDERS: dict[str, dict] = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_required": True, "max_tries": 60, "image_detail": "high",
               "formats": (CHAT, RESPONSES)},
    "azure": {"base_url": None, "key_required": True, "max_tries": 60, "image_detail": "high", "formats": (CHAT, RESPONSES),
              "key_var": "AZURE_OPENAI_KEY", "base_url_var": "AZURE_OPENAI_ENDPOINT"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_required": True, "max_tries": 60, "formats": (CHAT,)},
    # Google AI Studio (GEMINI_API_KEY); the SDK's endpoint unless --base-url / GEMINI_BASE_URL
    "gemini": {"base_url": "https://generativelanguage.googleapis.com", "key_required": True, "max_tries": 60,
               "formats": (GOOGLE,)},
    # Vertex AI (v0.4 GeminiSampler without GEMINI_API_KEY): application default credentials, project / location
    # from GEMINI_PROJECT_ID / GEMINI_LOCATION (names recorded, values not)
    "vertex": {"base_url": "", "key_required": False, "max_tries": 60, "formats": (GOOGLE,),
               "key_var": "", "extra_vars": ("GEMINI_PROJECT_ID", "GEMINI_LOCATION")},
    "sglang": {"base_url": "http://localhost:30000/v1", "key_required": False, "max_tries": 120, "served": True, "formats": (CHAT,)},
    "vllm": {"base_url": "http://localhost:8000/v1", "key_required": False, "max_tries": 120, "served": True, "formats": (CHAT,)},
    # serving/hf_server.py: custom-architecture models sglang / vLLM cannot serve (LLM-jp-VL checkpoints, Sarashina)
    "hf": {"base_url": "http://localhost:30050/v1", "key_required": False, "max_tries": 120, "served": True, "formats": (CHAT,)},
}


# providers whose <PROVIDER>_BASE_URL variable is refused (only --base-url may redirect them)
PUBLIC_APIS = ("openrouter", "openai", "gemini")
# google-genai reads these from the environment and would switch the backend / endpoint behind our back
GOOGLE_ENV = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL")


def check_google_env() -> None:
    bad = [v for v in GOOGLE_ENV if os.environ.get(v) is not None]
    if bad:
        raise ValueError(f"{', '.join(bad)} set: google-genai would switch the backend / endpoint from the environment. "
                         f"Unset them (`unset {' '.join(bad)}`, and remove them from .env); choose the backend with the "
                         "slug (gemini/ or vertex/) and the endpoint with --base-url")


def default_api_format(provider: str, model: str) -> str:
    formats = PROVIDERS[provider]["formats"]
    if RESPONSES in formats and model.startswith("gpt-5"):
        return RESPONSES
    return formats[0]


@dataclass
class SamplerSpec:
    provider: str
    model: str  # the model id of the slug (slug without the provider)
    api_format: str = CHAT
    api_model: str | None = None  # id sent to the API when it differs (Azure deployment names)
    preset: str | None = None  # the exact preset key, the --preset family, or "none"
    family: str | None = None  # the family the preset was built from
    sampling: dict = field(default_factory=dict)  # temperature / top_p / top_k / ... sent as given
    sampling_source: dict = field(default_factory=dict)  # field -> "cli" / "preset:<name>"
    max_output_tokens: int | None = None  # requested generation length (reasoning included); None = the endpoint maximum
    output_limit: int | None = None  # documented output limit of an API model (preset data; OpenAI / Azure / Gemini)
    max_tokens_field: str = "max_tokens"  # the chat API field that carries max_output_tokens
    effort: str | None = None
    effort_source: str | None = None
    efforts: tuple | None = None  # accepted levels (None = unverified, () = none)
    effort_field: str = ""  # reasoning / reasoning_effort / template / template_toggle / responses / thinking_level
    effort_key: str = "reasoning_effort"  # chat_template_kwargs key of the template effort fields
    toggle: bool = False  # OpenRouter: effort is on/off only (reasoning.enabled)
    reasoning_none: str = "disable"  # OpenRouter "none": {"enabled": false} ("disable") or {"effort": "none"} ("effort")
    template_kwargs: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)  # fixed request body fields (preset, --model-arg extra_body)
    system_message: str | None = None
    system_role: str = "system"
    provider_pin: list[str] | None = None
    quant: list[str] | None = None
    model_args: dict = field(default_factory=dict)  # the --model-arg values as given (recorded)
    server_args: dict = field(default_factory=dict)  # --model-arg values applied by the server, not sent
    expects_reasoning: bool = False
    reasoning_parser: str | None = None  # the --reasoning-parser the server needs
    response_parser: str | None = None  # "harmony": split the channels of the raw text client-side
    reasoning_summary: str | None = None  # Responses API reasoning.summary
    include_thoughts: bool | None = None  # Gemini thinking_config.include_thoughts
    image_detail: str | None = None
    max_image_px: int | None = None
    max_image_b64_mb: float | None = None
    max_request_mb: float | None = None
    max_tries: int = 60
    max_rate_limit_tries: int = 600
    alias: bool = False  # undated / alias model id (presets._alias): lock.json sampler.model_identity "alias"
    seed: str = "on"  # "off": the per-trial seed is not sent (--model-arg seed=off)
    vertex_project: str | None = None  # Vertex AI project / location (--model-arg, else GEMINI_PROJECT_ID / _LOCATION)
    vertex_location: str | None = None
    api_version: str = "2023-05-15"  # azure
    # machine-local / secret resolution: names only (never values)
    base_url_source: str = "default"  # "--base-url" / "<PROVIDER>_BASE_URL" / "default"
    api_key_name: str | None = None
    api_key_required: bool = True
    extra_vars: tuple = ()  # further variables the provider reads (Vertex project / location)

    _NOT_PUBLIC = ("base_url_source", "api_key_name", "api_key_required", "max_tries", "max_rate_limit_tries", "extra_vars")

    def public(self) -> dict:
        """The frozen part (everything that changes outputs)."""
        return {k: v for k, v in asdict(self).items() if k not in self._NOT_PUBLIC}

    @property
    def request_model(self) -> str:
        return self.api_model or self.model

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
        out = {
            "provider": self.provider, "model": self.model, "api_format": self.api_format,
            "api_key_var": self.api_key_name, "api_key_set": bool(os.environ.get(self.api_key_name or "")),
            "api_key_required": self.api_key_required,
            "base_url_from": self.base_url_source,
            "base_url_set": self.base_url_source in ("default", "--base-url") or bool(os.environ.get(self.base_url_source)),
        }
        if self.extra_vars:
            out["vars_set"] = {v: bool(os.environ.get(v)) for v in self.extra_vars}
        return out


def parse_slug(slug: str) -> tuple[str, str]:
    provider, sep, model = slug.partition("/")
    if not sep or not model:
        raise ValueError(f"model must be PROVIDER/MODEL, got {slug!r} (providers: {', '.join(PROVIDERS)})")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} in {slug!r} (providers: {', '.join(PROVIDERS)})")
    return provider, model


def preset_label(spec: SamplerSpec) -> str:
    return spec.family or spec.preset or "none"


def find_preset(provider: str, model: str, preset: str | None = None) -> tuple[str | None, dict]:
    """(preset name, preset) of a slug. preset None: the EXACT key `provider/model` (no prefix or wildcard matching),
    else an error; "none": no model-specific settings; a FAMILIES name: that family (any model id or path)."""
    if preset == "none":
        return "none", {}
    if preset is not None:
        if preset not in FAMILIES:
            raise ValueError(f"unknown --preset {preset!r} (families: {', '.join(FAMILIES)}; or 'none')")
        return preset, {**FAMILIES[preset], "family": preset}
    key = f"{provider}/{model}"
    if key in PRESETS:
        return key, dict(PRESETS[key])
    raise ValueError(
        f"no preset for {key!r}: presets are exact slugs (`sem models list`). Add an entry to samplers/presets.py "
        f"(PRESETS, or SERVED_MODELS for a served HF id), apply a family with --preset FAMILY ({', '.join(FAMILIES)}), "
        "or run with --preset none (no model-specific settings: only the CLI options, max_output_tokens = the endpoint "
        "maximum)")


def allowed_model_args(spec: SamplerSpec, preset: dict) -> tuple[str, ...]:
    return tuple(dict.fromkeys(FORMAT_ARGS.get(spec.api_format, ()) + PROVIDER_ARGS.get(spec.provider, ())
                               + tuple(preset.get("args", ()))))


def _cast(name: str, decl, v):
    try:
        if decl.type is bool:
            out = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif decl.type is dict:
            if not isinstance(v, dict):
                raise TypeError("a JSON object")
            out = v
        else:
            out = decl.type(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"--model-arg {name}: expected {decl.type.__name__}, got {v!r} ({e})") from e
    if decl.choices and out not in decl.choices:
        raise ValueError(f"--model-arg {name} must be one of {decl.choices}, got {out!r}")
    return out


def apply_model_args(spec: SamplerSpec, preset: dict, model_args: dict) -> None:
    """--model-arg key=value: declared per preset / provider / API format (presets.MODEL_ARGS); unknown keys are an
    error. Special keys first (they replace the sampling), so explicit sampling keys win."""
    allowed = allowed_model_args(spec, preset)
    unknown = sorted(set(model_args) - set(allowed))
    if unknown:
        raise ValueError(f"{spec.provider}/{spec.model}: unknown --model-arg {', '.join(unknown)} "
                         f"(accepted: {', '.join(allowed) or 'none'})")
    vals = {k: _cast(k, MODEL_ARGS[k], v) for k, v in model_args.items()}
    for k, v in sorted(vals.items(), key=lambda kv: MODEL_ARGS[kv[0]].target != "special"):
        d = MODEL_ARGS[k]
        key = d.key or k
        if d.target == "special":
            if k == "sampling":
                if v == "default":
                    spec.sampling, spec.sampling_source = {}, {}
            else:
                spec.sampling = {**spec.sampling, **SPECIAL_SAMPLING[k][v]}
                spec.sampling_source = {**spec.sampling_source, **{f: "cli" for f in SPECIAL_SAMPLING[k][v]}}
        elif d.target == "sampling":
            spec.sampling = {**spec.sampling, key: v}
            spec.sampling_source = {**spec.sampling_source, key: "cli"}
        elif d.target == "template":
            spec.template_kwargs = {**spec.template_kwargs, key: "" if k == "date" and v.lower() == "none" else v}
        elif d.target == "body":
            spec.extra_body = {**spec.extra_body, **v}
        elif d.target == "server":
            spec.server_args = {**spec.server_args, key: v}
        else:
            setattr(spec, key, v)
    spec.model_args = dict(model_args)


def resolve(slug: str, *, preset: str | None = None, effort: str | None = None, max_output_tokens: int | None = None,
            temperature: float | None = None, top_p: float | None = None, top_k: int | None = None,
            provider_pin: str | None = None, quantization: str | None = None, model_args: dict | None = None,
            base_url: str | None = None, api_key_name: str | None = None, api_format: str | None = None) -> SamplerSpec:
    """The full sampler spec of a slug: preset < --model-arg < CLI options; key / base-url variable names resolved.
    Provider / model defaults (OpenRouter default_parameters, generation_config.json, Gemini models.get) and the
    endpoint maximum of max_output_tokens are resolved later, at Sampler.prepare()."""
    provider, model = parse_slug(slug)
    pinfo = PROVIDERS[provider]
    if provider in ("gemini", "vertex"):
        check_google_env()
    if api_format and api_format not in pinfo["formats"]:
        raise ValueError(f"--api-format {api_format!r} is not available for provider {provider} "
                         f"(supported: {', '.join(pinfo['formats'])}; all formats: {', '.join(API_FORMATS)})")
    preset_name, preset = find_preset(provider, model, preset)
    spec = SamplerSpec(provider=provider, model=model, preset=preset_name, max_tries=pinfo["max_tries"], image_detail=pinfo.get("image_detail"),
                       api_key_required=pinfo["key_required"], extra_vars=tuple(pinfo.get("extra_vars", ())))
    key_var = preset.pop("api_key_var", None)
    url_var = preset.pop("base_url_var", None)
    preset_args = preset.pop("args", ())
    for k, v in preset.items():
        setattr(spec, k, v)
    label = f"preset:{preset_label(spec)}"
    spec.sampling_source = {k: label for k in spec.sampling}
    if spec.effort is not None:
        spec.effort_source = label
    spec.api_format = api_format or preset.get("api_format") or default_api_format(provider, model)
    if not spec.effort_field:
        spec.effort_field = {RESPONSES: "responses", GOOGLE: "thinking_level"}.get(spec.api_format) or {
            "openrouter": "reasoning", "openai": "reasoning_effort", "azure": "reasoning_effort"}.get(provider, "template")
    if provider == "openrouter":
        for k, v in OPENROUTER_IMAGE_POLICY.items():
            setattr(spec, k, v)
    if model_args:
        apply_model_args(spec, {"args": preset_args}, model_args)
    for k, v in (("temperature", temperature), ("top_p", top_p), ("top_k", top_k)):
        if v is not None:
            spec.sampling = {**spec.sampling, k: v}
            spec.sampling_source = {**spec.sampling_source, k: "cli"}
    if provider == "sglang" and spec.sampling.get("top_k") == 0:
        spec.sampling = {**spec.sampling, "top_k": -1}  # SGLang: "top_k must be -1 (disable) or at least 1" (v0.4)
    if max_output_tokens:
        spec.max_output_tokens = int(max_output_tokens)
    if effort is not None:
        if spec.efforts == ():
            raise ValueError(f"{slug}: the model has no reasoning effort control")
        if spec.efforts is not None and effort not in spec.efforts:
            raise ValueError(f"{slug}: --effort must be one of {spec.efforts}, got {effort!r}")
        spec.effort = effort
        spec.effort_source = "cli"
    if provider_pin:
        if provider != "openrouter":
            raise ValueError("--provider pins an OpenRouter provider; it needs an openrouter/... model")
        spec.provider_pin = provider_pin.split(",")
    if quantization:
        spec.quant = quantization.split(",")
    default_key = pinfo.get("key_var", f"{provider.upper()}_API_KEY")
    spec.api_key_name = api_key_name or key_var or default_key or None
    env_url = url_var or pinfo.get("base_url_var") or f"{provider.upper()}_BASE_URL"
    if base_url:
        spec.base_url_source = "--base-url"
    elif os.environ.get(env_url) and provider in PUBLIC_APIS:
        raise ValueError(f"{env_url} is set: a {provider} base URL is taken from --base-url only (pass --base-url "
                         f"explicitly, or unset {env_url} and remove it from .env)")
    elif os.environ.get(env_url):
        spec.base_url_source = env_url
    elif pinfo["base_url"] is None:
        raise ValueError(f"provider {provider!r} has no default base URL: pass --base-url or set {env_url}")
    else:
        spec.base_url_source = "default"
    if provider == "vertex":  # explicit, recorded; never the SDK's ADC project / global location
        spec.vertex_project = spec.vertex_project or os.environ.get("GEMINI_PROJECT_ID") or None
        spec.vertex_location = spec.vertex_location or os.environ.get("GEMINI_LOCATION") or None
    return spec


def make_sampler(spec: SamplerSpec, base_url: str | None = None):
    """The sampler class of the resolved API format."""
    if spec.api_format == CHAT:
        from simple_evals_mm.samplers.openai_chat import OpenAIChatSampler

        return OpenAIChatSampler(spec, base_url=base_url)
    if spec.api_format == RESPONSES:
        from simple_evals_mm.samplers.openai_responses import OpenAIResponsesSampler

        return OpenAIResponsesSampler(spec, base_url=base_url)
    if spec.api_format == GOOGLE:
        from simple_evals_mm.samplers.gemini import GeminiSampler

        return GeminiSampler(spec, base_url=base_url)
    raise ValueError(f"api format {spec.api_format!r} is not implemented")


def preset_rows() -> list[dict]:
    """`sem models list`: one row per exact slug (a served HF id once, for every engine), then one per family
    (`--preset NAME`)."""
    rows, seen = [], set()
    served = "{" + ",".join(SERVED_ENGINES) + "}/"
    for key, p in PRESETS.items():
        prov, _, model = key.partition("/")
        if prov in SERVED_ENGINES:
            if model in seen:
                continue
            seen.add(model)
        rows.append(_row(served + model if prov in SERVED_ENGINES else key, prov, model, p))
    for name, p in FAMILIES.items():
        rows.append(_row(f"--preset {name}", "sglang", "", p) | {"family": name})
    return rows


def _row(slug: str, prov: str, model: str, p: dict) -> dict:
    fmt = p.get("api_format") or default_api_format(prov, model)
    return {"slug": slug, "family": p.get("family"), "api_format": fmt, "efforts": p.get("efforts"),
            "effort": p.get("effort"), "output_limit": p.get("output_limit"), "sampling": dict(p.get("sampling") or {}),
            "pin": p.get("provider_pin"), "quant": p.get("quant"), "alias": bool(p.get("alias")),
            "args": allowed_model_args(SamplerSpec(provider=prov, model=model, api_format=fmt), p)}
