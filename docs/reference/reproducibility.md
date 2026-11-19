# How reproducibility works

The rules behind [Concepts](../concepts.md): what a job records, what is compared on resume, how jobs from different
commits are related, and how failures count. For using these features see [Jobs](../jobs/run-a-job.md).

## Principles

- **Pin by git commit, record everything else.** A job runs only from a clean, committed tree, so the commit pins
  every line of our code (prompts, templates, verifiers, samplers). Locks and digests cover only what the commit cannot:
  the data, the installed libraries, the endpoint or server, the resolved settings, what each request really carried,
  and external artifacts.
- **Everything that changes outputs is a CLI option or job config value** (or `--model-arg` / `--dataset-arg`), never an
  environment variable; it is recorded in the job.
- **Never store derived values.** Rewards, std, failure counts and costs are recomputed from the trial and verifier rows.

## Job files

```
jobs/<job>/config.json                  the requested settings (CLI + YAML, resolved defaults)
jobs/<job>/lock.json                    what they resolved to: version and commit(s), Python, library checks,
                                        one entry per session (run / resume), the endpoint or server manifest,
                                        model identity, dataset revisions and data-lock status, the resolved
                                        sampling and request body, the judge, the `--from` source
jobs/<job>/dirty.diff                   with --allow-dirty: the diff the job ran with
jobs/<job>/images/<sha256>.<ext>        images referenced by trajectories (stored once)
jobs/<job>/<dataset>/trials_a<k>.jsonl  one trial per line (Harbor's TrialResult shape: the ATIF trajectory, usage,
                                        failure cause, generation lock and commit); append-only, frozen when
                                        complete
jobs/<job>/<dataset>/trials_a<k>.meta.json     dataset revision, task-id fingerprint, sha256 of the file
jobs/<job>/<dataset>/verifier/<id>/a<k>.jsonl  one verifier result per line (reward, extracted answer, judge reply)
jobs/<job>/<dataset>/verifier/<id>/meta.json   the judge and its settings, verifier lock, commit, changed tasks,
                                               sha256 of the verified trial files and of the result files
jobs/<job>/<dataset>/verifier/current   the id of the current verification
```

The job name defaults to the start time `YYYY-MM-DD__HH-MM-SS`. Trajectories contain no absolute paths or secrets.

## What is pinned, and how

| what | how |
|---|---|
| code | `sem run` starts only from a clean tree and records the commit; `--allow-dirty` stores the diff and marks the job dirty. All package modules are imported at start, so later edits or pulls do not affect a running process. The installed package version must name HEAD (after a pull: `uv sync --frozen`) |
| Python / host | a resume requires the same Python version and build, OS family and architecture; kernel and hostname are recorded only, so a resumed job may land on another node |
| libraries | installed versions must equal `uv.lock`, and the installed files of the client and verifier libraries must match their wheel RECORD; else the start is refused |
| settings | everything that changes outputs is in `config.json`; a resume requires an equal config except the runtime-only options (concurrency, retry, request timeout, verification settings), which are recorded per session. `.env` is read for secrets only |
| served engine | `sem serve` writes a server manifest (engine build, patches, launch args, model files, chat template, hardware; [Serving internals](serving.md)); its digest is part of the job: a different stack is a new job |
| API models | the endpoint (OpenRouter provider and quantization, the model identity the provider reports) is recorded; a change of model or provider within the job stops it. Alias model ids (most OpenRouter models) are allowed but marked "(alias)" |
| datasets | the revision is pinned in code (`-d name@ref` selects another), the data by `datasets.lock` ([Data locks](../datasets/data-locks.md)); the selected tasks' digest is compared on resume; verification checks every trial's task against the reloaded task (`--allow-task-change` verifies anyway and records the changed ids) |
| per request | the images really sent (after any downscaling), the max tokens sent, the seed (derived from the task id and attempt; `--model-arg seed=off`) |
| verifiers | rule checkers are seeded per task, nltk reads only the vendored data, code runs under CPU-time limits; the judge, its settings and the external artifacts a verifier reads are in the verifier lock |
| results | only `run` / `job resume` (trials) and `job regrade` (verifications) write; both record hashes, which `sem job check` verifies |

## Resume across commits

A job resumes in place (same directory and job id) at a later commit only when the change cannot affect it:

1. both trees are clean and the stored commit is an ancestor of HEAD;
2. everything else is unchanged: Python, libraries, endpoint, resolved settings, server manifest, data, config;
3. no file that affects generation changed for any of the job's datasets (the file sets below). Changes to
   verification only are allowed: those datasets are verified again with the new code at the end;
4. every stored trial would still be reused by `sem run --from` under the new code; otherwise the resume is refused
   and points to `--from`.

`sem job resume --dry-run` prints the decision (except the endpoint and server checks, which need the running
server). `sem job show` lists the sessions with their commits.

Which changed files count:

| set | files | on a change |
|---|---|---|
| dataset | the dataset's own module and what it imports, the data files it names, its datasets.lock entry | rerun / refuse |
| generation | the agent, trajectory and trial code, the dataset base class, the error policy, data sources, the sampler of the job's API format | rerun / refuse |
| verification | verifiers, metrics, the code sandbox, the shared artifacts the dataset uses | regrade |
| neutral | orchestration, CLI, storage, serving (the manifest digest is compared instead), presets (a change counts only when this model's resolved settings change), other datasets and API formats, everything outside the package (tests, docs, scripts, site) | nothing |

Any other changed file in the package counts as a generation change. The leaderboard requires every commit of a job
to be pushed and every session clean.

## Derived jobs (`sem run --from JOB`)

A derived job reuses an earlier job's trials where valid. For each (dataset, attempt, task):

| decision | when |
|---|---|
| REUSE | same task content, the same generation lock (rendered request, settings, endpoint or server, seed) and the same verifier: trial and verifier result copied |
| REGRADE | the generation is reusable but the verifier differs (judge, verification code, `--generate-only`, or the source was never verified): trial copied, verified in the new job |
| RERUN | no source trial, a transient source failure, changed task content, another generation lock, or the dataset's own or generation files changed since the trial was generated |

Copies record their source (`reused_from`), and lock.json records `derived_from` with the counts.
`--from SRC --dry-run` prints the counts per dataset and the reasons. Datasets graded at corpus level (OmniDocBench)
re-verify every trial.

## Failure causes

| cause | example | retry | counts in reward |
|---|---|---|---|
| budget_exhausted | finish_reason length, step limit | no | yes, 0 (cut-off answer) |
| refused | provider refusal, content filter | no | yes, 0 |
| context_overflow | input longer than the context | no | yes, 0 |
| request_rejected | the provider answered every retry of one request with an error while the job's other requests went through | no | yes, 0 |
| provider_failed (transient) | rate limit, 5xx, timeout | yes (`-r`) | no (excluded, `sem job resume -f provider_failed`) |
| endpoint_unavailable | OpenRouter "No endpoints found", the pinned provider gone, a served engine refusing connections after every retry, a model / provider change within the job | no | no (excluded; circuit breaker) |
| sandbox_failed | container start failure | yes | no |
| judge_failed | judge error, unparseable or cut-off verdict | yes | no |
| verifier_failed | the trajectory lacks what the verifier needs | no | no |

Retries follow Harbor's RetryConfig: `retry: {max_retries (-r), include_causes, exclude_causes,
abort_after_consecutive (the circuit breaker, default 20), connection_budget_s}` in the job config.
