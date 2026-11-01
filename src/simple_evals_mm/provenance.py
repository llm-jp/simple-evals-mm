"""Which datasets a code change touches (for `sem run --from`, step 2).

Our code is pinned by the commit (a job runs only from a clean, committed tree), so no lock hashes our source. To
compare jobs across commits, `changed_datasets(commit_a, commit_b, datasets)` lists, per dataset, the files of its
file set that `git diff --name-only commit_a commit_b` reports:

  dataset       the dataset module's transitive `simple_evals_mm.*` imports and the package data files they name
                (the judge prompt .txt files, ...): changes here can affect generation and verification
  generation    framework files that only shape the request (agent, samplers, messages, trajectories)
  verification  framework files that only shape the reward (verifiers, metrics, the error policy)

The import closure is read from the sources at both commits (union), statically (ast), so lazy imports inside
functions count too.
"""

from __future__ import annotations

import ast
import os
import subprocess

PACKAGE = "simple_evals_mm"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# framework modules by the side of a trial they affect (every dataset depends on them)
GENERATION = ("agent", "atif", "models", "samplers.base", "samplers.openai_chat", "samplers.openai_responses",
              "samplers.gemini", "samplers.presets", "samplers.registry", "sources")
VERIFICATION = ("verifiers", "metrics", "errors", "dataset")


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


def file_set(tree: _Tree, module: str) -> set[str]:
    """Repo-relative files of a module's transitive import closure plus the package data files they name."""
    seen, todo, files = set(), [module], set()
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        path = tree.module_path(m)
        if path is None:
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


def changed_datasets(commit_a: str, commit_b: str, datasets: list[str], repo: str | None = None,
                     modules: dict[str, str] | None = None, package_dir: str = PACKAGE_DIR) -> dict[str, dict]:
    """{dataset: {"changed": bool, "dataset": [...], "generation": [...], "verification": [...]}}: the changed files
    of each dataset's file set between the two commits. `modules` maps dataset -> module (default: the registry)."""
    from simple_evals_mm import dataset as dataset_mod

    repo = repo or _git(package_dir, "rev-parse", "--show-toplevel").strip()
    pkg_rel = os.path.relpath(package_dir, repo).replace(os.sep, "/")
    diff = set(_git(repo, "diff", "--name-only", commit_a, commit_b).splitlines())
    trees = [_Tree(repo, c, pkg_rel) for c in (commit_a, commit_b)]

    def framework(names) -> set[str]:
        return {p for t in trees for n in names if (p := t.module_path(f"{PACKAGE}.{n}"))}

    gen, ver = framework(GENERATION), framework(VERIFICATION)
    out = {}
    for name in datasets:
        ref = (modules or {}).get(name) or dataset_mod.DATASETS[name]
        module = ref.split(":")[0] if isinstance(ref, str) else ref.__module__
        own = set().union(*(file_set(t, module) for t in trees)) - gen - ver
        rec = {"dataset": sorted(own & diff), "generation": sorted(gen & diff), "verification": sorted(ver & diff)}
        rec["changed"] = any(rec.values())
        out[name] = rec
    return out
