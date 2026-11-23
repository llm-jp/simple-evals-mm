# Data locks and snapshots

Two files in the repository pin what a dataset is, so that a result can be rerun and compared.

## `datasets.lock`: the data

`src/simple_evals_mm/datasets.lock.json` pins, per dataset, the tasks (a count and a digest) and every file the commit
cannot pin. A job checks its data against it before the first model call and records the result.

The data here equals the lock (exit 1 otherwise):

```bash
sem datasets lock --check
```

Rewrite one dataset's entry after an intended data change:

```bash
sem datasets lock -d mytask
```

Newer upstream revisions (pins are never moved automatically):

```bash
sem datasets check
```

### What the lock holds

| key | content |
|---|---|
| `datasets.<name>.revision` | the pinned source revision |
| `datasets.<name>.n_tasks` / `tasks_sha256` | the task count and digest over all the tasks the dataset loads (before `-l` / `-t` / `-i` / `-x` and dataset-arg selections such as LiveCodeBench's date window) |
| `datasets.<name>.sources` | url, sha256 and size of every file fetched from a source without an immutable revision (plain URLs such as the OpenAI blob CSVs; sources pinned by commit need none) |
| `_shared` | external artifacts: the nltk data of IFEval / IFBench, the OmniDocBench and MDPBench evaluator images |

A dataset arg that selects other data has its own entry, `name[key=value]` (e.g. `mathvision[split=test]`):

```bash
sem datasets lock -d mathvision --dataset-arg mathvision.split=test
```

- `sem run` compares the loaded data with the lock before any trial; a mismatch, or a dataset without an entry,
  refuses the job (`--allow-data-mismatch` runs anyway, recorded in the job; the leaderboard refuses such jobs).
  `--dry-run` reports the status without refusing.
- `sem job regrade` does not use the lock: it checks every trial's task against the reloaded task instead.
- A weekly workflow (`.github/workflows/datasets-check.yml`) runs `sem datasets check` (which also compares the
  locked files with upstream) and `sem datasets lock --check` on the small text datasets, both with `--markdown`.
  When something needs review it opens or updates the issue labelled `dataset-update`: a summary line ("Nothing to
  do" or "N dataset(s) need review"), a table of the sources that are not current (status, changed files, the action
  needed), the current ones collapsed, the unpinned datasets with their reason, the lock check's table and the raw
  log collapsed. When nothing needs review it closes the open issue with a comment.

The statuses of `sem datasets check`:

| status | meaning | action |
|---|---|---|
| ✅ current | the pin is the latest upstream revision, or the newer commits change only the card (README.md, .gitattributes) or files the dataset does not read | none |
| ⚠️ outdated | a newer commit changes a file the dataset reads, or a locked source file changed | review it; bump `revision` (new tasks / results) or hold it; for a source file, `sem datasets lock -d <name>` |
| ⏸ held | outdated, kept on purpose (`dataset_admin.HELD`, with the reason) | none |
| ❔ unpinned | no revision to compare (`revision_note` says why); the data is checked by `sem datasets lock` | none |
| ⏭ skipped | a gated repository and no `HF_TOKEN` | set the token |
| ❌ error | the lookup failed | rerun; fix the source if it persists |

The Markdown report alone:

```bash
sem datasets check --markdown
```

A dataset that reads only part of a repo shared with other datasets (one config or split) lists its files in
`dataset_admin.UPSTREAM_PATHS` (fnmatch globs relative to the repo root, from the card's `configs:` data_files):
`mechaja` reads `data/*`, `mechaja_v2_mini` `v2/testmini-*`, so a change to MECHA-ja v2 does not make `mechaja`
outdated.

## Dataset refs (`-d name@ref`)

A few datasets accept another revision of their source data; the others refuse `@`:

| datasets | ref |
|---|---|
| hakushobench, omnidocbench | a 40-digit Hugging Face dataset commit (branches / tags are refused: they move) |
| the JAMMEval subsets (heronbench, jdocqa, cvqaja, ...) | a GitLab commit whose shard hashes the code knows |
| livecodebench | `release_v1` .. `release_v6` |

A non-default ref has no datasets.lock entry: the job is not refused, and the revision and the selected tasks' digest
are still recorded and compared on resume.

## Golden snapshots: prompts and rewards

`tests/golden/` holds, per dataset, the prompts, request messages and rewards the tests produce on a tiny in-memory
source (a correct, a wrong and an unparseable answer, a judge saying yes / no). A change to a prompt, a request body
or a reward shows up as a diff of these files; it is reviewed and committed with `--update-golden`.

## What a release changes

```bash
sem datasets diff v0.5.8 v0.6.0
```

`sem datasets diff A B` lists, per dataset, what changed between two commits or tags. It reads only the two commits
(no download, no model call):

1. the files changed between them, sorted per dataset (its own code and data files) and into the shared generation /
   verification code;
2. **new**: the dataset's module is not in A;
3. **tasks changed**: its `datasets.lock` digest or task count differs; **source revision only**: the entry changed
   with the same tasks (e.g. a checker library);
4. **prompts changed** / **rewards changed**: the generation / reward part of its golden snapshot differs;
5. **code only**: files changed but the snapshots are equal (or it has none: check by hand).

A golden snapshot covers a few made-up tasks, and a dataset's default arguments are not compared; to decide whether
two actual jobs can be compared, `sem jobs compare` checks the prompts they really sent
([Compare results](../results/compare.md)).
