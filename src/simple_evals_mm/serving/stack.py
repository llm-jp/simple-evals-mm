"""What a served model runs on, as `sem serve` records it in the server manifest (docs/design/v0.5.md "Serving").

    engine_identity(engine, venv, patches)   engine version and a content hash of its installed files, verified against
                                              the wheel RECORD; an edited file is refused unless a requested patch
                                              provides exactly that content
    load_patches(names, engine, version)      serving/patches/<name>/: patch.json + files/<site-packages path>
    write_overlay(dir, patches)               a sitecustomize.py that loads the patch files instead of the installed
                                              modules (PYTHONPATH overlay, inherited by the engine's worker processes)
    model_identity(model)                     HF id + snapshot revision (local cache), or a local path; content digests
                                              of the config / tokenizer / processor / generation_config files, weights
                                              by size + mtime (+ the LFS sha256 the HF cache names the blob by)
    default_chat_template(model)              the snapshot's own template (chat_template.jinja, tokenizer_config.json)
    hardware(venv)                            GPU names, driver and CUDA versions (nvidia-smi), torch inside the venv

The engine venv is only READ here (dist-info metadata, files, torch/version.py); nothing in it is executed.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import importlib.metadata as md
import json
import os
import re
import subprocess

from simple_evals_mm.errors import UserError

PATCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patches")
# distribution of each engine, and the further distributions whose installed files are RECORD-verified with it
ENGINE_DIST = {"sglang": "sglang", "vllm": "vllm", "hf": "transformers"}
RECORD_VERIFIED = {"sglang": ("sglang", "transformers"), "vllm": ("vllm", "transformers"), "hf": ("transformers",)}
# versions recorded next to the engine (packages that change numerics)
STACK_PACKAGES = ("torch", "transformers", "triton", "flashinfer-python", "flashinfer-cubin", "sglang-kernel",
                  "sgl-kernel", "xformers", "flash-attn", "vllm-flash-attn", "accelerate", "tokenizers", "safetensors",
                  "pillow", "numpy")


class StackError(UserError, RuntimeError):
    pass


# ---------------------------------------------------------------- venv
def venv_python(venv: str) -> str:
    """The interpreter of an engine venv: <venv>/bin/python, else <venv>/.venv/bin/python."""
    for cand in (os.path.join(venv, "bin", "python"), os.path.join(venv, ".venv", "bin", "python")):
        if os.path.exists(cand):
            return cand
    raise StackError(f"{venv}: no bin/python (nor .venv/bin/python): not a virtual environment")


def site_packages(venv: str) -> str:
    for base in (venv, os.path.join(venv, ".venv")):
        found = sorted(glob.glob(os.path.join(base, "lib", "python*", "site-packages")))
        if found:
            return found[0]
    raise StackError(f"{venv}: no lib/python*/site-packages")


def _dist(name: str, site: str):
    norm = re.sub(r"[-_.]+", "-", name).lower()
    for d in md.distributions(path=[site]):
        n = d.metadata.get("Name") or ""
        if re.sub(r"[-_.]+", "-", n).lower() == norm:
            return d
    return None


def package_versions(site: str, names=STACK_PACKAGES) -> dict:
    out = {}
    for n in names:
        d = _dist(n, site)
        if d is not None:
            out[n] = d.version
    return out


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record_files(name: str, site: str) -> dict:
    """{"version", "files": {path: {"sha256", "record"}}, "missing": [...]}: every RECORD entry with a sha256 of one
    installed distribution, hashed as installed (record = the RECORD's urlsafe-base64 digest)."""
    d = _dist(name, site)
    if d is None:
        raise StackError(f"{name} is not installed in {site}")
    files, missing = {}, []
    for f in d.files or []:
        if not f.hash or f.hash.mode != "sha256":
            continue
        path = str(f.locate())
        try:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).digest()
        except OSError:
            missing.append(str(f))
            continue
        files[str(f)] = {"sha256": digest.hex(), "record": f.hash.value, "ok": _b64(digest) == f.hash.value}
    return {"version": d.version, "files": files, "missing": missing}


# ---------------------------------------------------------------- patches
def available_patches() -> list[str]:
    return sorted(d for d in os.listdir(PATCH_DIR) if os.path.isfile(os.path.join(PATCH_DIR, d, "patch.json"))) \
        if os.path.isdir(PATCH_DIR) else []


