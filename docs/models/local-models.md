# Local models (vLLM, SGLang)

**When**: open weights or an in-house checkpoint on your own GPUs. **Needs**: the engine installed in its **own**
venv (vLLM and SGLang are not dependencies of this repository), the model in the Hugging Face cache or a local
directory, and network access from the evaluation process to the server.

## 1. Install the engine once

```bash
uv venv /path/to/vllm-venv --python 3.12 && uv pip install \
    --python /path/to/vllm-venv/bin/python vllm
uv venv /path/to/sglang-venv --python 3.12 && uv pip install \
    --python /path/to/sglang-venv/bin/python "sglang[all]"
```

Any version works; `sem serve` records the engine version, a content hash of its installed files and the GPU stack
in the server manifest, so two jobs on different engine builds are told apart.

## 2. Serve, then run against the manifest

```bash
# terminal 1 (or a background process): launches the engine and writes the manifest when
# it is ready
uv run --frozen sem serve vllm Qwen/Qwen3.8-27B --engine-venv /path/to/vllm-venv \
    --dp 8 \
    --manifest jobs/_servers/qwen38.json -- --reasoning-parser qwen3
# terminal 2
uv run --frozen sem run -m vllm/Qwen/Qwen3.8-27B -d hakushobench,mmmu \
    --server jobs/_servers/qwen38.json \
    -n 256 --job-name qwen38-27b__served
```

- `sem serve vllm|sglang MODEL`: `--dp` / `--tp` data / tensor parallelism; arguments after `--` go to the engine
  as they are (recorded in the manifest). A **reasoning model needs the engine's reasoning parser**
  (`--reasoning-parser ...`): a thinking trace left in the answer stops the run with an error instead of being
  graded. LLM-jp's Harmony models are the exception: simple-evals-mm splits their channels
  itself ([LLM-jp models](llm-jp.md)).
- `--server MANIFEST` gives the run the base URL and the server's identity (engine, patches, model files, chat
  template); the job locks its digest, so a resume against another server is refused.
- A served engine has no rate limit: use a high `-n` (128-256).
- The watchdog restarts an engine that stops answering `/health`.

Without `sem serve` (a server you started yourself): `--base-url http://host:port/v1` (or `VLLM_BASE_URL` /
`SGLANG_BASE_URL`). The job then records less about the server, and the leaderboard does not accept it.

## Model settings

A model with a preset (`sem models list`) gets its recommended sampling. A checkpoint without one (an in-house
model) takes a **family** preset: `--preset qwen3.8` (the family's settings; `sem models list` ends with the
families) or `--preset none` (the engine's defaults), plus `--temperature` / `--top-p` / `--top-k` / `--model-arg k=v` as needed. Everything is recorded.

## On a cluster

Serve and run in the same batch job, so the server stops with it. `pbs/run_eval.sh` is a PBS example: it starts
`sem serve` in the background, waits for the manifest, runs `sem run --server` and stops the server:

```bash
VENV=/path/to/vllm-venv
JOB_NAME=qwen38-27b__served MODEL=vllm/Qwen/Qwen3.8-27B DATASETS="hakushobench mmmu" \
  CONCURRENCY=256 \
  SERVE="vllm Qwen/Qwen3.8-27B --engine-venv $VENV --dp 8 -- --reasoning-parser qwen3" \
  sh pbs/run_eval.sh
```

**Load the model once.** A server can be shared by any number of `sem run` jobs: effort, sampling, datasets and `-l`
are request settings, so only server-side options (engine arguments such as the reasoning parser, `--dp` / `--tp`,
patches, the chat template, a server-side `--model-arg`) need a new server. For large models, where loading takes
long, run all the settings of one server in one batch job: `EFFORTS="low xhigh"` runs one job per effort
(`JOB_NAME__low`, `JOB_NAME__xhigh`) in parallel against the same server. For other sweeps, start `sem serve` once and
run several `sem run --server MANIFEST` in the background. Resuming in a later batch job loads the model again (the
same server configuration passes the digest check). If loading takes over an hour, raise `sem serve --health-timeout`
(seconds, default 3600).

Compute nodes without internet: download the data first on a login node (`sem datasets download -d ...`) and keep
the model in the shared Hugging Face cache (`HF_HOME`).
