# Quick start

Install first: [Installation](installation.md).

## The first job

```bash
# what would run: the resolved settings, the trials, an estimated cost and time; nothing is generated
uv run --frozen sem run -m openrouter/qwen/qwen3.8-27b -d hakushobench -l 5 --dry-run
# 5 HakushoBench questions, 3 attempts each (the dataset default), judged by the default judge
uv run --frozen sem run -m openrouter/qwen/qwen3.8-27b -d hakushobench -l 5 --job-name first
uv run --frozen sem job show jobs/first
```

Example output:

```
dataset        reward           95% CI         pass@k / majority          trials done/total        verified   causes  cost model  cost judge
hakushobench   0.8667 ± 0.0577  0.6000–1.0000  pass@2 0.900 pass@3 0.900  a1 5/5, a2 5/5, a3 5/5  yes (...)  —       $0.0123     $0.0010
```

- **reward**: the mean over the attempts (± their standard deviation); **95% CI**: bootstrap over the questions
  ([View results](../results/view.md)).
- **causes**: trials that failed, by cause ([Failures and retries](../jobs/failures.md)): a cut-off answer or a refusal scores
  0; a transient API failure is excluded and retried by `sem job resume -p jobs/first -f provider_failed`.
- Run the same command again (same `--job-name`) to resume an interrupted job: only the missing trials run.

Most datasets download their data on first use. A few need a one-time step (gated terms, the code sandbox, local
data); `sem run` checks this before the first model call and says what to do ([Dataset setup](../task_setup.md)).

## Next

- choose datasets and models: [Datasets](../datasets/overview.md), [Models](../models/overview.md)
- an API model at scale: [API models](../models/api-models.md); your own checkpoint: [Local models](../models/local-models.md)
- the options of a job, resuming it, its failures: [Run a job](../jobs/run-a-job.md)