def load_patch(name: str, patch_dir: str = PATCH_DIR) -> dict:
    """patch.json {name, upstream_ref, description, engine, engine_versions, files: [site-packages paths], plugin?} and
    the sha256 of each file under files/ (plus one sha256 over the whole patch).
    `files` replace installed modules. `plugin` adds code the engine does not have: {root: a directory under the patch
    (put on sys.path), files: [paths under root], startup: a module imported when each engine process starts (it
    registers the plugin, e.g. through post-import hooks), env: {VAR: value} set as defaults}; its files are hashed
    into the patch's sha256 too."""
    d = os.path.join(patch_dir, name)
    meta_path = os.path.join(d, "patch.json")
    if not os.path.isfile(meta_path):
        raise StackError(f"unknown patch {name!r} (available: {', '.join(available_patches()) or 'none'})")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    for k in ("name", "upstream_ref", "description", "engine", "files"):
        if k not in meta:
            raise StackError(f"{meta_path}: missing {k!r}")
    if meta["name"] != name:
        raise StackError(f"{meta_path}: name {meta['name']!r} differs from its directory {name!r}")
    files = {}
    for rel in meta["files"]:
        src = os.path.join(d, "files", rel)
        if not os.path.isfile(src):
            raise StackError(f"patch {name}: {rel} is listed but files/{rel} is missing")
        if not rel.endswith(".py") or rel.endswith("__init__.py"):
            raise StackError(f"patch {name}: {rel}: only module files (not packages) can be overlaid")
        files[rel] = {"sha256": sha256_file(src), "path": src}
    plugin = None
    if meta.get("plugin"):
        pl = meta["plugin"]
        for k in ("root", "files", "startup"):
            if k not in pl:
                raise StackError(f"{meta_path}: plugin is missing {k!r}")
        root = os.path.join(d, pl["root"])
        pfiles = {}
        for rel in pl["files"]:
            src = os.path.join(root, rel)
            if not os.path.isfile(src):
                raise StackError(f"patch {name}: plugin file {pl['root']}/{rel} is missing")
            pfiles[rel] = sha256_file(src)
        unlisted = sorted(os.path.relpath(os.path.join(dp, f), root) for dp, _, fs in os.walk(root) for f in fs
                          if f.endswith(".py") and os.path.relpath(os.path.join(dp, f), root) not in pfiles)
        if unlisted:
            raise StackError(f"patch {name}: plugin files not listed in patch.json: {', '.join(unlisted)}")
        plugin = {"root": root, "files": pfiles, "startup": pl["startup"], "env": dict(pl.get("env") or {})}
    blob = json.dumps({"meta": meta, "files": {k: v["sha256"] for k, v in sorted(files.items())},
                       "plugin_files": dict(sorted((plugin or {}).get("files", {}).items()))}, sort_keys=True)
    return {"name": name, "upstream_ref": meta["upstream_ref"], "description": meta["description"],
            "engine": meta["engine"], "engine_versions": list(meta.get("engine_versions") or []),
            "files": files, "plugin": plugin, "sha256": hashlib.sha256(blob.encode()).hexdigest()}


def load_patches(names, engine: str, version: str | None, patch_dir: str = PATCH_DIR) -> list[dict]:
    out, seen = [], {}
    for n in names or []:
        p = load_patch(n, patch_dir)
        if p["engine"] != engine:
            raise StackError(f"patch {n} is for {p['engine']}, not {engine}")
        if p["engine_versions"] and version not in p["engine_versions"]:
            raise StackError(f"patch {n} was made for {p['engine']} {', '.join(p['engine_versions'])}, the venv has {version}")
        for rel in p["files"]:
            if rel in seen:
                raise StackError(f"patches {seen[rel]} and {n} both replace {rel}")
            seen[rel] = n
        out.append(p)
    return out


def module_name(rel: str) -> str:
    return rel[: -len(".py")].replace("/", ".")


OVERLAY_SITECUSTOMIZE = '''\
# Written by `sem serve`: load the registered patch files instead of the installed modules (PYTHONPATH overlay,
# inherited by the engine's worker processes). The server manifest records each patch's name, sha256 and upstream ref.
import importlib
import importlib.abc
import importlib.util
import sys

_FILES = {files!r}


class _SemPatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        src = _FILES.get(fullname)
        if src is None:
            return None
        print(f"[sem-patch] {{fullname}} <- {{src}}", file=sys.stderr, flush=True)
        return importlib.util.spec_from_file_location(fullname, src)


sys.meta_path.insert(0, _SemPatchFinder())

# plugins: code the engine does not have (its own package on sys.path, env defaults, a startup module that registers it)
import os  # noqa: E402

for _root, _env, _startup in {plugins!r}:
    for _k, _v in _env.items():
        os.environ.setdefault(_k, _v)
    if _root not in sys.path:
        sys.path.append(_root)
    print(f"[sem-patch] plugin {{_startup}} from {{_root}}", file=sys.stderr, flush=True)
    importlib.import_module(_startup)
'''


