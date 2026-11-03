"""Server manifests (`sem serve`, docs/design/v0.5.md "Serving"): what serves a model, written by the server side
and checked by `sem run --server MANIFEST`.

    {"format": "sem-server/1",
     "engine":   name, version, RECORD check, installed_sha256, package versions, patches (name, sha256, upstream ref)
     "launch":   argv (secrets removed, machine-local paths replaced by placeholders), dp, tp, env set for the engine,
                 deterministic flag, batch (hf), server-side model args
     "chat_template": source, sha256 of the file passed with --chat-template
     "model":    HF id + snapshot revision or a local path; digests of config / tokenizer / processor / template files,
                 weights by size + mtime (or the LFS sha256), generation_config.json digest + values
     "hardware": GPU names + counts, driver and CUDA versions, torch inside the engine venv
     "served":   what the running server reports (/v1/models model id + max_model_len, engine version)
     "probe":    sha256 of the token ids of a fixed conversation (system + user text + one small image) rendered by the
                 server's tokenize endpoint, or {"status": "unsupported", "note"}
     "runtime":  VOLATILE: host, port, base URL, pid, times, restarts, status, the overlay / template / log paths}

digest(manifest) is the sha256 of everything except `runtime` (hosts, ports, pids, times): it goes into lock.json, the
resume comparison (runner._lock_digest) and every trial's generation_lock.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request

from simple_evals_mm.errors import UserError

FORMAT = "sem-server/1"
VOLATILE = ("runtime",)
PROBE_SYSTEM = "You are a helpful assistant."
PROBE_TEXT = "What color is this image? Answer in one word."
# tokenize endpoints with chat messages, tried in order: sglang (/v1/tokenize, /tokenize: messages since 0.5.x), vLLM
# (/tokenize with messages), serving/hf_server.py (/tokenize)
TOKENIZE_PATHS = ("/tokenize", "/v1/tokenize")
SECRET_FLAG = re.compile(r"(?:api[-_]?key|token|secret|password)", re.IGNORECASE)


class ManifestError(UserError, RuntimeError):
    pass


def probe_image_data_url() -> str:
    """A fixed 8x8 red PNG (bytes generated once, so the probe never depends on the PIL version)."""
    return "data:image/png;base64," + PROBE_PNG_B64


# 8x8 RGB (255, 0, 0) PNG; tests check it decodes to that image
PROBE_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP8z4AdMOEQH6QSAM1BAQ/oQeJvAAAAAElFTkSuQmCC"


def probe_messages() -> list[dict]:
    return [{"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": probe_image_data_url()}},
                                         {"type": "text", "text": PROBE_TEXT}]}]


def _post_json(url: str, body: dict, timeout: float = 30.0, key: str | None = None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url: str, timeout: float = 15.0, key: str | None = None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def root_url(base_url: str) -> str:
    return re.sub(r"/v1/?$", "", base_url.rstrip("/"))


def run_probe(base_url: str, model_id: str | None, timeout: float = 60.0, key: str | None = None) -> dict:
    """Render the probe conversation through the server's tokenize endpoint: {"status": "ok", "endpoint", "sha256" of
    the token ids (JSON list), "n_tokens"}, or {"status": "unsupported", "note"} when no endpoint renders messages."""
    body = {"messages": probe_messages()}
    if model_id:
        body["model"] = model_id
    tried = []
    for path in TOKENIZE_PATHS:
        url = root_url(base_url) + path
        try:
            out = _post_json(url, body, timeout, key)
        except urllib.error.HTTPError as e:
            tried.append(f"{path}: HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001 - connection problems: reported
            tried.append(f"{path}: {type(e).__name__}")
            continue
        tokens = out.get("tokens") if isinstance(out, dict) else None
        if isinstance(tokens, list) and tokens and all(isinstance(t, int) for t in tokens):
            return {"status": "ok", "endpoint": path, "n_tokens": len(tokens),
                    "sha256": hashlib.sha256(json.dumps(tokens).encode()).hexdigest()}
        tried.append(f"{path}: no token ids in the response")
    return {"status": "unsupported", "note": "no tokenize endpoint rendered the probe messages (" + "; ".join(tried) + ")"}


def served_info(base_url: str, timeout: float = 15.0, key: str | None = None) -> dict:
    """/v1/models (model id, root, max_model_len) and the version the server reports (/server_info, /version)."""
    rows = (_get_json(base_url.rstrip("/") + "/models", timeout, key).get("data") or [])
    m = rows[0] if rows else {}
    out = {"model_id": m.get("id"), "root": m.get("root"), "max_model_len": m.get("max_model_len"), "version": None}
    for path in ("/server_info", "/version", "/get_server_info"):
        try:
            info = _get_json(root_url(base_url) + path, timeout, key)
        except Exception:  # noqa: BLE001, S112 - optional endpoints
            continue
        if isinstance(info, dict) and info.get("version"):
            out["version"] = info["version"]
            for k in ("batch", "backend", "model_revision", "deterministic"):
                if k in info:
                    out[k] = info[k]
            break
    return out


# ---------------------------------------------------------------- argv
def redact_argv(argv: list[str], replace: dict[str, str] | None = None) -> list[str]:
    """argv without secrets (the value of any --*key* / --*token* / --*secret* flag) and with machine-local paths
    replaced by placeholders (replace: {path: "<placeholder>"})."""
    out, hide = [], False
    for a in argv:
        if hide:
            out.append("<redacted>")
            hide = False
            continue
        if a.startswith("--") and SECRET_FLAG.search(a.split("=", 1)[0]):
            if "=" in a:
                out.append(a.split("=", 1)[0] + "=<redacted>")
            else:
                out.append(a)
                hide = True
            continue
        for path, ph in (replace or {}).items():
            if path and path in a:
                a = a.replace(path, ph)
        out.append(a)
    return out


# ---------------------------------------------------------------- digest / files
# Hardware fields recorded but not compared: the driver (and the CUDA version it reports) can differ between ABCI
# nodes without changing the engine's numerics; the GPU model and the torch / CUDA build inside the venv are compared.
HARDWARE_INFO_ONLY = ("driver_version", "cuda_driver_version", "note")


def stable(manifest: dict) -> dict:
    """The manifest without the volatile part (hosts, ports, pids, times, paths of this session; the GPU driver)."""
    out = {k: v for k, v in manifest.items() if k not in VOLATILE}
    if isinstance(out.get("hardware"), dict):
        out["hardware"] = {k: v for k, v in out["hardware"].items() if k not in HARDWARE_INFO_ONLY}
    return out


def digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(stable(manifest), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def write(path: str, manifest: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(path + ".part", path)


def read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        raise ManifestError(f"cannot read the server manifest {path}: {type(e).__name__}: {e}") from e
    if m.get("format") != FORMAT:
        raise ManifestError(f"{path}: not a server manifest (format {m.get('format')!r}, expected {FORMAT})")
    return m


# ---------------------------------------------------------------- client side
def check_spec(manifest: dict, provider: str, model: str, server_args: dict | None = None) -> list[str]:
    """The job's slug and server-side --model-arg values against the manifest (before any request)."""
    problems = []
    eng = (manifest.get("engine") or {}).get("name")
    if eng != provider:
        problems.append(f"the manifest's engine is {eng!r}, the model slug's provider is {provider!r}")
    served = (manifest.get("served") or {}).get("model_id") or (manifest.get("model") or {}).get("id")
    if served and served != model:
        problems.append(f"the server serves {served!r}, the job asks for {model!r}")
    st = (manifest.get("runtime") or {}).get("status")
    if st != "serving":
        problems.append(f"the manifest's status is {st!r} (not serving)")
    have = (manifest.get("launch") or {}).get("server_args") or {}
    for k, v in (server_args or {}).items():
        if k == "chat_template":
            want = _file_sha(v)
            got = (manifest.get("chat_template") or {}).get("sha256")
            if want != got:
                problems.append(f"--model-arg chat_template={v}: sha256 {want} differs from the template the server "
                                f"renders with ({got})")
        elif have.get(k) != v:
            problems.append(f"--model-arg {k}={v!r}: the server was launched with {k}={have.get(k)!r}")
    for k, v in have.items():
        if k not in (server_args or {}):
            problems.append(f"the server was launched with {k}={v!r}: pass --model-arg {k}={json.dumps(v)} so the job "
                            "records it")
    return problems


