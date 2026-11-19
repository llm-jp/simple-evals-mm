# Adding an API format

Most new models need only a [preset](adding-a-preset.md): any OpenAI-compatible endpoint (OpenRouter, vLLM, SGLang,
most hosted APIs) already works through `openai_chat_completions`. A new **API format** is for a request shape that
none of the three formats speaks (`openai_chat_completions`, `openai_responses`, `google_generate_content`).

## 1. The sampler

One class per format in `src/simple_evals_mm/samplers/<format>.py`, a subclass of `samplers.base.Sampler`:

```python
class MyFormatSampler(Sampler):
    api_format = "my_format"

    def _prepare(self) -> None:
        """Once before the first call: what the endpoint reports about itself."""
        self.endpoint_info = {...}      # identity fields that change outputs (model version, provider, ...)
        self.endpoint_max = ...         # the largest output the endpoint allows (+ endpoint_max_source)
        self.model_defaults = {...}     # sampling defaults the provider declares (sent explicitly, recorded)

    def __call__(self, messages, tools=None, seed=None) -> Response:
        """One request. Retries transient errors itself (rate limits do not use up the tries); gives up with
        errors.SamplerError(message, cause) whose cause is one of errors.POLICY (refused, context_overflow,
        provider_failed, ...)."""
        ...
        return Response(text=answer, reasoning=trace, input_tokens=..., output_tokens=..., reasoning_tokens=...,
                        finish_reason="stop" | "length" | "content_filter", model=..., cost_usd=...,
                        extra={"sent_images": [...], "max_tokens_sent": ..., "seed": seed})
```

What every sampler must do, so jobs stay reproducible and comparable:

- **Send what was resolved, nothing else**: the sampling values of `self.sampling()` (each with its source), the
  effort, `effective_max_output_tokens()`; no environment variable may change a request.
- **Separate the reasoning** from the answer (`Response.reasoning`): verifiers read only `text`.
- **Report what was really sent** in `extra`: the images after any downscaling (`describe_image`), the max tokens
  after a context-overflow refit, the seed.
- **Report the finish reason**: `length` becomes `budget_exhausted` (0), `content_filter` becomes `refused` (0).
- `config()` (inherited) is what the job locks; add fields there only if they change outputs.

## 2. Register it

- `samplers/registry.py`: the format's name in `API_FORMATS`; the providers that speak it in `PROVIDERS[...]["formats"]`
  (the first is the provider's default); a branch in `make_sampler`.
- `provenance.py`: `SAMPLER_MODULES["my_format"] = ("samplers.my_format",)`, so a change to the sampler counts as a
  generation change for jobs of this format only.
- `samplers/presets.py`: a family and presets for its models ([Adding a model preset](adding-a-preset.md)).

## 3. Test it

- `tests/test_samplers.py`: the request body of a representative call against a fake client, as a golden snapshot
  (`golden.check("samplers", "my_format: ...", {"body": ...})`), plus the error mapping (a refusal, a context
  overflow, a transient error, a cut-off answer).
- `pytest -m "not api and not local_model and not network"`, then one real job:
  `sem run -m <provider>/<model> -d gpqa -l 5`, and read the trials (reasoning separated, usage,
  finish reasons, cost).
