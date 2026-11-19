# Inference settings

How a model's settings are resolved, what is sent, and how the endpoint is pinned. Using them:
[Models](../models/overview.md).

## Output limit

**max_output_tokens** is the maximum number of generated tokens, reasoning / thinking tokens included, for every
backend. It is always sent explicitly, never left to the server's or API's default, and no preset sets it. Without
`--max-output-tokens` it is the largest generation the endpoint allows, recorded in lock.json:

| backend | endpoint maximum |
|---|---|
| OpenRouter | the pinned endpoint's `max_completion_tokens` (its context length when unset); unpinned (`--preset none` without `--provider`) needs `--max-output-tokens` |
| sglang / vLLM / hf | the server's context length. As the input length is not known client-side, a request that overflows is re-sent once with the room left (context - input - 16; under 256 tokens of room: `context_overflow`) |
| OpenAI / Azure / Gemini | the model's documented output limit, in its preset (e.g. gpt-5.1 128000, gemini-3.1-pro 65536) |

A larger `--max-output-tokens` is clamped to the endpoint maximum; both values are recorded. The judge follows the
same rule (a cut-off verdict is `judge_failed`).

## Sampling

**Every sampling field** (temperature, top_p, top_k, min_p, presence / frequency / repetition penalty), the effort and
max_output_tokens get a value and a source, recorded in lock.json and in every trial. Precedence:

1. CLI / job config (`--temperature`, `--model-arg`, ...): `cli`
2. the preset: `preset:<name>`
3. defaults the provider or model declares, then sent explicitly: OpenRouter's `default_parameters`, the served
   model's `generation_config.json`, Gemini's model info
4. nobody declares it (OpenAI / Azure): not sent, recorded as `provider default (not reported)`

**Presets are exact slugs.** No prefix or wildcard matching: a model released later never inherits settings silently.
Shared settings are families; a served Hugging Face id has the same preset under every engine prefix (sglang / vllm /
hf). An unknown slug is refused; `--preset FAMILY` applies a family to any model id or path (internal checkpoints),
`--preset none` runs without model-specific settings. `sem models list` shows the exact slugs and the families.

## Model options

`--model-arg` keys are declared per preset (`sem models list`; unknown keys are an error) and recorded. Server-side
keys (`max_soft_tokens`, `chat_template`) are not sent with requests: `sem serve --model-arg` applies them when it
launches the engine, and `sem run --server` checks the job's values against the server manifest. Dataset options are
`--dataset-arg` values.

## Endpoint changes and unavailable providers

The endpoint is part of the job: the OpenRouter endpoint (provider, quantization, context length, output limit) or
the served engine's identity is recorded, compared on resume (a difference refuses it) and hashed into every trial.
Requests to a pinned OpenRouter model name exactly that provider and quantization, with fallbacks off. An
unreachable model catalog refuses the start (the settings could not be recorded).

**Changing provider.** `--provider` / `--quantization` override the preset's pin; they make a different job config, so
a different provider is a new job. The leaderboard combines datasets from different jobs, one config per dataset:
finish the unfinished datasets in a new job with the other provider.

**Circuit breaker.** A pinned provider that stops serving the model fails every request with `endpoint_unavailable`
(or connection errors after every retry); rate limits, 5xx and timeouts are retried and end as ordinary
`provider_failed`. When `retry.abort_after_consecutive` (default 20; 0 = off) trials in a row fail this way, the job
stops without writing those trials; a plain `sem job resume` later regenerates exactly them, or start a new job with
another `--provider` for the unfinished datasets (`sem job show` lists them). A model or provider change within the
job stops it at once.

**Request timeout.** `--request-timeout-s` (default: 6 h for served engines, 3600 s for OpenAI / Azure / OpenRouter,
the SDK's for Gemini) is runtime-only, like `-n`. A dead served engine is detected by `sem serve`'s watchdog
([Serving internals](serving.md)), not by the timeout: the restart drops the open connections, and the requests retry
until the engine is back. Connection failures have their own budget, `retry.connection_budget_s` (default 1200 s,
about a restart plus the model load); with `--server`, a server that `sem serve` has given up on ends the requests at
once, so the circuit breaker stops the job within minutes.