def _file_sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def verify_running(manifest: dict, base_url: str, key: str | None = None, info: dict | None = None) -> list[str]:
    """The running server at base_url against the manifest: /v1/models model id and max_model_len, the reported
    version, and the probe hash (when the manifest has one). info: an already fetched /v1/models record."""
    problems = []
    try:
        info = info or served_info(base_url, key=key)
    except Exception as e:  # noqa: BLE001
        return [f"the server at {base_url} does not answer GET /models ({type(e).__name__})"]
    want = manifest.get("served") or {}
    for k in ("model_id", "max_model_len", "version"):
        if want.get(k) is not None and info.get(k) is not None and want[k] != info[k]:
            problems.append(f"server {k} {info[k]!r} differs from the manifest's {want[k]!r}")
    probe = manifest.get("probe") or {}
    if probe.get("status") == "ok":
        now = run_probe(base_url, want.get("model_id"), key=key)
        if now.get("sha256") != probe.get("sha256"):
            problems.append(f"the rendered probe differs from the manifest's ({now.get('sha256') or now.get('note')} vs "
                            f"{probe.get('sha256')}): another chat template / tokenizer is serving")
    return problems


def probe_image_ok() -> bool:
    """The embedded probe PNG decodes to an 8x8 red image (tests)."""
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(PROBE_PNG_B64))) as im:
        return im.size == (8, 8) and im.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
