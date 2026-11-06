"""Job files (docs/design/v0.5.md "Files"). Only `sem run` / `sem job resume` (trials) and
`sem job regrade` (verifier results) write here.

    <jobs_dir>/<job>/config.json                          requested JobConfig (CLI + YAML, resolved defaults)
    <jobs_dir>/<job>/lock.json                            resolved replay inputs + job UUID (see checks / runner)
    <jobs_dir>/<job>/images/<sha256>.<ext>                images referenced by trajectories (stored once)
    <jobs_dir>/<job>/<dataset>/trials_a<k>.jsonl          one TrialResult per line (Harbor's shape, the ATIF trajectory
                                                          in agent_result.trajectory); append-only
    <jobs_dir>/<job>/<dataset>/trials_a<k>.meta.json      written when attempt k is complete (= frozen): sha256 of the
                                                          file, task-id fingerprint, dataset revision, usage
    <jobs_dir>/<job>/<dataset>/verifier/<verification-id>/a<k>.jsonl   one VerifierResult per line
    <jobs_dir>/<job>/<dataset>/verifier/<verification-id>/meta.json    judge + params, commit, sha256 of the verified
                                                                       trial files and of the result files
    <jobs_dir>/<job>/<dataset>/verifier/current           name of the current verification-id

The trial layout is behind write_trial() / iter_trials() (packed JSONL per attempt now; Harbor's one directory per
trial would only change these two functions). Harbor also has a config.json + lock.json per trial; here the pair
exists at job level only.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
from datetime import datetime

from simple_evals_mm.atif import ImageStore
from simple_evals_mm.errors import SetupError
from simple_evals_mm.models import TrialResult, VerifierResult

FORMAT = "sem-job/1"
DEFAULT_JOBS_DIR = "jobs"  # as in Harbor (the PBS scripts moved to pbs/)
_TRIALS_RE = re.compile(r"^trials_a(\d+)\.jsonl$")


def _local_now() -> datetime:
    return datetime.now()  # noqa: DTZ005 - local time, like Harbor's job names


def now_name() -> str:
    return _local_now().strftime("%Y-%m-%d__%H-%M-%S")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utf8(text: str) -> bytes:
    """UTF-8 of JSON text that never fails: a lone surrogate (only possible inside a JSON string) is written as its
    JSON escape \\udXXX (backslashreplace produces exactly that), so json.loads reads the same string back. Samplers
    already replace them (models.sanitize_response); this keeps any other path from losing a trial line."""
    return text.encode("utf-8", errors="backslashreplace")


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(_utf8(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"))
    os.replace(path + ".part", path)


def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_jsonl(path: str, rows) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.writelines(_utf8(json.dumps(r, ensure_ascii=False) + "\n") for r in rows)
    os.replace(path + ".part", path)


_WRITE_LOCK = threading.Lock()


def _trials_file(job_dir: str, dataset: str, attempt: int) -> str:
    return os.path.join(job_dir, dataset, f"trials_a{attempt}.jsonl")


def write_trial(job_dir: str, dataset: str, trial: TrialResult) -> None:
    """Append one trial (fsync'd). A completed attempt (meta written) is frozen: appending is refused."""
    path = _trials_file(job_dir, dataset, trial.attempt)
    line = _utf8(json.dumps(trial.to_dict(), ensure_ascii=False) + "\n")
    with _WRITE_LOCK:
        if os.path.exists(path[: -len(".jsonl")] + ".meta.json"):
            raise RuntimeError(f"{path} is complete (frozen): not appending")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def iter_trials(job_dir: str, dataset: str, attempt: int):
    """The trials of an attempt. A torn last line (process killed mid-write) is cut off, so appends stay valid."""
    path = _trials_file(job_dir, dataset, attempt)
    if not os.path.exists(path):
        return
    with _WRITE_LOCK:
        with open(path, "rb") as f:
            data = f.read()
        if data and not data.endswith(b"\n"):
            keep = data.rfind(b"\n") + 1
            print(f"[store] {path}: dropping a torn last line ({len(data) - keep} bytes)")
            with open(path, "r+b") as f:
                f.truncate(keep)
            data = data[:keep]
    # split on "\n" only: str.splitlines() also breaks on U+2028 / U+2029 / \x85 / \x0c ..., which json.dumps
    # (ensure_ascii=False) leaves raw inside strings, so a model answer containing one would cut its trial line in two
    for line in data.decode("utf-8").split("\n"):
        if line.strip():
            yield TrialResult.from_dict(json.loads(line))


class JobStore:
    def __init__(self, jobs_dir: str, job_name: str):
        self.jobs_dir, self.job_name = jobs_dir, job_name
        self.dir = os.path.join(jobs_dir, job_name)
        self.images = ImageStore(self.dir)

    @classmethod
    def from_path(cls, job_dir: str) -> JobStore:
        job_dir = os.path.normpath(job_dir)
        return cls(os.path.dirname(job_dir) or ".", os.path.basename(job_dir))

    @contextlib.contextmanager
    def lock(self):
        """Exclusive lock on the job for writers (run / resume / regrade): a second process on the same job stops
        instead of interleaving appends, truncating a line another process is writing, or rewriting config / lock."""
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, ".job.lock"), "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError(f"{self.dir} is in use by another sem process (run / resume / regrade)") from None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def exists(self) -> bool:
        return os.path.exists(os.path.join(self.dir, "config.json"))

    # ---- job files
    def read_config(self) -> dict:
        return _read_json(os.path.join(self.dir, "config.json"))

    def write_config(self, cfg: dict) -> None:
        _write_json(os.path.join(self.dir, "config.json"), cfg)

    def read_lock(self) -> dict:
        return _read_json(os.path.join(self.dir, "lock.json"))

    def write_lock(self, lock: dict) -> None:
        _write_json(os.path.join(self.dir, "lock.json"), lock)

    def datasets(self) -> list[str]:
        if not os.path.isdir(self.dir):
            return []
        return sorted(d for d in os.listdir(self.dir) if d not in ("images",) and not d.startswith(".") and os.path.isdir(os.path.join(self.dir, d)))

    # ---- trials
    def trials_path(self, ds: str, k: int) -> str:
        return os.path.join(self.dir, ds, f"trials_a{k}.jsonl")

    def meta_path(self, ds: str, k: int) -> str:
        return os.path.join(self.dir, ds, f"trials_a{k}.meta.json")

    def attempts(self, ds: str) -> list[int]:
        d = os.path.join(self.dir, ds)
        if not os.path.isdir(d):
            return []
        return sorted(int(m.group(1)) for f in os.listdir(d) if (m := _TRIALS_RE.match(f)))

    def is_complete(self, ds: str, k: int) -> bool:
        return os.path.exists(self.meta_path(ds, k))

    def read_meta(self, ds: str, k: int) -> dict | None:
        p = self.meta_path(ds, k)
        return _read_json(p) if os.path.exists(p) else None

    def read_trials(self, ds: str, k: int) -> list[TrialResult]:
        return list(iter_trials(self.dir, ds, k))

    def count_trials(self, ds: str, k: int) -> int:
        """Complete trial lines of attempt k (a torn last line is not counted), without parsing them."""
        p = self.trials_path(ds, k)
        if not os.path.exists(p):
            return 0
        with open(p, "rb") as f:  # one trial per line, never blank: count the newlines
            return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))

    def append_trial(self, ds: str, trial: TrialResult) -> None:
        write_trial(self.dir, ds, trial)

    def finalize_attempt(self, ds: str, k: int, *, revision, task_ids_sha256: str, n_tasks: int) -> dict:
        trials = self.read_trials(ds, k)
        costs = [t.usage.get("cost_usd") for t in trials if t.usage.get("cost_usd") is not None]
        meta = {
            "format": FORMAT, "dataset": ds, "attempt": k, "complete": True, "dataset_revision": revision,
            "task_ids_sha256": task_ids_sha256, "n_tasks": n_tasks, "sha256": sha256_file(self.trials_path(ds, k)),
            "usage": {"prompt_tokens": sum(t.usage.get("prompt_tokens", 0) for t in trials),
                      "completion_tokens": sum(t.usage.get("completion_tokens", 0) for t in trials),
                      "cost_usd": round(sum(costs), 6) if costs else None},
            "completed_at": _local_now().isoformat(timespec="seconds"),
        }
        _write_json(self.meta_path(ds, k), meta)
        return meta

    def drop_trials(self, ds: str, k: int, causes: set[str]) -> int:
        """Remove the trials whose failure cause is in `causes` and unfreeze the attempt (job resume -f)."""
        trials = self.read_trials(ds, k)
        keep = [t for t in trials if t.cause not in causes]
        if len(keep) == len(trials):
            return 0
        _write_jsonl(self.trials_path(ds, k), [t.to_dict() for t in keep])
        if os.path.exists(self.meta_path(ds, k)):
            os.remove(self.meta_path(ds, k))
        return len(trials) - len(keep)

    # ---- verifier results
    def verifier_dir(self, ds: str) -> str:
        return os.path.join(self.dir, ds, "verifier")

    def current_verification_id(self, ds: str) -> str | None:
        p = os.path.join(self.verifier_dir(ds), "current")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return f.read().strip() or None

    def new_verification_id(self, ds: str) -> str:
        base = now_name()
        gid, n = base, 1
        while os.path.exists(os.path.join(self.verifier_dir(ds), gid)):
            n += 1
            gid = f"{base}-{n}"
        return gid

    def write_verification_diff(self, ds: str, vid: str, diff: str) -> None:
        """`sem job regrade --allow-dirty`: the diff the verification ran with (its sha256 is in meta.json)."""
        with open(os.path.join(self.verifier_dir(ds), vid, "dirty.diff"), "w", encoding="utf-8") as f:
            f.write(diff)

    def write_verification(self, ds: str, vid: str, results: dict[int, list[VerifierResult]], meta: dict) -> None:
        d = os.path.join(self.verifier_dir(ds), vid)
        if os.path.exists(d):
            raise RuntimeError(f"verification id {vid} exists")
        files = {}
        for k, gs in sorted(results.items()):
            p = os.path.join(d, f"a{k}.jsonl")
            _write_jsonl(p, [g.to_dict() for g in gs])
            files[f"a{k}"] = {"sha256": sha256_file(p), "trials_sha256": sha256_file(self.trials_path(ds, k))}
        _write_json(os.path.join(d, "meta.json"), {"format": FORMAT, "verification_id": vid, **meta, "files": files,
                                                   "created_at": _local_now().isoformat(timespec="seconds")})
        cur = os.path.join(self.verifier_dir(ds), "current")
        with open(cur + ".part", "w", encoding="utf-8") as f:
            f.write(vid + "\n")
        os.replace(cur + ".part", cur)

    def read_verification_meta(self, ds: str, vid: str | None = None) -> dict | None:
        vid = vid or self.current_verification_id(ds)
        p = os.path.join(self.verifier_dir(ds), vid, "meta.json") if vid else None
        return _read_json(p) if p and os.path.exists(p) else None

    def read_verification(self, ds: str, vid: str | None = None) -> tuple[str | None, dict[int, list[VerifierResult]]]:
        vid = vid or self.current_verification_id(ds)
        if vid is None:
            return None, {}
        d = os.path.join(self.verifier_dir(ds), vid)
        out = {}
        for f in sorted(os.listdir(d)):
            m = re.match(r"^a(\d+)\.jsonl$", f)
            if m:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    out[int(m.group(1))] = [VerifierResult.from_dict(json.loads(line)) for line in fh if line.strip()]
        return vid, out

    # ---- verification
    def check(self) -> list[str]:
        """Problems found (empty = every hash matches): completed trial files vs their meta, the current verifier
        result files and the trial files they verified vs the verification meta."""
        problems = []
        if not self.exists():
            return [f"{self.dir}: no config.json"]
        if not os.path.exists(os.path.join(self.dir, "lock.json")):
            problems.append(f"{self.dir}: no lock.json")
        for ds in self.datasets():
            for k in self.attempts(ds):
                meta = self.read_meta(ds, k)
                if meta is None:
                    problems.append(f"{ds}/trials_a{k}.jsonl: incomplete (no meta)")
                elif sha256_file(self.trials_path(ds, k)) != meta.get("sha256"):
                    problems.append(f"{ds}/trials_a{k}.jsonl: sha256 differs from trials_a{k}.meta.json (edited)")
            vid = self.current_verification_id(ds)
            if vid:
                d = os.path.join(self.verifier_dir(ds), vid)
                gmeta = _read_json(os.path.join(d, "meta.json"))
                for name, h in gmeta.get("files", {}).items():
                    p = os.path.join(d, f"{name}.jsonl")
                    if not os.path.exists(p) or sha256_file(p) != h["sha256"]:
                        problems.append(f"{ds}/verifier/{vid}/{name}.jsonl: sha256 differs from meta.json (edited)")
                    tp = self.trials_path(ds, int(name[1:]))
                    if not os.path.exists(tp) or sha256_file(tp) != h["trials_sha256"]:
                        problems.append(f"{ds}/verifier/{vid}: verified {name} trials differ from the current trials file (regrade)")
        return problems
