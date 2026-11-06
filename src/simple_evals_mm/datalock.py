"""datasets.lock (src/simple_evals_mm/datasets.lock.json): the canonical data digests, committed with the code.

The commit pins our code; this file pins the data the code reads. Per registered dataset:

Entry names: the dataset name, or `name[key=value,...]` for a dataset arg that selects other data (Dataset.data_args,
e.g. `mathvision[split=test]`): that data has its own n_tasks / tasks_sha256.

    revision       the dataset's pinned revision (dataset code)
    n_tasks        number of tasks load() returns (ALL tasks: before -l / -t / -i / -x and the dataset's own subset())
    tasks_sha256   dataset.tasks_sha256 over the sorted (task_id, task_checksum) pairs of those tasks (the job lock's
                   definition; files by content)
    sources        [{url, sha256, size}] of every file fetched from a source WITHOUT an immutable revision (OpenAI blob
                   CSVs, okvqa.allenai.org, the ScienceQA S3 zip, the DocVQA archive, the JAMMEval shards); Hugging Face
                   and git sources pinned by commit need none
    missing        instead of the above: why no entry could be built (gated without a token, ...)

and `_shared` entries for artifacts several datasets use (`nltk`: the vendored nltk data; `omnidocbench_eval_sif`: the
official OmniDocBench evaluator image).

Checks:
  * every fetched source file is compared with its entry when it is downloaded or used (sources.fetch / download /
    the JAMMEval shards): check_source()
  * after load(), the runner (and `sem run` preflight / --dry-run) compares n_tasks / tasks_sha256 / revision:
    check_dataset()
  * a mismatch, or a dataset without an entry (also `missing`), refuses the job with the dataset, expected vs got and
    the files, unless --allow-data-mismatch: the job then runs and lock.json datasets[name] records
    `data_lock: "mismatch"` and `data_mismatch` (the leaderboard build refuses such jobs)
  * `-d name@ref` with a non-default ref (or another revision) has no entry: `data_lock: "none"`, not refused

`sem datasets lock [-d ...]` rebuilds the entries (track(mode="write")), `--check` compares without writing.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from simple_evals_mm.errors import UserError

LOCK_PATH = Path(__file__).with_name("datasets.lock.json")
COMMITTED = LOCK_PATH  # the file in the package (LOCK_PATH may be redirected by tests)
FORMAT = "sem-datasets-lock/1"
_cache: dict | None = None


class DataMismatch(UserError, RuntimeError):
    """The data differs from datasets.lock (refused unless --allow-data-mismatch)."""


def read() -> dict:
    """The committed lock ({"format", "datasets", "_shared"}); an absent file reads as empty."""
    global _cache
    if _cache is None:
        _cache = json.loads(LOCK_PATH.read_text()) if LOCK_PATH.exists() else {"format": FORMAT, "datasets": {}, "_shared": {}}
    return _cache


def reset() -> None:
    """Forget the cached lock (tests, and after write())."""
    global _cache
    _cache = None


def write(lock: dict) -> None:
    lock = {"format": FORMAT, "datasets": dict(sorted(lock.get("datasets", {}).items())),
            "_shared": dict(sorted(lock.get("_shared", {}).items()))}
    tmp = LOCK_PATH.with_suffix(".json.part")
    tmp.write_text(json.dumps(lock, indent=1, ensure_ascii=False, sort_keys=False) + "\n")
    os.replace(tmp, LOCK_PATH)
    reset()


def entry(name: str) -> dict | None:
    return read().get("datasets", {}).get(name)


def shared(name: str) -> dict | None:
    return read().get("_shared", {}).get(name)


# ---------------------------------------------------------------- source files
@dataclass
class Recorder:
    """What one dataset's load() fetched, and the mismatches let through by --allow-data-mismatch."""

    dataset: str
    mode: str = "check"  # "check": compare with the lock; "write": record only (`sem datasets lock`)
    allow: bool = False
    fetched: dict = field(default_factory=dict)  # url -> {"sha256", "size"}
    mismatches: list = field(default_factory=list)


_CURRENT: contextvars.ContextVar[Recorder | None] = contextvars.ContextVar("datalock", default=None)


@contextlib.contextmanager
def track(dataset: str, mode: str = "check", allow: bool = False):
    """Record (and in check mode compare) the source files fetched while loading `dataset`."""
    rec = Recorder(dataset, mode, bool(allow))
    token = _CURRENT.set(rec)
    try:
        yield rec
    finally:
        _CURRENT.reset(token)


def expected_source(url: str) -> dict | None:
    """The locked {url, sha256, size} of a source file (any dataset's entry or a _shared artifact), else None."""
    lock = read()
    for e in [*lock.get("datasets", {}).values(), *lock.get("_shared", {}).values()]:
        for s in (e or {}).get("sources") or (e or {}).get("files") or []:
            if s.get("url") == url:
                return s
    return None


def entry_sources_with_prefix(prefix: str) -> list[dict]:
    """The locked source files whose URL starts with `prefix` (the shards of one JAMMEval subset)."""
    lock = read()
    return [s for e in lock.get("datasets", {}).values() for s in (e or {}).get("sources") or []
            if s.get("url", "").startswith(prefix)]


