"""Which datasets a code change touches (`sem run --from` and `sem job resume` across commits).

Our code is pinned by the commit (a job runs only from a clean, committed tree), so no lock hashes our source. To
compare jobs across commits, `changed_datasets(commit_a, commit_b, datasets)` sorts the package files that
`git diff --name-only commit_a commit_b` reports, per dataset, into:

  dataset       the dataset module's transitive `simple_evals_mm.*` imports and the package data files they name
                (the judge prompt .txt files, ...), minus the framework files below: changes here can affect
                generation and verification; plus the dataset's own datasets.lock entries (compared by content, not as
                a file: an edit to another dataset's entry does not count)
  generation    framework files that shape the request or how a trial is written: GENERATION, the sampler modules of
                the job's API format (`sampler_modules`; another format's sampler does not count) and the files these
                import that are not listed elsewhere
  verification  framework files that only shape the reward (VERIFICATION and their unlisted imports), the
                `_shared` datasets.lock artifacts the dataset uses (nltk, the evaluator image)
  unknown       any other changed package file that is in none of the sets, not NEUTRAL, not another dataset's module
                and not another API format's sampler: unknown means unsafe (treated like generation)

NEUTRAL files cannot change a trial: the job store, the CLI (its config is compared on resume), the start checks,
derive / provenance / runner orchestration (the trial itself is built in trial.py), datalock / dataset_admin (the lock
entries are compared instead), parity, `sem serve` and its manifest / stack / patches (the server manifest digest is
compared), and the model presets (a preset change matters only when this model's resolved settings change, which the
settings comparison and the generation_lock catch). Files outside the package (tests, docs, scripts, site, pbs) are
never compared; uv.lock is covered by the lock check.

The import closures are read from the sources at both commits (union), statically (ast), so lazy imports inside
functions count too.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess

PACKAGE = "simple_evals_mm"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = "datasets.lock.json"

# framework modules by the side of a trial they affect (every dataset depends on them). dataset.py (the Dataset base
# renders prompts / next turns / subsets), errors.py (the cause written into a trial) and trial.py (how a trial is
# built and retried) shape trials, so they are generation-side.
GENERATION = ("agent", "atif", "models", "dataset", "errors", "trial", "sources", "samplers", "samplers.base",
              "samplers.registry")
VERIFICATION = ("verifiers", "metrics")
# per API format, the sampler module that builds and sends the request (its imports under samplers/ count too)
SAMPLER_MODULES = {"openai_chat_completions": ("samplers.openai_chat",),
                   "openai_responses": ("samplers.openai_responses",),
                   "google_generate_content": ("samplers.gemini",)}
FAKE_SAMPLER = ("samplers.fake",)
# the OpenAI-compatible HF server (serving/hf_server.py): a chat job's served engine unless a sglang / vLLM manifest
# pins another engine
HF_SERVER = ("serving.hf_server", "serving.hf_backends")
NEUTRAL = ("store", "cli", "checks", "derive", "provenance", "runner", "datalock", "dataset_admin", "parity",
           "serving", "serving.serve", "serving.manifest", "serving.stack", "samplers.presets")
NEUTRAL_PREFIXES = ("serving/patches/",)
NEUTRAL_FILES = ("py.typed",)


def sampler_modules(provider: str | None, api_format: str | None, engine: str | None = None) -> list[str] | None:
    """The sampler modules of a job (module names relative to the package); None = unknown: every sampler counts."""
    if provider == "fake":
        return list(FAKE_SAMPLER)
    mods = SAMPLER_MODULES.get(api_format or "")
    if mods is None:
        return None
    out = list(mods)
    if api_format == "openai_chat_completions" and engine not in ("sglang", "vllm"):
        out += HF_SERVER
    return out


def _git(repo: str, *args: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL)


def _show(repo: str, commit: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


class _Tree:
    """The package's files at one commit (repo-relative paths)."""

    def __init__(self, repo: str, commit: str, pkg_rel: str):
        self.repo, self.commit, self.pkg_rel = repo, commit, pkg_rel
        self.files = set(_git(repo, "ls-tree", "-r", "--name-only", commit, "--", pkg_rel).splitlines())

    def module_path(self, module: str) -> str | None:
        rel = module.split(".")[1:]
        base = "/".join([self.pkg_rel, *rel]) if rel else self.pkg_rel
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in self.files:
                return cand
        return None

    def source(self, path: str) -> str:
        return _show(self.repo, self.commit, path) or ""


