# Contributing

Read [docs/design/v0.5.md](docs/design/v0.5.md) first: it explains what a job records and why. The user docs are in
[docs/](docs/README.md).

## Development setup

```bash
uv sync --frozen                     # + --extra hf-server for `sem serve hf`
uv run --frozen pytest -m "not api and not local_model and not network"     # offline tests (fakes, golden snapshots)
uv run --frozen ruff check src tests
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

## Before opening a pull request

```bash
uv run --frozen ruff check src tests
uv run --frozen pytest -m "not api and not local_model and not network"
```

Keep the docs in step: a changed option or a new command belongs in `docs/` (`tests/test_docs.py` parses every
`sem ...` command in them and checks their links).

## Before a release

`scripts/release_check.sh` runs on a machine with the cached data (ABCI login node or a local checkout with `data/`;
not CI: it needs every dataset's data). From a clean, committed tree:

```bash
nice -n 19 bash scripts/release_check.sh      # RELEASE_CHECK_DIR=... to keep the jobs, RELEASE_CHECK_SKIP=a,b to leave datasets out
```

It uses the scripted test-only provider `fake/` (`src/simple_evals_mm/samplers/fake.py`, registered only with
`SEM_FAKE_PROVIDER=1`; no network, no paid call): `fake/scripted` answers every prompt deterministically (an
`Answer: X` line, `\boxed{}`, code, a click point or a short text) and, seeded by the trial, sometimes cuts off,
refuses, writes U+2028 / U+0085 / a lone surrogate, or fails once transiently; `fake/judge` answers in the format each
judge prompt asks for. On every registered dataset (`-l 3 -k 1`) it runs, through the real runner, store, verifiers
and derive code:

1. `sem run --generate-only`, killed partway (SIGKILL), then `sem job resume` and `sem job show` (every trial written);
2. `sem job regrade --judge-model fake/judge`, `sem job show` (every dataset verified), `sem job check`. HumanEval /
   HumanEval+ / LiveCodeBench get `allow_exec` when `bwrap` exists; OmniDocBench is verified only with
   `OMNIDOCBENCH_EVAL_SIF` and `apptainer`; otherwise they are generated, not verified, and listed as skipped;
3. `sem run --from` the job on the same commit: 0 RERUN expected;
4. `sem datasets lock --check`;
5. `pytest -m network` of the leaderboard datasets (`LEADERBOARD_DATASETS` to change the set).

It prints a PASS / FAIL / SKIP summary and exits 1 on any failure. The offline counterparts in CI are
`tests/test_fake_provider.py` (the same provider on in-memory datasets) and `tests/test_real_trials.py` (about 20
trimmed trial lines of a real job in `tests/golden/real_trials/`, rebuilt with its `build.py`).

Then follow the [release checklist](docs/versioning.md#release-checklist-maintainers).
