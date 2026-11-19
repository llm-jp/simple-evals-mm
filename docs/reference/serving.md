# Serving internals

What `sem serve` records and checks. Using it: [Local models](../models/local-models.md).

`sem serve ENGINE MODEL` launches an OpenAI-compatible server (`sglang`, `vllm`, or `hf`, our own server for
custom-architecture models) from the engine's own venv (`--engine-venv`, or `engine_venv` in `-c serve.yaml`; `hf` runs
from this venv), supervises it, and writes the **server manifest** `<jobs_dir>/_servers/<id>.json` (or `--manifest
PATH`) once the server is ready. It prints the base URL and the manifest path. `--dry-run` builds and prints the
manifest without launching.

| manifest field | content |
|---|---|
| engine | name, version, a hash of its installed files (checked against the wheel RECORD), versions of torch / triton / flashinfer / tokenizers / ..., the patches applied |
| launch | the arguments (secrets removed), dp / tp, the deterministic flag, server-side model args, the engine-relevant environment variables |
| chat_template | where it came from and the sha256 of the file passed |
| model | Hugging Face id and snapshot revision, or a local path; hashes of the config, tokenizer, processor, template and remote-code files; the weights by size and the hash the HF cache names them by; generation_config.json |
| hardware | GPU names and counts, torch and its CUDA build (compared); the GPU driver is recorded only |
| served / probe | what the running server reports (model id, context length, version) and the hash of a fixed probe conversation rendered by the server's tokenizer |
| runtime | host, port, pid, restarts, events, status: recorded, not part of the digest |

**Engine check.** Every installed file of the engine (and transformers) is compared with its wheel RECORD: an edited
engine is refused (the files are listed) unless a requested `--patch` provides exactly that content.

**Patches** live in `src/simple_evals_mm/serving/patches/<name>/` and are loaded through a `PYTHONPATH` overlay, so the
engine's venv is never modified. Registered: `llmjpvl_sglang` (LLM-jp-4-VL on SGLang 0.5.23, [LLM-jp
models](../models/llm-jp.md)) and the Gemma 3/4 fixes `sglang_pr43753` (SGLang 0.5.17), `sglang_pr43753_full` and
`sglang_pr47809` (SGLang 0.5.23); each patch's `patch.json` says what it fixes and for which engine versions.

**Chat template.** Always passed explicitly: by default the model snapshot's own template, so the engine never picks
a built-in one; `--chat-template FILE` overrides it.

**Probe.** After start-up and after every restart, the probe conversation is rendered by the server. After a restart,
a different probe, served model or engine identity is fatal: the server is stopped and the reason recorded.

**Watchdog.** A scheduler can die while the HTTP process keeps answering. `sem serve` polls `/health` every
`--watch-interval` seconds (default 30; 0 = off); after `--watch-failures` consecutive failures (default 4), or a crash
message in the engine log, it kills the engine and restarts it (re-checking the identity and the probe as above). Each
restart is recorded in the manifest.

**Determinism.** `--deterministic`: sglang `--enable-deterministic-inference`, vLLM `VLLM_BATCH_INVARIANT=1` (needs
compute capability >= 9.0), hf deterministic torch algorithms. Recorded in the manifest.

**Client side.** `sem run --server MANIFEST` takes the base URL from the manifest (unless `--base-url`), checks the
model id and the server-side `--model-arg` values against it, and verifies the running server (model id, context
length, version, probe). The manifest digest goes into lock.json, the resume comparison and every trial; the manifest
path does not (another session may use another manifest with the same digest).

**hf server** (`sem serve hf`, needs `uv sync --frozen --extra hf-server`): loads LLM-jp-VL, Sarashina, InternVL3 and
Qwen3-VL models with their Hugging Face code (`--backend`, or chosen by model id / architecture). Every request
field is applied (sampling, seed, max_tokens, chat-template arguments, reasoning effort) and unknown fields are
refused. Batching (`--batch N`, default 1 = off) is recorded, since padding changes greedy outputs.
