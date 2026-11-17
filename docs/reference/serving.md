# Serving internals

What `sem serve` records and checks. Using it: [Local models](../models/local-models.md).

`sem serve ENGINE MODEL` launches an OpenAI-compatible server (`sglang`, `vllm`, or `hf` = serving/hf_server.py) from
the engine's own venv (`--engine-venv`, or `engine_venv` in `-c serve.yaml`; sglang / vLLM are not dependencies of this
repo; hf runs from this venv), supervises it (new process group, restart loop, health wait, the group is killed and the
manifest marked stopped on exit) and writes the **server manifest** `<jobs_dir>/_servers/<id>.json` (or `--manifest
PATH`; the file appears once the server is ready). It prints the base URL and the manifest path. `--dry-run` builds and
prints the manifest without launching (exit 1 when the engine check refuses).

| manifest field | content |
|---|---|
| engine | name, version, `installed_sha256` (sha256 over every RECORD entry of the engine distribution and transformers, as installed), RECORD check (`edited`: files that differ from the wheel RECORD; `patched_in_place`: edited files whose content equals a requested patch), versions of torch / triton / flashinfer / kernels / tokenizers / ..., patches (name, sha256, upstream ref, file hashes); hf: our package version + commit |
| launch | argv (secrets removed, the venv interpreter and the template path as placeholders), dp / tp, deterministic flag, batch (hf), server-side model args, the environment variables set for the engine (`PATH` = `<engine_venv>/bin:<inherited>`; PYTHONPATH = the overlay; the inherited PYTHONPATH is dropped) and the engine-relevant ones present (SGLANG_*, VLLM_*, GEMMA_*, ...) |
| chat_template | source (`chat_template.jinja`, `tokenizer_config.json:chat_template`, `--chat-template`) and sha256 of the file passed |
| model | HF id + snapshot revision (local HF cache refs) or a local path; sha256 of config / tokenizer / processor / template / remote-code files; weights by size + the LFS sha256 the HF cache names each blob by (local directories: size + mtime; full weight hashing is too slow for every launch); generation_config.json sha256 + values |
| hardware | GPU names and counts, torch and its CUDA build inside the engine venv (compared); the GPU driver and the CUDA version it reports (nvidia-smi) are recorded only: they differ between nodes without changing the engine's numerics |
| served / probe | what the running server reports (/v1/models model id + max_model_len, version) and the sha256 of the token ids of a fixed probe conversation (system + user text + an 8x8 PNG) rendered by the tokenize endpoint, or `probe: unsupported` with a note |
| runtime | volatile, not in the digest: host, port, base URL, pid, start / stop times, restarts, watchdog settings and watchdog restarts (time, reason), events, status, paths of the overlay / template / log |

**Engine check.** Every installed file of the engine distribution (and transformers) is hashed and compared with its
wheel RECORD. An edit is refused (the files are listed) unless a requested `--patch` provides exactly that content.
**Patches** live in `serving/patches/<name>/` (`patch.json`: name, upstream_ref, description, engine, engine_versions,
files; the files under `files/<site-packages path>`) and are loaded through a generated `sitecustomize.py` overlay on
PYTHONPATH that maps the module names to the patch files (inherited by the engine's worker processes; the venv is never
modified). Registered: `sglang_pr43753` (the minimal Gemma 4 pooler fix, sglang 0.5.17) and
`sglang_pr43753_full` (the full PR, sglang 0.5.23; only for Gemma 4 models with a vision tower) and `sglang_pr47809` (the
Triton extend kernel honoring custom masks past the causal tile, sglang 0.5.23: without it Gemma 3/4 image tokens attend
bidirectionally only within their own query tile; Gemma-4-12B-it CountBenchQA 69 -> 96, same answers as HF). Gemma 4's image resolution
is a flag, not a patch: `--model-arg max_soft_tokens=1120` becomes sglang `--mm-process-config '{"image":
{"max_soft_tokens": 1120}}'` (images_kwargs of the HF processor; an in-place edit of processors/gemma4.py setting it is refused by the engine
check: reinstall the file).

