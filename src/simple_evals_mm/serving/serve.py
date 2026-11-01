"""`sem serve ENGINE MODEL`: launch an OpenAI-compatible server (sglang / vLLM / serving/hf_server.py) from its own
venv, supervise it and write the server manifest (serving/manifest.py) that `sem run --server MANIFEST` checks.

    sem serve sglang google/gemma-4-12B-it --engine-venv /path/sglang_venv --dp 8 [--patch NAME ...]
              [--chat-template FILE] [--model-arg max_soft_tokens=1120] [--deterministic] [--port P] [--tp N]
              [--manifest PATH | -o JOBS_DIR] [--dry-run] [-- extra engine args]

  1. engine identity: version + content hash of the installed files, verified against the wheel RECORD; an edited file
     is refused (listed) unless a requested patch (serving/patches/<name>/) provides exactly that content. Patches are
     loaded through a sitecustomize overlay on PYTHONPATH (the venv is never modified).
  2. chat template: always passed explicitly (--chat-template), by default the model snapshot's own template written
     next to the manifest, so the engine never auto-selects a built-in template; its sha256 is recorded.
  3. launch: the engine in a new process group; restart loop; health wait; on exit (signal, Ctrl-C) the process
     group is killed and the manifest's status becomes "stopped".
  4. after the health wait (and after every restart): /v1/models + the rendered-prompt probe (tokenize endpoint). The
     first launch writes the manifest (the file appears once the server is ready); after a restart a different probe
     hash, served identity or engine identity is fatal: the server is stopped and the reason recorded.

`--dry-run` builds and prints the manifest without launching (no probe, no hardware beyond nvidia-smi); exit 1 when the
engine check refuses.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime

from simple_evals_mm.serving import manifest as mf
from simple_evals_mm.serving import stack

ENGINES = ("sglang", "vllm", "hf")
DEFAULT_PORTS = {"sglang": 30000, "vllm": 8000, "hf": 30050}
# server-side --model-arg keys (samplers/presets.MODEL_ARGS target "server") and how each engine receives them
SERVER_ARGS = {
    "max_soft_tokens": {"sglang": ("--mm-process-config", "image"), "vllm": ("--mm-processor-kwargs", None)},
    "max_image_tokens": {"sglang": ("--mm-process-config", "image"), "vllm": ("--mm-processor-kwargs", None)},
}
# flags sem serve sets itself: refused in the extra engine args
MANAGED_FLAGS = {"--model-path", "--model", "--host", "--port", "--dp-size", "--tp-size", "--data-parallel-size",
                 "--tensor-parallel-size", "-dp", "-tp", "--chat-template", "--enable-deterministic-inference",
                 "--mm-process-config", "--mm-processor-kwargs", "--dp", "--batch", "--deterministic", "--revision"}
# environment variables recorded in the manifest (their values change engine behaviour); secrets never
ENV_PREFIXES = ("SGLANG_", "VLLM_", "TORCH_", "PYTORCH_", "CUBLAS_", "NVIDIA_TF32", "FLASHINFER_", "NCCL_ALGO",
                "TRITON_", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "GEMMA_", "MUSE_", "HARMONY_", "QWEN_DECODE",
                "SWEEP_", "REP_PEN", "EVAL_CHAT_TEMPLATE")
DETERMINISTIC = {"sglang": "--enable-deterministic-inference", "vllm": "VLLM_BATCH_INVARIANT=1",
                 "hf": "--deterministic (torch.use_deterministic_algorithms)"}


class ServeError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(model.rstrip("/")) if os.path.isdir(model) else model)


# ---------------------------------------------------------------- config
def parse_server_args(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise ServeError(f"--model-arg expects k=v, got {it!r}")
        if k not in SERVER_ARGS:
            raise ServeError(f"--model-arg {k}: not a server-side key (server keys: {', '.join(SERVER_ARGS)}; "
                             "chat_template: use --chat-template)")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def engine_flags(engine: str, server_args: dict) -> list[str]:
    """--mm-process-config / --mm-processor-kwargs for the server-side model args (hf: none are supported)."""
    if not server_args:
        return []
    if engine == "hf":
        raise ServeError(f"hf_server takes no server-side model args ({', '.join(server_args)})")
    sg: dict = {}
    vl: dict = {}
    for k, v in server_args.items():
        _flag, section = SERVER_ARGS[k][engine]
        if engine == "sglang":
            sg.setdefault(section, {})[k] = v
        else:
            vl[k] = v
    if engine == "sglang":
        return ["--mm-process-config", json.dumps(sg, sort_keys=True)]
    return ["--mm-processor-kwargs", json.dumps(vl, sort_keys=True)]


def check_extra(extra: list[str]) -> None:
    bad = sorted({a.split("=", 1)[0] for a in extra if a.split("=", 1)[0] in MANAGED_FLAGS})
    if bad:
        raise ServeError(f"extra engine args set flags sem serve manages: {', '.join(bad)} (use the sem serve options)")


def build_argv(engine: str, python: str, model: str, *, host: str, port: int, dp: int, tp: int, chat_template: str,
               deterministic: bool, server_args: dict, extra: list[str], batch: int = 1,
               revision: str | None = None) -> tuple[list[str], dict]:
    """(argv, environment variables set for the engine)."""
    env: dict = {}
    if engine == "sglang":
        argv = [python, "-m", "sglang.launch_server", "--model-path", model, "--host", host, "--port", str(port),
                "--dp-size", str(dp), "--tp-size", str(tp), "--chat-template", chat_template]
        if deterministic:
            argv.append("--enable-deterministic-inference")
    elif engine == "vllm":
        argv = [os.path.join(os.path.dirname(python), "vllm"), "serve", model, "--host", host, "--port", str(port),
                "--data-parallel-size", str(dp), "--tensor-parallel-size", str(tp), "--chat-template", chat_template]
        if deterministic:
            env["VLLM_BATCH_INVARIANT"] = "1"
    elif engine == "hf":
        if tp != 1:
            raise ServeError("hf_server has no tensor parallelism (--tp 1)")
        argv = [python, "-m", "simple_evals_mm.serving.hf_server", "--model", model, "--host", host, "--port", str(port),
                "--dp-size", str(dp), "--chat-template", chat_template, "--batch", str(batch)]
        if revision:
            argv += ["--revision", revision]
        if deterministic:
            argv.append("--deterministic")
    else:
        raise ServeError(f"unknown engine {engine!r} ({', '.join(ENGINES)})")
    if batch != 1 and engine != "hf":
        raise ServeError("--batch is an hf_server option (sglang / vLLM batch continuously)")
    return argv + engine_flags(engine, server_args) + list(extra), env


def recorded_env(environ: dict) -> dict:
    return {k: ("<redacted>" if mf.SECRET_FLAG.search(k) else v) for k, v in sorted(environ.items())
            if k.startswith(ENV_PREFIXES)}


# ---------------------------------------------------------------- manifest
def build_manifest(args, *, venv: str, run_hardware=subprocess.run, cache: str | None = None) -> tuple[dict, dict]:
    """(manifest without served / probe, launch context {argv, env, chat_template_path, overlay, problems})."""
    engine = args.engine
    python = stack.venv_python(venv)
    ident = stack.engine_identity(engine, venv, args.patch)
    problems = stack.engine_problems(ident)
    loaded = ident.pop("_loaded_patches")
    if engine == "hf":  # the engine is our hf_server: pinned by our commit; transformers is RECORD-verified
        ident["transformers"] = ident["version"]
        ident["version"] = _hf_version()
        ident["commit"] = _commit()
    model = stack.model_identity(args.model, cache=cache)
    if not model.get("found"):
        problems.append(f"model {args.model}: {model.get('note')}")
    # chat template: always explicit
    if args.chat_template:
        with open(args.chat_template, encoding="utf-8") as f:
            tpl_text, tpl_source = f.read(), "--chat-template"
    else:
        tpl_text, tpl_source = stack.default_chat_template(model.get("path"))
        if tpl_text is None:
            problems.append(f"model {args.model}: no chat template in the snapshot (chat_template.jinja / "
                            "tokenizer_config.json); pass --chat-template FILE")
    import hashlib

    tpl = {"source": tpl_source, "sha256": hashlib.sha256(tpl_text.encode("utf-8")).hexdigest() if tpl_text else None}
    server_args = parse_server_args(args.model_arg)
    extra = list(args.extra or [])
    check_extra(extra)
    port = args.port or DEFAULT_PORTS[engine]
    host = args.host
    tpl_path = "<chat-template>"
    argv, env = build_argv(engine, python, args.model, host=host, port=port, dp=args.dp, tp=args.tp,
                           chat_template=tpl_path, deterministic=args.deterministic, server_args=server_args,
                           extra=extra, batch=args.batch, revision=model.get("revision"))
    manifest = {
        "format": mf.FORMAT,
        "engine": ident,
        "launch": {"argv": mf.redact_argv(argv, {python: "<engine-python>", os.path.dirname(python): "<engine-bin>"}),
                   "dp": args.dp, "tp": args.tp, "deterministic": {"on": bool(args.deterministic),
                                                                     "how": DETERMINISTIC[engine] if args.deterministic else None},
                   "batch": args.batch if engine == "hf" else None, "server_args": server_args,
                   "env_set": dict(env, **({"PYTHONPATH": "<overlay>"} if loaded else {})),
                   "env_recorded": recorded_env(dict(os.environ)),
                   "inherited_pythonpath_dropped": bool(os.environ.get("PYTHONPATH"))},
        "chat_template": tpl,
        "model": {k: v for k, v in model.items() if k != "path"} | {"path": model.get("path") if model.get("source") == "local" else None},
        "hardware": stack.hardware(venv, run=run_hardware),
        "served": None,
        "probe": {"status": "not run"},
        "runtime": {"host": socket.gethostname(), "port": port, "base_url": f"http://{socket.gethostname()}:{port}/v1",
                    "engine_venv": os.path.abspath(venv), "status": "starting", "created_at": _now(), "restarts": [],
                    "events": []},
    }
    ctx = {"argv": argv, "env": env, "python": python, "chat_template_text": tpl_text, "patches": loaded,
           "problems": problems, "snapshot": model.get("path")}
    return manifest, ctx


def _hf_version() -> str:
    from simple_evals_mm.serving.hf_server import server_version

    return server_version()


def _commit() -> str | None:
    from simple_evals_mm.checks import repo_root

    repo = repo_root()
    if not repo:
        return None
    try:
        return subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def manifest_path(args, ident: str) -> str:
    if args.manifest:
        return args.manifest
    from simple_evals_mm.store import DEFAULT_JOBS_DIR

    return os.path.join(args.jobs_dir or DEFAULT_JOBS_DIR, "_servers", f"{ident}.json")


# ---------------------------------------------------------------- supervision
def wait_health(base_url: str, proc, timeout_s: float, interval: float = 5.0, sleep=time.sleep) -> bool:
    url = mf.root_url(base_url) + "/health"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001, S110 - not up yet
            pass
        sleep(interval)
    return False


def _kill(proc, grace: float = 30.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def identity_changes(first: dict, now: dict) -> list[str]:
    """What differs between two launches: served model id / max_model_len / version, probe hash or status."""
    out = []
    for k in ("model_id", "max_model_len", "version"):
        if (first.get("served") or {}).get(k) != (now.get("served") or {}).get(k):
            out.append(f"served {k} {(first.get('served') or {}).get(k)!r} -> {(now.get('served') or {}).get(k)!r}")
    a, b = first.get("probe") or {}, now.get("probe") or {}
    if (a.get("status"), a.get("sha256")) != (b.get("status"), b.get("sha256")):
        out.append(f"probe {a.get('sha256') or a.get('status')} -> {b.get('sha256') or b.get('status')}")
    return out


def supervise(manifest: dict, ctx: dict, path: str, *, health_timeout: float = 3600.0, restart_delay: float = 10.0,
              max_failed_starts: int = 3, recheck_engine=None, log_path: str | None = None, popen=subprocess.Popen,
              interval: float = 5.0, max_restarts: int | None = None) -> int:
    """Run the engine until a signal (0), a fatal identity change after a restart (3) or repeated failed starts (4).
    recheck_engine() -> engine identity dict, compared after each restart (installed_sha256 + patches)."""
    rt = manifest["runtime"]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # nothing unrecorded on the engine's import path
    env.update(ctx["env"])
    if ctx.get("overlay"):
        env["PYTHONPATH"] = ctx["overlay"]
    state: dict = {"proc": None, "stop": False}

    def on_signal(signum, _frame):
        state["stop"] = True
        _kill(state["proc"], grace=10)

    main = threading.current_thread() is threading.main_thread()  # signal handlers: main thread only (tests run here)
    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)} if main else {}
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL  # noqa: SIM115 - closed in finally
    first: dict | None = None
    failed_starts = launches = 0
    try:
        while not state["stop"]:
            proc = popen(ctx["argv"], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            state["proc"] = proc
            launches += 1
            rt["pid"] = proc.pid
            rt["started_at"] = _now()
            if not wait_health(rt["base_url"], proc, health_timeout, interval=interval):
                if state["stop"]:
                    break
                _kill(proc)
                failed_starts += 1
                rt["events"].append({"at": _now(), "event": f"start failed (exit {proc.poll()}), {failed_starts} in a row"})
                if failed_starts >= max_failed_starts:
                    rt["status"] = f"stopped: {failed_starts} failed starts"
                    if first is not None:
                        mf.write(path, manifest)
                    print(f"[serve] {failed_starts} failed starts in a row; giving up", file=sys.stderr)
                    return 4
                time.sleep(restart_delay)
                continue
            failed_starts = 0
            try:
                served = mf.served_info(rt["base_url"])
            except Exception as e:  # noqa: BLE001
                served = {"error": f"{type(e).__name__}: {e}"}
            probe = mf.run_probe(rt["base_url"], served.get("model_id"))
            now = {"served": {k: served.get(k) for k in ("model_id", "max_model_len", "version")}, "probe": probe}
            if first is None:
                first = now
                manifest["served"], manifest["probe"] = now["served"], probe
                rt["root"] = served.get("root")
                rt["status"] = "serving"
                mf.write(path, manifest)
                print(f"[serve] {rt['base_url']}")
                print(f"[serve] manifest {path} (digest {mf.digest(manifest)[:16]}; probe {probe.get('status')})", flush=True)
            else:
                changes = identity_changes(first, now)
                if recheck_engine is not None:
                    eng = recheck_engine()
                    for k in ("installed_sha256", "version", "patches"):
                        if eng.get(k) != manifest["engine"].get(k):
                            changes.append(f"engine {k} changed")
                if changes:
                    rt["status"] = "stopped: identity changed after a restart"
                    rt["events"].append({"at": _now(), "event": "fatal: " + "; ".join(changes)})
                    mf.write(path, manifest)
                    print(f"[serve] FATAL after restart: {'; '.join(changes)}; server stopped", file=sys.stderr)
                    _kill(proc)
                    return 3
                rt["restarts"].append({"at": _now(), "probe": probe.get("sha256") or probe.get("status")})
                mf.write(path, manifest)
                print(f"[serve] restarted ({len(rt['restarts'])}); identity unchanged", flush=True)
            while proc.poll() is None and not state["stop"]:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1.0)
            if state["stop"]:
                break
            rt["events"].append({"at": _now(), "event": f"engine exited ({proc.returncode})"})
            if max_restarts is not None and launches > max_restarts:
                rt["status"] = "stopped: restart limit"
                mf.write(path, manifest)
                return 5
            print(f"[serve] engine exited ({proc.returncode}); restart in {restart_delay}s", file=sys.stderr, flush=True)
            time.sleep(restart_delay)
        return 0
    finally:
        _kill(state["proc"])
        if first is not None and rt.get("status") == "serving":
            rt["status"] = "stopped"
            rt["stopped_at"] = _now()
            mf.write(path, manifest)
        for s, h in old.items():
            signal.signal(s, h)
        if log is not subprocess.DEVNULL:
            log.close()


# ---------------------------------------------------------------- CLI
def add_parser(sub) -> None:
    p = sub.add_parser("serve", help="launch an OpenAI-compatible server and write its manifest")
    a = p.add_argument
    a("engine", choices=ENGINES)
    a("model", help="HF id (resolved in the local HF cache) or a local checkpoint directory")
    a("-c", "--config", help="YAML with the options below (engine_venv, patch, chat_template, dp, tp, ...)")
    a("--engine-venv", help="venv of the engine (sglang / vLLM are not dependencies of this repo); hf: this venv")
    a("--patch", action="append", help=f"registered patch (serving/patches/: {', '.join(stack.available_patches())})")
    a("--chat-template", help="template file passed to the engine (default: the snapshot's own template)")
    a("--model-arg", action="append", help=f"server-side model arg k=v ({', '.join(SERVER_ARGS)})")
    a("--port", type=int)
    a("--host", default="0.0.0.0")
    a("--dp", type=int, default=1)
    a("--tp", type=int, default=1)
    a("--batch", type=int, default=1, help="hf_server requests per generate() call (default 1 = off)")
    a("--deterministic", action="store_true", help="sglang --enable-deterministic-inference, vLLM VLLM_BATCH_INVARIANT=1, "
                                                   "hf torch deterministic algorithms")
    a("--manifest", help="manifest path (default <jobs_dir>/_servers/<id>.json)")
    a("-o", "--jobs-dir")
    a("--health-timeout", type=float, default=3600.0)
    a("--log", help="engine log file (default next to the manifest)")
    a("--dry-run", action="store_true", help="build and print the manifest without launching")
    p.epilog = "Extra engine args follow `--` (recorded in the manifest's argv)."


def split_extra(argv: list[str]) -> tuple[list[str], list[str]]:
    """`sem serve ... -- extra engine args`: (sem args, engine args)."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


