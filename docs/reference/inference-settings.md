# Inference settings

How a model's settings are resolved, what is sent, and how the endpoint is pinned. Using them:
[Models](../models/overview.md).

**max_output_tokens** is the maximum number of generated tokens, reasoning / thinking tokens included, for every
backend. Requests carry it in the API's own field (chat `max_tokens` / `max_completion_tokens`, Responses and Gemini
`max_output_tokens`, sglang / vLLM / hf `max_tokens`), always explicitly: never the server's or API's default (sglang's
default max_new_tokens, vLLM's max_model_len - prompt). No preset sets it. Without `--max-output-tokens` it is the largest
generation the endpoint allows, resolved once at `prepare()` and recorded in lock.json (`settings.max_output_tokens`:
value, requested, endpoint_max, source):

| backend | endpoint maximum |
|---|---|
| OpenRouter | the pinned endpoint's `max_completion_tokens` (`context_length` when null); unpinned (`--preset none` without `--provider`) needs `--max-output-tokens` |
| sglang / vLLM / hf | the server's context length (`/v1/models` max_model_len; hf_server reports the config's max_position_embeddings). The input length is not known client-side: the request sends the context length, the server rejects it with its overflow error (which names the input length) and the request is re-sent once with context - input - 16 (under 256 tokens of room: context_overflow) |
| OpenAI / Azure / Gemini | the preset's `output_limit`, the model's documented output limit (gpt-5.1 128000, gpt-4o 16384, gemini-3.1-pro 65536); Gemini without one: models.get output_token_limit |

A larger `--max-output-tokens` is clamped to the endpoint maximum; both values are recorded. The judge follows the
same rule (a cut-off verdict is judge_failed).