def check_source(url: str, sha256: str, size: int, what: str = "") -> None:
    """A fetched / used source file against datasets.lock. A file without an entry or with another sha256 raises
    DataMismatch unless the current track(allow=True) records it; track(mode="write") only records."""
    rec = _CURRENT.get()
    if rec is not None:
        rec.fetched[url] = {"sha256": sha256, "size": size}
        if rec.mode == "write":
            return
    exp = expected_source(url)
    if exp is not None and exp.get("sha256") == sha256:
        return
    problem = {"source": url, "expected": exp.get("sha256") if exp else None, "got": sha256, "size": size,
               "note": "not in datasets.lock" if exp is None else "sha256 differs"}
    if rec is not None and rec.allow:
        rec.mismatches.append(problem)
        print(f"[datalock] {rec.dataset}: {url}: {problem['note']} (allowed by --allow-data-mismatch, recorded)")
        return
    raise DataMismatch(f"{what or (rec.dataset if rec else 'data')}: source file {url} does not match datasets.lock "
                       f"({problem['note']}: expected {problem['expected']}, got {sha256}, {size} bytes). The upstream "
                       "file changed: review it, then `sem datasets lock -d <dataset>`; or run with --allow-data-mismatch "
                       "(recorded in lock.json)")


# ---------------------------------------------------------------- datasets
def digest(tasks, file_fields=()) -> dict:
    """n_tasks + tasks_sha256 of ALL loaded tasks (the job lock's definition); file_fields: the dataset's
    Dataset.file_fields (hashed by content)."""
    from simple_evals_mm.dataset import tasks_sha256
    from simple_evals_mm.models import task_checksum

    return {"n_tasks": len(tasks), "tasks_sha256": tasks_sha256((t.id, task_checksum(t, file_fields)) for t in tasks)}


def lock_key(ds) -> str:
    """The entry name of `ds`: the dataset name, or `name[key=value]` for a non-default data arg (MathVision's split)."""
    return getattr(ds, "lock_key", None) or ds.name


def lock_command(ds) -> str:
    key = lock_key(ds)
    return f"`sem datasets lock -d '{key}'`" if key != ds.name else f"`sem datasets lock -d {ds.name}`"


def is_default(ds) -> bool:
    """The dataset at its class-default revision (no `@ref`, no revision dataset arg): the lock applies."""
    return ds.ref is None and ds.revision == type(ds).revision


def compare(ds, got: dict, rec: Recorder | None = None) -> list[dict]:
    """Differences between the loaded data (`got` = digest()) and the lock entry of `ds`, plus the recorded source
    mismatches. [] = matches."""
    problems = list(rec.mismatches) if rec is not None else []
    key = lock_key(ds)
    e = entry(key)
    if e is None:
        which = "" if key == ds.name else f" {key}"
        return problems + [{"what": "entry", "note": f"no datasets.lock entry{which} ({lock_command(ds)})"}]
    if "missing" in e:
        return problems + [{"what": "entry", "note": f"no datasets.lock entry: {e['missing']}"}]
    for k, v in (("revision", ds.revision), ("n_tasks", got["n_tasks"]), ("tasks_sha256", got["tasks_sha256"])):
        if e.get(k) != v:
            problems.append({"what": k, "expected": e.get(k), "got": v})
    return problems


def format_problems(name: str, problems: list[dict]) -> str:
    out = []
    for p in problems:
        if "source" in p:
            out.append(f"source {p['source']}: {p['note']} (expected {p['expected']}, got {p['got']})")
        elif "expected" in p:
            out.append(f"{p['what']}: expected {p['expected']}, got {p['got']}")
        else:
            out.append(p["note"])
    return f"{name}: " + "; ".join(out)


def check_dataset(ds, tasks, rec: Recorder | None = None, allow: bool = False) -> dict:
    """lock.json datasets[name] fields after load(): {"data_lock": "ok" | "none" | "mismatch"[, "data_mismatch"]}.
    A mismatch (or no entry) raises DataMismatch unless `allow` (--allow-data-mismatch), which records the details."""
    if not is_default(ds):
        return {"data_lock": "none"}
    problems = compare(ds, digest(tasks, ds.file_fields), rec)
    if not problems:
        return {"data_lock": "ok"}
    if not allow:
        raise DataMismatch("the data differs from datasets.lock (--allow-data-mismatch runs anyway and records it):\n- "
                           + format_problems(ds.name, problems))
    print(f"[datalock] {format_problems(ds.name, problems)} (allowed by --allow-data-mismatch, recorded)")
    return {"data_lock": "mismatch", "data_mismatch": problems}


def preflight(ds, allow: bool = False) -> list[str]:
    """Before loading: a default-revision dataset needs a usable entry (else the job would be refused after load)."""
    if allow or not is_default(ds):
        return []
    key = lock_key(ds)
    e = entry(key)
    if e is None or "missing" in e:
        missing = f": {e['missing']}" if e else ""
        which = "" if key == ds.name else f" {key}"
        return [f"{ds.name}: no datasets.lock entry{which}{missing} (--allow-data-mismatch runs it anyway and records it)"]
    return []
