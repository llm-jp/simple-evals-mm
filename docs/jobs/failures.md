# Failures and retries

Every failed trial gets one cause; one table decides whether it is retried and whether it counts:

| Cause | Example | Score | Retried | What to do |
|---|---|---|---|---|
| `budget_exhausted` | the answer was cut off at the output limit | 0 | no | a larger `--max-output-tokens` needs a new job (`sem run --from`) |
| `refused` | a provider / content-filter refusal, including Gemini stopping on a content filter (finish reason `SAFETY`, `RECITATION`, `PROHIBITED_CONTENT`, `BLOCKLIST`, `SPII`, natively or through OpenRouter) (StrongREJECT: the safe outcome, scored 1) | 0 | no | – |
| `context_overflow` | the input is longer than the model's context, or over OpenRouter's 8 MB text limit (input + output limit over the context is re-sent with a smaller output limit first: [Output limit](../reference/inference-settings.md#output-limit)) | 0 | no | – |
| `request_rejected` | the API answered every retry of this one request with an error | 0 | no | – |
| `provider_failed` | rate limit, 5xx, timeout | excluded | `sem job resume -f provider_failed` | regenerate them |
| `endpoint_unavailable` | the model / pinned provider / server is gone | excluded | no; the job stops | resume when it is back, or pin another provider in a new job |
| `config_error` | invalid or missing API key, no credits, no permission for the model | excluded | no; the job stops at the first one | fix it (`.env`), then `sem job resume` |
| `judge_failed` / `sandbox_failed` | the judge or the code sandbox failed | excluded | at verification | `sem job regrade` |

A dataset is complete when every trial has a counted result; `sem job show` lists the causes next to the score and,
under the table, what each one means and what to do.

## Retries

- A transient failure (rate limit, 5xx, timeout, connection error) is retried inside the trial with backoff
  (`retry.max_retries` in the [job config](run-a-job.md#job-config)); rate limits do not use up the retries.
- What still fails is written as `provider_failed`, excluded from the score, and regenerated later:
  `sem job resume -p jobs/NAME -f provider_failed`.
- An API that answers every retry of one request with an error, while the job's other requests go through, rejects
  that request: `request_rejected` (0, not retried).
- **Circuit breaker**: when many consecutive trials fail at once (`retry.abort_after_consecutive`, a provider outage or
  a pinned provider gone) the job stops instead of failing every trial; resume later, or pin another provider in a
  new job.
- Judge and sandbox failures are retried at verification and, when they persist, excluded (`judge_failed` /
  `sandbox_failed`); `sem job regrade` verifies them again.
