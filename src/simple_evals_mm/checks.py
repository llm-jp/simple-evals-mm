"""Start checks of `sem run` (docs/design/v0.5.md "Reproducibility"):

  * clean tree: `git status --porcelain --untracked-files=all` of the package's repository is empty and no ignored
    (hence invisible) `.py` file sits under the package; `--allow-dirty` records the diff and marks the job dirty;
  * installed packages equal `uv.lock` (importlib.metadata versions of the locked packages that are installed;
    the project itself is skipped);
  * commit + package version (+ whether the install is editable);
  * eager import of every simple_evals_mm module, so later edits or pulls do not change a running process.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as md
import json
import os
import re
import subprocess
import tomllib

PACKAGE = "simple_evals_mm"
PROJECT = "simple-evals-mm"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# v0.4 modules not imported eagerly: in-process HF samplers and the HF server pull torch / transformers / fastapi
# (seconds of import time and GPU-side state) and are never used by v0.5 jobs. Removed in step 2.
SKIP_IMPORT = (
    "simple_evals_mm.sampler.qwenvl_sampler",
    "simple_evals_mm.sampler.internvl_sampler",
    "simple_evals_mm.sampler.llmjpvl_sampler",
    "simple_evals_mm.sampler.sarashina_sampler",
    "simple_evals_mm.serving.hf_server",
)


class CheckError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL)


def repo_root(path: str = PACKAGE_DIR) -> str | None:
    try:
        return _git(path, "rev-parse", "--show-toplevel").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_state(repo: str, package_dir: str = PACKAGE_DIR) -> dict:
    """commit, porcelain status lines and ignored .py files under the package."""
    status = [line for line in _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() if line.strip()]
    rel = os.path.relpath(package_dir, repo)
    ignored = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--", rel).splitlines()
    return {"commit": _git(repo, "rev-parse", "HEAD").strip(), "status": status,
            "ignored_py": [f for f in ignored if f.endswith(".py")]}


def git_diff(repo: str) -> str:
    """Tracked changes (git diff HEAD) plus every untracked file as an addition."""
    out = _git(repo, "diff", "HEAD", "--binary")
    for f in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        try:
            out += subprocess.run(["git", "-C", repo, "diff", "--no-index", "--binary", "/dev/null", f],
                                  capture_output=True, text=True, check=False).stdout
        except OSError:
            pass
    return out


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _same_version(a: str, b: str) -> bool:
    try:
        from packaging.version import Version

        return Version(a) == Version(b)
    except Exception:  # noqa: BLE001 - unparsable versions: compare as strings
        return a == b


def lock_check(lock_path: str | None) -> dict:
    """Locked vs installed versions. Locked packages that are not installed (other platforms, optional groups) are
    skipped; so is the project itself."""
    if not lock_path or not os.path.exists(lock_path):
        return {"ok": False, "reason": f"no uv.lock at {lock_path}", "mismatches": []}
    with open(lock_path, "rb") as f:
        raw = f.read()
    lock = tomllib.loads(raw.decode("utf-8"))
    installed = {}
    for d in md.distributions():
        name = d.metadata.get("Name")
        if name:
            installed[_norm(name)] = d.version
    mismatches, checked = [], 0
    for p in lock.get("package", []):
        name = _norm(p["name"])
        src = p.get("source", {})
        if name == _norm(PROJECT) or "editable" in src or "virtual" in src or name not in installed:
            continue
        checked += 1
        if not _same_version(str(p.get("version", "")), installed[name]):
            mismatches.append({"package": p["name"], "locked": p.get("version"), "installed": installed[name]})
    return {"ok": not mismatches, "lock_sha256": hashlib.sha256(raw).hexdigest(), "n_checked": checked,
            "mismatches": mismatches}


def package_info() -> dict:
    try:
        dist = md.distribution(PROJECT)
        version = dist.version
        direct = dist.read_text("direct_url.json")
        editable = bool(direct and json.loads(direct).get("dir_info", {}).get("editable"))
    except md.PackageNotFoundError:
        version, editable = None, None
    return {"version": version, "editable": editable}


_IMPORTED: dict | None = None


def eager_import(skip: tuple[str, ...] = SKIP_IMPORT) -> dict:
    """Import every module of the package once per process; failures are reported, not raised."""
    global _IMPORTED
    if _IMPORTED is not None:
        return _IMPORTED
    # every .py under the package (tasks/ and sampler/ are namespace packages: pkgutil.walk_packages skips them)
    names = sorted(PACKAGE + "." + os.path.relpath(os.path.join(d, f), PACKAGE_DIR)[:-3].replace(os.sep, ".").removesuffix(".__init__")
                   for d, _, files in os.walk(PACKAGE_DIR) if "__pycache__" not in d for f in files if f.endswith(".py"))
    imported, failed, skipped = 0, {}, []
    for name in names:
        if name in skip:
            skipped.append(name)
            continue
        try:
            importlib.import_module(name)
            imported += 1
        except Exception as e:  # noqa: BLE001 - optional deps of v0.4 modules
            failed[name] = f"{type(e).__name__}: {e}"[:200]
    _IMPORTED = {"imported": imported, "skipped": skipped, "failed": failed}
    return _IMPORTED


def start_checks(allow_dirty: bool = False, repo: str | None = None, lock_path: str | None = None,
                 package_dir: str = PACKAGE_DIR, do_import: bool = True) -> dict:
    """Run every start check; returns the lock.json `code` section. Raises CheckError on a dirty tree or a failed
    lock check unless allow_dirty (then the job is marked dirty and the diff is returned under "diff")."""
    # import first: what this process runs is then what the clean-tree check below sees (later edits cannot matter)
    imports = eager_import() if do_import else None
    repo = repo or repo_root(package_dir)
    out: dict = {**package_info(), "commit": None, "dirty": False}
    problems = []
    if repo is None:
        problems.append(f"{package_dir} is not in a git checkout: the commit cannot be recorded")
    else:
        st = git_state(repo, package_dir)
        out["commit"] = st["commit"]
        if st["status"]:
            problems.append("uncommitted changes:\n  " + "\n  ".join(st["status"][:20]))
        if st["ignored_py"]:
            problems.append("ignored .py files under the package:\n  " + "\n  ".join(st["ignored_py"][:20]))
    lc = lock_check(lock_path or (os.path.join(repo, "uv.lock") if repo else None))
    out["lock_check"] = lc
    if not lc["ok"]:
        problems.append(f"installed packages differ from uv.lock: {lc.get('reason') or lc['mismatches'][:10]}")
    if problems:
        if not allow_dirty:
            raise CheckError("start checks failed (commit / `uv sync --frozen`, or pass --allow-dirty):\n- " + "\n- ".join(problems))
        out["dirty"] = True
        out["problems"] = problems
        if repo is not None:
            out["diff"] = git_diff(repo)
    if imports is not None:
        out["imports"] = imports
    return out