**Chat template.** Always passed explicitly: by default the snapshot's own template (chat_template.jinja, else
tokenizer_config.json `chat_template`) is written next to the manifest and passed with `--chat-template`, so the engine
never auto-selects a built-in template; `--chat-template FILE` overrides it. The digest of the file passed is recorded.

**Probe.** After the health wait and after every restart, the probe conversation is rendered through the tokenize
endpoint (`/tokenize` with `messages`: sglang 0.5.17 / 0.5.23 and vLLM 0.32 both support it; hf_server implements it
for the backends that render with a Jinja template). After a restart, a different probe hash, served model id /
max_model_len / version or engine identity (re-checked against RECORD) is fatal: the server is stopped and the reason
recorded in `runtime.events` (status `stopped: identity changed after a restart`).

**PATH.** The engine runs with `<engine_venv>/bin` first on PATH (recorded as `"PATH": "<engine_venv>/bin:<inherited>"`
in `launch.env_set`, not this machine's PATH): flashinfer / sglang build JIT kernels with the venv's `ninja` at the
first request of a new shape. Without it the schedulers crash with `FileNotFoundError: 'ninja'`.

**Watchdog.** Process exit is not the only failure: a scheduler can die while the HTTP process keeps answering. While
serving, `sem serve` polls `<root>/health` every `--watch-interval` s (default 30; 0 = off). `--watch-failures`
consecutive failures (default 4; a failure is a non-200 or no answer within 20 s), or a crash marker in the engine log
written since this launch (sglang `Scheduler hit an exception`, vLLM `EngineCore encountered a fatal error`; one match
is enough), make it log the reason, kill the engine's process group (SIGTERM, SIGKILL after a grace period) and restart
through the restart loop: the restart re-checks the engine identity and the probe as above (a change is still fatal).
Each watchdog restart is recorded in `runtime.watchdog_restarts` (time, reason) and `runtime.events`. What `/health`
reports with a dead scheduler: sglang 0.5.23 `/health` is `/health_generate` (`SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION`
defaults on): it submits a 1-token generation and returns 503 when the scheduler / detokenizer sends nothing within
`SGLANG_HEALTH_CHECK_TIMEOUT` (20 s), so a dead scheduler fails it (the watchdog's 20 s timeout counts the slow answer
as a failure too); vLLM 0.32 `/health` returns 503 once the engine core is dead (`EngineDeadError`; the API server then
also shuts itself down unless `VLLM_KEEP_ALIVE_ON_ENGINE_DEATH`); hf_server `/health` returns 503 when no worker is
ready (dead workers are failed and respawned by the front itself).

**Determinism.** `--deterministic`: sglang `--enable-deterministic-inference`, vLLM `VLLM_BATCH_INVARIANT=1` (vLLM 0.32
has no CLI flag; needs compute capability >= 9.0), hf `torch.use_deterministic_algorithms` in the workers. Recorded in
`launch.deterministic`.

**Client side.** `sem run --server MANIFEST` (config key `server`) takes the base URL from the manifest (unless
`--base-url`), checks the slug's engine and model id and the server-side `--model-arg` values against the manifest,
and after prepare() verifies the running server (/v1/models model id and max_model_len, version, the probe when
supported). The served generation_config.json values and revision come from the manifest, not the client's HF cache.
lock.json gets `server: {digest, manifest}` (the manifest without `runtime`), each session the runtime part (host,
port, pid); the digest is part of the resume comparison and of every trial's generation_lock. The manifest path is
runtime-only (another session may use another manifest with the same digest).

**hf_server.** Loads models itself (serving/hf_backends.py: LLM-jp-VL / Harmony, Sarashina, InternVL3, Qwen3-VL;
`--backend` or by model id / config architectures); reads no environment variable. Every request field is applied (temperature, top_p,
top_k, min_p, repetition / presence / frequency penalty (OpenAI semantics as a logits processor), seed (torch seed per
request), max_tokens, skip_special_tokens, chat_template_kwargs, reasoning_effort) through a complete GenerationConfig
built from the request only; unknown fields are refused with 400. Batching (`sem serve hf --batch N`, default 1 = off)
is recorded: padding changes greedy outputs. `/v1/models` reports the revision, `/server_info` the version, backend,
batch config, determinism and the template digest.
