"""`sem convert`: audit a v0.4 run (runs/<run_id>/) and convert it into a v0.5 job marked legacy
(docs/design/v0.5.md "Converting v0.4 runs").

The v0.4 run is only read. The audit comes first and is a list of structured findings:

  history     <task>/history.jsonl replayed against the result files (runs.verify_history): ok / no history / untracked
  commit      the commit of the code repository whose task module and common.py hash to the provenance's
              task_module_sha256 / common_sha256 (searched over the history of those two files), or none; code_dirty
  config      every repeat's score_r<k>.json (model, sampler config, request params, eval config, flags, provider
              served) agrees with the other repeats and with config.json
  ids         every v0.4 item maps to exactly one v0.5 task of the dataset at its pinned revision (by id, by row, CharXiv
              by position, MMDU / MultiVerse turns `<id>#t<k>` grouped into one dialogue)
  aggregates  score_r<k>.json / summary.json recomputed from the rows; stale stored values are listed
  prompt      the v0.5 rendering of a task vs the v0.4 row's question (informational)
  regrade     (a) the v0.4 scores as the `legacy` verification, (b) the v0.5 verifiers on the converted trials; judge
              verdicts come from the judge cache (the v0.4 grader reply of the identical prompt and the same judge
              model) or from the judge (never with --judge-cache-only: judge_not_cached)

Then the job `<jobs_dir>/<run_id>__legacy/`: config.json (v0.5 options mapped from the v0.4 config), lock.json
(`legacy: true`, the audit, the classification, the commit found, the v0.4 serving / behavior env as recorded,
`code.checked: false`), one TrialResult per v0.4 row (per dialogue for MMDU / MultiVerse) with the ATIF trajectory
rendered by the v0.5 Dataset.prompt() (user steps `extra.reconstructed: true`: v0.4 did not store what was sent),
`generation_lock: null` and `reused_from: {v04_run, item_ids, repeat}`, and the verifications.

Classification: `legacy-verified` when the histories verify, a commit is found, the configs agree, the ids map 1:1
and the regrade agrees (no flip for deterministic verifiers, flips <= max_judge_flips of the compared trials for
judge-verified datasets, every verdict regraded); else `legacy-unverified` with the reasons.
"""

from __future__ import annotations

import ast
import concurrent.futures
import contextlib
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from simple_evals_mm import atif, errors
from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm import store as store_mod
from simple_evals_mm.agent import Direct
from simple_evals_mm.models import Response, TrialResult, VerifierResult, task_checksum

LEGACY_SUFFIX = "__legacy"
V04_COMMON = "src/simple_evals_mm/common.py"
VERIFIED, UNVERIFIED = "legacy-verified", "legacy-unverified"
DEFAULT_MAX_JUDGE_FLIPS = 0.01
LEGACY_VID = "legacy"

# v0.4 task ids that are not the v0.5 task id (docs/design/v0.5.md "Task ids that changed vs v0.4")
ROW_DATASETS = ("realworldqa", "countbenchqa", "mmlu", "math", "simpleqa", "gpqa", "simpleqa_verified", "scienceqa")
TURN_DATASETS = ("mmdu", "multiverse")  # one v0.4 row per turn `<dialogue id>#t<k>`, one v0.5 trial per dialogue

# v0.4 grader names -> the v0.5 judge slugs of the same model ("Model names")
V04_JUDGES = {
    "gpt-5.1-2025-11-13": ("openrouter/openai/gpt-5.1", "openai/gpt-5.1-2025-11-13", "azure/gpt-5.1-2025-11-13"),
    "gpt-4o-2024-11-20": ("openrouter/openai/gpt-4o-2024-11-20", "openai/gpt-4o-2024-11-20", "azure/gpt-4o-2024-11-20"),
    "gemini-3.1-pro-preview": ("gemini/gemini-3.1-pro-preview", "vertex/gemini-3.1-pro-preview"),
}
# behavior_env / env names of v0.4 that became job options
_SAMPLING_ENV = {"temperature": ("OPENROUTER_TEMP", "SGLANG_TEMP", "SWEEP_TEMP"),
                 "top_p": ("OPENROUTER_TOP_P", "SGLANG_TOP_P", "SWEEP_TOP_P"),
                 "top_k": ("OPENROUTER_TOP_K", "SGLANG_TOP_K", "SWEEP_TOP_K")}
_POLICY_NOTES_PREFIX = ("cut off at the token limit", "request refused by the provider", "input longer than the model")


class ConvertError(RuntimeError):
    pass


