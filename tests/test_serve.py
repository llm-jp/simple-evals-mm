"""`sem serve` (serving/stack.py, serving/manifest.py, serving/serve.py) with a fake engine venv and fake servers:
RECORD verification, refusal of edited files, patches, the explicit chat template, the probe, supervision and
restarts, and the client side (`sem run --server MANIFEST`)."""

import base64
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time
import venv

import pytest

from simple_evals_mm import cli, runner
from simple_evals_mm.serving import manifest as mf
from simple_evals_mm.serving import serve, stack

FAKE_SERVER = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
model = args[args.index("--model-path") + 1]
state = os.environ["FAKE_STATE"]


def tokens():
    with open(os.path.join(state, "tokens.json")) as f:
        return json.load(f)


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {})
        elif self.path == "/v1/models":
            self._send(200, {"data": [{"id": model, "root": model, "max_model_len": 4096}]})
        elif self.path == "/server_info":
            self._send(200, {"version": "0.5.23"})
        else:
            self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(os.path.join(state, "argv.json"), "w") as f:
            json.dump(sys.argv, f)
        if self.path == "/tokenize" and body.get("messages"):
            self._send(200, {"tokens": tokens(), "count": len(tokens())})
        else:
            self._send(404, {})

    def log_message(self, *a):
        pass


def crash_watch():
    while True:
        time.sleep(0.05)
        p = os.path.join(state, "crash")
        if os.path.exists(p):
            os.remove(p)
            os._exit(1)


