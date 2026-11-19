# Models

`-m PROVIDER/MODEL` names the model; the provider decides where requests go and in which API format.

| Provider | Example | API key / location |
|---|---|---|
| `openrouter` | `openrouter/qwen/qwen3.8-27b` | `OPENROUTER_API_KEY` |
| `openai` | `openai/gpt-5.1-2025-11-13` | `OPENAI_API_KEY` |
| `azure` | `azure/<deployment>` | `AZURE_OPENAI_KEY`, `AZURE_OPENAI_ENDPOINT` |
| `gemini`, `vertex` | `gemini/gemini-3.1-pro-preview` | `GEMINI_API_KEY`; Vertex: `GEMINI_PROJECT_ID`, `GEMINI_LOCATION` |
| `sglang`, `vllm` | `vllm/Qwen/Qwen3.8-27B` | a server you start with `sem serve` ([Local models](local-models.md)) |
| `hf` | `hf/llm-jp/llm-jp-4-vl-9b` | `sem serve hf`: custom-architecture models the engines cannot serve |

Three API formats carry the requests: `openai_chat_completions` (OpenRouter, served engines, OpenAI / Azure chat),
`openai_responses` (OpenAI / Azure GPT-5.x) and `google_generate_content` (Gemini / Vertex).

## Presets

A **preset** is the settings a model's card recommends, kept as data in `src/simple_evals_mm/samplers/presets.py`
and keyed by the exact slug: sampling (temperature, top-p, ...), the effort levels and the default, the output
limit, the chat-template arguments, and for OpenRouter the pinned provider.

```bash
# exact slugs, efforts (default), provider pin, --model-arg keys; then the families
uv run --frozen sem models list
```

- `--effort LEVEL`: one of the preset's levels. Each setting of a model is its own job (`<model>__<effort>`).
- `--temperature` / `--top-p` / `--top-k` / `--max-output-tokens` override the preset; `--model-arg k=v` sets the
  options a preset declares (e.g. `max_soft_tokens` of Gemma 4).
- A model without a preset (an in-house checkpoint) runs with a family, `--preset qwen3.8`, or `--preset none` (the
  engine's own defaults).
- `--dry-run` prints every resolved value with its source (cli, preset, the provider's declared defaults); the job
  records them in `lock.json`.

Next: [API models](api-models.md) · [Local models](local-models.md) · [Adding a model preset](adding-a-preset.md) ·
[Adding an API format](adding-an-api-format.md) · [Model card parity](model-cards.md)
