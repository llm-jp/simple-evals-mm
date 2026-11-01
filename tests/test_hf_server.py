"""serving/hf_server.py and serving/hf_backends.py without a GPU: protocol conversion, request fields applied /
refused, generation config, penalties, seed, identity endpoints (fake backend / fake model)."""

import base64
import io
import json
import threading
import urllib.error
import urllib.request

import pytest
from PIL import Image

from simple_evals_mm.serving import hf_backends, hf_server
from simple_evals_mm.serving.hf_backends import GenOutput, RequestError
from simple_evals_mm.serving.hf_server import (
    build_openai_response,
    extract_images_and_text,
    load_image_from_data_url,
    to_turns,
)


def _data_url(color="red", size=(8, 8)) -> str:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


class TestLoadImage:
    def test_roundtrip(self):
        img = load_image_from_data_url(_data_url(size=(8, 12)))
        assert img.size == (8, 12) and img.mode == "RGB"

    def test_rejects_plain_url(self):
        with pytest.raises(ValueError):
            load_image_from_data_url("https://example.com/x.png")

    def test_rejects_non_base64(self):
        with pytest.raises(ValueError):
            load_image_from_data_url("data:image/png,rawbytes")


class TestExtractImagesAndText:
    def test_plain_string(self):
        assert extract_images_and_text("hello") == ([], "hello")

    def test_none(self):
        assert extract_images_and_text(None) == ([], "")

    def test_chat_completions_parts(self):
        content = [
            {"type": "image_url", "image_url": {"url": _data_url()}},
            {"type": "image_url", "image_url": _data_url("blue")},  # bare-str variant
            {"type": "text", "text": "what "},
            {"type": "text", "text": "is this?"},
        ]
        images, text = extract_images_and_text(content)
        assert len(images) == 2
        assert text == "what is this?"

    def test_responses_api_parts(self):
        content = [
            {"type": "input_image", "image_url": _data_url()},
            {"type": "input_text", "text": "q"},
        ]
        images, text = extract_images_and_text(content)
        assert len(images) == 1 and text == "q"


class TestTurns:
    def test_openai_messages_to_turns(self):
        messages = [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": _data_url()}},
                                         {"type": "text", "text": "describe"}]},
        ]
        turns = to_turns(messages)
        assert turns[0] == {"role": "system", "images": [], "text": "be helpful"}
        assert turns[1]["role"] == "user" and len(turns[1]["images"]) == 1 and turns[1]["text"] == "describe"

    def test_bad_messages(self):
        with pytest.raises(RequestError):
            to_turns([{"role": "tool", "content": "x"}])
        with pytest.raises(RequestError):
            to_turns([{"role": "user", "content": [{"type": "audio_url", "audio_url": "x"}]}])


class TestBuildResponse:
    def test_maps_output_fields(self):
        out = build_openai_response(GenOutput(text="Answer: C", reasoning="thought about it", raw="...", input_tokens=100,
                                              output_tokens=50, reasoning_tokens=30, finish_reason="length"),
                                    "some/model", "abc")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "Answer: C"
        assert msg["reasoning_content"] == "thought about it"
        assert out["choices"][0]["finish_reason"] == "length"
        u = out["usage"]
        assert u["prompt_tokens"] == 100 and u["completion_tokens"] == 50
        assert u["total_tokens"] == 150 and u["reasoning_tokens"] == 30
        assert out["model"] == "some/model"

    def test_no_reasoning_omits_field_and_defaults_finish(self):
        out = build_openai_response(GenOutput(text="B", raw="B", finish_reason=""), "m", "id")
        assert "reasoning_content" not in out["choices"][0]["message"]
        assert out["choices"][0]["finish_reason"] == "stop"


class TestClientCompatibility:
    def test_openai_sdk_parses_our_response(self):
        """The openai SDK objects the v0.5 / v0.4 clients build expose exactly the fields our server emits."""
        from openai.types.chat import ChatCompletion

        parsed = ChatCompletion.model_validate(build_openai_response(
            GenOutput(text="A", reasoning="think", raw="...", input_tokens=10, output_tokens=5, reasoning_tokens=3), "m", "id"))
        message = parsed.choices[0].message
        assert message.content == "A"
        assert (getattr(message, "reasoning_content", None) or "") == "think"
        assert parsed.usage.prompt_tokens == 10
        assert (getattr(parsed.usage, "reasoning_tokens", 0) or 0) == 3
        assert parsed.choices[0].finish_reason == "stop"