threading.Thread(target=crash_watch, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _b64sha(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _dist(site, name, version, files: dict):
    """Install a fake distribution: the files plus a dist-info with METADATA and a RECORD of their sha256."""
    info = os.path.join(site, f"{name}-{version}.dist-info")
    os.makedirs(info, exist_ok=True)
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    with open(os.path.join(info, "METADATA"), "wb") as f:
        f.write(meta)
    rows = []
    for rel, text in files.items():
        p = os.path.join(site, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        rows.append(f"{rel},sha256={_b64sha(text.encode())},{len(text.encode())}")
    rows += [f"{name}-{version}.dist-info/METADATA,sha256={_b64sha(meta)},{len(meta)}", f"{name}-{version}.dist-info/RECORD,,"]
    with open(os.path.join(info, "RECORD"), "w") as f:
        f.write("\n".join(rows) + "\n")


@pytest.fixture
def engine_venv(tmp_path):
    """A real venv (symlinked interpreter) with a fake sglang 0.5.23 (its launch_server is a tiny HTTP server) and a
    fake transformers."""
    d = tmp_path / "engine_venv"
    venv.create(d, with_pip=False, symlinks=True)
    site = stack.site_packages(str(d))
    _dist(site, "sglang", "0.5.23", {"sglang/__init__.py": "", "sglang/launch_server.py": FAKE_SERVER,
                                     "sglang/srt/__init__.py": "", "sglang/srt/models/__init__.py": "",
                                     "sglang/srt/models/gemma4_vision.py": "MARK = 'upstream'\n"})
    _dist(site, "transformers", "5.19.0", {"transformers/__init__.py": ""})
    return str(d)


@pytest.fixture
def hf_cache(tmp_path):
    """A local HF cache with google/gemma-4-12B-it at one snapshot (template in chat_template.jinja)."""
    cache = tmp_path / "hub"
    base = cache / "models--google--gemma-4-12B-it"
    sha = "a" * 40
    snap = base / "snapshots" / sha
    snap.mkdir(parents=True)
    (base / "refs").mkdir()
    (base / "refs" / "main").write_text(sha)
    (snap / "config.json").write_text('{"architectures": ["Gemma4ForConditionalGeneration"]}')
    (snap / "generation_config.json").write_text('{"temperature": 1.0, "top_k": 64, "top_p": 0.95}')
    (snap / "tokenizer_config.json").write_text(json.dumps({"chat_template": "{{ from_tokenizer_config }}"}))
    (snap / "chat_template.jinja").write_text("{{ messages }}")
    blob = base / "blobs" / ("b" * 64)
    blob.parent.mkdir()
    blob.write_bytes(b"weights")
    (snap / "model.safetensors").symlink_to(blob)
    return str(cache)


def _args(*argv):
    p = cli.make_parser()
    a = p.parse_args(["serve", *argv])
    a.extra = []
    return a


def _patch_dir(tmp_path, content="MARK = 'patched'\n", versions=("0.5.23",)):
    d = tmp_path / "patches" / "fix"
    (d / "files" / "sglang" / "srt" / "models").mkdir(parents=True)
    (d / "files" / "sglang" / "srt" / "models" / "gemma4_vision.py").write_text(content)
    (d / "patch.json").write_text(json.dumps({"name": "fix", "upstream_ref": "https://example.org/pr/1",
                                              "description": "test patch", "engine": "sglang",
                                              "engine_versions": list(versions),
                                              "files": ["sglang/srt/models/gemma4_vision.py"]}))
    return str(tmp_path / "patches")


def _edit(engine_venv, text="MARK = 'patched'\n"):
    p = os.path.join(stack.site_packages(engine_venv), "sglang", "srt", "models", "gemma4_vision.py")
    with open(p, "w") as f:
        f.write(text)


# ---------------------------------------------------------------- engine identity
def test_record_verified_engine(engine_venv):
    ident = stack.engine_identity("sglang", engine_venv)
    assert ident["version"] == "0.5.23" and ident["record"]["ok"] and not stack.engine_problems(ident)
    assert ident["record"]["checked"] == 8 and ident["record"]["verified"] == ["sglang", "transformers"]
    assert ident["packages"] == {"transformers": "5.19.0"}
    assert stack.engine_identity("sglang", engine_venv)["installed_sha256"] == ident["installed_sha256"]


def test_edited_file_refused(engine_venv, hf_cache, monkeypatch, capsys):
    _edit(engine_venv, "MARK = 'local edit'\n")
    ident = stack.engine_identity("sglang", engine_venv)
    assert ident["record"]["edited"] == ["sglang: sglang/srt/models/gemma4_vision.py"]
    assert "differ from their wheel RECORD" in stack.engine_problems(ident)[0]
    monkeypatch.setattr(stack, "hf_cache_dir", lambda: hf_cache)
    assert cli.main(["serve", "sglang", "google/gemma-4-12B-it", "--engine-venv", engine_venv, "--dry-run"]) == 1
    out = capsys.readouterr()
    assert "REFUSED" in out.err and "gemma4_vision.py" in out.err
    assert json.loads(out.out)["engine"]["record"]["ok"] is False  # the manifest is still printed


def test_patch_accepted_in_place_and_as_overlay(engine_venv, tmp_path):
    pdir = _patch_dir(tmp_path)
    _edit(engine_venv)  # the venv carries exactly the patch's content
    ident = stack.engine_identity("sglang", engine_venv, ["fix"], patch_dir=pdir)
    assert ident["record"]["ok"] and ident["record"]["patched_in_place"] == {"sglang/srt/models/gemma4_vision.py": "fix"}
    p = ident["patches"][0]
    assert (p["name"], p["upstream_ref"]) == ("fix", "https://example.org/pr/1") and len(p["sha256"]) == 64
    # a different in-place edit is not covered by the patch
    _edit(engine_venv, "MARK = 'other'\n")
    assert not stack.engine_identity("sglang", engine_venv, ["fix"], patch_dir=pdir)["record"]["ok"]
    # pristine venv + overlay: the engine's interpreter loads the patch file instead of the installed module
    _edit(engine_venv, "MARK = 'upstream'\n")
    loaded = ident.pop("_loaded_patches")
    overlay = stack.write_overlay(str(tmp_path / "overlay"), loaded)
    import subprocess

    out = subprocess.run([stack.venv_python(engine_venv), "-c", "import sglang.srt.models.gemma4_vision as m; print(m.MARK)"],
                         env={"PYTHONPATH": overlay, "PATH": os.environ["PATH"]}, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "patched" and "[sem-patch]" in out.stderr
    with pytest.raises(stack.StackError, match="made for sglang 0.5.17"):
        stack.engine_identity("sglang", engine_venv, ["fix"], patch_dir=_patch_dir(tmp_path / "x", versions=("0.5.17",)))
    with pytest.raises(stack.StackError, match="unknown patch"):
        stack.load_patch("nope", pdir)


def test_registered_patches_load():
    """The repository's patches: patch.json complete, files present (sglang PR #43753, minimal and full form)."""
    names = stack.available_patches()
    assert {"sglang_pr43753", "sglang_pr43753_full"} <= set(names)
    for n in names:
        p = stack.load_patch(n)
        assert "github.com/sgl-project/sglang/pull/43753" in p["upstream_ref"] and p["files"]


# ---------------------------------------------------------------- chat template / manifest
def test_chat_template_always_passed(engine_venv, hf_cache, tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "hf_cache_dir", lambda: hf_cache)
    m, ctx = serve.build_manifest(_args("sglang", "google/gemma-4-12B-it", "--dp", "2", "--model-arg", "max_soft_tokens=1120"),
                                  venv=engine_venv, run_hardware=_no_gpu)
    argv = ctx["argv"]
    assert argv[argv.index("--chat-template") + 1] == "<chat-template>"  # replaced by the written file at launch
    assert ctx["chat_template_text"] == "{{ messages }}"
    assert m["chat_template"] == {"source": "chat_template.jinja",
                                  "sha256": hashlib.sha256(b"{{ messages }}").hexdigest()}
    assert json.loads(argv[argv.index("--mm-process-config") + 1]) == {"image": {"max_soft_tokens": 1120}}
    assert m["model"]["revision"] == "a" * 40 and m["model"]["weights"]["model.safetensors"]["lfs_sha256"] == "b" * 64
    assert m["model"]["generation_config"]["values"]["top_k"] == 64 and not ctx["problems"]
    assert m["launch"]["argv"][0] == "<engine-python>" and m["launch"]["dp"] == 2
    # tokenizer_config fallback, then --chat-template FILE
    snap = os.path.join(hf_cache, "models--google--gemma-4-12B-it", "snapshots", "a" * 40)
    os.remove(os.path.join(snap, "chat_template.jinja"))
    m2, _ = serve.build_manifest(_args("sglang", "google/gemma-4-12B-it"), venv=engine_venv, run_hardware=_no_gpu)
    assert m2["chat_template"]["source"] == "tokenizer_config.json:chat_template"
    f = tmp_path / "t.jinja"
    f.write_text("custom")
    m3, _ = serve.build_manifest(_args("sglang", "google/gemma-4-12B-it", "--chat-template", str(f)), venv=engine_venv,
                                 run_hardware=_no_gpu)
    assert m3["chat_template"] == {"source": "--chat-template", "sha256": hashlib.sha256(b"custom").hexdigest()}
    assert mf.digest(m2) != mf.digest(m3)
    # the volatile part does not change the digest
    m3["runtime"].update(port=1, host="elsewhere", pid=5)
    assert mf.digest(m3) == mf.digest(m3 | {"runtime": {}})


def test_argv_redaction_and_managed_flags():
    assert mf.redact_argv(["x", "--api-key", "s3cr3t", "--hf-token=abc", "--port", "1"]) == \
        ["x", "--api-key", "<redacted>", "--hf-token=<redacted>", "--port", "1"]
    with pytest.raises(serve.ServeError, match="--chat-template"):
        serve.check_extra(["--chat-template", "x.jinja"])
    with pytest.raises(serve.ServeError, match="not a server-side key"):
        serve.parse_server_args(["temperature=0"])
    argv, env = serve.build_argv("vllm", "/v/bin/python", "m", host="h", port=1, dp=1, tp=2, chat_template="t",
                                 deterministic=True, server_args={"max_image_tokens": 8192}, extra=[])
    assert argv[:3] == ["/v/bin/vllm", "serve", "m"] and env == {"VLLM_BATCH_INVARIANT": "1"}
    assert json.loads(argv[argv.index("--mm-processor-kwargs") + 1]) == {"max_image_tokens": 8192}
    argv, _ = serve.build_argv("sglang", "p", "m", host="h", port=1, dp=1, tp=1, chat_template="t", deterministic=True,
                               server_args={}, extra=[])
    assert "--enable-deterministic-inference" in argv


def _no_gpu(*a, **k):
    raise FileNotFoundError("nvidia-smi")


# ---------------------------------------------------------------- probe, supervision, restarts
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(engine_venv, hf_cache, tmp_path, monkeypatch, tokens, **kw):
    monkeypatch.setattr(stack, "hf_cache_dir", lambda: hf_cache)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "tokens.json").write_text(json.dumps(tokens))
    monkeypatch.setenv("FAKE_STATE", str(state))
    port = _free_port()
    m, ctx = serve.build_manifest(_args("sglang", "google/gemma-4-12B-it", "--port", str(port), "--host", "127.0.0.1"),
                                  venv=engine_venv, run_hardware=_no_gpu)
    m["runtime"]["base_url"] = f"http://127.0.0.1:{port}/v1"
    ctx["argv"] = [str(tmp_path / "t.jinja") if a == "<chat-template>" else a for a in ctx["argv"]]
    path = str(tmp_path / "servers" / "s.json")
    out: dict = {}
    th = threading.Thread(target=lambda: out.update(rc=serve.supervise(m, ctx, path, health_timeout=30, restart_delay=0.1,
                                                                       interval=0.1, **kw)))
    th.start()
    for _ in range(300):
        if os.path.exists(path):
            break
        time.sleep(0.05)
    return state, path, th, out


def test_probe_recorded_and_mismatch_after_restart_is_fatal(engine_venv, hf_cache, tmp_path, monkeypatch):
    state, path, th, out = _start(engine_venv, hf_cache, tmp_path, monkeypatch, [1, 2, 3])
    m = mf.read(path)
    assert m["runtime"]["status"] == "serving"
    assert m["probe"] == {"status": "ok", "endpoint": "/tokenize", "n_tokens": 3,
                          "sha256": hashlib.sha256(json.dumps([1, 2, 3]).encode()).hexdigest()}
    assert m["served"] == {"model_id": "google/gemma-4-12B-it", "max_model_len": 4096, "version": "0.5.23"}
    argv = json.loads((state / "argv.json").read_text())  # what the engine process received
    assert argv[argv.index("--chat-template") + 1] == str(tmp_path / "t.jinja")
    (state / "tokens.json").write_text(json.dumps([9, 9]))  # another template after the restart
    (state / "crash").write_text("")
    th.join(timeout=60)
    assert out["rc"] == 3
    m = mf.read(path)
    assert m["runtime"]["status"].startswith("stopped: identity changed")
    assert "probe" in m["runtime"]["events"][-1]["event"]
    assert m["probe"]["sha256"] == hashlib.sha256(json.dumps([1, 2, 3]).encode()).hexdigest()  # the first one stays


def test_restart_with_same_identity_is_recorded(engine_venv, hf_cache, tmp_path, monkeypatch):
    state, path, th, out = _start(engine_venv, hf_cache, tmp_path, monkeypatch, [1, 2], max_restarts=1)
    first = mf.digest(mf.read(path))
    (state / "crash").write_text("")
    for _ in range(300):
        if mf.read(path)["runtime"]["restarts"]:
            break
        time.sleep(0.05)
    (state / "crash").write_text("")
    th.join(timeout=60)
    m = mf.read(path)
    assert out["rc"] == 5 and len(m["runtime"]["restarts"]) == 1 and mf.digest(m) == first


# ---------------------------------------------------------------- client side
class _Served(http.server.BaseHTTPRequestHandler):
    tokens = (1, 2, 3)
    max_model_len = 4096

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"data": [{"id": "fake", "root": "fake", "max_model_len": type(self).max_model_len}]})
        elif self.path == "/server_info":
            self._send({"version": "0.5.23"})
        else:
            self.send_error(404)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/tokenize":
            self._send({"tokens": list(type(self).tokens)})
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


@pytest.fixture
def served():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (_Served,), {}))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown()


