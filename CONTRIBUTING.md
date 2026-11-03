# Contributing

Read [docs/design/v0.5.md](docs/design/v0.5.md) first: it explains what a job records and why.

## Adding a dataset

A dataset only describes data, prompts and verification; the framework owns generation, failure handling,
aggregation and the job files. A minimal dataset (this block is run by `tests/test_contributing.py`):

```python
# src/simple_evals_mm/tasks/mytask.py
from simple_evals_mm import sources, verifiers
from simple_evals_mm.dataset import Dataset, check_unique
from simple_evals_mm.models import Image, Task, user


def score(task: Task, response: str) -> tuple[float, str]:
    """(reward, extracted answer): the last line, compared case-insensitively."""
    lines = response.strip().splitlines()
    extracted = lines[-1].strip() if lines else ""
    return float(extracted.lower() == str(task.answer).lower()), extracted


class MyTask(Dataset):
    """org/my-task test: one image and a question, exact match of the last line (no judge)."""

    name = "mytask"
    revision = "0123456789abcdef0123456789abcdef01234567"  # the HF dataset commit (never a branch)
    file_fields = ("image",)  # task.data keys holding file paths: hashed by content in the task checksum
    verifier = verifiers.exact(score)

    def load(self) -> list[Task]:
        rows = sources.hf_dataset("org/my-task", self.revision, split="test")
        return check_unique([
            Task(id=str(ex["id"]), answer=ex["answer"], category=ex.get("category"),
                 data={"question": ex["question"], "image": sources.image_file(ex["image"], self.name)})
            for ex in rows])

    def prompt(self, task: Task):
        return [user(Image(task.data["image"]), task.data["question"] + "\nAnswer on the last line.")]
```

- **Ids**: use the dataset's own id; without one, `dataset.content_id(...)` + `dedupe_ids(...)`. `check_unique`
  refuses duplicates.
- **Images and files**: `sources.image_file()` writes each image once under `data/images/<dataset>/` (below the data
  root, the checkout holding `data/`) and returns the path; tasks hold paths, never bytes. Declare every `task.data`
  key that holds a path in `file_fields` (`"image"`, `"images"` for a list, `"turns.*.images"` inside a list of dicts):
  the task checksum hashes those files by content, relative paths resolve against the data root (not the working
  directory), and a missing file raises. `sem datasets lock` refuses task data with an undeclared file path.