def _imports(src: str, module: str, is_pkg: bool) -> set[str]:
    """`simple_evals_mm.*` module names imported anywhere in the source (absolute and relative imports)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    pkg_parts = module.split(".") if is_pkg else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.split(".")[0] == PACKAGE}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - node.level + 1] if node.level > 1 else pkg_parts
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if mod.split(".")[0] != PACKAGE:
                continue
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}  # `from pkg import module` (non-modules are dropped later)
    return out


def file_set(tree: _Tree, module: str, stop=frozenset()) -> set[str]:
    """Repo-relative files of a module's transitive import closure plus the package data files they name. Files in
    `stop` (NEUTRAL, other API formats' samplers) are neither included nor followed."""
    seen, todo, files = set(), [module], set()
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        path = tree.module_path(m)
        if path is None or path in stop:
            continue
        files.add(path)
        # parent packages run their __init__ on import
        parts = m.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        todo += sorted(_imports(tree.source(path), m, path.endswith("__init__.py")))
    data = [f for f in tree.files if not f.endswith(".py")]
    srcs = [tree.source(f) for f in files]
    files |= {f for f in data if any(os.path.basename(f) in s for s in srcs)}
    return files


def is_ancestor(commit_a: str, commit_b: str, repo: str | None = None, package_dir: str = PACKAGE_DIR) -> bool:
    """commit_a is an ancestor of (or equal to) commit_b (`git merge-base --is-ancestor`); an unknown commit raises
    ValueError."""
    repo = repo or _git(package_dir, "rev-parse", "--show-toplevel").strip()
    r = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", commit_a, commit_b], capture_output=True, check=False,
                       text=True)
    if r.returncode not in (0, 1):
        raise ValueError(f"cannot compare commits {str(commit_a)[:12]}..{str(commit_b)[:12]}: {r.stderr.strip()[:200]}")
    return r.returncode == 0


_DIFF_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def diff_files(diff: str | None) -> set[str]:
    """Repo-relative paths a `git diff` text touches (an --allow-dirty diff)."""
    return {p for m in _DIFF_RE.finditer(diff or "") for p in m.groups()}


def _json(text: str | None) -> dict:
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}


