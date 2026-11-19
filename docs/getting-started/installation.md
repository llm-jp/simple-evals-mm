# Installation

simple-evals-mm runs from a git checkout (a job records the commit it ran at, so the checkout must be clean). Pick a
release tag for results you will compare with others ([Versioning](../versioning.md)):

Clone a release and enter it:

```bash
git clone --branch v0.7.0 https://github.com/llm-jp/simple-evals-mm.git
cd simple-evals-mm
```

Install Python 3.13 and the locked dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen
```

Activate the environment, so that `sem` runs as a command (the docs write it so):

```bash
source .venv/bin/activate
```

Without activating, prefix every command with `uv run --frozen` (`uv run --frozen sem run ...`). After `git pull` or
switching to another tag, run `uv sync --frozen` again: the installed package must name the checked-out commit, and
`sem run` refuses to start otherwise.

Check the installed version:

```bash
sem --version
```

Put your API keys in `.env` at the repository root. Only secrets are read from it; nothing in the environment changes
prompts, sampling or scores. An OpenRouter key alone covers every `openrouter/...` model and the default judge:

```
OPENROUTER_API_KEY=sk-or-...
HF_TOKEN=hf_...          # gated Hugging Face datasets (HLE, ZeroBench) and `sem job publish`
```


## Optional

- **Served models**: vLLM / SGLang are installed in their own venvs, not in this one ([Local models](../models/local-models.md));
  `sem serve hf` (custom-architecture models) needs `uv sync --frozen --extra hf-server`.
- **Code execution datasets** (HumanEval, LiveCodeBench) need [bubblewrap](https://github.com/containers/bubblewrap);
  gated datasets need accepting their terms; see [Dataset setup](../task_setup.md).
- **Compute nodes without internet**: prefetch the data on a login node, `sem datasets download -d ...`.

After a `git pull` or `git checkout`, run `uv sync --frozen` again: a job starts only when the installed package names
the checked-out commit ([Troubleshooting](../troubleshooting.md#start-checks-failed)).