- **Verification**: `verifiers.mcq(letters)` (option letter, judge as fallback; set `needs_judge = True`),
  `verifiers.judge()` (LLM judge with the shared template; `needs_judge = True`), `verifiers.own_judge(...)` (a
  benchmark's own judge prompt), `exact()` / `boxed()` / `click_in_box()` / `rules()` / `execute()`, or override
  `verify()`. Multi-turn datasets implement `next_turn()`; corpus-level ones `verify_all()` with their own metric.
- **Options** that change outputs are dataset args (`accepted_args = {"key": type}`, `--dataset-arg mytask.key=v`),
  never environment variables. `load()` returns all tasks; an option that selects tasks goes in `subset(tasks)`.
- **Data** from a source without an immutable revision (a plain URL) is fetched with `sources.fetch(url)` /
  `sources.download(url, dest)` / `sources.fetch_bytes(url)`, which check it against `datasets.lock`; CSVs are read
  with `sources.read_csv` (literal text, no NA / dtype guessing).

Register it in `DATASETS` in `src/simple_evals_mm/dataset.py` (`"mytask": "simple_evals_mm.tasks.mytask:MyTask"`) and
its upstream repo in `HF_REPOS` in `dataset_admin.py` (for `sem datasets check`). Then:

```bash
uv run --frozen sem datasets lock -d mytask                                 # its datasets.lock entry (commit the file)
uv run --frozen pytest tests/test_dataset_registry.py tests/test_datalock.py  # the contract every dataset must meet
uv run --frozen pytest tests/test_dataset_registry.py -m network -k mytask  # the real data loads and a task verifies
uv run --frozen sem run -m openrouter/qwen/qwen3.8-27b -d mytask -l 5
```

Add the dataset's prompts and rewards on a tiny in-memory source to `tests/test_datasets_*.py` as a golden snapshot
(`golden.check(...)`, then `uv run --frozen pytest <file> --update-golden` once and commit `tests/golden/`).

## Adding a model preset

Presets are data in `src/simple_evals_mm/samplers/presets.py`, keyed by the exact `provider/model` slug (no prefix
matching: a new model never inherits settings silently).

- **OpenRouter**: add the model to `OPENROUTER_MODELS` with its pinned provider, the recommended sampling of an
  open-weight model and the effort levels the endpoint accepts:

  ```python
  "qwen/qwen3.8-27b": {"provider": ["alibaba"], "sampling": QWEN38, "efforts": ("low", "medium", "xhigh"), "open_weight": True},
  ```

  Check it with `uv run --frozen python scripts/openrouter_smoke.py --models qwen/qwen3.8-27b` (one paid request per effort).
- **Served model** (`sglang/`, `vllm/`, `hf/`): add its Hugging Face id to `SERVED_MODELS` with a family from
  `FAMILIES` (or add a family: sampling, efforts, chat-template kwargs, reasoning parser). Every engine prefix gets it.
- **OpenAI / Azure / Gemini**: an entry in `PRESETS` built with `_family(...)`, with the documented `output_limit`.

A model id without an entry (an internal checkpoint) runs with `--preset FAMILY` or `--preset none`. `uv run --frozen sem
models list` shows the result; `tests/test_samplers.py` snapshots the request bodies of the main presets
(`tests/golden/samplers.json`).

## Before opening a pull request

```bash
uv run --frozen ruff check src tests
uv run --frozen pytest -m "not api and not local_model and not network"
```

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

## Parity with model cards

`references/model_cards.yaml` holds the scores the model cards report; `sem job parity JOB` puts a job's rewards
next to them:

```bash
uv run --frozen sem job parity jobs/qwen35-9b__default__b131072__c2   # table + checklist; --json; --strict exits 1 on an unexplained ⚠
```

**Adding a card.** One entry per model under `models:`: `slugs` (the exact served / OpenRouter slugs) and / or `hf_id`
(matches `sglang/` `vllm/` `hf/<hf_id>`), `source` (the card URL), `revision` (the card's commit), `retrieved` (date),
and `scores.<our dataset>`: `value` (points), `metric` (the card's benchmark name and footnotes; say "unknown" when the
card does not state the sub-metric), `setting` (the dataset settings the number corresponds to, e.g.
`{split: test}`; keys are `--dataset-arg` names, dataset config fields recorded in lock.json, `ref` or dataset class
attributes; the value `unknown` never matches), `comparable: false` + `reason` for another protocol or dataset,
`status` (ok | open | explained) and `link` (the PR / issue that explains or fixes a difference). Re-read the card for
every value; mark what is uncertain in `metric`.

**Reading the result.** Per dataset: `n/a` (not comparable, with the reason), `setting differs` (not compared), or
✓ / ⚠ with the tolerance `tol = min(5, max(2, 2 x SE_card))` points, where `SE_card = 100 x sqrt(p (1 - p) / n)` is
the binomial standard error of one run at the card's value (n = the dataset's task count). It is the expected noise of
the card's single run, never our observed spread, which a bug can inflate. Independently, `⚠ variance` flags an
observed std over attempts above twice the binomial std of one attempt at our mean. Datasets with non-0/1 rewards
(OmniDocBench, MMDU, MultiVerse) use a fixed 2-point tolerance and no variance flag.

**Investigating a ⚠.** Work through the printed checklist with the facts it shows (the job's truncation rate, the
settings and their sources): split / subset, dataset version, date window, protocol (prompt, CoT, tools, harness),
answer format (extraction, judge), sampling, effort, truncation (budget_exhausted count as 0), serving bug (engine,
chat template, image processing). For `⚠ variance`: non-deterministic grading, a flaky judge, serving instability, a
dataset / attempt mix-up. Record the outcome in the YAML: `status: explained` with the `link`, or fix the cause and
set `status: ok`.
