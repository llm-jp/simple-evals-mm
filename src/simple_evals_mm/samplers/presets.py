"""Model presets: DATA, not classes (docs/design/v0.5.md). One entry per model (exact slug), built from a named family:

  max_output_tokens   NOT a preset field. It is the maximum number of generated tokens INCLUDING the reasoning /
                  thinking tokens, for every backend; the request carries it in the API's own field: chat `max_tokens` /
                  `max_completion_tokens`, Responses / Gemini `max_output_tokens`, sglang / vLLM / hf `max_tokens`. Every
                  request sends it explicitly (never the server's or API's own default). Without --max-output-tokens it
                  is the largest generation the endpoint allows, resolved once at prepare() and recorded in the sampler
                  config (settings.max_output_tokens: value, requested, endpoint_max, source):
                    OpenRouter     the pinned endpoint's max_completion_tokens (context_length when that is null)
                    sglang / vllm / hf   the server's context length (/v1/models max_model_len); a request whose input
                                   leaves less room is re-sent once with context_length - input_tokens - 16 (the
                                   server's overflow error names the input length)
                    OpenAI / Azure / Gemini   `output_limit`, the model's documented output limit (preset data; Gemini
                                   without one: models.get output_token_limit)
                  A larger --max-output-tokens is clamped to the endpoint maximum; requested and effective are recorded.
  sampling        the recommended decoding, sent as given (temperature / top_p / top_k / min_p / penalties)
  efforts         the effort levels the model accepts (None = unverified, () = no effort control) and the default
                  `effort`; `effort_field` says where the level goes:
                    reasoning         OpenRouter's `reasoning` object ({"effort": l}; "none" -> {"enabled": false}
                                      unless reasoning_none = "effort"; toggle models -> {"enabled": true})
                    reasoning_effort  the top-level request field (OpenAI chat, DeepSeek-V4.1 on sglang)
                    template          chat_template_kwargs[effort_key] (Qwen3.8, GLM-5.3, Harmony, Muse Glimmer)
                    template_toggle   chat_template_kwargs[effort_key] = (level != "none") (on / off models)
                    responses / thinking_level   the Responses API `reasoning.effort` / Gemini `thinking_level`
  provider_pin / quant   OpenRouter endpoint pin (provider.order, no fallbacks) and quantization filter
  template_kwargs / extra_body   fixed chat_template_kwargs / request body fields
  reasoning_parser       the server's --reasoning-parser the model needs (expects_reasoning: the reply must carry a
                         separated trace); response_parser "harmony": channels split client-side
  args            the model-specific `--model-arg` keys the preset accepts (MODEL_ARGS); provider- and
                  format-level keys are added by the registry. Unknown keys are an error.
  api_model / api_key_var / base_url_var / api_version   request id and variable NAMES that differ from the
                  provider default (Azure deployments)

Keys are EXACT slugs (`provider/model`); there is no prefix or wildcard matching, so a model released later never
inherits settings silently. A served HF id listed in SERVED_MODELS gets its family under every engine prefix (sglang /
vllm / hf). Any other slug is refused unless `--preset FAMILY` (a FAMILIES name, e.g. internal checkpoints) or
`--preset none` (no model-specific settings) is given; the choice is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

EVAL_FIXED_DATE = "2026-10-01"  # Harmony conversation_start_date of the llm-jp-4 text models

# ---------------------------------------------------------------- OpenRouter registry: one pinned provider per model.
#   provider  - the endpoint we pin (`provider.order` + `allow_fallbacks: false`)
#   quant     - `provider.quantizations` filter for open-weight models without a first-party endpoint
#   sampling  - the model card's recommended decoding (open-weight models); None = provider default
#   efforts   - `reasoning.effort` values the endpoint accepts (None = unverified, () = no effort control)
#   effort    - the default level, sent explicitly (source "preset:<slug>") so a job never depends on the provider's
#               default: the model's catalog `reasoning.default_effort` (GET /api/v1/models, 2026-11-03; gemma-4:
#               default_enabled false -> "none"). Required for every model with effort levels (tests/test_samplers.py).
#               OpenRouter maps the level to the provider's own parameter (budget tokens, thinking level, ...): the
#               sampler config records that next to the effort (settings.effort.note).
QWEN38 = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
GEMMA4 = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
QWEN35 = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}  # Qwen3.5 card: thinking, general tasks
GLM53 = {"temperature": 1.0, "top_p": 0.95}
DSV41 = {"temperature": 1.0, "top_p": 0.95}

OPENROUTER_MODELS: dict[str, dict] = {
    # ---- API models: first-party endpoint, provider-default decoding -------------------------
    "anthropic/claude-fable-5": {"provider": ["anthropic"], "efforts": ("low", "medium", "high", "max"), "effort": "high", "open_weight": False},
    "openai/gpt-6-astra": {"provider": ["openai"], "efforts": ("low", "medium", "high", "xhigh", "max"), "effort": "medium", "open_weight": False},  # OpenAI docs; reasoning mandatory, no none/minimal (2026-10-24)
    # README usage example (model and judge); OpenRouter supported_efforts, reasoning optional (2026-10-30)
    "openai/gpt-6.1-terra": {"provider": ["openai"], "efforts": ("none", "low", "medium", "high", "xhigh", "max"), "effort": "medium", "open_weight": False},
    "openai/gpt-6-luna": {"provider": ["openai"], "efforts": ("none", "low", "medium", "high", "xhigh", "max"), "effort": "medium", "open_weight": False},
    "google/gemini-3.8-flash": {"provider": ["google-ai-studio"], "efforts": ("low", "medium", "high"), "effort": "medium", "open_weight": False},  # Gemini docs: minimal unavailable on 3.8 (OpenRouter maps it to low)
    # muse-wonder: "none" rejected (reasoning mandatory); minimal/low/medium/high accepted (smoke 2026-10-21)
    "meta/muse-wonder": {"provider": ["meta"], "efforts": ("minimal", "low", "medium", "high", "xhigh", "max"), "effort": "medium", "open_weight": False},  # OpenRouter supported_efforts / Cursor docs (2026-10-24)
    # Alibaba's endpoint exposes no effort control for Qwen3.8 Flash (thinking on, default budget).
    # Qwen3.8 Flash: thinking on / off only (OpenRouter reasoning optional, default on; no effort levels): sent
    # explicitly as reasoning.enabled so a changed provider default cannot switch it (2026-11-09)
    "qwen/qwen3.8-flash": {"provider": ["alibaba"], "sampling": QWEN38, "efforts": ("none", "high"), "effort": "high",
                           "toggle": True, "open_weight": False},
    # ---- open-weight models: first-party endpoint where it exists --------------------------
    "moonshotai/kimi-k3": {"provider": ["moonshotai"], "efforts": ("none", "low", "high", "max"), "effort": "max", "open_weight": True},  # OpenRouter supported_efforts; others are mapped (2026-10-24)
    # Kimi K3.1 Pro: only Moonshot's endpoints (mxfp4 = the released weight format); no sampling params accepted (2026-10-24).
    "moonshotai/kimi-k3.1-pro": {"provider": ["moonshotai"], "efforts": ("none", "low", "high", "max"), "effort": "max", "open_weight": True},  # OpenRouter page lists these 4; minimal/medium/xhigh are accepted but not distinct (probe 2026-10-24)
    # DeepSeek's own endpoint trains on prompts ("Paid model training violation" guardrail: OpenRouter's
    # account privacy setting blocks it) and benchmark questions must not leak into training data,
    # so DeepSeek-V4.1-Flash is pinned to Alibaba's non-training host (smoke 2026-10-21).
    "deepseek/deepseek-v4.1-flash-1017": {"provider": ["alibaba"], "sampling": DSV41, "efforts": ("low", "high", "max"), "effort": "high", "open_weight": True},
    "z-ai/glm-5.3-flash": {"provider": ["z-ai"], "sampling": GLM53, "efforts": ("low", "high", "max"), "effort": "max", "open_weight": True},
    "qwen/qwen3.8-27b": {"provider": ["alibaba"], "sampling": QWEN38, "efforts": ("low", "medium", "xhigh"), "effort": "xhigh", "open_weight": True},
    # ---- open-weight models WITHOUT a first-party endpoint: pinned bf16 host ---------------
    # gemma-4 has thinking on/off only: "none" -> reasoning.enabled=false, "high" -> enabled.
    "google/gemma-4-31b-it": {"provider": ["crusoe"], "quant": ["bf16"], "sampling": GEMMA4, "efforts": ("none", "high"), "effort": "none", "toggle": True, "open_weight": True},
    "meta/muse-glimmer-30b": {"provider": ["together"], "efforts": ("low", "medium", "high", "xhigh"), "effort": "medium", "open_weight": True},  # model card
    # Qwen3.5-9B: no first-party endpoint; Parasail serves bf16 with a 235k-token limit (thinking on by default).
    "qwen/qwen3.5-9b": {"provider": ["parasail"], "quant": ["bf16"], "sampling": QWEN35, "efforts": (), "open_weight": True},
    # ---- reference models of the OmniDocBench leaderboard (parity check of the omnidocbench task, 2026-10-28) ----
    "google/gemini-3-flash-preview": {"provider": ["google-ai-studio"], "efforts": (), "open_weight": False},
    "qwen/qwen3-vl-235b-a22b-instruct": {"provider": ["deepinfra"], "efforts": (), "open_weight": True},  # fp8 hosts only
    # ---- judge candidates ----------------------------------------------------------------------
    "openai/gpt-5.1": {"provider": ["openai"], "efforts": ("none", "low", "medium", "high"), "effort": "none", "open_weight": False},
    "openai/gpt-6-sol": {"provider": ["openai"], "efforts": None, "open_weight": False},
    "anthropic/claude-sonnet-5.5": {"provider": ["anthropic"], "efforts": None, "open_weight": False},
    "anthropic/claude-opus-5.5": {"provider": ["anthropic"], "efforts": None, "open_weight": False},
    "google/gemini-3.1-pro-preview": {"provider": ["google-ai-studio"], "efforts": None, "open_weight": False},
}

# Image policy for OpenRouter: downscale only when the longest side exceeds 4096 px or the
# base64 payload 9 MB; shrink the whole request under 19 MB (Google AI Studio rejects bodies over 20 MB).
OPENROUTER_IMAGE_POLICY = {"max_image_px": 4096, "max_image_b64_mb": 9.0, "max_request_mb": 19.0}


# ---------------------------------------------------------------- --model-arg declarations
@dataclass(frozen=True)
class ModelArg:
    """One `--model-arg key=value` option. target: where the value goes
      sampling  request sampling field (spec.sampling[key])
      template  chat_template_kwargs[key]
      body      merged into the request body (extra_body: a JSON object)
      spec      a SamplerSpec field (image limits, reasoning summary, ...)
      server    not sent: a server-side setting the job records (lock.json); `sem serve` applies it and
                checks it against the server manifest
      special   handled by the registry (sampling=default, decode=qwen)"""

    target: str
    type: type
    help: str
    key: str | None = None
    choices: tuple | None = None


MODEL_ARGS: dict[str, ModelArg] = {
    # served engines (sglang / vllm / hf): sampling fields next to --temperature / --top-p / --top-k
    "presence_penalty": ModelArg("sampling", float, "presence penalty"),
    "repetition_penalty": ModelArg("sampling", float, "repetition penalty"),
    "min_p": ModelArg("sampling", float, "min-p sampling"),
    # OpenRouter
    "sampling": ModelArg("special", str, "'default' drops the preset's recommended sampling (provider default decoding)", choices=("recommended", "default")),
    "max_image_px": ModelArg("spec", int, "downscale images whose longest side exceeds this"),
    "max_image_b64_mb": ModelArg("spec", float, "downscale images whose base64 PNG exceeds this many MB"),
    "max_request_mb": ModelArg("spec", float, "shrink all images until the request is under this many MB"),
    # every chat-completions request
    "extra_body": ModelArg("body", dict, "JSON object merged into the request body (recorded)"),
    # the per-trial seed (sha256 of "<task_id>__a<k>"; chat `seed`, Gemini GenerateContentConfig.seed)
    "seed": ModelArg("spec", str, "'off': do not send the per-trial seed (recorded)", choices=("on", "off")),
    # Vertex AI project / location (else GEMINI_PROJECT_ID / GEMINI_LOCATION; recorded)
    "project": ModelArg("spec", str, "Vertex AI project", key="vertex_project"),
    "location": ModelArg("spec", str, "Vertex AI location", key="vertex_location"),
    # Responses API / Gemini
    "reasoning_summary": ModelArg("spec", str, "Responses API reasoning.summary (the summary becomes the reasoning)",
                                  choices=("auto", "concise", "detailed")),
    "include_thoughts": ModelArg("spec", bool, "Gemini thinking_config.include_thoughts (thought summaries become the reasoning)"),
    # Harmony (llm-jp-4 / LLM-jp-VL)
    "date": ModelArg("template", str, "Harmony conversation_start_date ('' or 'none' = no date line)",
                     key="conversation_start_date"),
    "identity": ModelArg("template", str, "Harmony model_identity", key="model_identity"),
    "chat_template": ModelArg("server", str, "chat template file the server renders with"),
    "decode": ModelArg("special", str, "'qwen': Qwen3-VL decoding (temperature 1.0, top_p 1.0, top_k 40, repetition "
                                       "penalty 1.3 unless repetition_penalty is given)", choices=("qwen",)),
    # server-side image resolution
    "max_soft_tokens": ModelArg("server", int, "Gemma 4 image soft tokens of the patched server (default 280; 1120 = max)"),
}
SERVED_ARGS = ("presence_penalty", "repetition_penalty", "min_p")
# per api format / provider (added to every preset of that format / provider)
FORMAT_ARGS = {"openai_chat_completions": ("extra_body", "seed"), "openai_responses": ("reasoning_summary",),
               "google_generate_content": ("include_thoughts", "seed")}
PROVIDER_ARGS = {"openrouter": ("sampling", "max_image_px", "max_image_b64_mb", "max_request_mb"), "vertex": ("project", "location"),
                 "sglang": SERVED_ARGS, "vllm": SERVED_ARGS, "hf": SERVED_ARGS}
SPECIAL_SAMPLING = {"decode": {"qwen": {"temperature": 1.0, "top_p": 1.0, "top_k": 40, "repetition_penalty": 1.3}}}


# ---------------------------------------------------------------- presets
# Families: the shared settings of one model family, referenced by name from the exact entries below and applied to
# any other model id with `--preset NAME` (internal checkpoints: `-m sglang/models/LLM-jp-VL-... --preset llm-jp-vl`).
# No family carries max_output_tokens: the default is the endpoint maximum (module docstring). `output_limit` is the documented
# output limit of an API model (OpenAI / Azure / Gemini have no endpoint to ask), not a tuning choice.
# effort default medium: Harmony's default reasoning level (the system message says "Reasoning: medium" unless set)
_HARMONY_VL = {"sampling": {"temperature": 0.0, "skip_special_tokens": False}, "efforts": ("low", "medium", "high"), "effort": "medium",
               "template_kwargs": {"conversation_start_date": ""}, "response_parser": "harmony",
               "args": ("date", "identity", "chat_template", "decode")}
FAMILIES: dict[str, dict] = {
    # The v0.4 judge (the default judge is now gpt-6-luna, effort medium: DEFAULT_JUDGE); still available with
    # --judge-model openrouter/openai/gpt-5.1. Effort "none": the setting of the judge-agreement check (2026-10-21);
    # no system message (the request is the grading template alone).
    # output_limit 128000: OpenAI model page; OpenRouter lists 128000 for the OpenAI and Azure endpoints (2026-11-01)
    "gpt-5.1": {"effort": "none", "efforts": ("none", "low", "medium", "high"), "output_limit": 128000,
                "max_tokens_field": "max_completion_tokens"},
    # output_limit 16384: OpenAI model page; OpenRouter's OpenAI endpoint lists 16384 (2026-11-01). No system message
    # (upstream simple-evals sent "You are a helpful assistant."): every model gets the same prompt.
    "gpt-4o": {"sampling": {"temperature": 0.0}, "output_limit": 16384, "efforts": ()},
    # temperature 0, thinking_level "low" (the API floor of the thinking-only gemini-3 models). output_limit 65536: Gemini model page; OpenRouter lists 65536 for AI Studio and Vertex (2026-11-01)
    "gemini-3": {"sampling": {"temperature": 0.0}, "effort": "low", "efforts": ("low", "medium", "high"), "output_limit": 65536},
    # ---- served models (sglang / vllm / hf). Serve thinking models with the reasoning parser named here.
    "qwen3.8": {"sampling": {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 0.0, "top_k": 20, "min_p": 0.0, "repetition_penalty": 1.0},
                "template_kwargs": {"enable_thinking": True, "preserve_thinking": False},
                # effort default xhigh: Qwen3.8-27B's default (OpenRouter catalog default_effort, 2026-11-03)
                "efforts": ("low", "medium", "xhigh"), "effort": "xhigh", "expects_reasoning": True, "reasoning_parser": "qwen3"},
    # Qwen3.5: thinking on / off through the template's enable_thinking (the template's default is on); sent explicitly
    "qwen3.5": {"sampling": {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 1.5, "top_k": 20, "min_p": 0.0, "repetition_penalty": 1.0},
                "efforts": ("none", "high"), "effort": "high", "effort_field": "template_toggle", "effort_key": "enable_thinking",
                "expects_reasoning": True, "reasoning_parser": "qwen3"},
    "qwen3-vl": {"sampling": {"temperature": 0.0}, "efforts": ()},  # greedy
    "glm-5": {"sampling": {"temperature": 1.0, "top_p": 0.95}, "template_kwargs": {"clear_thinking": True},
              "efforts": ("low", "high", "max"), "effort": "max",  # GLM-5.3's default (catalog default_effort)
              "expects_reasoning": True, "reasoning_parser": "glm45"},
    "deepseek-v4": {"sampling": {"temperature": 1.0, "top_p": 0.95}, "template_kwargs": {"thinking": True},
                    "efforts": ("low", "high", "max"), "effort": "high",  # DeepSeek-V4.1's default (catalog default_effort)
                    "effort_field": "reasoning_effort", "expects_reasoning": True,
                    "reasoning_parser": "auto"},
    # gemma-4: thinking on/off through the chat template; default "none" (thinking off, the model card's default;
    # OpenRouter catalog default_enabled false), sent explicitly as enable_thinking=false
    "gemma-4": {"sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
                "efforts": ("none", "high"), "effort": "none", "effort_field": "template_toggle", "effort_key": "enable_thinking",
                "reasoning_parser": "gemma4", "args": ("max_soft_tokens",)},
    # Muse Glimmer: strength through the template's reasoning_strength
    "muse-glimmer": {"sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
                     "efforts": ("low", "medium", "high", "xhigh"), "effort": "medium",  # catalog default_effort
                     "effort_key": "reasoning_strength",
                     "expects_reasoning": True, "reasoning_parser": "muse_glimmer"},
    "internvl3": {"sampling": {"temperature": 0.0}, "efforts": ()},
    "sarashina": {"sampling": {"temperature": 0.0}, "efforts": ()},
    # Harmony: channels split client-side (skip_special_tokens false), explicit date line
    "llm-jp-vl": _HARMONY_VL,
    "llm-jp-4-text": dict(_HARMONY_VL, template_kwargs={"conversation_start_date": EVAL_FIXED_DATE}),
}

# Served models by their exact Hugging Face id: every served engine prefix (sglang / vllm / hf) of one of these ids gets the family. A model not listed here
# needs an entry, or `--preset FAMILY`, or `--preset none`.
SERVED_ENGINES = ("sglang", "vllm", "hf")
SERVED_MODELS: dict[str, str] = {
    "Qwen/Qwen3.8-27B": "qwen3.8", "Qwen/Qwen3.8-Flash-Next": "qwen3.8", "Qwen/Qwen3.8-Flash-Next-FP8": "qwen3.8",
    "Qwen/Qwen3.5-4B": "qwen3.5", "Qwen/Qwen3.5-9B": "qwen3.5", "Qwen/Qwen3.5-397B-A17B": "qwen3.5",
    "Qwen/Qwen3-VL-2B-Instruct": "qwen3-vl", "Qwen/Qwen3-VL-4B-Instruct": "qwen3-vl", "Qwen/Qwen3-VL-8B-Instruct": "qwen3-vl",
    "Qwen/Qwen3-VL-30B-A3B-Instruct": "qwen3-vl", "Qwen/Qwen3-VL-235B-A22B-Instruct": "qwen3-vl",
    "zai-org/GLM-5.3-Flash": "glm-5",
    "deepseek-ai/DeepSeek-V4.1-Flash": "deepseek-v4",
    "google/gemma-4-E4B-it": "gemma-4", "google/gemma-4-12B-it": "gemma-4",
    # 31B: Gemma4ForConditionalGeneration with a vision tower (12B is encoder-free); sglang 0.5.23 needs --patch
    # sglang_pr47809 (image-token mask) and --patch sglang_pr43753_full (vision pooler), --model-arg max_soft_tokens=1120
    "google/gemma-4-31B-it": "gemma-4",
    "meta-models/Muse-Glimmer-30B": "muse-glimmer",
    "OpenGVLab/InternVL3_5-1B": "internvl3", "OpenGVLab/InternVL3_5-2B": "internvl3", "OpenGVLab/InternVL3_5-4B": "internvl3",
    "OpenGVLab/InternVL3_5-8B": "internvl3",
    "sbintuitions/sarashina2.2-vision-3b": "sarashina",
    "llm-jp/llm-jp-4-vl-9b": "llm-jp-vl", "llm-jp/llm-jp-4-vl-9b-beta": "llm-jp-vl",
    "llm-jp/llm-jp-4-8b-thinking": "llm-jp-4-text", "llm-jp/llm-jp-4.1-8b-thinking": "llm-jp-4-text",
}

# Recommended sampling per served model, not per family: sizes of one family can differ (Qwen3.5-397B thinks at
# temperature 0.6 / presence_penalty 0, the 4B / 9B at 1.0 / 1.5). Each entry: (sampling, source). The source is the
# model card's line (revision-independent wording) or the snapshot's generation_config.json. `sem models check` compares
# every entry with the generation_config.json of the local Hugging Face cache. Families whose sampling is greedy by
# design (qwen3-vl, internvl3, sarashina, llm-jp: fixed for comparability) have no entry.
_QWEN35_THINK_GENERAL = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5,
                         "repetition_penalty": 1.0}
_QWEN38_THINK = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
                 "repetition_penalty": 1.0}
_GEMMA4 = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
SERVED_SAMPLING: dict[str, tuple[dict, str]] = {
    "Qwen/Qwen3.5-4B": (_QWEN35_THINK_GENERAL, "card: Thinking mode for general tasks"),
    "Qwen/Qwen3.5-9B": (_QWEN35_THINK_GENERAL, "card: Thinking mode for general tasks (no generation_config.json)"),
    "Qwen/Qwen3.5-397B-A17B": ({"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
                                "repetition_penalty": 1.0}, "card: Thinking mode; generation_config.json 0.6 / 0.95 / 20"),
    "Qwen/Qwen3.8-27B": (_QWEN38_THINK, "card: thinking sampling; generation_config.json 1.0 / 0.95 / 20"),
    "Qwen/Qwen3.8-Flash-Next": (_QWEN38_THINK, "card: thinking sampling; generation_config.json 1.0 / 0.95 / 20"),
    "Qwen/Qwen3.8-Flash-Next-FP8": (_QWEN38_THINK, "card: thinking sampling; generation_config.json 1.0 / 0.95 / 20"),
    "zai-org/GLM-5.3-Flash": ({"temperature": 1.0, "top_p": 0.95}, "generation_config.json"),
    "deepseek-ai/DeepSeek-V4.1-Flash": ({"temperature": 1.0, "top_p": 0.95}, "card: temperature=1.0, top_p=0.95"),
    "google/gemma-4-E4B-it": (_GEMMA4, "generation_config.json"),
    "google/gemma-4-12B-it": (_GEMMA4, "generation_config.json"),
    "google/gemma-4-31B-it": (_GEMMA4, "generation_config.json"),
    "meta-models/Muse-Glimmer-30B": (_GEMMA4, "generation_config.json"),
}
GREEDY_FAMILIES = ("qwen3-vl", "internvl3", "sarashina", "llm-jp-vl", "llm-jp-4-text")


def _served(model_id: str, family: str) -> dict:
    """A served model's preset: its family, with the model's own recommended sampling (SERVED_SAMPLING)."""
    p = _family(family)
    if model_id in SERVED_SAMPLING:
        p["sampling"] = dict(SERVED_SAMPLING[model_id][0])
    return p


_AZURE_GPT5 = {"api_key_var": "AZURE_OPENAI_KEY_GPT5", "base_url_var": "AZURE_OPENAI_ENDPOINT_GPT5",
               "api_version": "2025-04-01-preview"}


def _family(name: str, **over) -> dict:
    return {**FAMILIES[name], "family": name, **over}


def _alias(slug: str, open_weight: bool = False) -> bool:
    """An undated / alias model id: the API may serve another snapshot under it at any time (a -preview, or a closed
    model without a dated id). Open-weight ids name released weights (hugging_face_id recorded). Recorded as
    lock.json sampler.model_identity "alias"; not refused (most OpenRouter models have no dated id)."""
    import re

    return "-preview" in slug or (not open_weight and not re.search(r"\d{4}-\d{2}-\d{2}|-\d{4}$", slug))


# Exact slugs only: a model released later never inherits settings silently (an unknown slug is an error).
PRESETS: dict[str, dict] = {
    # ---- OpenRouter: every registry model, pinned; the default max_tokens is the pinned endpoint's maximum
    **{f"openrouter/{m}": {"provider_pin": list(s["provider"]), "quant": list(s["quant"]) if s.get("quant") else None,
                           "sampling": dict(s.get("sampling") or {}), "efforts": s.get("efforts"), "effort": s.get("effort"),
                           "toggle": bool(s.get("toggle")),
                           "alias": _alias(m, s.get("open_weight", False))}
       for m, s in OPENROUTER_MODELS.items()},
    # the judge sends reasoning {"effort": "none"} (not {"enabled": false}): reasoning_none "effort"
    "openrouter/openai/gpt-5.1": {**{k: v for k, v in FAMILIES["gpt-5.1"].items() if k != "output_limit"},
                                  "family": "gpt-5.1", "provider_pin": ["openai"], "max_tokens_field": "max_tokens",
                                  "reasoning_none": "effort", "alias": True},
    # the default judge (DEFAULT_JUDGE, since 2026-11-02): effort medium
    # ($0.10 / $0.50 per 1M tokens on OpenRouter, 2026-11-01)
    "openrouter/openai/gpt-6-luna": {"provider_pin": ["openai"], "effort": "medium",
                                     "efforts": ("none", "low", "medium", "high", "xhigh", "max"),
                                     "max_tokens_field": "max_tokens", "alias": True},
    # image detail "high" as on OpenAI
    "openrouter/openai/gpt-4o-2024-11-20": {**{k: v for k, v in FAMILIES["gpt-4o"].items() if k != "output_limit"},
                                            "family": "gpt-4o", "provider_pin": ["openai"], "image_detail": "high"},
    # ---- OpenAI / Azure: Responses API for gpt-5*, chat for gpt-4o
    "openai/gpt-5.1-2025-11-13": _family("gpt-5.1"),
    "azure/gpt-5.1-2025-11-13": _family("gpt-5.1", api_model="gpt-5-1-2025-11-13", **_AZURE_GPT5),  # deployment: dashes
    "openai/gpt-4o-2024-11-20": _family("gpt-4o"),
    "azure/gpt-4o-2024-11-20": _family("gpt-4o"),
    # ---- Gemini (AI Studio key, or Vertex AI with GEMINI_PROJECT_ID / GEMINI_LOCATION)
    "gemini/gemini-3.1-pro-preview": _family("gemini-3", alias=True),
    "vertex/gemini-3.1-pro-preview": _family("gemini-3", alias=True),
    # ---- served models: one exact entry per engine and HF id
    **{f"{eng}/{mid}": _served(mid, fam) for mid, fam in SERVED_MODELS.items() for eng in SERVED_ENGINES},
}

# Default judge (2026-11-02): GPT-6 Luna, effort medium. On the 100 human-labelled HakushoBench verdicts it agrees
# with the humans as often as gpt-5.1 (0.94 vs 0.95, kappa 0.88 vs 0.90) at about 1/20 of the cost of gpt-5.1 medium
# ($0.009 vs $0.185 per 100 verdicts). gpt-5.1 stays available with --judge-model openrouter/openai/gpt-5.1.
DEFAULT_JUDGE = "openrouter/openai/gpt-6-luna"


SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty")


def check_generation_configs(hf_cache: str) -> list[dict]:
    """Per served model of SERVED_SAMPLING with a snapshot in `hf_cache`: the sampling fields where our preset and the
    snapshot's generation_config.json both set a value and differ (a field only one of them sets is not a conflict:
    cards add presence_penalty / min_p, generation_config.json often has none). Returns rows {model, snapshot, field,
    preset, generation_config, source}; models without a snapshot are listed with field None."""
    import glob
    import json
    import os

    out = []
    for mid, (sampling, source) in sorted(SERVED_SAMPLING.items()):
        snaps = sorted(glob.glob(os.path.join(hf_cache, "models--" + mid.replace("/", "--"), "snapshots", "*")))
        cfgs = [os.path.join(p, "generation_config.json") for p in snaps if os.path.exists(os.path.join(p, "generation_config.json"))]
        if not cfgs:
            out.append({"model": mid, "snapshot": None, "field": None, "source": source})
            continue
        with open(cfgs[-1], encoding="utf-8") as f:
            g = json.load(f)
        for k in SAMPLING_KEYS:
            if k in g and k in sampling and float(g[k]) != float(sampling[k]):
                out.append({"model": mid, "snapshot": os.path.basename(os.path.dirname(cfgs[-1])), "field": k,
                            "preset": sampling[k], "generation_config": g[k], "source": source})
    return out
