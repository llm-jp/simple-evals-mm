"""Minimal OpenAI-compatible server for models sglang / vLLM cannot serve (custom remote-code archs: LLM-jp-VL
checkpoints, Sarashina; also InternVL3 / Qwen3-VL in-process), data parallel. Launched by `sem serve hf MODEL`, which
records it in the server manifest:

    sem serve hf llm-jp/llm-jp-4-vl-9b-beta --dp 8 [--batch N] [--deterministic] [-- --backend llm-jp-vl]
    (directly: python -m simple_evals_mm.serving.hf_server --model M --dp-size 8 --port 30050 --chat-template FILE)

Endpoints:
    POST /v1/chat/completions   every sampling field of the body is applied (serving/hf_backends.REQUEST_FIELDS:
                                temperature, top_p, top_k, min_p, repetition / presence / frequency penalty, seed,
                                max_tokens, skip_special_tokens, chat_template_kwargs, reasoning_effort); unknown fields
                                are refused with 400. The torch seed is set per request from the request seed.
    POST /tokenize              the prompt token ids of {"messages"} (the manifest probe); 501 when the backend
                                cannot render (InternVL's model.chat)
    GET  /v1/models             model id, root, max_model_len (the config's max_position_embeddings), revision
    GET  /server_info           version, backend, model revision, batch config, deterministic, chat template sha256
    GET  /health                200 while at least one worker is ready

Nothing is read from the environment (v0.4: HF_SERVER_BATCH / _BATCH_WAIT / _REQUEST_TIMEOUT, and the samplers'
SWEEP_* / QWEN_DECODE / REP_PEN / HARMONY_*): every setting is a flag or a request field.

Architecture (one process tree, one port, stdlib only in the front):

    front (this process)
      - ThreadingHTTPServer; task_q (mp.Queue): workers PULL when idle -> automatic load balancing
      - result_q (mp.Queue): collector thread routes replies to waiting handlers
    worker x N (spawn subprocesses, CUDA_VISIBLE_DEVICES pinned per worker): loads the backend, converts OpenAI
      messages (base64 data URLs) to turns, runs generate() / render(), returns OpenAI JSON

Batching (--batch N, default 1 = off): requests with identical parameters may share one generate() call when the
backend supports it. Padding changes greedy outputs, so it is off by default and recorded in the manifest.

Crash robustness: workers announce ("start", req_id) before processing; a monitor thread fails a dead worker's
in-flight requests at once (HTTP 500, the client retries) and respawns it on the same GPU; a worker exits after
MAX_CONSECUTIVE_FAILURES request errors (likely a poisoned CUDA context) and is respawned.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import multiprocessing as mp
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from simple_evals_mm.serving.hf_backends import GenParams, RequestError, parse_params

MAX_CONSECUTIVE_FAILURES = 3
MONITOR_INTERVAL_S = 5
RESPAWN_DELAY_S = 10


def server_version() -> str:
    try:
        from importlib.metadata import version

        return f"hf_server simple-evals-mm {version('simple-evals-mm')}"
    except Exception:  # noqa: BLE001
        return "hf_server simple-evals-mm unknown"


# ---------------------------------------------------------------------------
# Pure conversion helpers (unit-tested without a GPU)
# ---------------------------------------------------------------------------


def load_image_from_data_url(data_url: str):
    """Decode a base64 data URL ("data:image/...;base64,....") to a PIL image."""
    from PIL import Image

    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise RequestError("Only base64 data URLs are supported.")
    header, _, b64 = data_url.partition(",")
    if ";base64" not in header or not b64:
        raise RequestError("Only base64 data URLs are supported.")
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def extract_images_and_text(content) -> tuple[list, str]:
    """Split OpenAI-style message content into (PIL images, joined text)."""
    if content is None:
        return [], ""
    if isinstance(content, str):
        return [], content
    images, texts = [], []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text"):
            texts.append(part.get("text", ""))
        elif ptype in ("image_url", "input_image"):
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            images.append(load_image_from_data_url(url))
        else:
            raise RequestError(f"unsupported content part type {ptype!r}")
    return images, "".join(texts)


def to_turns(messages: list) -> list[dict]:
    """OpenAI messages -> [{"role", "images", "text"}] (the backends' input)."""
    if not isinstance(messages, list) or not messages:
        raise RequestError("messages must be a non-empty list")
    out = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant", "developer"):
            raise RequestError(f"bad message {str(m)[:80]}")
        images, text = extract_images_and_text(m.get("content"))
        out.append({"role": m["role"], "images": images, "text": text})
    return out


def build_openai_response(out, model_id: str, req_id: str) -> dict:
    """GenOutput -> OpenAI chat.completion JSON (content, reasoning_content, usage, finish_reason)."""
    message = {"role": "assistant", "content": out.text}
    if out.reasoning:
        message["reasoning_content"] = out.reasoning
    return {
        "id": f"chatcmpl-{req_id}", "object": "chat.completion", "created": int(time.time()), "model": model_id,
        "choices": [{"index": 0, "message": message, "finish_reason": out.finish_reason or "stop"}],
        "usage": {"prompt_tokens": out.input_tokens, "completion_tokens": out.output_tokens,
                  "total_tokens": out.input_tokens + out.output_tokens, "reasoning_tokens": out.reasoning_tokens},
    }


def handle_request(backend, op: str, body: dict, model_id: str, req_id: str, max_model_len: int | None = None) -> dict:
    """One request against a loaded backend -> {"status", "json"} (400 on a malformed request)."""
    try:
        if body.get("model") not in (None, model_id):
            raise RequestError(f"this server serves {model_id!r}, not {body.get('model')!r}")
        if op == "tokenize":
            params = GenParams(max_tokens=1, template_kwargs=dict(body.get("chat_template_kwargs") or {}))
            ids = backend.render(to_turns(body.get("messages")), params)
            if ids is None:
                return {"status": 501, "json": {"error": {"message": f"backend {backend.name} cannot render prompts"}}}
            return {"status": 200, "json": {"tokens": ids, "count": len(ids), "max_model_len": max_model_len}}
        params = parse_params(body)
        out = backend.generate(to_turns(body.get("messages")), params)
        return {"status": 200, "json": build_openai_response(out, model_id, req_id)}
    except RequestError as e:
        return {"status": 400, "json": {"error": {"message": str(e)}}}


# ---------------------------------------------------------------------------
# Worker subprocess
# ---------------------------------------------------------------------------


def worker_main(opts: dict, gpu_id: int, task_q, result_q):
    # Pin the GPU BEFORE anything imports torch.
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    if opts.get("deterministic"):
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    import queue as _queue

    if opts.get("deterministic"):
        import torch

        torch.use_deterministic_algorithms(True)
        torch.backends.cudnn.benchmark = False
    from simple_evals_mm.serving.hf_backends import BACKENDS

    backend = BACKENDS[opts["backend"]](opts["model"], chat_template=opts.get("chat_template"))
    result_q.put(("ready", gpu_id, None))
    consecutive_failures = 0
    batch = int(opts.get("batch") or 1)
    can_batch = batch > 1 and backend.supports_batch

    while True:
        items = [task_q.get()]
        if can_batch:
            deadline = time.time() + float(opts.get("batch_wait") or 0.2)
            while len(items) < batch:
                try:
                    items.append(task_q.get(timeout=max(deadline - time.time(), 0.001)))
                except _queue.Empty:
                    break
        for req_id, _, _ in items:
            result_q.put(("start", gpu_id, req_id))
        for req_id, op, body in items:  # backends without batch support: one generate() per request
            try:
                payload = handle_request(backend, op, body, opts["model"], req_id, opts.get("max_model_len"))
                consecutive_failures = 0
            except Exception as e:  # noqa: BLE001 - the worker must survive and report
                consecutive_failures += 1
                payload = {"status": 500, "json": {"error": {"message": f"{type(e).__name__}: {e}"}}}
                try:
                    import torch

                    torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001, S110
                    pass
            result_q.put(("done", gpu_id, (req_id, payload)))
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"[worker {gpu_id}] {consecutive_failures} consecutive failures, exiting for respawn", flush=True)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Front server
# ---------------------------------------------------------------------------


