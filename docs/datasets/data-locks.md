# Data locks and snapshots

Two files in the repository pin what a dataset is, so that a result can be rerun and compared.

## `datasets.lock`: the data

`src/simple_evals_mm/datasets.lock.json` records, per dataset, the task count and a digest of all its tasks
(`tasks_sha256`), the pinned source revision, and the sha256 of every file fetched from a source without an immutable
revision (a plain URL). A job checks its data against it before the first model call and records the result.

```bash
uv run --frozen sem datasets lock --check          # the data here equals the lock (exit 1 otherwise)
uv run --frozen sem datasets lock -d mytask        # rewrite one dataset's entry after an intended data change
uv run --frozen sem datasets check                 # newer upstream revisions (pins are never moved automatically)
```

### What the lock holds

`src/simple_evals_mm/datasets.lock.json` (in the package, so it ships in the wheel) pins the data the commit cannot:

| key | content |
|---|---|
| `datasets.<name>.revision` | the dataset's pinned revision (dataset code). `<name>` is the dataset name, or `name[key=value]` for a dataset arg that selects other data (`Dataset.data_args`: `mathvision[split=test]`); that data has its own n_tasks / tasks_sha256, written with `sem datasets lock -d mathvision --dataset-arg mathvision.split=test` (or `-d 'mathvision[split=test]'`) |
| `datasets.<name>.n_tasks` / `tasks_sha256` | over ALL tasks load() returns (before `-l` / `-t` / `-i` / `-x` and the dataset-arg selection `Dataset.subset()`, e.g. livecodebench's date window): `dataset.tasks_sha256` of the sorted (task id, task_checksum) pairs, the job lock's definition |
| `datasets.<name>.sources` | `{url, sha256, size}` of every file fetched from a source without an immutable revision: the OpenAI blob CSVs (gpqa, mmlu, math, simpleqa), the okvqa.allenai.org zips, the ScienceQA S3 image zip, the DocVQA archive, the JAMMEval shards (moved here from sources.py). Hugging Face and GitHub sources pinned by commit need none (ChartQA, MMDU and the ScienceQA json files are pinned by commit) |
| `datasets.<name>.missing` | instead of the above: why the entry could not be built (`sem datasets lock` records the error) |
| `_shared.nltk` | the nltk_data commit and `{url, sha256, size}` of each package zip (punkt, punkt_tab, stopwords, averaged_perceptron_tagger_eng) |
| `_shared.omnidocbench_eval_sif` | `{image, sha256, size}` of the OmniDocBench evaluator `.sif` |

Checks:
- Unversioned files are fetched with `sources.fetch` (cached in `data/_sources/`, checked on download and every use),
  `sources.download` (to a path, removed on a mismatch) or `sources.fetch_bytes`; JAMMEval shards are checked at every
  load (hashed once per process). A file without an entry or with another sha256 raises `DataMismatch`.
- `sem run`: the preflight refuses a default-revision dataset without an entry (or with `missing`); right after load,
  before any trial, n_tasks / tasks_sha256 / revision are compared (`runner.load_checked`). A mismatch refuses the job
  with the dataset, expected vs got and the files. `--allow-data-mismatch` (runtime-only, recorded per session) runs
  anyway and records lock.json `datasets[name].data_lock: "mismatch"` + `data_mismatch` (kept on resume); otherwise
  `data_lock: "ok"`, or `"none"` for a non-default ref. The leaderboard build refuses mismatched jobs.
  `--dry-run` reports the status without refusing. Verification (`sem job regrade`) does not refuse: the per-trial
  task_checksum comparison decides there.
- A dataset without an entry is refused like a mismatch: an unlocked dataset is not reproducible, so running it is an
  explicit, recorded choice (`--allow-data-mismatch`).
- `sem datasets lock [-d ...]` loads (downloading if needed) each dataset and rewrites its entry; for sources whose
  data was built earlier (`Dataset.unpinned_sources()`: the vqa_data inputs) the existing digest is kept, else the
  upstream file is streamed and hashed. `--check` compares without writing (exit 1 on a difference; a `missing` entry
  is reported, not a failure). With all datasets (or `--shared`) the `_shared` artifacts are fetched / hashed too.
- The weekly workflow runs `sem datasets check`: the HF / GitLab / git pins as before, plus every locked unpinned file
  against upstream (sha256 up to `--max-download-mb`, default 300; bigger files by HEAD Content-Length / ETag), and
  `sem datasets lock --check` on the small ungated text datasets.

Vendored nltk data: `sem datasets download -d ifeval` (or ifbench) fetches the zips of the pinned nltk_data commit
(`sources.NLTK_COMMIT`) into `data/nltk_data/` and extracts them. `sources.use_nltk_data()` sets `nltk.data.path` to
that directory only and replaces `nltk.download` with a no-op before the checkers are imported (the vendored ifeval_lib
no longer downloads at import; ifbench's import-time download becomes a no-op and its own directory is removed from the
path again). The IFEval / IFBench preflight (when verifying) requires the packages to match `_shared.nltk`; their
digests are in the verifier_lock (`nltk_data`).

OmniDocBench evaluator: the `.sif` at OMNIDOCBENCH_EVAL_SIF is hashed once per file version (`sources.sha256_persistent`:
cache in `~/.cache/simple-evals-mm/sha256.json` by path + size + mtime) and must equal `_shared.omnidocbench_eval_sif`:
checked in `preflight(verify=True)` and before every evaluator run; its digest is in the verifier_lock.

## Dataset refs (`-d name@ref`)

Datasets declare `accepts_ref` and how a ref maps to their loader; the others refuse `@`:

| kind | datasets | ref |
|---|---|---|
| hf_revision | hakushobench, omnidocbench (= `--dataset-arg revision`, not both) | a 40-digit HF dataset commit (branches / tags refused: they move) |
| jammeval_revision | the JAMMEval subsets (heronbench, jdocqa, cvqaja, ...) | the pinned GitLab commit (shards in datasets.lock) or one with a shard sha256 table in `sources.JAMMEVAL_SHA256_BY_REVISION` (another directory `JAMMEVAL_DIR@<rev>`) |
| lcb_release | livecodebench | `release_v1` .. `release_v6`: the files of that release at the pinned commit (`$LCB_DATA@release_vN`) |

The entry stays `name@ref` in the job config; lock.json `datasets[name]` has the resolved `revision` and `ref`
({given, kind, revision, class_default, ...}); both are compared on resume. `sem datasets check` reports the class
default vs upstream as before. A non-default ref has no datasets.lock entry: lock.json records `data_lock: "none"`
and the job is not refused (the revision and the selected tasks' digest are still recorded and compared on resume).

## Golden snapshots: prompts and rewards

`tests/golden/` holds, per dataset, the prompts, request messages and rewards the tests produce on a tiny in-memory
source (a correct, a wrong and an unparseable answer, a judge saying yes / no). A change to a prompt, a request body
or a reward shows up as a diff of these files; it is reviewed and committed with `--update-golden`.

## What a release changes

```bash
uv run --frozen sem datasets diff v0.5.8 v0.6.0
```

`sem datasets diff A B` lists, per dataset, what changed between two commits or tags. It reads only the two commits
(no download, no model call):

1. the files changed between them (`git diff`), sorted per dataset by what the dataset's module imports (its own files
   and the data files it names) and into the shared generation / verification code;
2. **new**: the dataset's module is not in A;
3. **tasks changed**: its `datasets.lock` digest or task count differs; **source revision only**: the entry changed
   with the same tasks (e.g. a checker library);
4. **prompts changed** / **rewards changed**: the generation / reward part of its golden snapshot differs;
5. **code only**: files changed but the snapshots are equal (or it has none: check by hand).

A golden snapshot covers a few made-up tasks, and a dataset's default arguments are not compared; to decide whether
two actual jobs can be compared, `sem jobs compare` checks the prompts they really sent
([Compare results](../results/compare.md)).