def changed_datasets(commit_a: str, commit_b: str, datasets: list[str], repo: str | None = None,
                     modules: dict[str, str] | None = None, package_dir: str = PACKAGE_DIR,
                     sampler_modules: list[str] | None = None, extra_files=()) -> dict[str, dict]:
    """{dataset: {"changed", "generation_relevant", "dataset", "generation", "verification", "unknown"}}: the changed
    package files between the two commits sorted per dataset (see the module docstring). `modules` maps dataset ->
    module (default: the registry); `sampler_modules`: the job's sampler modules (see sampler_modules(); None = every
    sampler counts); `extra_files`: repo-relative paths changed on top of commit_b (an --allow-dirty diff)."""
    from simple_evals_mm import dataset as dataset_mod

    repo = repo or _git(package_dir, "rev-parse", "--show-toplevel").strip()
    pkg_rel = os.path.relpath(package_dir, repo).replace(os.sep, "/")
    diff = set(_git(repo, "diff", "--name-only", commit_a, commit_b).splitlines()) | set(extra_files)
    pkg_diff = {f for f in diff if f.startswith(pkg_rel + "/")}
    trees = [_Tree(repo, c, pkg_rel) for c in (commit_a, commit_b)]
    all_files = set().union(*(t.files for t in trees))

    def paths(names) -> set[str]:
        return {p for t in trees for n in names if (p := t.module_path(f"{PACKAGE}.{n}"))}

    def closure(names, stop: frozenset) -> set[str]:
        out: set[str] = set()
        for t in trees:
            for n in names:
                mod = n if n == PACKAGE or n.startswith(PACKAGE + ".") else f"{PACKAGE}.{n}"
                out |= file_set(t, mod, stop - {t.module_path(mod)})  # the start module itself is never stopped
        return out

    def rel(f: str) -> str:
        return f[len(pkg_rel) + 1:]

    lock_path = f"{pkg_rel}/{LOCK_FILE}"
    all_samplers = {f for f in all_files if rel(f).startswith("samplers/")} | paths(HF_SERVER)
    if sampler_modules is None:
        sampler_modules = [*FAKE_SAMPLER, *HF_SERVER, *(m for v in SAMPLER_MODULES.values() for m in v)]
    neutral = frozenset(paths(NEUTRAL) | {f for f in all_files
                                          if rel(f).startswith(NEUTRAL_PREFIXES) or rel(f) in NEUTRAL_FILES})
    job_samplers = closure(sampler_modules, neutral) & all_samplers
    explicit_gen = (paths(GENERATION) | job_samplers) - neutral
    explicit_ver = paths(VERIFICATION) - explicit_gen
    other_samplers = all_samplers - explicit_gen - neutral
    explicit = frozenset(explicit_gen | explicit_ver | neutral | other_samplers | {lock_path})
    # the unlisted files the framework imports, by the side that imports them (a closure does not pass through a file
    # of the other side, a NEUTRAL file or another API format's sampler); the generation side wins
    gen_rest = closure([*GENERATION, *sampler_modules], neutral | other_samplers | explicit_ver) - explicit
    ver_rest = closure(VERIFICATION, neutral | other_samplers | explicit_gen) - explicit - gen_rest
    other_tasks = {f for f in all_files if rel(f).startswith("tasks/")}
    locks = [_json(t.source(lock_path)) for t in trees] if lock_path in pkg_diff else None
    out = {}
    for name in datasets:
        ref = (modules or {}).get(name) or dataset_mod.DATASETS[name]
        module = ref.split(":")[0] if isinstance(ref, str) else ref.__module__
        own = closure([module], explicit) - explicit  # the dataset's own files (framework files are sorted above)
        gen, ver = gen_rest - own, ver_rest - own
        rec = {"dataset": sorted(own & diff), "generation": sorted((explicit_gen | gen) & diff),
               "verification": sorted((explicit_ver | ver) & diff),
               "unknown": sorted(pkg_diff - explicit - own - gen - ver - other_tasks)}
        if locks is not None:  # the dataset's own entries and the shared artifacts it names, by content
            old, new = ({**lk.get("datasets", {})} for lk in locks)
            keys = sorted(k for k in set(old) | set(new) if k == name or k.startswith(name + "["))
            rec["dataset"] += [f"{lock_path}:datasets.{k}" for k in keys if old.get(k) != new.get(k)]
            srcs = [t.source(f) for t in trees for f in own if f.endswith(".py")]
            s_old, s_new = (lk.get("_shared", {}) for lk in locks)
            rec["verification"] += [f"{lock_path}:_shared.{k}" for k in sorted(set(s_old) | set(s_new))
                                    if s_old.get(k) != s_new.get(k) and any(k in s for s in srcs)]
        rec["generation_relevant"] = bool(rec["dataset"] or rec["generation"] or rec["unknown"])
        rec["changed"] = rec["generation_relevant"] or bool(rec["verification"])
        out[name] = rec
    return out


def generation_files(rec: dict) -> list[str]:
    """The files of a changed_datasets record that rule out reusing a generation (dataset / generation / unknown)."""
    return sorted({*rec.get("dataset", []), *rec.get("generation", []), *rec.get("unknown", [])})