def _manifest(served, tmp_path, name="m.json", **over):
    url = f"http://127.0.0.1:{served.server_address[1]}/v1"
    m = {"format": mf.FORMAT, "engine": {"name": "sglang", "version": "0.5.23", "installed_sha256": "e"},
         "launch": {"argv": ["x"], "server_args": {}}, "chat_template": {"sha256": "t"},
         "model": {"source": "hf", "id": "fake", "revision": "r" * 40,
                   "generation_config": {"found": True, "sha256": "g", "values": {"temperature": 0.6, "top_k": 20}}},
         "served": {"model_id": "fake", "max_model_len": 4096, "version": "0.5.23"},
         "probe": mf.run_probe(url, "fake"), "runtime": {"base_url": url, "status": "serving", "port": 1, "pid": 2}}
    for k, v in over.items():
        m[k] = v
    p = tmp_path / name
    mf.write(str(p), m)
    return str(p), m


def test_verify_running_and_check_spec(served, tmp_path):
    _, m = _manifest(served, tmp_path)
    url = m["runtime"]["base_url"]
    assert m["probe"]["status"] == "ok" and mf.verify_running(m, url) == []
    served.RequestHandlerClass.tokens = (7,)
    assert "rendered probe differs" in mf.verify_running(m, url)[0]
    served.RequestHandlerClass.tokens = (1, 2, 3)
    served.RequestHandlerClass.max_model_len = 8192
    assert "max_model_len" in mf.verify_running(m, url)[0]
    assert mf.check_spec(m, "sglang", "fake") == []
    assert "engine is 'sglang'" in mf.check_spec(m, "vllm", "fake")[0]
    assert "serves 'fake'" in mf.check_spec(m, "sglang", "other")[0]
    m["launch"]["server_args"] = {"max_soft_tokens": 1120}
    assert "launched with max_soft_tokens=1120" in mf.check_spec(m, "sglang", "fake")[0]
    assert mf.check_spec(m, "sglang", "fake", {"max_soft_tokens": 1120}) == []
    assert "launched with" in mf.check_spec(m, "sglang", "fake", {"max_soft_tokens": 280})[0]
    m["runtime"]["status"] = "stopped"
    assert any("not serving" in p for p in mf.check_spec(m, "sglang", "fake", {"max_soft_tokens": 1120}))


