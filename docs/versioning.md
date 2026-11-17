# Versioning

Releases are git tags `vX.Y.Z` on `main`; every job records the commit and version it ran at (`lock.json`). This
policy applies **from v0.6.0**; the tags before it (v0.5.0 - v0.5.8) were cut without a fixed rule.

## While X = 0 (now)

The job format and the CLI may still change, so 0.y.z follows the Semantic Versioning convention for initial
development:

| Bump | When | Examples |
|---|---|---|
| **0.Y.0** | a large change or addition to the framework | the job file format, an incompatible CLI change, the error policy, a change to the requests of every dataset, the default judge |
| **0.y.Z** | datasets and fixes | a new benchmark, a dataset or verifier fix, a bug fix, a convenience feature |

## v1.0.0

The version becomes 1.0.0 when all of these hold, and stays 1.x while they do:

1. The job files (`config.json`, `lock.json`, trials, verifications) are frozen: every 1.x reads, resumes, regrades and
   compares the jobs of every earlier 1.x.
2. The arguments of the main commands (`run`, `job show / resume / regrade`, `jobs compare`, `serve`) are frozen.
3. Another team can run its evaluations from these docs alone.
4. Comparability is checked mechanically (`sem jobs compare`, `sem site build`).

After 1.0: **X** for a change that breaks one of these (old jobs no longer readable or resumable, an incompatible
CLI change, a change that makes every result incomparable such as the default judge); **Y** for new features and
changes that touch several datasets (a new agent or API format, the error policy); **Z** for datasets, fixes and
conveniences.

## A version number does not say two results are comparable

A patch release may fix one dataset's verifier; a minor one may leave a dataset untouched. Whether two jobs can be
compared is decided per dataset from what the jobs recorded, not from their versions:

- `sem jobs compare jobs/A jobs/B`: per dataset ✓ / ↻ / ? / ✗, for jobs of any versions
  ([Comparing results](concepts.md#comparing-results)).
- Each release lists the datasets it changes, made by `sem datasets diff <previous tag> <tag>`: new datasets, other
  tasks, a changed source revision, changed prompts or rewards (golden snapshots), code-only changes.

## Using a version

```bash
git clone --branch v0.6.0 https://github.com/llm-jp/simple-evals-mm.git   # or: git checkout v0.6.0
uv sync --frozen
```

A job runs only from a clean checkout whose installed package names HEAD (`sem run` checks it); after a
`git checkout` or `git pull`, run `uv sync --frozen` (`uv run --frozen` does it too). An interrupted job resumes at a
later version only when the change cannot affect its trials; otherwise `sem run --from JOB` continues it in a new job
and reuses the trials that are still valid.

## Release checklist (maintainers)

1. Tests: `uv run --frozen pytest -m "not api and not local_model and not network"` and `ruff check src tests`.
2. `bash scripts/release_check.sh` (every dataset end to end with the scripted provider, `datasets lock --check`, the
   leaderboard datasets' live smoke tests).
3. `uv run --frozen sem datasets diff <previous tag> HEAD` for the release notes; decide the bump with the table
   above.
4. Tag on `main` (`git tag -a vX.Y.Z -m "..."`, push), then a GitHub release with the notes and the dataset table.