**Every sampling field** (temperature, top_p, top_k, min_p, presence / frequency / repetition penalty), the effort and
max_output_tokens get a value and a source at `prepare()`, recorded in the sampler config (`settings`, in lock.json and
every trial's generation_lock). Precedence:

1. CLI / job config (`--temperature`, `--model-arg`, ...): `cli`
2. the preset: `preset:<name>`
3. defaults the provider / model declares, queried at `prepare()` and then sent explicitly:
   `openrouter:default_parameters` (GET /models; the row's canonical_slug and hugging_face_id are recorded too),
   `model:generation_config.json` (served: the directory the server reports as root, else the HF snapshot in the local
   cache, else the Hub; its sha256 and snapshot revision are recorded), `gemini:models.get`
4. nobody declares it (OpenAI / Azure): not sent, recorded as `provider default (not reported)`

A response that echoes its sampling (the Responses API: temperature / top_p; Chat Completions, OpenRouter and Gemini echo
nothing) is stored per step in `extra.sampling_echo` with the fields that differ from what was sent.

**Presets are exact slugs.** No prefix or wildcard matching: a model released later never inherits settings silently.
Shared settings are families (`samplers/presets.py` FAMILIES) referenced by each exact entry; a served HF id listed in
SERVED_MODELS has the same preset under every engine prefix (sglang / vllm / hf). An unknown slug is refused; `--preset
FAMILY` applies a family to any model id or path (internal checkpoints), `--preset none` runs without model-specific
settings (only the CLI options; max_output_tokens = the endpoint maximum). `--preset` is a job config key (recorded,
compared on resume); `sem models list` shows the exact slugs and the families.

## Model options

Presets live in `samplers/presets.py` (`sem models list`): exact slugs, families, the accepted `--model-arg` keys
(unknown keys are an error; values are recorded in config.json and the lock.json sampler config). Server-side keys
(max_soft_tokens, chat_template) are not sent: `sem serve --model-arg` applies them (sglang
`--mm-process-config '{"image": {...}}'`, vLLM `--mm-processor-kwargs`; chat_template: `sem serve --chat-template`)
and `sem run --server` checks the job's values against the manifest (a server launched with a key the job does not
pass is refused too). hf_server.py applies every sampling field of the request body ([Serving internals](serving.md)). Dataset options
(livecodebench start / end, omnidocbench revision / max_pred_chars, perceptionbench use_hint, allow_exec, ...) are
`--dataset-arg` values.

## Endpoint changes and unavailable providers

The endpoint is part of the job identity: `prepare()` records the OpenRouter pinned-endpoint row (provider, tag,
quantization, context_length, max_completion_tokens, canonical_slug) or the served engine's identity (model id, root,
max_model_len, version, generation_config.json digest). These fields and the resolved settings values are compared on
resume (a difference refuses it with the field diff, like the commit) and hashed into every trial's generation_lock;
volatile catalog fields (pricing, uptime) are not. Requests to a pinned OpenRouter model name the matched row itself:
`provider.order = [its tag]` (e.g. `openai`, not `openai/flex`), `provider.quantizations = [its quantization]` (also
`unknown`, the catalog's value for closed models), `allow_fallbacks: false`; the catalog is read from the base URL the
requests go to, and an unreachable catalog refuses the start (the settings could not be recorded). The base URL's
identity is compared on resume too (served engines excepted: their URL names this session's machine; the server
identity is compared instead).

**Changing provider.** `--provider` / `--quantization` override the preset's pin; they are recorded and make a different
job config, so a different provider is a new job (a resume with them is refused). The leaderboard combines datasets from
different jobs, one config per dataset: finish the unfinished datasets in a new job with the other provider.

**Circuit breaker.** A pinned provider that stops serving the model fails every request with endpoint_unavailable (not
retried) or with connection-level errors (refused, DNS, TLS) after every retry; only these count (rate limits, 5xx and
timeouts are retried with backoff and end as ordinary provider_failed). A served model / provider change within the job
stops it at once. When `retry.abort_after_consecutive` (default 20; 0 = off) trials in a row
end in one of these causes, the runner submits no new trials, lets the running ones finish, does not write the failed
trials of that streak or after it (a streak broken by a normal trial is written as it is), and stops with "the pinned
endpoint is failing; resume later with `sem job resume`, or start a new job with another --provider for the unfinished
datasets (`sem job show` lists them)". A plain `sem job resume` regenerates exactly the unwritten trials.

**Request timeout vs. a dead server.** The per-request client timeout is an explicit setting, `request_timeout_s`
(`--request-timeout-s`; served engines 6 h so the longest generation under full batch load fits, OpenAI / Azure /
OpenRouter 3600 s, Gemini / Vertex the SDK's), passed to the client with `max_retries=0` (the sampler's own loop
retries; SDK retries would multiply them). It is recorded in the sampler config (lock.json) and per session, and is
runtime-only: it is left out of the locks and the resume comparison, like `n_concurrent`. The timeout is not how a dead
served engine is detected: `sem serve`'s watchdog ([Serving internals](serving.md)) restarts an engine whose `/health` keeps failing or
whose log shows a scheduler crash; the restart drops the open connections, and the connection errors (reset, then
refused while the engine loads: `APIConnectionError`) are retried with backoff and, if the engine does not come back,
end as endpoint_unavailable with the connection flag, which the circuit breaker counts.

**Connection budget.** A served engine's connection-level failures (refused / reset / DNS) do not use the request's
`max_tries`: they have their own wall-clock budget, `retry.connection_budget_s` (default 1200 s = a watchdog restart
plus the model load; runtime-only, recorded per session with the rest of `retry`). When it is spent the request ends
as endpoint_unavailable (connection). With `--server MANIFEST` every connection failure re-reads the manifest: a
`runtime.status` of `stopped: ...` (`sem serve` gave up: failed starts, identity change, restart limit) ends the request
at once, so `retry.abort_after_consecutive` such trials trip the breaker within minutes instead of the job retrying
against a dead port until the walltime (120 tries x 60 s backoff would take about 2 h). The runner's
stdout is line-buffered and its retry / breaker lines are flushed, so a PBS job log shows them as they happen.
