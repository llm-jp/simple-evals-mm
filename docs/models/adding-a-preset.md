# Adding a model preset

Presets are data in `src/simple_evals_mm/samplers/presets.py`, keyed by the exact `provider/model` slug (no prefix
matching: a new model never inherits settings silently).

- **OpenRouter**: add the model to `OPENROUTER_MODELS` with its pinned provider, the recommended sampling of an
  open-weight model and the effort levels the endpoint accepts:

  ```python
  "qwen/qwen3.8-27b": {"provider": ["alibaba"], "sampling": QWEN38, "efforts": ("low", "medium", "xhigh"), "open_weight": True},
  ```

  Check it with `python scripts/openrouter_smoke.py --models qwen/qwen3.8-27b` (one paid request per effort).
- **Served model** (`sglang/`, `vllm/`, `hf/`): add its Hugging Face id to `SERVED_MODELS` with a family from
  `FAMILIES` (or add a family: sampling, efforts, chat-template kwargs, reasoning parser). Every engine prefix gets it.
- **OpenAI / Azure / Gemini**: an entry in `PRESETS` built with `_family(...)`, with the documented `output_limit`.

A model id without an entry (an internal checkpoint) runs with `--preset FAMILY` or `--preset none`. `sem
models list` shows the result; `tests/test_samplers.py` snapshots the request bodies of the main presets
(`tests/golden/samplers.json`).


## Checklist

- [ ] the entry, with the card's recommended sampling and the efforts the endpoint accepts (cite the card in a comment)
- [ ] `sem models list` shows it; `sem run -m ... -d gpqa -l 5 --dry-run` resolves every setting from the preset
- [ ] OpenRouter: `scripts/openrouter_smoke.py --models <slug>` (one paid request per effort)
- [ ] a served model: `sem serve ... --dry-run` builds the manifest; a short `sem run --server` on a real task
- [ ] `tests/test_samplers.py` (and `--update-golden` when a snapshotted request body changes on purpose)
- [ ] the card's scores in `references/model_cards.yaml` ([Model card parity](model-cards.md))
