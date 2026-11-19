# Contributing

Read [Concepts](docs/concepts.md) and [How reproducibility works](docs/reference/reproducibility.md) first: they
explain what a job records and why. The user docs are in [docs/](docs/README.md).

## Development setup

Install the dependencies (add `--extra hf-server` for `sem serve hf`):

```bash
uv sync --frozen
```

Offline tests (fakes, golden snapshots):

```bash
pytest -m "not api and not local_model and not network"
```

Lint:

```bash
ruff check src tests
```

## What to add, and where

| You want to add | Guide |
|---|---|
| a benchmark | [Adding a dataset](docs/datasets/adding-a-dataset.md) |
| a model (its recommended settings) | [Adding a model preset](docs/models/adding-a-preset.md) |
| a new request format (an API none of the samplers speaks) | [Adding an API format](docs/models/adding-an-api-format.md) |
| the scores a model card reports | [Model card parity](docs/models/model-cards.md) |

Rules that keep jobs reproducible (they are checked by the tests): everything that changes outputs is a CLI option,
a job config value, a `--model-arg` or a `--dataset-arg`, never an environment variable; a dataset pins its revision
and its data (`datasets.lock`); a change to a prompt, a request body or a reward shows up as a diff of
`tests/golden/`, reviewed before `--update-golden`. The full list is in `CLAUDE.md`.

## Tests

One test file per module, offline with shared fakes (`tests/fakes.py`: a scripted sampler, a toy dataset, the
`job_env` fixture that runs `sem` on a temporary jobs dir). The ones a contributor touches most:

| file | covers |
|---|---|
| test_datasets_mcq.py / _judge.py / _rules.py | per dataset family on tiny in-memory sources: prompts, judge prompts and rewards against golden snapshots (`tests/golden/`) |
| test_dataset_registry.py | the contract every registered dataset meets; `-m network`: each dataset's real data loads and one task verifies |
| test_datalock.py | `datasets.lock` covers every registered dataset; the data checks |
| test_samplers.py | slugs, presets, provider pins, request bodies (golden snapshots) |
| test_verifiers.py | verifiers, MCQ letter extraction |
| test_job.py, test_derive.py | run, resume (also across commits), regrade, `--from` |
| test_docs.py | every `sem ...` command in the docs parses, links exist, shell examples fit the page |

`pytest --update-golden` rewrites the snapshots a run touches after an intended change; the diff of `tests/golden/`
is the review. Live tests (`-m network`, `-m api`) stay opt-in.

## Before opening a pull request

Run the lint and the offline tests above. Keep the docs in step: a changed option or a new command belongs in
`docs/` (`tests/test_docs.py` parses every `sem ...` command in them and checks their links).

## Before a release

`scripts/release_check.sh` runs on a machine with the cached data (ABCI login node or a local checkout with `data/`;
not CI: it needs every dataset's data). From a clean, committed tree:

```bash
# RELEASE_CHECK_DIR=... to keep the jobs, RELEASE_CHECK_SKIP=a,b to leave datasets out
nice -n 19 bash scripts/release_check.sh
```

It runs every registered dataset (`-l 3 -k 1`) end to end with a scripted test-only provider (`fake/`, no network, no
paid call; its answers sometimes cut off, refuse or fail transiently): generation killed partway and resumed,
regrade, `sem job check`, `sem run --from` (0 reruns expected), then `sem datasets lock --check` and the network tests
of the leaderboard datasets. It prints a PASS / FAIL / SKIP summary and exits 1 on any failure.

Then follow the [release checklist](docs/versioning.md#release-checklist-maintainers).