def write_overlay(overlay_dir: str, patches: list[dict]) -> str | None:
    """The overlay directory (put first on PYTHONPATH) with a sitecustomize.py mapping module names to the patch
    files; None without patches."""
    if not patches:
        return None
    files = {module_name(rel): f["path"] for p in patches for rel, f in sorted(p["files"].items())}
    plugins = [(p["plugin"]["root"], p["plugin"]["env"], p["plugin"]["startup"]) for p in patches if p.get("plugin")]
    os.makedirs(overlay_dir, exist_ok=True)
    with open(os.path.join(overlay_dir, "sitecustomize.py"), "w", encoding="utf-8") as f:
        f.write(OVERLAY_SITECUSTOMIZE.format(files=files, plugins=plugins))
    return overlay_dir


# ---------------------------------------------------------------- engine
def engine_identity(engine: str, venv: str, patches: list[str] | None = None, patch_dir: str = PATCH_DIR) -> dict:
    """Engine name + version, the patches (name, sha256, upstream ref) and the RECORD check of the engine's
    distributions: `edited` lists installed files that differ from RECORD and no requested patch provides (refused by
    `sem serve`), `patched_in_place` the edited files whose content equals a requested patch's file.
    `installed_sha256`: sha256 over the sorted (distribution, path, sha256 as installed) of every RECORD entry."""
    site = site_packages(venv)
    dist = ENGINE_DIST[engine]
    main = _dist(dist, site)
    if main is None:
        raise StackError(f"{dist} is not installed in {venv} (engine {engine})")
    version = main.version
    loaded = load_patches(patches, engine, version, patch_dir)
    by_path = {rel: (p["name"], f["sha256"]) for p in loaded for rel, f in p["files"].items()}
    h = hashlib.sha256()
    edited, in_place, missing, checked = [], {}, [], 0
    for name in RECORD_VERIFIED[engine]:
        try:
            rec = record_files(name, site)
        except StackError:
            missing.append(f"{name}: not installed")
            continue
        missing += [f"{name}: {m}" for m in rec["missing"]]
        for path, f in sorted(rec["files"].items()):
            checked += 1
            h.update(f"{name}\0{path}\0{f['sha256']}\n".encode())
            if f["ok"]:
                continue
            patch = by_path.get(path)
            if patch and patch[1] == f["sha256"]:
                in_place[path] = patch[0]
            else:
                edited.append(f"{name}: {path}")
    sitecustomize = os.path.join(site, "sitecustomize.py")
    return {
        "name": engine, "distribution": dist, "version": version,
        "record": {"checked": checked, "ok": not edited and not missing, "edited": edited, "missing": missing,
                   "patched_in_place": in_place, "verified": list(RECORD_VERIFIED[engine])},
        "installed_sha256": h.hexdigest(),
        "packages": package_versions(site),
        "venv_sitecustomize": sha256_file(sitecustomize) if os.path.exists(sitecustomize) else None,
        "patches": [{"name": p["name"], "sha256": p["sha256"], "upstream_ref": p["upstream_ref"],
                     "description": p["description"], "files": {k: v["sha256"] for k, v in p["files"].items()},
                     **({"plugin": {"files": p["plugin"]["files"], "startup": p["plugin"]["startup"],
                                    "env": p["plugin"]["env"]}} if p.get("plugin") else {})}
                    for p in loaded],
        "_loaded_patches": loaded,  # dropped before the manifest is written
    }


def engine_problems(ident: dict) -> list[str]:
    rec = ident["record"]
    out = []
    if rec["edited"]:
        out.append("installed engine files differ from their wheel RECORD and no requested patch provides them "
                   "(reinstall the package, or register the edit under serving/patches/ and pass --patch):\n  "
                   + "\n  ".join(rec["edited"][:30]))
    if rec["missing"]:
        out.append("files listed in RECORD are missing:\n  " + "\n  ".join(rec["missing"][:30]))
    if ident.get("venv_sitecustomize"):
        out.append("the engine venv has its own sitecustomize.py in site-packages (it would run inside the engine "
                   "unrecorded): remove it, or register its effect as a patch")
    return out


# ---------------------------------------------------------------- model
MODEL_FILES = ("config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "tokenizer.model",
               "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt", "chat_template.jinja",
               "chat_template.json", "preprocessor_config.json", "processor_config.json", "video_preprocessor_config.json")
WEIGHT_EXT = (".safetensors", ".bin", ".pt", ".pth", ".gguf")
WEIGHTS_NOTE = ("weights by size + mtime (local directory) or by the HF cache blob name, which is the file's LFS "
                "sha256; full hashing of the weights is too slow for every launch")