CONFIG_KEYS = ("engine_venv", "patch", "chat_template", "model_arg", "port", "host", "dp", "tp", "batch", "deterministic",
               "manifest", "jobs_dir", "health_timeout", "log", "extra")


def apply_config(args) -> None:
    if not args.config:
        return
    import yaml

    with open(args.config, encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}
    unknown = set(y) - set(CONFIG_KEYS)
    if unknown:
        raise ServeError(f"{args.config}: unknown keys {sorted(unknown)} (known: {', '.join(CONFIG_KEYS)})")
    defaults = {"host": "0.0.0.0", "dp": 1, "tp": 1, "batch": 1, "health_timeout": 3600.0}
    for k, v in y.items():
        cur = getattr(args, k, None)
        if cur in (None, False, [], defaults.get(k)):  # CLI options override the file
            setattr(args, k, v)


def cmd_serve(args) -> int:
    try:
        apply_config(args)
        venv = args.engine_venv or (sys.prefix if args.engine == "hf" else None)
        if not venv:
            raise ServeError(f"--engine-venv is required for {args.engine} (not a dependency of this repo)")
        manifest, ctx = build_manifest(args, venv=venv)
    except (ServeError, stack.StackError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        manifest["runtime"]["status"] = "dry run (not launched)"
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        print(f"digest (without runtime): {mf.digest(manifest)}", file=sys.stderr)
        for p in ctx["problems"]:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 1 if ctx["problems"] else 0
    if ctx["problems"]:
        print("error: sem serve refuses to start:\n- " + "\n- ".join(ctx["problems"]), file=sys.stderr)
        return 2
    ident = f"{args.engine}-{_slug(args.model)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{socket.gethostname().split('.')[0]}"  # noqa: DTZ005
    path = manifest_path(args, ident)
    side = path.removesuffix(".json")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tpl_file = side + ".chat_template.jinja"
    with open(tpl_file, "w", encoding="utf-8") as f:
        f.write(ctx["chat_template_text"])
    ctx["argv"] = [tpl_file if a == "<chat-template>" else a for a in ctx["argv"]]
    ctx["overlay"] = stack.write_overlay(side + ".overlay", ctx["patches"])
    rt = manifest["runtime"]
    rt.update(manifest_path=os.path.abspath(path), chat_template_path=os.path.abspath(tpl_file),
              overlay=ctx["overlay"], log=os.path.abspath(args.log or side + ".log"))
    print(f"[serve] {args.engine} {args.model} on port {rt['port']} (log {rt['log']}); waiting for health...", flush=True)

    def recheck():
        e = stack.engine_identity(args.engine, venv, args.patch)
        e.pop("_loaded_patches", None)
        if args.engine == "hf":
            e["version"] = _hf_version()
        return e

    return supervise(manifest, ctx, path, health_timeout=args.health_timeout, recheck_engine=recheck, log_path=rt["log"])