class WorkerPool:
    """Worker pool with crash detection and respawn."""

    def __init__(self, opts: dict, dp_size: int, ctx, request_timeout: float = 3600):
        self.opts, self.model_id, self.dp_size, self.ctx = opts, opts["model"], dp_size, ctx
        self.request_timeout = request_timeout
        self.task_q, self.result_q = ctx.Queue(), ctx.Queue()
        self.lock = threading.Lock()
        self.pending: dict = {}  # req_id -> {"event", "payload"}
        self.inflight: dict = {}  # gpu_id -> set of in-flight req_ids
        self.ready: set = set()
        self.workers: dict = {}  # gpu_id -> Process
        self.shutting_down = False
        for gpu in range(dp_size):
            self._spawn(gpu)
        threading.Thread(target=self._collect, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    def _spawn(self, gpu: int):
        p = self.ctx.Process(target=worker_main, args=(self.opts, gpu, self.task_q, self.result_q), daemon=True)
        p.start()
        self.workers[gpu] = p
        print(f"[front] worker {gpu} spawned (pid {p.pid})", flush=True)

    def _collect(self):
        while True:
            kind, gpu, data = self.result_q.get()
            with self.lock:
                if kind == "ready":
                    self.ready.add(gpu)
                    print(f"[front] worker {gpu} ready", flush=True)
                elif kind == "start":
                    self.inflight.setdefault(gpu, set()).add(data)
                elif kind == "done":
                    req_id, payload = data
                    self.inflight.get(gpu, set()).discard(req_id)
                    slot = self.pending.get(req_id)
                    if slot is not None:
                        slot["payload"] = payload
                        slot["event"].set()

    def _monitor(self):
        while not self.shutting_down:
            time.sleep(MONITOR_INTERVAL_S)
            for gpu, proc in list(self.workers.items()):
                if proc.is_alive():
                    continue
                with self.lock:
                    self.ready.discard(gpu)
                    slots = [self.pending.get(r) for r in self.inflight.pop(gpu, set())]
                for slot in slots:
                    if slot is not None:
                        slot["payload"] = {"status": 500, "json": {"error": {
                            "message": f"worker {gpu} died (exit {proc.exitcode}) mid-request"}}}
                        slot["event"].set()
                print(f"[front] worker {gpu} died (exit {proc.exitcode}); respawning in {RESPAWN_DELAY_S}s", flush=True)
                if self.shutting_down:
                    return
                time.sleep(RESPAWN_DELAY_S)
                self._spawn(gpu)

    def submit(self, op: str, body: dict) -> dict:
        req_id = uuid.uuid4().hex
        slot = {"event": threading.Event(), "payload": None}
        with self.lock:
            self.pending[req_id] = slot
        try:
            self.task_q.put((req_id, op, body))
            if not slot["event"].wait(timeout=self.request_timeout):
                return {"status": 504, "json": {"error": {"message": "request timed out"}}}
            return slot["payload"]
        finally:
            with self.lock:
                self.pending.pop(req_id, None)

    def health(self) -> tuple:
        with self.lock:
            ready = len(self.ready)
            alive = sum(1 for p in self.workers.values() if p.is_alive())
        return (200 if ready >= 1 else 503), {"ready": ready, "alive": alive, "dp_size": self.dp_size}

    def shutdown(self):
        self.shutting_down = True
        for p in self.workers.values():
            if p.is_alive():
                p.terminate()


def model_context_length(model_id: str) -> int | None:
    """The model's context length from its config (max_position_embeddings, of the text config for VLMs); None when the
    config cannot be read. GET /v1/models reports it as max_model_len, as sglang and vLLM do."""
    cfg = _auto_config(model_id)
    for c in (getattr(cfg, "text_config", None), getattr(cfg, "llm_config", None), cfg):
        n = getattr(c, "max_position_embeddings", None) if c is not None else None
        if n:
            return int(n)
    return None


def _auto_config(model_id: str):
    try:
        from transformers import AutoConfig

        return AutoConfig.from_pretrained(model_id, trust_remote_code=True)
    except Exception:  # noqa: BLE001 - reported as unknown
        return None


def models_payload(model_id: str, max_model_len: int | None, revision: str | None = None) -> dict:
    return {"object": "list", "data": [{"id": model_id, "object": "model", "root": model_id, "owned_by": "hf_server",
                                        "max_model_len": max_model_len, "revision": revision}]}


def server_info_payload(opts: dict) -> dict:
    """What GET /server_info reports (the client compares the version with the server manifest)."""
    from simple_evals_mm.serving.hf_backends import BACKENDS

    tpl, backend = opts.get("chat_template"), BACKENDS[opts["backend"]]
    batch = int(opts.get("batch") or 1)
    return {"version": server_version(), "backend": opts["backend"], "model": opts["model"],
            "model_revision": opts.get("revision"),
            "batch": {"size": batch, "wait_s": float(opts.get("batch_wait") or 0.2),
                      "effective": batch > 1 and backend.supports_batch},
            "deterministic": bool(opts.get("deterministic")),
            "chat_template_sha256": hashlib.sha256(tpl.encode("utf-8")).hexdigest() if tpl else None,
            "chat_template_applied": bool(tpl) and backend.uses_chat_template,
            "max_model_len": opts.get("max_model_len")}


def make_handler(pool, opts: dict):
    max_model_len = opts.get("max_model_len")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, obj: dict):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.rstrip("/")
            if path == "/health":
                status, body = pool.health()
                self._send(status, body)
            elif path == "/v1/models":
                self._send(200, models_payload(opts["model"], max_model_len, opts.get("revision")))
            elif path in ("/server_info", "/version"):
                self._send(200, server_info_payload(opts))
            else:
                self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            path = self.path.rstrip("/")
            op = {"/v1/chat/completions": "chat", "/tokenize": "tokenize", "/v1/tokenize": "tokenize"}.get(path)
            if op is None:
                self._send(404, {"error": {"message": "not found"}})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise RequestError("the body must be a JSON object")
                if op == "chat":
                    parse_params(body)  # unknown fields / bad values: 400 before a worker sees the request
            except (ValueError, RequestError) as e:
                self._send(400, {"error": {"message": f"bad request: {e}"}})
                return
            result = pool.submit(op, body)
            self._send(result["status"], result["json"])

        def log_message(self, fmt, *args):  # quiet per-request access logs
            pass

    return Handler


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OpenAI-compatible DP server for HF backends (launch with `sem serve hf`)")
    p.add_argument("--model", required=True, help="HF id or local checkpoint directory")
    p.add_argument("--backend", help="llm-jp-vl / sarashina / internvl / qwen3-vl (default: from the model id / config)")
    p.add_argument("--dp-size", type=int, default=8, help="worker / GPU count")
    p.add_argument("--port", type=int, default=30050)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--chat-template", help="Jinja chat template file (sem serve always passes one)")
    p.add_argument("--revision", help="model snapshot revision, reported on /v1/models and /server_info")
    p.add_argument("--batch", type=int, default=1, help="requests per generate() call (1 = off; padding changes "
                                                        "greedy outputs)")
    p.add_argument("--batch-wait", type=float, default=0.2, help="seconds to fill a batch")
    p.add_argument("--request-timeout", type=float, default=3600)
    p.add_argument("--deterministic", action="store_true", help="torch.use_deterministic_algorithms in the workers")
    return p


def main(argv: list[str] | None = None):
    args = make_parser().parse_args(argv)
    from simple_evals_mm.serving.hf_backends import backend_name

    cfg = _auto_config(args.model)
    backend = backend_name(args.model, args.backend, getattr(cfg, "architectures", None) if cfg else None)
    template = None
    if args.chat_template:
        with open(args.chat_template, encoding="utf-8") as f:
            template = f.read()
    opts = {"model": args.model, "backend": backend, "chat_template": template, "revision": args.revision,
            "batch": args.batch, "batch_wait": args.batch_wait, "deterministic": args.deterministic,
            "max_model_len": model_context_length(args.model)}
    pool = WorkerPool(opts, args.dp_size, mp.get_context("spawn"), request_timeout=args.request_timeout)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(pool, opts))
    print(f"[front] serving {args.model} ({backend}) on {args.host}:{args.port} (dp={args.dp_size}, batch={args.batch}); "
          "waiting for workers to load...", flush=True)
    try:
        server.serve_forever()
    finally:
        pool.shutdown()


if __name__ == "__main__":
    main()