def hf_cache_dir() -> str:
    try:
        from huggingface_hub import constants

        return constants.HF_HUB_CACHE
    except ImportError:  # pragma: no cover
        return os.path.join(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")), "hub")


def snapshot_dir(model: str, revision: str | None = None, cache: str | None = None) -> tuple[str | None, str | None]:
    """(snapshot directory, commit) of an HF id in the local cache (refs/<revision or main>), or (None, None)."""
    base = os.path.join(cache or hf_cache_dir(), "models--" + model.replace("/", "--"))
    rev = revision or "main"
    if re.fullmatch(r"[0-9a-f]{40}", rev):
        commit = rev
    else:
        try:
            with open(os.path.join(base, "refs", rev), encoding="utf-8") as f:
                commit = f.read().strip()
        except OSError:
            return None, None
    d = os.path.join(base, "snapshots", commit)
    return (d, commit) if os.path.isdir(d) else (None, None)


def model_identity(model: str, revision: str | None = None, cache: str | None = None) -> dict:
    """HF id + snapshot commit (local cache), or a local directory; the sha256 of every config / tokenizer / processor
    / template / remote-code file; weights by size + mtime (+ the LFS sha256 the HF cache names the blob by)."""
    if os.path.isdir(model):
        d, out = model, {"source": "local", "path": os.path.abspath(model), "revision": None}
    else:
        d, commit = snapshot_dir(model, revision, cache)
        out = {"source": "hf", "id": model, "revision": commit}
        if d is None:
            return {**out, "found": False,
                    "note": f"not in the local HF cache ({cache or hf_cache_dir()}): download it before serving"}
        out["path"] = d
    files, weights = {}, {}
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        if name in MODEL_FILES or name.endswith((".py", ".jinja")):
            files[name] = sha256_file(p)
        elif name.endswith(WEIGHT_EXT):
            st = os.stat(p)  # follows the cache symlink to the blob
            w = {"size": st.st_size, "mtime": int(st.st_mtime)}
            blob = os.path.basename(os.path.realpath(p))
            if re.fullmatch(r"[0-9a-f]{64}", blob):
                w = {"size": st.st_size, "lfs_sha256": blob}  # content-addressed: no mtime needed
            weights[name] = w
    gen = files.get("generation_config.json")
    gen_values = {}
    if gen:
        with open(os.path.join(d, "generation_config.json"), encoding="utf-8") as f:
            gen_values = json.load(f)
    return {**out, "found": True, "files": files, "weights": weights, "weights_note": WEIGHTS_NOTE,
            "generation_config": {"found": bool(gen), "sha256": gen, "values": gen_values}}


def default_chat_template(model_dir: str | None) -> tuple[str | None, str | None]:
    """(template text, source) of a model snapshot: chat_template.jinja, else tokenizer_config.json chat_template
    (the "default" entry of a named list), else chat_template.json."""
    if not model_dir:
        return None, None
    p = os.path.join(model_dir, "chat_template.jinja")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read(), "chat_template.jinja"
    for name in ("tokenizer_config.json", "chat_template.json"):
        p = os.path.join(model_dir, name)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            t = json.load(f).get("chat_template")
        if isinstance(t, list):
            t = next((x.get("template") for x in t if x.get("name") == "default"), None)
        if isinstance(t, str) and t:
            return t, f"{name}:chat_template"
    return None, None


# ---------------------------------------------------------------- hardware
def hardware(venv: str | None = None, run=subprocess.run) -> dict:
    """GPU names, driver and CUDA versions (nvidia-smi) and the torch version / CUDA build inside the engine venv."""
    out: dict = {}
    try:
        q = run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"], capture_output=True,
                text=True, timeout=30, check=True).stdout
        rows = [[c.strip() for c in line.split(",")] for line in q.splitlines() if line.strip()]
        names = [r[0] for r in rows]
        out["gpus"] = {n: names.count(n) for n in dict.fromkeys(names)}
        out["driver_version"] = rows[0][1] if rows else None
        head = run(["nvidia-smi"], capture_output=True, text=True, timeout=30, check=True).stdout
        m = re.search(r"CUDA Version:\s*([\d.]+)", head)
        out["cuda_driver_version"] = m.group(1) if m else None
    except (OSError, subprocess.SubprocessError) as e:
        out["gpus"] = None
        out["note"] = f"nvidia-smi unavailable ({type(e).__name__}): no GPU on this host"
    if venv:
        try:
            site = site_packages(venv)
            d = _dist("torch", site)
            out["torch"] = d.version if d else None
            vp = os.path.join(site, "torch", "version.py")
            if os.path.exists(vp):
                with open(vp, encoding="utf-8") as f:
                    m = re.search(r"^cuda[^=]*=\s*['\"]([^'\"]+)['\"]", f.read(), re.MULTILINE)
                out["torch_cuda"] = m.group(1) if m else None
        except StackError:
            pass
    return out