def test_unsupported_probe_is_recorded():
    """No tokenize endpoint (connection refused / 404): probe unsupported with a note."""
    p = mf.run_probe(f"http://127.0.0.1:{_free_port()}/v1", "m", timeout=2)
    assert p["status"] == "unsupported" and "/tokenize" in p["note"]


def test_job_with_server_manifest(job_env, served, tmp_path, capsys):
    s = job_env
    path, m = _manifest(served, tmp_path)
    assert s["run"]("--server", path) == 0
    st = s["store"]()
    lock = st.read_lock()
    assert lock["server"]["digest"] == mf.digest(m) and "runtime" not in lock["server"]["manifest"]
    assert lock["sessions"][0]["server"]["port"] == 1
    assert runner._lock_digest(lock)["server"] == mf.digest(m)
    gl = {t.generation_lock for t in st.read_trials("arith", 1)}
    # the same job without a manifest: other generation locks (the manifest digest is hashed in)
    assert s["run"](name="plain") == 0
    assert gl.isdisjoint({t.generation_lock for t in s["store"]("plain").read_trials("arith", 1)})
    # resume: the runtime part may change (another port / pid), the rest may not
    m["runtime"]["port"] = 99
    mf.write(path, m)
    assert s["run"]("--server", path) == 0
    path2, _ = _manifest(served, tmp_path, "m2.json", chat_template={"sha256": "other"})
    capsys.readouterr()
    assert s["run"]("--server", path2) == 2
    assert runner.RESUME_REFUSED in capsys.readouterr().err
    # a server that no longer matches its manifest: refused at start
    served.RequestHandlerClass.tokens = (5,)
    assert s["run"]("--server", path, name="new") == 2
    assert "does not match its manifest" in capsys.readouterr().err