def test_models_payload_reports_context_length():
    """GET /v1/models: model id and max_model_len, read by the v0.5 sampler's prepare() (as from sglang / vLLM)."""
    from simple_evals_mm.serving import hf_server

    row = hf_server.models_payload("llm-jp/llm-jp-4-vl-9b", 32768)["data"][0]
    assert (row["id"], row["root"], row["max_model_len"]) == ("llm-jp/llm-jp-4-vl-9b", "llm-jp/llm-jp-4-vl-9b", 32768)


# ---------------------------------------------------------------- request fields (step 2c)
FULL = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 30, "temperature": 0.7,
        "top_p": 0.9, "top_k": 20, "min_p": 0.05, "repetition_penalty": 1.1, "presence_penalty": 0.5,
        "frequency_penalty": 0.2, "seed": 7, "skip_special_tokens": False,
        "chat_template_kwargs": {"conversation_start_date": "", "model_identity": "x"}, "reasoning_effort": "high"}


class FakeBackend(hf_backends.Backend):
    name = "fake"

    def __init__(self):
        super().__init__("m", chat_template="{{ t }}")
        self.calls = []

    def generate(self, turns, p):
        self.calls.append((turns, p))
        return GenOutput(text="ok", input_tokens=4, output_tokens=2)

    def render(self, turns, p):
        return [1, 2, len(turns)]


def test_every_request_field_is_applied():
    b = FakeBackend()
    r = hf_server.handle_request(b, "chat", FULL, "m", "id")
    assert r["status"] == 200 and r["json"]["choices"][0]["message"]["content"] == "ok"
    p = b.calls[0][1]
    assert (p.max_tokens, p.temperature, p.top_p, p.top_k, p.min_p) == (30, 0.7, 0.9, 20, 0.05)
    assert (p.repetition_penalty, p.presence_penalty, p.frequency_penalty, p.seed) == (1.1, 0.5, 0.2, 7)
    assert p.skip_special_tokens is False
    assert p.template_kwargs == {"conversation_start_date": "", "model_identity": "x", "reasoning_effort": "high"}
    kw = hf_backends.generation_kwargs(p)
    assert kw == {"max_new_tokens": 30, "do_sample": True, "num_beams": 1, "repetition_penalty": 1.1, "temperature": 0.7,
                  "top_p": 0.9, "top_k": 20, "min_p": 0.05}
    greedy = hf_backends.generation_kwargs(hf_backends.parse_params({"max_tokens": 5, "temperature": 0}))
    assert greedy["do_sample"] is False and greedy["repetition_penalty"] == 1.0 and greedy["top_k"] == 0
    # no temperature: the OpenAI default 1.0 (not the model's generation_config.json)
    assert hf_backends.parse_params({"max_tokens": 5}).temperature == 1.0


@pytest.mark.parametrize("bad", [{"logprobs": True}, {"stop": ["x"]}, {"tools": []}, {"stream": True}, {"n": 2},
                                 {"top_p": 0}, {"temperature": "hot"}, {"max_tokens": None}, {"model": "other"}])
def test_unknown_or_invalid_fields_are_refused(bad):
    b = FakeBackend()
    body = {**FULL, **bad}
    if bad == {"max_tokens": None}:
        body.pop("max_tokens")
    r = hf_server.handle_request(b, "chat", body, "m", "id")
    assert r["status"] == 400 and not b.calls