class JudgeNotCached(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _read_json1(path: str):
    """score_r / summary files: one JSON object (v0.4 wrote it on one line)."""
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read() or "{}")


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _num(v):
    """A stored score / count as a float (None stays None; v0.4 sometimes wrote numbers as strings)."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _close(a, b, tol: float = 1e-9) -> bool:
    a, b = _num(a), _num(b)
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


# ================================================================ git: the commit of the v0.4 code
class Git:
    """Read-only queries of the code repository (git log / show / rev-parse), cached."""

    def __init__(self, repo: str | None):
        self.repo = repo
        self._blob: dict[tuple[str, str], str | None] = {}
        self._text: dict[str, str | None] = {}
        self._log: dict[tuple, list[str]] = {}
        self._found: dict[tuple, dict] = {}

    def _git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", self.repo, *args], text=True, stderr=subprocess.DEVNULL)

    def ok(self) -> bool:
        if not self.repo:
            return False
        try:
            self._git("rev-parse", "--git-dir")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
            return False

    def blob(self, commit: str, path: str) -> str | None:
        key = (commit, path)
        if key not in self._blob:
            try:
                self._blob[key] = self._git("rev-parse", f"{commit}:{path}").strip()
            except subprocess.CalledProcessError:
                self._blob[key] = None
        return self._blob[key]

    def text(self, commit: str, path: str) -> str | None:
        b = self.blob(commit, path)
        if b is None:
            return None
        if b not in self._text:
            try:
                self._text[b] = self._git("cat-file", "blob", b)
            except subprocess.CalledProcessError:
                self._text[b] = None
        return self._text[b]

    def sha16(self, commit: str, path: str) -> str | None:
        t = self.text(commit, path)
        return None if t is None else _sha16(t.encode("utf-8"))

    def log(self, *paths: str) -> list[str]:
        """Commits (oldest first, every ref) that changed any of the paths: the states the files went through."""
        if paths not in self._log:
            try:
                out = self._git("log", "--all", "--format=%H", "--", *paths).split()
            except subprocess.CalledProcessError:
                out = []
            self._log[paths] = out[::-1]
        return self._log[paths]

    def resolve(self, rev: str | None) -> str | None:
        if not rev:
            return None
        try:
            return self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip() or None
        except subprocess.CalledProcessError:
            return None

    def find_commit(self, task_module: str, task_sha: str | None, common_sha: str | None) -> dict:
        """The commit whose task module hashes to task_sha and whose common.py hashes to common_sha (sha256, first 16
        hex digits as runs.task_provenance stores them). Candidates are the commits that changed either file: between
        two of them both files keep their content, so these cover every state. Returns {"commit" (the oldest
        match), "n_matching", "task_found", "common_found"}."""
        key = (task_module, task_sha, common_sha)
        if key in self._found:
            return self._found[key]
        paths = tuple(p for p, s in ((task_module, task_sha), (V04_COMMON, common_sha)) if p and s)
        out = {"commit": None, "n_matching": 0, "task_found": False, "common_found": False}
        if not paths or not self.ok():
            self._found[key] = out
            return out
        matching = []
        for c in self.log(*paths):
            t_ok = task_sha is None or self.sha16(c, task_module) == task_sha
            c_ok = common_sha is None or self.sha16(c, V04_COMMON) == common_sha
            out["task_found"] |= bool(task_sha) and t_ok
            out["common_found"] |= bool(common_sha) and c_ok
            if t_ok and c_ok:
                matching.append(c)
        if task_sha and not out["task_found"]:  # the task file alone, over its own history
            out["task_found"] = any(self.sha16(c, task_module) == task_sha for c in self.log(task_module))
        if common_sha and not out["common_found"]:
            out["common_found"] = any(self.sha16(c, V04_COMMON) == common_sha for c in self.log(V04_COMMON))
        out["commit"], out["n_matching"] = (matching[0] if matching else None), len(matching)
        self._found[key] = out
        return out

    def segment(self, commit: str, path: str, names: tuple | None) -> str | None:
        """The file at a commit (names None), or the source of its top-level definitions `names` (ast-normalized)."""
        t = self.text(commit, path)
        return None if t is None else source_segment(t, names)

    def history_segments(self, path: str, names: tuple | None) -> set[str]:
        """Every distinct value the file / definitions had over the repository history."""
        return {s for c in self.log(path) if (s := self.segment(c, path, names)) is not None}


def source_segment(text: str, names: tuple | None) -> str | None:
    if names is None:
        return text
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    segs = {}
    for node in tree.body:
        n = getattr(node, "name", None)
        if n is None and isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
        if n in names:
            segs[n] = ast.unparse(node)
    return "\n".join(segs.get(n, f"<missing {n}>") for n in names)


def default_repo(run_dir: str) -> str | None:
    """The git checkout the v0.4 run directory lives in (runs/ sits in the repository that produced it), else this
    package's checkout."""
    for start in (os.path.dirname(os.path.abspath(run_dir)), os.path.dirname(os.path.abspath(__file__))):
        try:
            return subprocess.check_output(["git", "-C", start, "rev-parse", "--show-toplevel"], text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
            continue
    return None


# ================================================================ reading the v0.4 run
@dataclass
class V04Task:
    name: str
    dir: str
    repeats: dict = field(default_factory=dict)  # k -> {"rows": [...], "score": dict | None}
    incomplete: list = field(default_factory=list)  # k with partial_r<k>.jsonl and no items_r<k>.jsonl
    summary: dict | None = None
    grader: str | None = None  # the last grader of grades.jsonl

    def provenance(self, k: int) -> dict:
        return ((self.repeats.get(k) or {}).get("score") or {}).get("provenance") or {}


@dataclass
class V04Run:
    run_id: str
    dir: str
    config: dict
    tasks: dict  # name -> V04Task


def read_run(run_dir: str, only: list[str] | None = None) -> V04Run:
    run_dir = os.path.normpath(run_dir)
    cfg_path = os.path.join(run_dir, "config.json")
    if not os.path.exists(cfg_path):
        raise ConvertError(f"{run_dir}: no config.json (not a v0.4 run directory)")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    tasks = {}
    for name in sorted(os.listdir(run_dir)):
        d = os.path.join(run_dir, name)
        if not os.path.isdir(d) or (only and name not in only):
            continue
        items = glob.glob(os.path.join(d, "items_r*.jsonl"))
        partial = glob.glob(os.path.join(d, "partial_r*.jsonl"))
        if not items and not partial:
            continue
        t = V04Task(name, d)
        for p in items:
            k = int(re.search(r"items_r(\d+)\.jsonl$", p).group(1))
            sp = os.path.join(d, f"score_r{k}.json")
            t.repeats[k] = {"rows": _read_jsonl(p), "score": _read_json1(sp) if os.path.exists(sp) else None}
        for p in partial:
            m = re.search(r"partial_r(\d+)\.jsonl$", p)
            if m and int(m.group(1)) not in t.repeats:
                t.incomplete.append(int(m.group(1)))
        sp = os.path.join(d, "summary.json")
        t.summary = _read_json1(sp) if os.path.exists(sp) else None
        gp = os.path.join(d, "grades.jsonl")
        if os.path.exists(gp):
            graded = [g for g in _read_jsonl(gp) if g.get("grader")]
            t.grader = graded[-1]["grader"] if graded else None
        tasks[name] = t
    return V04Run(os.path.basename(run_dir), run_dir, cfg, tasks)


def v04_judge(t: V04Task, cfg: dict) -> str | None:
    """The grader of the stored verdicts: grades.jsonl, else score_r judge_model_name, else the CLI's."""
    if t.grader:
        return t.grader
    for k in sorted(t.repeats):
        j = (t.repeats[k]["score"] or {}).get("judge_model_name")
        if j:
            return j
    return (cfg.get("cli") or {}).get("grader_model")


def same_judge(v04_name: str | None, slug: str) -> bool:
    if not v04_name:
        return False
    return slug in V04_JUDGES.get(v04_name, (v04_name, f"openrouter/{v04_name}"))


# ================================================================ the v0.5 job config from the v0.4 config
def v05_slug(cfg: dict) -> tuple[str | None, list[str]]:
    """The v0.5 slug of the v0.4 model ("Model names"): the provider from the v0.4 backend / sampler class."""
    path = (cfg.get("model") or {}).get("path") or ""
    cls = cfg.get("sampler_class") or ""
    backend = cfg.get("backend") or (cfg.get("serving") or {}).get("backend")
    notes = []
    if path.startswith("openrouter/"):
        return path, notes
    if path in ("", "dummy"):
        return None, ["dummy / unknown model: no v0.5 slug"]
    if path.startswith(("gpt-4o", "gpt-5")):
        if backend == "openrouter" or "OpenRouter" in cls:
            return "openrouter/openai/" + ("gpt-5.1" if path.startswith("gpt-5.1") else path), notes
        return ("azure/" if "Azure" in cls else "openai/") + path, notes
    if path.startswith("gemini-"):
        return ("vertex/" if "Vertex" in cls else "gemini/") + path, notes
    if "Muse-Glimmer" in path or "vllm" in cls.lower():
        engine = "vllm"
    elif backend == "sglang":
        engine = "sglang"
    elif backend == "hf_server":
        engine = "hf"
    else:
        engine = "hf"
        notes.append(f"v0.4 backend {backend!r} (in-process / unknown): mapped to the hf server")
    return f"{engine}/{path}", notes


def job_config(run: V04Run, datasets: list[str], n_attempts: int, jobs_dir: str, judge_model: str) -> tuple[dict, list[str]]:
    from simple_evals_mm import runner

    cfg = run.config
    slug, notes = v05_slug(cfg)
    env = {**(cfg.get("env") or {}), **(cfg.get("behavior_env") or {})}
    effort = cfg.get("reasoning_effort")
    if effort is None and str(cfg.get("effective_model_name") or "").endswith("_think"):
        effort = "high"
        notes.append("v0.4 thinking template (effective model name *_think) -> effort high")
    out = {**runner.DEFAULTS, "model": slug, "datasets": list(datasets), "job_name": run.run_id + LEGACY_SUFFIX,
           "jobs_dir": jobs_dir, "n_attempts": n_attempts, "effort": effort, "max_output_tokens": cfg.get("max_new_tokens"),
           "judge_model": judge_model, "n_concurrent": (cfg.get("cli") or {}).get("eval_threads") or runner.DEFAULTS["n_concurrent"]}
    for opt, names in _SAMPLING_ENV.items():
        for n in names:
            if env.get(n) not in (None, ""):
                out[opt] = float(env[n]) if opt != "top_k" else int(float(env[n]))
                break
    if env.get("OPENROUTER_PROVIDER"):
        out["provider"] = env["OPENROUTER_PROVIDER"]
    if env.get("OPENROUTER_QUANT"):
        out["quantization"] = env["OPENROUTER_QUANT"]
    margs = {}
    for v, k in (("OPENROUTER_SAMPLING", "sampling"), ("OPENROUTER_MAX_IMAGE_PX", "max_image_px"),
                 ("GEMMA_MAX_SOFT_TOKENS", "max_soft_tokens"), ("MUSE_MAX_IMAGE_TOKENS", "max_image_tokens"),
                 ("HARMONY_DATE", "date"), ("SGLANG_PRESENCE", "presence_penalty"), ("REP_PEN", "repetition_penalty")):
        if env.get(v) not in (None, ""):
            margs[k] = env[v]
    if env.get("QWEN_DECODE") == "1":
        margs["decode"] = "qwen"
    out["model_args"] = margs
    dargs: dict = {}
    if "livecodebench" in datasets:
        for v, k in (("LCB_START", "start"), ("LCB_END", "end")):
            if env.get(v):
                dargs.setdefault("livecodebench", {})[k] = env[v]
    if "omnidocbench" in datasets:
        for v, k in (("OMNIDOCBENCH_REVISION", "revision"), ("OMNIDOCBENCH_MAX_PRED_CHARS", "max_pred_chars")):
            if env.get(v):
                dargs.setdefault("omnidocbench", {})[k] = int(env[v]) if k == "max_pred_chars" else env[v]
    out["dataset_args"] = dargs
    if (cfg.get("cli") or {}).get("cot"):
        notes.append("v0.4 --cot: no v0.5 option (gpt-5.1 / Gemini: --effort medium)")
    if slug:
        try:
            from simple_evals_mm.samplers import registry

            registry.find_preset(*registry.parse_slug(slug), None)
        except Exception as e:  # noqa: BLE001 - recorded, the legacy job never runs
            notes.append(f"{slug}: {e}"[:300])
    out["legacy"] = {"v04_run": run.run_id, "converted_by": "sem convert"}
    return out, notes


# ================================================================ id mapping
def map_items(name: str, rows: list[dict], tasks: list) -> tuple[list[tuple], list[str]]:
    """[(task, [row indices])] in v0.4 row order and the problems (unmapped / duplicate / out-of-order items).
    Every v0.4 item maps to exactly one v0.5 task; MMDU / MultiVerse rows `<id>#t<k>` of one dialogue form one group."""
    problems: list[str] = []
    by_id = {t.id: t for t in tasks}
    by_row = {t.data.get("row"): t for t in tasks if t.data.get("row") is not None}
    groups: dict[str, list[int]] = {}
    order: list[str] = []

    def add(task, i):
        if task.id not in groups:
            groups[task.id] = []
            order.append(task.id)
        groups[task.id].append(i)

    if name in TURN_DATASETS:
        for i, r in enumerate(rows):
            base, sep, k = str(r.get("id")).rpartition("#t")
            t = by_id.get(base) if sep and k.isdigit() else None
            if t is None:
                problems.append(f"item {r.get('id')!r}: no v0.5 task")
                continue
            add(t, i)
        for tid in order:
            ks = [int(str(rows[i]["id"]).rpartition("#t")[2]) for i in groups[tid]]
            if ks != list(range(1, len(ks) + 1)):
                problems.append(f"dialogue {tid}: turns {ks} are not 1..n in order")
        return [(by_id[tid], groups[tid]) for tid in order], problems

    if name == "charxiv":
        if len(rows) != len(tasks):
            problems.append(f"{len(rows)} v0.4 items vs {len(tasks)} v0.5 tasks: position mapping needs the same filter")
        for i, r in enumerate(rows[: len(tasks)]):
            t = tasks[i]
            arxiv = str(r.get("id")).split("#", 1)[0]
            if str(t.data.get("original_id")) != arxiv:
                problems.append(f"item {i} {r.get('id')!r}: v0.5 task {t.id} at that position has original_id "
                                f"{t.data.get('original_id')!r}")
                continue
            add(t, i)
        for i in range(len(tasks), len(rows)):
            problems.append(f"item {i} {rows[i].get('id')!r}: no v0.5 task at that position")
        return [(by_id[tid], groups[tid]) for tid in order], problems

    for i, r in enumerate(rows):
        rid = r.get("id")
        t = None
        if name == "mmlu_redux":
            subj, _, row = str(rid).rpartition("/")
            t = next((x for x in tasks if x.data.get("subject") == subj and str(x.data.get("row")) == row), None) \
                if row.isdigit() else None
        elif name == "chartqapro":
            head = str(rid).split("#", 1)[0]
            t = by_row.get(int(head)) if head.isdigit() else None
        elif name in ROW_DATASETS:
            if rid is None and name == "scienceqa":
                t = by_row.get(i)  # v0.4 stored no id: the row's position (full runs)
            elif str(rid).isdigit():
                t = by_row.get(int(rid))
            else:
                t = by_id.get(str(rid))
        else:
            t = by_id.get(str(rid))
        if t is None:
            problems.append(f"item {rid!r}: no v0.5 task")
        elif t.id in groups:
            problems.append(f"item {rid!r}: maps to task {t.id} already mapped from item "
                            f"{rows[groups[t.id][0]].get('id')!r}")
        else:
            add(t, i)
    return [(by_id[tid], groups[tid]) for tid in order], problems


# ================================================================ trajectories
class _HashOnlyImages(atif.ImageStore):
    """--dry-run: the image references of a trajectory without writing files."""

    def put(self, path=None, data=None, media_type=None):
        if path is not None and path in self._seen:
            return atif.ImageSource(media_type=media_type or atif._media_type(self._seen[path]), path=self._seen[path])
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        media_type = media_type or (atif._media_type(path) if path else "image/png")
        import mimetypes

        ext = mimetypes.guess_extension(media_type) or ".bin"
        ext = {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)
        rel = f"images/{hashlib.sha256(data).hexdigest()}{ext}"
        if path is not None:
            self._seen[path] = rel
        return atif.ImageSource(media_type=media_type, path=rel)


class _Replay:
    """A sampler that answers with the stored v0.4 rows in order (the direct agent then builds the trajectory v0.5
    would have built); a model_failed row raises the v0.4 failure through the v0.5 error policy."""

    def __init__(self, slug: str, rows: list[dict]):
        self.model_id, self.rows, self.i = slug, rows, 0

    def __call__(self, messages, tools=None, seed=None):
        if self.i >= len(self.rows):
            raise errors.SamplerError("the v0.4 run has no row for this turn", cause=errors.PROVIDER_FAILED,
                                      exc_type="LegacyMissingTurn")
        r = self.rows[self.i]
        self.i += 1
        err = r.get("error") or ""
        if err.startswith("model_failed"):
            msg = err.split(":", 1)[1].strip() if ":" in err else err
            raise errors.SamplerError(msg, cause=errors.classify_error(msg), exc_type="LegacyModelFailed")
        return Response(text=r.get("response_text") or "", reasoning=r.get("reasoning") or "",
                        input_tokens=int(_num(r.get("input_tokens")) or 0), output_tokens=int(_num(r.get("output_tokens")) or 0),
                        reasoning_tokens=int(_num(r.get("reasoning_tokens")) or 0), finish_reason=r.get("finish_reason") or "",
                        extra={"v04_item_id": r.get("id"), "v04_duration_seconds": _num(r.get("duration_seconds")),
                               "sent_images": _sent_images(messages)})


_DESCRIBED: dict = {}


def _sent_images(messages) -> list[dict]:
    """extra.sent_images reconstructed from the v0.5 rendering (v0.4 did not record what it sent; a sampler-side
    downscaling of v0.4 is not reflected): {sha256, width, height, media_type, reconstructed: true}."""
    from simple_evals_mm.samplers.base import describe_image

    out = []
    for m in messages:
        for p in m.images:
            key = p.path if p.path is not None else hashlib.sha256(p.data).hexdigest()
            if key not in _DESCRIBED:
                data = p.data
                if data is None:
                    with open(p.path, "rb") as f:
                        data = f.read()
                _DESCRIBED[key] = describe_image(data, p.media_type)
            out.append({**_DESCRIBED[key], "reconstructed": True})
    return out


class _MemoPrompt:
    """The dataset with prompt(task) memoized per task id: every repeat (and every run of `--all`) renders the same
    first turn, and some prompts re-encode large images (ScreenSpot-Pro)."""

    def __init__(self, ds):
        self._ds, self._memo = ds, {}

    def __getattr__(self, name):
        return getattr(self._ds, name)

    def prompt(self, task):
        if task.id not in self._memo:
            self._memo[task.id] = self._ds.prompt(task)
        return list(self._memo[task.id])


def build_trial(run: V04Run, ds, task, rows: list[dict], k: int, slug: str, effort, job_id: str, images,
                checksum: str) -> TrialResult:
    replay = _Replay(slug or "legacy", rows)
    traj = Direct().run(task, ds, replay, job_id=job_id, attempt=k, images=images)
    for st in traj.steps:
        st.timestamp = None  # v0.4 recorded no per-step times
        if st.source == "agent" and not st.is_copied_context:
            st.reasoning_effort = effort
            ex = dict(st.extra or {})
            ex.pop("duration_seconds", None)
            if ex.get("v04_duration_seconds") is not None:
                ex["duration_seconds"] = ex.pop("v04_duration_seconds")
            ex.pop("v04_duration_seconds", None)
            st.extra = ex
        elif st.source != "agent":
            st.extra = {**(st.extra or {}), "reconstructed": True}
    failure = traj.failure
    traj.extra = {**(traj.extra or {}), "legacy": True, "reconstructed": True,
                  "duration_seconds": round(sum(_num(r.get("duration_seconds")) or 0 for r in rows), 3)}
    if len(rows) > replay.i and not failure:
        traj.extra["unused_v04_rows"] = [r.get("id") for r in rows[replay.i:]]
    fm = traj.final_metrics
    return TrialResult(
        task_name=task.id, trial_name=traj.trajectory_id, attempt=k, task_checksum=checksum,
        agent_info={"name": traj.agent.name, "version": traj.agent.version, "model_name": traj.agent.model_name},
        trajectory=traj,
        usage={"prompt_tokens": fm.total_prompt_tokens or 0, "completion_tokens": fm.total_completion_tokens or 0,
               "cached_tokens": 0, "cost_usd": None},
        exception_info={"exception_type": failure["error"].split(":", 1)[0], "message": failure["error"],
                        "cause": failure["cause"]} if failure else None,
        generation_lock=None, verifier_lock=None,  # type: ignore[arg-type] - unknown for legacy trials
        reused_from={"v04_run": run.run_id, "item_ids": [r.get("id") for r in rows], "repeat": k})


# ================================================================ (a) the v0.4 verdicts
def row_cause(r: dict) -> str | None:
    """The v0.5 failure cause of a v0.4 row (error policy)."""
    err = r.get("error") or ""
    if err.startswith("model_failed"):
        return errors.classify_error(err)
    if err.startswith("grader_failed"):
        return errors.JUDGE_FAILED
    return errors.classify_finish(r.get("finish_reason"))


def legacy_result(task, rows: list[dict], turns: bool, judge_name: str | None) -> VerifierResult:
    if not turns:
        r = rows[0]
        gr = r.get("grader_response")
        return VerifierResult(task.id, _num(r.get("score")), extracted=r.get("extracted_answer") or None,
                              judge={"model": judge_name, "response": gr} if gr else None, method="v0.4",
                              cause=row_cause(r), category=r.get("category") if r.get("category") is not None else task.category,
                              error=r.get("error"))
    out = [{"turn": int(str(r["id"]).rpartition("#t")[2]), "reward": _num(r.get("score")),
            "category": r.get("category"), "cause": row_cause(r), "judge": (r.get("grader_response") or "")[:1500] or None}
           for r in rows]
    counted = [t["reward"] for t in out if t["reward"] is not None]
    return VerifierResult(task.id, sum(counted) / len(counted) if counted else None, method="v0.4",
                          cause=next((row_cause(r) for r in rows if (r.get("error") or "").startswith("model_failed")), None),
                          details={"turns": out}, judge={"model": judge_name}, category=task.category)


# ================================================================ (b) the judge cache
# How the v0.4 code built the judge prompt of a row, per dataset family. `sources` are the definitions that decide the
# text (file path in the code repository, top-level names or None for the whole file); the v0.4 prompt is rebuilt with
# this tree's copy only when that copy equals the v0.4 one (at the run's commit, or the only value in the history).
@dataclass
class JudgeFamily:
    name: str
    sources: tuple  # ((repo path, names | None), ...)
    build: object  # (dataset, task, rows, i) -> prompt text
    reply: object  # (row) -> (reply text, how) | None
    # older v0.4 versions of a source reviewed to render the same text: {repo path: {sha256[:16] of the ast segment}}
    equivalent: dict = field(default_factory=dict)


def _template_build(template_fn):
    def build(ds, task, rows, i):
        r = rows[i]
        return template_fn().format(question=r.get("question") or "", correct_answer=r.get("correct_answer") or "",
                                    response=r.get("response_text") or "")
    return build


def _grader_reply(r: dict):
    """The stored reply when its verdict is the row's score; else the verdict of the score (a later grading that
    stored no reply: v0.4 kept grader_response only for failures after 2026-10-2x, so an older reply can be stale)."""
    from simple_evals_mm.common import parse_correct_verdict

    score = _num(r.get("score"))
    if (r.get("error") or "") or score not in (0.0, 1.0):
        return None
    gr = r.get("grader_response") or ""
    if gr and not gr.startswith(_POLICY_NOTES_PREFIX):
        v = parse_correct_verdict(gr)
        if v is not None and float(v == "yes") == score:
            return gr, "grader_response"
        return f"correct: {'yes' if score == 1.0 else 'no'}", "score (stored grader_response is stale)"
    return f"correct: {'yes' if score == 1.0 else 'no'}", "score"


def _mmdu_build(ds, task, rows, i):
    from simple_evals_mm.tasks.mmdu import judge_prompt

    r = rows[i]
    return judge_prompt(r.get("question") or "", r.get("correct_answer") or "", r.get("response_text") or "(empty response)")


def _mmdu_reply(r: dict):
    if (r.get("error") or "") or _num(r.get("score")) is None:
        return None
    try:
        g = json.loads(r.get("grader_response") or "")
    except ValueError:
        return None
    from simple_evals_mm.tasks.mmdu import parse_judge

    raw = g.get("raw") or ""
    if raw and len(raw) < 1500 and parse_judge(raw) is not None:
        return raw, "grader_response"
    if isinstance(g.get("dims"), dict) and g.get("overall") is not None:
        return json.dumps({**g["dims"], "Overall Score": g["overall"]}, ensure_ascii=False), "grader_response dims"
    return None


def _multiverse_build(ds, task, rows, i):
    from simple_evals_mm.tasks.multiverse import _turn_context, judge_prompt

    k = int(str(rows[i]["id"]).rpartition("#t")[2])
    own = [rows[j].get("response_text") or "" for j in range(k - 1)]
    history, user_msg, checklist = _turn_context({"turns": task.data["turns"]}, k, own)
    return judge_prompt(history, user_msg, rows[i].get("response_text") or "", checklist)


def _multiverse_reply(r: dict):
    if (r.get("error") or "") or _num(r.get("score")) is None:
        return None
    try:
        g = json.loads(r.get("grader_response") or "")
    except ValueError:
        return None
    v = g.get("verdicts")
    if not isinstance(v, list):
        return None
    return json.dumps({str(i + 1): "yes" if x else "no" for i, x in enumerate(v)}), "grader_response verdicts"


def _grader_template():
    from simple_evals_mm.common import GRADER_TEMPLATE

    return GRADER_TEMPLATE


def _hle_template():
    from simple_evals_mm.tasks.hle import JUDGE_TEMPLATE

    return JUDGE_TEMPLATE


GRADER = JudgeFamily("grader_template", ((V04_COMMON, ("GRADER_TEMPLATE",)),), _template_build(_grader_template), _grader_reply)
HLE_FAMILY = JudgeFamily("hle_template", (("src/simple_evals_mm/tasks/hle_judge_prompt.txt", None),),
                         _template_build(_hle_template), _grader_reply)
MMDU_FAMILY = JudgeFamily("mmdu_judge", (("src/simple_evals_mm/tasks/mmdu_judge_prompt.txt", None),
                                         ("src/simple_evals_mm/tasks/mmdu.py", ("judge_prompt",))), _mmdu_build, _mmdu_reply,
                          # 2026-10-23..10-30: the same text with tasks/mmdu_judge_prompt.txt (guarded above) read lazily
                          equivalent={"src/simple_evals_mm/tasks/mmdu.py": {"e46a97cf549a3e4c"}})
MULTIVERSE_FAMILY = JudgeFamily("multiverse_judge", (("src/simple_evals_mm/tasks/multiverse.py", ("JUDGE", "judge_prompt", "_turn_context")),),
                                _multiverse_build, _multiverse_reply)


def judge_family(name: str, ds) -> JudgeFamily | None:
    """The family whose v0.4 prompt can be rebuilt (None: no rebuild, every judge call is a cache miss)."""
    if name in ("hle", "hle_text", "hle_diamond"):
        return HLE_FAMILY
    if name == "mmdu":
        return MMDU_FAMILY
    if name == "multiverse":
        return MULTIVERSE_FAMILY
    if getattr(ds.verifier, "name", None) in ("judge", "mcq"):
        return GRADER
    return None


_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def family_guard(fam: JudgeFamily, git: Git, commits: list[str]) -> tuple[bool, str]:
    """This tree's prompt definitions equal the v0.4 ones: at every known v0.4 commit, or (no commit known) the only
    value the repository history has."""
    for path, names in fam.sources:
        local = os.path.join(_PKG_ROOT, path)
        if not os.path.exists(local):
            return False, f"{path}: not in this tree"
        with open(local, encoding="utf-8") as f:
            mine = source_segment(f.read(), names)
        eq = fam.equivalent.get(path, set())

        def same(seg, mine=mine, eq=eq):
            return seg == mine or (seg is not None and _sha16(seg.encode("utf-8")) in eq)

        if commits:
            for c in commits:
                if not same(git.segment(c, path, names)):
                    return False, f"{path} {names or ''} differs at {c[:12]}"
        else:
            hist = git.history_segments(path, names) if git.ok() else set()
            if not hist or not all(same(h) for h in hist):
                return False, (f"{path} {names or ''}: no v0.4 commit known and the history has "
                               f"{len(hist)} distinct value(s)" + ("" if mine in hist else ", none equal to this tree"))
    return True, "ok"


class CachedJudge:
    """The judge of one trial: a prompt identical to a v0.4 row's prompt gets that row's reply (cost 0); any other
    prompt goes to the live judge, or raises JudgeNotCached (--judge-cache-only / dry run)."""

    def __init__(self, slug: str, entries: dict, live=None, stats: Counter | None = None):
        self.slug, self.entries, self.live = slug, entries, live
        self.stats = stats if stats is not None else Counter()

    def __call__(self, messages, tools=None, seed=None):
        prompt = messages[-1].text
        hit = self.entries.get(prompt)
        if hit is not None:
            self.stats["hits"] += 1
            self.stats[f"hits_from_{hit[1].split(' ')[0]}"] += 1
            return Response(text=hit[0], provider="judge-cache:v0.4", model=hit[1], finish_reason="stop", cost_usd=0.0)
        self.stats["misses"] += 1
        if self.live is None:
            raise JudgeNotCached("no v0.4 verdict for this judge prompt")
        self.stats["live_calls"] += 1
        return self.live(messages, seed=seed) if seed is not None else self.live(messages)


def _mark(g: VerifierResult) -> VerifierResult:
    """judge_not_cached instead of judge_failed for cache misses; judge records of cache hits marked."""
    if g.error and "JudgeNotCached" in g.error:
        g.cause, g.error = errors.JUDGE_NOT_CACHED, "judge_not_cached"
    if g.judge and str(g.judge.get("provider") or "").startswith("judge-cache"):
        g.judge = {**g.judge, "cached": {"from": "v0.4", "source": g.judge.get("served_model")}, "cost_usd": 0.0}
    for t in (g.details or {}).get("turns") or []:
        if "JudgeNotCached" in str(t.get("error") or ""):
            t["cause"], t["error"] = errors.JUDGE_NOT_CACHED, "judge_not_cached"
    return g


def not_cached(g: VerifierResult) -> bool:
    return g.cause == errors.JUDGE_NOT_CACHED or any(
        t.get("cause") == errors.JUDGE_NOT_CACHED for t in (g.details or {}).get("turns") or [])


# ================================================================ aggregates
def recompute(t: V04Task, name: str) -> tuple[dict, list[dict]]:
    """Per repeat {score, num_examples, failure counts} from the rows, and the stored values that differ."""
    per, diffs = {}, []
    for k in sorted(t.repeats):
        rows = t.repeats[k]["rows"]
        scores = [s for r in rows if (s := _num(r.get("score"))) is not None]
        kinds = Counter()
        from simple_evals_mm.common import classify_model_failure

        for r in rows:
            err = r.get("error") or ""
            kinds["model_failed"] += err.startswith("model_failed")
            kinds["grader_failed"] += err.startswith("grader_failed")
            kinds["truncated"] += r.get("finish_reason") == "length"
            kinds["content_filter"] += r.get("finish_reason") == "content_filter"
            kind = classify_model_failure(err)
            kinds["refused"] += kind == "refused"
            kinds["context_overflow"] += kind == "context_overflow"
        per[k] = {"score": sum(scores) / len(scores) if scores else None, "num_examples": len(rows), **kinds}
        s = t.repeats[k]["score"]
        if s is None:
            diffs.append({"file": f"score_r{k}.json", "field": "*", "stored": None, "recomputed": "missing file"})
            continue
        checks = [("num_examples", per[k]["num_examples"]), ("num_model_failed", kinds["model_failed"]),
                  ("num_grader_failed", kinds["grader_failed"])]
        if name != "omnidocbench":  # the corpus-level score is not the row mean
            checks.insert(0, ("score", per[k]["score"]))
        for f, v in checks:
            if f in s and not _close(s.get(f), v):
                diffs.append({"file": f"score_r{k}.json", "field": f, "stored": s.get(f), "recomputed": v})
    sm = t.summary
    if sm is not None:
        ks = sorted(per)
        rep = [per[k]["score"] for k in ks]
        valid = [x for x in rep if x is not None]
        mean = sum(valid) / len(valid) if valid else None
        std = math.sqrt(sum((x - mean) ** 2 for x in valid) / (len(valid) - 1)) if len(valid) > 1 else None
        fields = [("n_repeats", len(ks)), ("num_examples", per[ks[-1]]["num_examples"] if ks else None),
                  ("total_model_failed", sum(per[k]["model_failed"] for k in ks)),
                  ("total_grader_failed", sum(per[k]["grader_failed"] for k in ks))]
        if name != "omnidocbench":
            fields = [("scores", rep), ("mean_score", mean), ("std_score", std), *fields]
        for key, kind in (("num_truncated_per_repeat", "truncated"), ("num_model_failed_per_repeat", "model_failed"),
                          ("num_grader_failed_per_repeat", "grader_failed"), ("num_refused_per_repeat", "refused"),
                          ("num_content_filter_per_repeat", "content_filter"),
                          ("num_context_overflow_per_repeat", "context_overflow")):
            fields.append((key, [per[k][kind] for k in ks]))
        for f, v in fields:
            if f not in sm:
                continue
            a = sm[f]
            same = (isinstance(a, list) and isinstance(v, list) and len(a) == len(v) and all(_close(x, y) for x, y in zip(a, v))) \
                if isinstance(v, list) else _close(a, v)
            if not same:
                diffs.append({"file": "summary.json", "field": f, "stored": a, "recomputed": v})
    elif t.repeats:
        diffs.append({"file": "summary.json", "field": "*", "stored": None, "recomputed": "missing file (task unfinished)"})
    return per, diffs


# ================================================================ config agreement
_SCORE_KEYS = ("model_name", "sampler_config", "request_params", "run_flags")


def _eval_config(s: dict) -> dict:
    return {k: v for k, v in (s.get("eval_config") or {}).items() if k != "grader_model_id"}


def config_problems(run: V04Run, t: V04Task) -> list[str]:
    cfg, out = run.config, []
    scores = {k: r["score"] for k, r in sorted(t.repeats.items()) if r["score"] is not None}
    if not scores:
        return ["no score_r<k>.json"]
    first_k = min(scores)
    for k, s in scores.items():
        for key in _SCORE_KEYS:
            if key in s and key in scores[first_k] and s[key] != scores[first_k][key]:
                out.append(f"r{k} {key} differs from r{first_k}")
        if _eval_config(s) != _eval_config(scores[first_k]):
            out.append(f"r{k} eval_config differs from r{first_k}")
        if s.get("request_params") is not None and cfg.get("request_params") is not None \
                and s["request_params"] != cfg["request_params"]:
            out.append(f"r{k} request_params differ from config.json")
        mnt = (s.get("sampler_config") or {}).get("max_new_tokens")
        if mnt is not None and cfg.get("max_new_tokens") is not None and mnt != cfg["max_new_tokens"]:
            out.append(f"r{k} max_new_tokens {mnt} != config.json {cfg['max_new_tokens']}")
        mid = (s.get("sampler_config") or {}).get("model_id")
        path = (cfg.get("model") or {}).get("path") or ""
        if mid and path and not (path == mid or path.endswith(("/" + mid, mid))):
            out.append(f"r{k} model_id {mid!r} is not config.json model {path!r}")
        want = (((cfg.get("serving") or {}).get("endpoint")) or {}).get("provider_name")
        got = sorted((s.get("model_usage") or {}).get("providers") or {})
        if want and got and any(p.lower() != want.lower() for p in got):
            out.append(f"r{k} served by {got}, config.json pins {want}")
        p = s.get("provenance") or {}
        p0 = scores[first_k].get("provenance") or {}
        if p and p0 and (p.get("task_module_sha256"), p.get("common_sha256")) != (p0.get("task_module_sha256"), p0.get("common_sha256")):
            out.append(f"r{k} generated by other task code than r{first_k} (provenance hashes differ)")
    return out


# ================================================================ datasets (cached per process: --all)
_DATASETS: dict = {}


def load_dataset(name: str, args: dict | None = None):
    key = (name, json.dumps(args or {}, sort_keys=True))
    if key not in _DATASETS:
        ds = dataset_mod.get_dataset(name, **(args or {}))
        tasks = ds.load()
        _DATASETS[key] = (ds, tasks, {t.id: task_checksum(t) for t in tasks}, _MemoPrompt(ds))
    return _DATASETS[key]


# ================================================================ one dataset
@dataclass
class DatasetResult:
    name: str
    audit: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    trials: dict = field(default_factory=dict)  # k -> [TrialResult]
    legacy: dict = field(default_factory=dict)  # k -> [VerifierResult]
    regrade: dict | None = None  # k -> [VerifierResult]
    regrade_meta: dict | None = None
    tasks: list = field(default_factory=list)  # mapped tasks
    ds: object = None
    judge_calls_needed: int = 0
    cache: dict = field(default_factory=dict)
    commit: str | None = None
    rows: dict = field(default_factory=dict)  # k -> {task id: [v0.4 rows]}
    max_judge_flips: float = DEFAULT_MAX_JUDGE_FLIPS

    @property
    def verified(self) -> bool:
        return not self.reasons


def _find(audit, check, status, **kw):
    audit.append({"check": check, "status": status, **kw})


def convert_dataset(run: V04Run, t: V04Task, git: Git, opts: dict, slug: str | None, effort, job_id: str, images) -> DatasetResult:
    name = t.name
    res = DatasetResult(name, max_judge_flips=opts.get("max_judge_flips", DEFAULT_MAX_JUDGE_FLIPS))
    a = res.audit
    # ---- history
    from simple_evals_mm import runs as runs_v04

    h = runs_v04.verify_history(t.dir)
    if h["status"] == "ok":
        _find(a, "history", "ok", dataset=name)
    else:
        why = "no history" if h["status"] == "no_history" else \
            "untracked edits: " + "; ".join(f"{k} {v}" for k, v in h.items() if k != "status" and v)
        _find(a, "history", "problem", dataset=name, detail=why)
        res.reasons.append(f"history: {why}")
    # ---- commit
    provs = {k: t.provenance(k) for k in sorted(t.repeats)}
    combos = {}
    for k, p in provs.items():
        key = (p.get("task_module"), p.get("task_module_sha256"), p.get("common_sha256"))
        combos.setdefault(key, []).append(k)
    commits = []
    for (mod, tsha, csha), ks in combos.items():
        if not tsha and not csha:
            _find(a, "commit", "problem", dataset=name, repeats=ks, detail="no provenance (task code hash not recorded)")
            res.reasons.append(f"commit: repeats {ks} have no provenance")
            continue
        f = git.find_commit(mod, tsha, csha)
        dirty = sorted({bool(provs[k].get("code_dirty")) for k in ks})
        rec = {"repeats": ks, "task_module": mod, "task_module_sha256": tsha, "common_sha256": csha,
               "code_git": sorted({provs[k].get("code_git") for k in ks if provs[k].get("code_git")}),
               "code_dirty": dirty, **f}
        if f["commit"]:
            commits.append(f["commit"])
            _find(a, "commit", "ok", dataset=name, **rec)
        else:
            rec["detail"] = "no matching commit" + (" (v0.4 provenance says code_dirty)" if True in dirty else "")
            _find(a, "commit", "problem", dataset=name, **rec)
            res.reasons.append(f"commit: repeats {ks}: {rec['detail']}")
    res.commit = commits[0] if commits and len(set(commits)) == 1 and len(commits) == len(combos) else None
    # ---- config
    cp = config_problems(run, t)
    _find(a, "config", "ok" if not cp else "problem", dataset=name, **({"detail": cp} if cp else {}))
    res.reasons += [f"config: {p}" for p in cp]
    # ---- aggregates
    per, diffs = recompute(t, name)
    _find(a, "aggregates", "ok" if not diffs else "stale", dataset=name, recomputed=per, **({"differences": diffs} if diffs else {}))
    if t.incomplete:
        _find(a, "repeats", "problem", dataset=name, detail=f"incomplete repeats (partial_r only, not converted): {sorted(t.incomplete)}")
    # ---- ids
    if name not in dataset_mod.DATASETS:
        _find(a, "ids", "problem", dataset=name, detail="no v0.5 dataset of this name")
        res.reasons.append("ids: no v0.5 dataset")
        return res
    try:
        ds, tasks, checksums, memo = load_dataset(name, (opts.get("dataset_args") or {}).get(name))
    except Exception as e:  # noqa: BLE001 - reported; the dataset is not converted
        _find(a, "ids", "problem", dataset=name, detail=f"v0.5 dataset cannot be loaded: {type(e).__name__}: {e}"[:400])
        res.reasons.append("ids: dataset not loadable")
        return res
    res.ds = ds
    mapped: dict = {}
    id_problems = []
    for k in sorted(t.repeats):
        groups, probs = map_items(name, t.repeats[k]["rows"], tasks)
        id_problems += [f"r{k}: {p}" for p in probs]
        mapped[k] = groups
    task_ids = {g[0].id for gs in mapped.values() for g in gs}
    sets = {k: {g[0].id for g in gs} for k, gs in mapped.items()}
    if len({frozenset(s) for s in sets.values()}) > 1:
        id_problems.append("repeats cover different task sets: " + ", ".join(f"r{k} {len(s)}" for k, s in sets.items()))
    _find(a, "ids", "ok" if not id_problems else "problem", dataset=name, n_tasks_v05=len(tasks), n_tasks_mapped=len(task_ids),
          revision=ds.revision, **({"detail": id_problems[:20], "n_problems": len(id_problems)} if id_problems else {}))
    if len(task_ids) < len(tasks):
        _find(a, "ids", "info", dataset=name, detail=f"the v0.4 run covers {len(task_ids)} of {len(tasks)} v0.5 tasks (a subset)")
    if id_problems:
        res.reasons.append(f"ids: {len(id_problems)} unmapped / duplicate / out-of-order item(s)")
    by_id = {x.id: x for x in tasks}
    res.tasks = [by_id[i] for i in sorted(task_ids)]
    # ---- trials and the legacy verdicts
    judge_name = v04_judge(t, run.config)
    turns = name in TURN_DATASETS
    prompt_diffs = []
    for k, groups in mapped.items():
        rows = t.repeats[k]["rows"]
        res.trials[k], res.legacy[k], res.rows[k] = [], [], {}
        for task, idx in groups:
            grp = [rows[i] for i in idx]
            res.rows[k][task.id] = grp
            tr = build_trial(run, memo, task, grp, k, slug, effort, job_id, images, checksums[task.id])
            res.trials[k].append(tr)
            res.legacy[k].append(legacy_result(task, grp, turns, judge_name))
            if not turns:
                u = next((s for s in tr.trajectory.steps if s.source == "user"), None)
                if u is not None and u.text != (grp[0].get("question") or ""):
                    prompt_diffs.append(task.id)
                    u.extra = {**(u.extra or {}), "v04_question": grp[0].get("question")}
    if prompt_diffs:
        _find(a, "prompt", "info", dataset=name, n_differ=len(prompt_diffs), first=sorted(set(prompt_diffs))[:5],
              detail="the v0.5 rendering differs from the v0.4 row's question (kept in the user step's extra.v04_question)")
    # ---- (b) regrade
    if opts.get("no_regrade"):
        res.reasons.append("regrade: not run (--no-regrade)")
        return res
    regrade(res, run, t, git, commits, opts, judge_name)
    return res


def regrade(res: DatasetResult, run: V04Run, t: V04Task, git: Git, commits: list[str], opts: dict, judge_name) -> None:
    ds, name, a = res.ds, res.name, res.audit
    problems = ds.preflight(True)
    if problems:
        _find(a, "regrade", "problem", dataset=name, detail=f"verifier unavailable: {'; '.join(problems)}"[:400])
        res.reasons.append("regrade: verifier unavailable here (" + "; ".join(problems)[:200] + ")")
        return
    judge_slug = opts["judge_model"]
    fam = judge_family(name, ds) if ds.needs_judge else None
    cache_info: dict = {"family": fam.name if fam else None, "v04_judge": judge_name, "judge": judge_slug if ds.needs_judge else None}
    usable = False
    if ds.needs_judge:
        if fam is None:
            cache_info["unusable"] = "no rebuild of the v0.4 judge prompt for this verifier"
        elif not same_judge(judge_name, judge_slug):
            cache_info["unusable"] = f"v0.4 judge {judge_name!r} is not {judge_slug}"
        else:
            ok, why = family_guard(fam, git, sorted(set(commits)))
            usable = ok
            if not ok:
                cache_info["unusable"] = f"prompt definitions: {why}"
    stats: Counter = Counter()
    live = opts.get("live_judge") if ds.needs_judge else None
    by_task = {x.id: x for x in res.tasks}
    out: dict = {}
    corpus = type(ds).verify_all is not dataset_mod.Dataset.verify_all
    try:
        for k, trials in res.trials.items():
            rows_of = res.rows[k]
            if corpus:
                gs = ds.verify_all([by_task[x.task_name] for x in trials], [x.trajectory for x in trials], None)
                out[k] = [_mark(g) for g in gs]
                continue

            def one(tr, rows_of=rows_of):
                task = by_task[tr.task_name]
                entries = {}
                if usable:
                    rs = rows_of.get(tr.task_name) or []
                    for i, r in enumerate(rs):
                        rep = fam.reply(r)
                        if rep is not None:
                            with contextlib.suppress(Exception):  # no rebuild for this row: a miss
                                entries[fam.build(ds, task, rs, i)] = rep
                judge = CachedJudge(judge_slug, entries, live, stats) if ds.needs_judge else None
                g = ds.verify_trial(task, tr.trajectory, judge)
                for _ in range(opts.get("max_retries", 2) if live is not None else 0):
                    if g.cause != errors.JUDGE_FAILED:
                        break
                    g = ds.verify_trial(task, tr.trajectory, judge)
                return _mark(g)

            threads = ds.verify_threads if live is not None else 1
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                out[k] = list(ex.map(one, trials))
    except RuntimeError as e:  # a verifier requirement (execution permission, judge) not met
        _find(a, "regrade", "problem", dataset=name, detail=f"{type(e).__name__}: {e}"[:400])
        res.reasons.append(f"regrade: {e}"[:200])
        return
    res.regrade = out
    res.judge_calls_needed = stats["misses"] - stats["live_calls"] if live is None else stats["live_calls"]
    cache_info.update({"hits": stats["hits"], "misses": stats["misses"], "live_calls": stats["live_calls"],
                       **{k: v for k, v in stats.items() if k.startswith("hits_from")}})
    res.cache = cache_info
    res.regrade_meta = {"judge": {"slug": judge_slug, "cache": cache_info} if ds.needs_judge else None}
    compare(res)


def compare(res: DatasetResult) -> None:
    """(a) legacy vs (b) regrade: per-attempt means, flipped tasks, first flips; the classification threshold."""
    ds, a = res.ds, res.audit
    flips, compared, uncached, coverage = [], 0, 0, 0
    mean_a, mean_b = [], []
    for k in sorted(res.legacy):
        la = {g.task_id: g for g in res.legacy[k]}
        lb = {g.task_id: g for g in (res.regrade or {}).get(k, [])}
        mean_a.append(ds.aggregate(res.legacy[k]).get("reward"))
        mean_b.append(ds.aggregate((res.regrade or {}).get(k, [])).get("reward"))
        for tid, ga in la.items():
            gb = lb.get(tid)
            if gb is None:
                continue
            if not_cached(gb):
                uncached += 1
                continue
            if ga.reward is None or gb.reward is None:
                coverage += (ga.reward is None) != (gb.reward is None)
                continue
            compared += 1
            if abs(ga.reward - gb.reward) > 1e-9:
                flips.append({"attempt": k, "task": tid, "legacy": ga.reward, "regrade": gb.reward,
                              "method": gb.method, "cause": gb.cause})
    sa, sb = dataset_mod.summarize([{"reward": x} for x in mean_a]), dataset_mod.summarize([{"reward": x} for x in mean_b])
    diff = None if sa["reward"] is None or sb["reward"] is None else sb["reward"] - sa["reward"]
    rec = {"legacy_reward": sa["reward"], "regrade_reward": sb["reward"], "diff": diff, "legacy_rewards": sa["rewards"],
           "regrade_rewards": sb["rewards"], "compared": compared, "flipped": len(flips), "not_cached": uncached,
           "coverage_changed": coverage, "first_flips": flips[:10], "cache": res.cache}
    thr = res.max_judge_flips
    ok = True
    if uncached:
        res.reasons.append(f"regrade: {uncached} judge verdict(s) not cached (regrade with a judge)")
        ok = False
    if ds.needs_judge:
        if compared and len(flips) / compared > thr:
            res.reasons.append(f"regrade: {len(flips)}/{compared} flips > {thr:.1%}")
            ok = False
    elif flips:
        res.reasons.append(f"regrade: {len(flips)} flip(s) with a deterministic verifier")
        ok = False
    if coverage:
        res.reasons.append(f"regrade: {coverage} trial(s) counted in one verification only")
        ok = False
    _find(a, "regrade", "ok" if ok else "problem", dataset=res.name, **rec)


# ================================================================ the run
@dataclass
class RunResult:
    run_id: str
    job_name: str
    classification: str
    reasons: list
    audit: list
    datasets: dict  # name -> DatasetResult
    config: dict
    notes: list
    judge_calls_needed: int = 0
    job_dir: str | None = None


def convert_run(run_dir: str, jobs_dir: str = store_mod.DEFAULT_JOBS_DIR, *, dry_run: bool = False, no_regrade: bool = False,
                judge_cache_only: bool = False, judge_model: str | None = None, datasets: list[str] | None = None,
                repo: str | None = None, max_judge_flips: float = DEFAULT_MAX_JUDGE_FLIPS, live_judge=None,
                images_store=None, log=print) -> RunResult:
    """Audit the v0.4 run and (unless dry_run) write the legacy job. live_judge: a prepared judge sampler for cache
    misses (None with judge_cache_only / dry_run: misses are judge_not_cached and counted)."""
    from simple_evals_mm.samplers.presets import DEFAULT_JUDGE

    run = read_run(run_dir, datasets)
    if datasets:
        missing = sorted(set(datasets) - set(run.tasks))
        if missing:
            raise ConvertError(f"{run.run_id}: no task(s) {missing} in the run")
    if not run.tasks:
        raise ConvertError(f"{run.run_id}: no task with items")
    git = Git(repo or default_repo(run_dir))
    judge_model = judge_model or DEFAULT_JUDGE
    job_name = run.run_id + LEGACY_SUFFIX
    st = store_mod.JobStore(jobs_dir, job_name)
    if not dry_run and st.exists():
        raise ConvertError(f"{st.dir} exists (a run is converted once; remove the job to convert again)")
    names = sorted(run.tasks)
    n_attempts = max((max(t.repeats) for t in run.tasks.values() if t.repeats), default=1)
    cfg, notes = job_config(run, names, n_attempts, jobs_dir, judge_model)
    opts = {"no_regrade": no_regrade, "judge_model": judge_model, "dataset_args": cfg.get("dataset_args"),
            "live_judge": None if (dry_run or judge_cache_only) else live_judge, "max_retries": cfg["retry"]["max_retries"],
            "max_judge_flips": max_judge_flips}
    job_id = str(uuid.uuid4())
    images = images_store or (_HashOnlyImages(st.dir) if dry_run else st.images)
    audit: list = []
    if not git.ok():
        audit.append({"check": "commit", "status": "problem", "detail": f"no git repository at {git.repo!r}: commits cannot be looked up"})
    results = {}
    for name in names:
        t = run.tasks[name]
        if not t.repeats:
            audit.append({"check": "repeats", "status": "problem", "dataset": name, "detail": "no complete repeat"})
            continue
        log(f"[convert] {run.run_id}/{name}: {sum(len(r['rows']) for r in t.repeats.values())} rows, repeats {sorted(t.repeats)}")
        r = convert_dataset(run, t, git, opts, cfg["model"], cfg["effort"], job_id, images)
        results[name] = r
        audit += r.audit
    if cfg["model"] is None:
        notes.append("no v0.5 slug")
    reasons = [f"{n}: {x}" for n, r in results.items() for x in r.reasons]
    reasons += [f"{x.get('dataset') or 'run'}: {x['check']}: {x.get('detail')}" for x in audit
                if x["status"] == "problem" and (x["check"] == "repeats" or (x["check"] == "commit" and "dataset" not in x))]
    classification = VERIFIED if not reasons and results else UNVERIFIED
    rr = RunResult(run.run_id, job_name, classification, reasons, audit, results, cfg, notes,
                   judge_calls_needed=sum(r.judge_calls_needed for r in results.values()))
    if not dry_run:
        write_job(st, run, rr, git, job_id, opts, judge_cache_only)
        rr.job_dir = st.dir
    return rr


def _converter_code() -> dict:
    from simple_evals_mm import checks

    repo = checks.repo_root()
    if repo is None:
        return {"commit": None, "dirty": None}
    try:
        s = checks.git_state(repo)
        return {"commit": s["commit"], "dirty": bool(s["status"])}
    except Exception:  # noqa: BLE001
        return {"commit": None, "dirty": None}


def write_job(st: store_mod.JobStore, run: V04Run, rr: RunResult, git: Git, job_id: str, opts: dict, judge_cache_only: bool) -> None:
    from simple_evals_mm import runs as runs_v04

    cfg = run.config
    lock_ds = {}
    commits = {n: r.commit for n, r in rr.datasets.items()}
    one = {c for c in commits.values()}
    conv = _converter_code()
    for name, r in rr.datasets.items():
        if r.ds is None:
            continue
        pairs = [(x.id, task_checksum(x)) for x in r.tasks]
        lock_ds[name] = {"revision": r.ds.revision, "ref": None, "task_ids_sha256": dataset_mod.task_ids_fingerprint(x.id for x in r.tasks),
                         "tasks_sha256": dataset_mod.tasks_sha256(pairs), "n_tasks": len(r.tasks), "config": r.ds.config(),
                         "verifier_lock": None, "v04_commit": r.commit, "classification": VERIFIED if r.verified else UNVERIFIED,
                         "reasons": r.reasons}
    lock = {
        "format": store_mod.FORMAT, "job_id": job_id, "legacy": True,
        "classification": rr.classification, "classification_reasons": rr.reasons,
        "audit": rr.audit,
        "code": {"commit": next(iter(one)) if len(one) == 1 else None, "checked": False,
                 "v04_commits": commits, "repo": git.repo, "v04_code": cfg.get("code"),
                 "converter": conv},
        "datasets": lock_ds,
        "sampler": {"slug": rr.config["model"], "config": None, "model_identity": None, "notes": rr.notes,
                    "v04": {k: cfg.get(k) for k in ("model", "sampler_class", "wrappers", "backend", "serving", "server_args",
                                                     "request_params", "max_new_tokens", "reasoning_effort", "template_kwargs",
                                                     "chat_template_path", "chat_template_sha256", "server_code", "env",
                                                     "behavior_env", "host", "pbs_jobid", "python", "created_at",
                                                     "effective_model_name")},
                    "v04_serving_stack": runs_v04.serving_stack(cfg)},
        "judge": {"slug": opts["judge_model"], "cache_only": judge_cache_only},
        "agent": {"name": "direct", "version": Direct.version, "args": {}, "replayed_from_v04": True},
        "skip_preflight": None, "sessions": [],
        "converted": {"at": _now(), "from": os.path.abspath(run.dir), "v04_run": run.run_id, "by": "sem convert",
                      "options": {"no_regrade": opts["no_regrade"], "judge_cache_only": judge_cache_only,
                                  "judge_model": opts["judge_model"]}},
    }
    with st.lock():
        os.makedirs(st.dir, exist_ok=True)
        st.write_config({**rr.config, "datasets": [n for n in rr.config["datasets"] if n in lock_ds]})
        st.write_lock(lock)
        for name, r in rr.datasets.items():
            if r.ds is None:
                continue
            m = lock_ds[name]
            for k, trials in sorted(r.trials.items()):
                for tr in trials:
                    st.append_trial(name, tr)
                st.finalize_attempt(name, k, revision=r.ds.revision, task_ids_sha256=m["task_ids_sha256"], n_tasks=len(trials))
            legacy_meta = {"dataset": name, "legacy": True, "judge": {"v04": v04_judge(run.tasks[name], cfg)},
                           "verifier_lock": None, "commit": r.commit, "dirty": None, "job_id": job_id,
                           "source": f"v0.4 {run.run_id}/{name}/items_r<k>.jsonl scores"}
            regrade_meta = None
            if r.regrade is not None:
                regrade_meta = {"dataset": name, "legacy_regrade": True, "judge": (r.regrade_meta or {}).get("judge"),
                                "verifier_lock": None, "verifier_deps": dataset_mod.verifier_deps(r.ds),
                                "commit": conv["commit"], "dirty": conv["dirty"], "job_id": job_id,
                                "judge_cache_only": judge_cache_only}
            complete = r.regrade is not None and not any(not_cached(g) for gs in r.regrade.values() for g in gs)
            # the current verification: the regrade when it verified every trial, else the v0.4 verdicts
            order = [("legacy", r.legacy, legacy_meta)]
            if r.regrade is not None:
                order = [*order, ("regrade", r.regrade, regrade_meta)] if complete else [("regrade", r.regrade, regrade_meta), *order]
            for kind, results, meta in order:
                vid = LEGACY_VID if kind == "legacy" else st.new_verification_id(name)
                st.write_verification(name, vid, results, meta)


# ================================================================ printing
def summary_row(rr: RunResult) -> dict:
    return {"run": rr.run_id, "datasets": len(rr.datasets), "classification": rr.classification,
            "reasons": rr.reasons, "judge_calls_needed": rr.judge_calls_needed}


def print_report(rr: RunResult, verbose: bool = True, out=print) -> None:
    out(f"== {rr.run_id} -> {rr.job_name}: {rr.classification}" + (f" ({rr.job_dir})" if rr.job_dir else ""))
    for n in rr.notes:
        out(f"   note: {n}")
    for name, r in rr.datasets.items():
        checks = {x["check"]: x["status"] for x in r.audit}
        rg = next((x for x in r.audit if x["check"] == "regrade" and "compared" in x), None)
        line = f"   {name:18s} " + " ".join(f"{c}={s}" for c, s in checks.items())
        if rg:
            fmt = lambda v: "-" if v is None else f"{v:.4f}"
            line += (f"  legacy={fmt(rg['legacy_reward'])} regrade={fmt(rg['regrade_reward'])} flips={rg['flipped']}/"
                     f"{rg['compared']} not_cached={rg['not_cached']} cache_hits={(rg.get('cache') or {}).get('hits', 0)}")
        out(line)
        if verbose:
            for x in r.reasons:
                out(f"      - {x}")
    if verbose:
        for x in rr.reasons:
            if not any(x.startswith(f"{n}: ") for n in rr.datasets):
                out(f"   - {x}")
    out(f"   judge calls needed beyond the cache: {rr.judge_calls_needed}")