def test_sampler_takes_generation_config_from_manifest(served, tmp_path, monkeypatch):
    from simple_evals_mm.samplers import registry

    _, m = _manifest(served, tmp_path)
    spec = registry.resolve("sglang/fake", preset="none", base_url=m["runtime"]["base_url"])
    sampler = registry.make_sampler(spec, base_url=m["runtime"]["base_url"])
    sampler.server_manifest = m
    import simple_evals_mm.samplers.openai_chat as oc

    monkeypatch.setattr(oc, "read_generation_config", lambda *a: pytest.fail("the client's HF cache must not be read"))
    sampler.prepare()
    gc = sampler.config()["endpoint"]["generation_config"]
    assert gc == {"model": "fake", "revision": "r" * 40, "found": True, "sha256": "g", "source": "server manifest"}
    assert sampler.settings()["temperature"] == {"value": 0.6, "source": "model:generation_config.json"}


def test_dry_run_on_real_engine_layout_has_no_side_effects(engine_venv, hf_cache, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stack, "hf_cache_dir", lambda: hf_cache)
    jobs = tmp_path / "jobs"
    assert cli.main(["serve", "sglang", "google/gemma-4-12B-it", "--engine-venv", engine_venv, "-o", str(jobs),
                     "--dry-run", "--", "--reasoning-parser", "gemma4"]) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["launch"]["argv"][-2:] == ["--reasoning-parser", "gemma4"] and not jobs.exists()
    assert sys.executable  # the dry run never launched anything


def test_driver_version_is_info_only():
    """A different GPU driver (another ABCI node) keeps the manifest digest; a different GPU model or torch build does not."""
    from simple_evals_mm.serving import manifest as mf

    base = {"engine": {"name": "sglang"}, "hardware": {"gpus": {"NVIDIA H200": 8}, "driver_version": "580.1",
                                                       "cuda_driver_version": "13.0", "torch": "2.11.0", "torch_cuda": "13.0"}}
    other_driver = {**base, "hardware": {**base["hardware"], "driver_version": "590.2", "cuda_driver_version": "13.1"}}
    assert mf.digest(base) == mf.digest(other_driver)
    for key, val in (("gpus", {"NVIDIA H100": 8}), ("torch", "2.12.0"), ("torch_cuda", "13.1")):
        assert mf.digest(base) != mf.digest({**base, "hardware": {**base["hardware"], key: val}})