def test_penalties_and_seed():
    import torch

    proc = hf_backends.make_penalty_processor(0.5, 0.25, prompt_len=1)
    ids = torch.tensor([[9, 2, 2, 3]])  # prompt token 9, generated 2, 2, 3
    out = proc(ids, torch.zeros(1, 5))
    assert out[0].tolist() == [0.0, 0.0, -1.0, -0.75, 0.0]  # 2: 0.5 + 2 * 0.25; 3: 0.5 + 0.25; 9 is the prompt
    assert hf_backends.make_penalty_processor(None, 0.0, 0) is None

    class FakeModel:
        generation_config = type("G", (), {"eos_token_id": 1, "pad_token_id": 0, "bos_token_id": 2})()

        def generate(self, **kw):
            self.kw = kw
            return "out"

    b = FakeBackend()
    b.model = FakeModel()
    p = hf_backends.parse_params(FULL)
    assert b._generate({"input_ids": "x"}, p, prompt_len=3) == "out"
    cfg = b.model.kw["generation_config"]
    assert (cfg.do_sample, cfg.temperature, cfg.top_k, cfg.top_p, cfg.min_p, cfg.repetition_penalty, cfg.max_new_tokens) == \
        (True, 0.7, 20, 0.9, 0.05, 1.1, 30)
    assert cfg.eos_token_id == 1 and len(b.model.kw["logits_processor"]) == 1
    assert torch.initial_seed() == 7  # the request seed


def test_harmony_split_and_reasoning_tokens():
    raw = "<|channel|>analysis<|message|>think<|end|><|start|>assistant<|channel|>final<|message|>42<|return|>"
    assert hf_backends.split_harmony(raw) == ("42", "think")
    assert hf_backends.split_harmony("<|channel|>analysis<|message|>cut off") == ("", "cut off")
    assert hf_backends.count_analysis_tokens([5, 6, 7, 1, 1, 8, 9], 5, 6, 7, 8) == 2
    assert hf_backends.count_analysis_tokens([1], 5, None, 7, 8) is None


def test_backend_selection():
    assert hf_backends.backend_name("llm-jp/llm-jp-4-vl-9b-beta") == "llm-jp-vl"
    assert hf_backends.backend_name("sbintuitions/sarashina2.2-vision-3b") == "sarashina"
    assert hf_backends.backend_name("/ckpt/x", architectures=["Qwen3VLForConditionalGeneration"]) == "qwen3-vl"
    assert hf_backends.backend_name("/ckpt/x", "internvl") == "internvl"
    with pytest.raises(ValueError, match="--backend"):
        hf_backends.backend_name("/ckpt/unknown")


def test_no_environment_reads():
    """The served backends and the server read no environment variable (v0.4: SWEEP_*, QWEN_DECODE, REP_PEN, HARMONY_*,
    HF_SERVER_*); they only set CUDA_VISIBLE_DEVICES / CUBLAS_WORKSPACE_CONFIG in the worker."""
    import inspect
    import re

    for mod in (hf_backends, hf_server):
        src = inspect.getsource(mod)
        assert not re.search(r"os\.environ\.get|os\.getenv|environ\[[^]]+\](?!\s*=)", src), mod.__name__


def test_http_endpoints_with_a_fake_pool():
    class Pool:
        def __init__(self):
            self.backend = FakeBackend()

        def submit(self, op, body):
            return hf_server.handle_request(self.backend, op, body, "m", "rid", 4096)

        def health(self):
            return 200, {"ready": 1}

    pool = Pool()
    opts = {"model": "m", "backend": "qwen3-vl", "chat_template": "{{ t }}", "revision": "r" * 40, "batch": 1,
            "deterministic": False, "max_model_len": 4096}
    srv = hf_server.ThreadingHTTPServer(("127.0.0.1", 0), hf_server.make_handler(pool, opts))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert post("/v1/chat/completions", FULL)[0] == 200
        code, body = post("/v1/chat/completions", {**FULL, "logprobs": True})
        assert code == 400 and "logprobs" in body["error"]["message"] and len(pool.backend.calls) == 1
        assert post("/tokenize", {"model": "m", "messages": [{"role": "user", "content": "x"}]}) == \
            (200, {"tokens": [1, 2, 1], "count": 3, "max_model_len": 4096})
        with urllib.request.urlopen(base + "/server_info") as r:
            info = json.loads(r.read())
        assert info["version"].startswith("hf_server") and info["batch"] == {"size": 1, "wait_s": 0.2, "effective": False}
        assert info["model_revision"] == "r" * 40 and info["chat_template_applied"] is True
        with urllib.request.urlopen(base + "/v1/models") as r:
            row = json.loads(r.read())["data"][0]
        assert (row["id"], row["max_model_len"], row["revision"]) == ("m", 4096, "r" * 40)
    finally:
        srv.shutdown()
