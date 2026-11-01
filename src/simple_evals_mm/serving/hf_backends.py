"""Served HF backends of serving/hf_server.py: models sglang / vLLM cannot serve (custom remote-code architectures),
loaded in-process by the server's workers. Moved from the v0.4 in-process samplers (sampler/llmjpvl_sampler.py,
sarashina_sampler.py, internvl_sampler.py, qwenvl_sampler.py), with every setting taken from the request:

  * no environment reads (v0.4 read SWEEP_*, QWEN_DECODE, REP_PEN, HARMONY_DATE): sampling comes from the request
    body (GenParams), template kwargs from chat_template_kwargs;
  * generation runs with an explicit GenerationConfig built from the request only (generation_kwargs), so the model's
    own generation_config.json never fills in a field silently (the client sends those values explicitly);
  * presence / frequency penalties as OpenAI defines them (PenaltyProcessor: HF has no native ones);
  * the chat template is the file the server was given (`--chat-template`), passed to apply_chat_template.

    Backend(model_id, chat_template)      load
    backend.generate(turns, params)       -> GenOutput (text, reasoning, raw, token counts, finish_reason)
    backend.render(turns, params)         -> prompt token ids (the /tokenize probe); None when not supported

A turn is {"role", "images": [PIL.Image], "text"} (the OpenAI message with its images decoded).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# request fields of POST /v1/chat/completions the server applies; anything else is refused with 400
SAMPLING_FIELDS = ("temperature", "top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty")
REQUEST_FIELDS = ("model", "messages", "max_tokens", "max_completion_tokens", "seed", "skip_special_tokens",
                  "chat_template_kwargs", "reasoning_effort", "stream", "n", *SAMPLING_FIELDS)


@dataclass
class GenParams:
    max_tokens: int
    temperature: float = 1.0  # OpenAI default when the request has none
    top_p: float | None = None
    top_k: int | None = None  # None / 0 / -1: disabled
    min_p: float | None = None
    repetition_penalty: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    skip_special_tokens: bool = True
    template_kwargs: dict = field(default_factory=dict)  # chat_template_kwargs (+ reasoning_effort)

    def group_key(self) -> tuple:
        """Requests that can share one generate() call: every field equal (the seed too unless greedy)."""
        return (self.max_tokens, self.temperature, self.top_p, self.top_k, self.min_p, self.repetition_penalty,
                self.presence_penalty, self.frequency_penalty, self.skip_special_tokens,
                tuple(sorted((k, repr(v)) for k, v in self.template_kwargs.items())),
                None if not self.temperature else self.seed)


class RequestError(ValueError):
    """A malformed request (HTTP 400)."""


def _num(body: dict, k: str, typ):
    v = body.get(k)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RequestError(f"{k} must be a number, got {v!r}")
    return typ(v)


def parse_params(body: dict, default_max_tokens: int | None = None) -> GenParams:
    """GenParams of a chat-completions body; RequestError on unknown fields or invalid values."""
    unknown = sorted(set(body) - set(REQUEST_FIELDS))
    if unknown:
        raise RequestError(f"unknown request field(s) {', '.join(unknown)} (hf_server applies: {', '.join(REQUEST_FIELDS)})")
    if body.get("stream"):
        raise RequestError("stream is not supported")
    if body.get("n") not in (None, 1):
        raise RequestError("n must be 1")
    mt = _num(body, "max_tokens", int) or _num(body, "max_completion_tokens", int) or default_max_tokens
    if not mt or mt < 1:
        raise RequestError("max_tokens is required (a positive integer)")
    tk = body.get("chat_template_kwargs") or {}
    if not isinstance(tk, dict):
        raise RequestError("chat_template_kwargs must be an object")
    tk = dict(tk)
    if body.get("reasoning_effort") is not None:
        tk.setdefault("reasoning_effort", body["reasoning_effort"])
    sst = body.get("skip_special_tokens", True)
    if not isinstance(sst, bool):
        raise RequestError("skip_special_tokens must be a boolean")
    temp = _num(body, "temperature", float)
    p = GenParams(max_tokens=mt, temperature=1.0 if temp is None else temp, top_p=_num(body, "top_p", float),
                  top_k=_num(body, "top_k", int), min_p=_num(body, "min_p", float),
                  repetition_penalty=_num(body, "repetition_penalty", float),
                  presence_penalty=_num(body, "presence_penalty", float),
                  frequency_penalty=_num(body, "frequency_penalty", float), seed=_num(body, "seed", int),
                  skip_special_tokens=sst, template_kwargs=tk)
    if p.temperature < 0:
        raise RequestError("temperature must be >= 0")
    if p.top_p is not None and not 0 < p.top_p <= 1:
        raise RequestError("top_p must be in (0, 1]")
    return p


def generation_kwargs(p: GenParams) -> dict:
    """HF GenerationConfig fields of a request, complete: greedy at temperature 0; top_k None / 0 / -1 disabled (0);
    top_p 1.0 and repetition_penalty 1.0 when not given (no fallback to the model's generation_config.json)."""
    sample = p.temperature > 0
    kw = {"max_new_tokens": p.max_tokens, "do_sample": sample, "num_beams": 1,
          "repetition_penalty": p.repetition_penalty if p.repetition_penalty is not None else 1.0}
    if sample:
        kw.update(temperature=p.temperature, top_p=p.top_p if p.top_p is not None else 1.0,
                  top_k=p.top_k if p.top_k and p.top_k > 0 else 0, min_p=p.min_p or None)
    else:  # the sampling warpers are off; neutral values keep GenerationConfig from warning
        kw.update(temperature=1.0, top_p=1.0, top_k=0, min_p=None)
    return kw


def make_penalty_processor(presence: float | None, frequency: float | None, prompt_len: int):
    """OpenAI presence / frequency penalties over the generated tokens (logit -= presence * [count > 0] + frequency *
    count); None when both are 0 / absent."""
    if not presence and not frequency:
        return None
    import torch
    from transformers import LogitsProcessor

    class PenaltyProcessor(LogitsProcessor):
        def __call__(self, input_ids, scores):
            gen = input_ids[:, prompt_len:]
            if gen.shape[1] == 0:
                return scores
            counts = torch.zeros_like(scores)
            counts.scatter_add_(1, gen, torch.ones_like(gen, dtype=scores.dtype))
            return scores - (presence or 0.0) * (counts > 0).to(scores.dtype) - (frequency or 0.0) * counts

    return PenaltyProcessor()


def seed_everything(seed: int | None) -> None:
    """torch's CPU and CUDA generators from the request seed (none: the generators run on)."""
    if seed is None:
        return
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


@dataclass
class GenOutput:
    text: str
    reasoning: str = ""
    raw: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str = "stop"


class Backend:
    name = ""
    uses_chat_template = True  # apply_chat_template gets the server's --chat-template
    supports_batch = False

    def __init__(self, model_id: str, chat_template: str | None = None):
        self.model_id = model_id
        self.chat_template = chat_template

    def generate(self, turns: list[dict], p: GenParams) -> GenOutput:
        raise NotImplementedError

    def render(self, turns: list[dict], p: GenParams) -> list[int] | None:
        return None

    def _generate(self, inputs: dict, p: GenParams, prompt_len: int):
        """model.generate with the request's complete GenerationConfig, penalties and seed."""
        from transformers import GenerationConfig

        base = self.model.generation_config  # token ids only: eos / pad / bos
        cfg = GenerationConfig(**generation_kwargs(p), eos_token_id=base.eos_token_id, pad_token_id=base.pad_token_id,
                               bos_token_id=base.bos_token_id)
        procs = []
        pen = make_penalty_processor(p.presence_penalty, p.frequency_penalty, prompt_len)
        if pen is not None:
            from transformers import LogitsProcessorList

            procs = LogitsProcessorList([pen])
        seed_everything(p.seed)
        return self.model.generate(**inputs, generation_config=cfg, **({"logits_processor": procs} if procs else {}))


def _finish(out_tokens: int, max_tokens: int) -> str:
    return "length" if out_tokens >= max_tokens else "stop"


# ---------------------------------------------------------------- LLM-jp-VL (Harmony)
_FINAL_RE = re.compile(r"<\|channel\|>\s*final\s*<\|message\|>")
_ANALYSIS_RE = re.compile(r"<\|channel\|>\s*analysis\s*<\|message\|>(.*?)(?:<\|end\|>|<\|start\|>|<\|channel\|>|$)", re.DOTALL)
_STOP_RE = re.compile(r"<\|(?:return|end)\|>")


def split_harmony(raw: str, eos: str | None = None) -> tuple[str, str]:
    """(final answer, analysis) of a Harmony completion decoded with its special tokens (v0.4 LLMjpVLSampler): the text
    after the LAST final-channel marker; empty when generation stopped mid-analysis."""
    matches = list(_FINAL_RE.finditer(raw))
    answer = raw[matches[-1].end():] if matches else ""
    answer = _STOP_RE.sub("", answer)
    if eos:
        answer = answer.replace(eos, "")
    am = _ANALYSIS_RE.search(raw)
    return answer.strip(), am.group(1).strip() if am else ""


def count_analysis_tokens(ids: list[int], channel: int, analysis: int | None, message: int, end: int) -> int | None:
    """Tokens strictly between each <|channel|>analysis<|message|> and its <|end|> (or the end when cut off); None when
    the marker ids are unknown."""
    if analysis is None or None in (channel, message, end):
        return None
    n = i = 0
    while i < len(ids) - 2:
        if ids[i] == channel and ids[i + 1] == analysis and ids[i + 2] == message:
            j = i + 3
            while j < len(ids) and ids[j] != end:
                j += 1
            n += j - (i + 3)
            i = j
        else:
            i += 1
    return n


class LLMjpVL(Backend):
    """LLM-jp-VL checkpoints (Harmony format, remote code). skip_special_tokens false returns the raw completion with
    its channel markers (the client splits them); true returns the final channel as content and the analysis channel
    as reasoning_content. Template kwargs (reasoning_effort, conversation_start_date, model_identity) come from the
    request only; v0.4 defaulted the effort to medium and the date to HARMONY_DATE."""

    name = "llm-jp-vl"

    def __init__(self, model_id: str, chat_template: str | None = None):
        super().__init__(model_id, chat_template)
        import torch
        from transformers import AutoModel, AutoProcessor

        self.model = AutoModel.from_pretrained(model_id, torch_dtype=torch.bfloat16, trust_remote_code=True,
                                               use_flash_attn=True).eval().cuda()
        self.processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        tk = self.processor.tokenizer
        self._ids = (tk.convert_tokens_to_ids("<|channel|>"), None, tk.convert_tokens_to_ids("<|message|>"),
                     tk.convert_tokens_to_ids("<|end|>"))
        a = tk.encode("analysis", add_special_tokens=False)
        self._ids = (self._ids[0], a[0] if len(a) == 1 else None, self._ids[2], self._ids[3])

    def _inputs(self, turns: list[dict], p: GenParams):
        from PIL import Image

        flat, images = [], []
        for t in turns:
            images += [im.convert("RGB") if isinstance(im, Image.Image) else Image.open(im).convert("RGB") for im in t["images"]]
            flat.append({"role": t["role"], "content": "<image>" * len(t["images"]) + t["text"]})
        kw = {"chat_template": self.chat_template} if self.chat_template else {}
        text = self.processor.tokenizer.apply_chat_template(flat, tokenize=False, add_generation_prompt=True,
                                                            **kw, **p.template_kwargs)
        inputs = self.processor(images=images or None, text=text, return_tensors="pt")
        inputs.pop("num_patches_list", None)
        return {k: (v.to(self.model.device) if hasattr(v, "to") else v) for k, v in inputs.items()}

    def render(self, turns, p):
        return self._inputs(turns, p)["input_ids"][0].tolist()

    def generate(self, turns, p):
        inputs = self._inputs(turns, p)
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(dtype=self.model.dtype)
        n_in = inputs["input_ids"].shape[-1]
        out = self._generate(inputs, p, prompt_len=0)  # generate(inputs_embeds=...) returns the new tokens only
        ids = out[0].tolist()
        raw = self.processor.decode(out[0], skip_special_tokens=False)
        answer, reasoning = split_harmony(raw, self.processor.tokenizer.eos_token)
        r_tok = count_analysis_tokens(ids, *self._ids)
        if r_tok is None or (r_tok == 0 and reasoning):
            r_tok = len(self.processor.tokenizer.encode(reasoning, add_special_tokens=False)) if reasoning else 0
        if not p.skip_special_tokens:
            answer, reasoning = raw.strip(), ""
        return GenOutput(text=answer, reasoning=reasoning, raw=raw, input_tokens=n_in, output_tokens=len(ids),
                         reasoning_tokens=r_tok, finish_reason=_finish(len(ids), p.max_tokens))


# ---------------------------------------------------------------- Sarashina
class Sarashina(Backend):
    """sbintuitions/sarashina2.2-vision (remote code); images of the first user turn, as v0.4 SarashinaSampler."""

    name = "sarashina"

    def __init__(self, model_id: str, chat_template: str | None = None):
        super().__init__(model_id, chat_template)
        from transformers import AutoModelForCausalLM, AutoProcessor

        self.model = AutoModelForCausalLM.from_pretrained(model_id, device_map="auto", torch_dtype="auto",
                                                          trust_remote_code=True)
        self.processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)

    def _inputs(self, turns, p):
        msgs = [{"role": t["role"], "content": [*({"type": "image", "image": im} for im in t["images"]),
                                                {"type": "text", "text": t["text"]}]} for t in turns]
        kw = {"chat_template": self.chat_template} if self.chat_template else {}
        text = self.processor.apply_chat_template(msgs, add_generation_prompt=True, **kw, **p.template_kwargs)
        first_user = next((t for t in turns if t["role"] == "user"), {"images": []})
        images = [im.convert("RGB") for im in first_user["images"]]
        return self.processor(text=[text], images=images or None, padding=True, return_tensors="pt").to(self.model.device)

    def render(self, turns, p):
        return self._inputs(turns, p).input_ids[0].tolist()

    def generate(self, turns, p):
        inputs = self._inputs(turns, p)
        n_in = inputs.input_ids.shape[-1]
        out = self._generate(dict(inputs), p, prompt_len=n_in)
        gen = out[0][n_in:]
        text = self.processor.batch_decode([gen], skip_special_tokens=p.skip_special_tokens,
                                           clean_up_tokenization_spaces=True)[0]
        return GenOutput(text=text, raw=text, input_tokens=n_in, output_tokens=len(gen),
                         finish_reason=_finish(len(gen), p.max_tokens))


# ---------------------------------------------------------------- InternVL3
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def _closest_ratio(aspect, ratios, width, height, size):
    best, best_diff, area = (1, 1), float("inf"), width * height
    for r in ratios:
        diff = abs(aspect - r[0] / r[1])
        if diff < best_diff:
            best_diff, best = diff, r
        elif diff == best_diff and area > 0.5 * size * size * r[0] * r[1]:
            best = r
    return best


def dynamic_preprocess(image, min_num=1, max_num=12, image_size=448, use_thumbnail=False):
    """InternVL's tiling (v0.4 internvl_sampler.dynamic_preprocess, unchanged)."""
    w, h = image.size
    ratios = sorted({(i, j) for n in range(min_num, max_num + 1) for i in range(1, n + 1) for j in range(1, n + 1)
                     if min_num <= i * j <= max_num}, key=lambda x: x[0] * x[1])
    r = _closest_ratio(w / h, ratios, w, h, image_size)
    tw, th, blocks = image_size * r[0], image_size * r[1], r[0] * r[1]
    resized = image.resize((tw, th))
    tiles = [resized.crop(((i % (tw // image_size)) * image_size, (i // (tw // image_size)) * image_size,
                           ((i % (tw // image_size)) + 1) * image_size, ((i // (tw // image_size)) + 1) * image_size))
             for i in range(blocks)]
    if use_thumbnail and len(tiles) != 1:
        tiles.append(image.resize((image_size, image_size)))
    return tiles


class InternVL(Backend):
    """OpenGVLab/InternVL3 (remote code model.chat): the first user turn's images and text, as v0.4 InternVLSampler.
    model.chat renders with the model's own conversation template, not a Jinja chat template, so --chat-template is
    recorded but not applied (server_info chat_template_applied false) and /tokenize is unsupported."""

    name = "internvl"
    uses_chat_template = False

    def __init__(self, model_id: str, chat_template: str | None = None):
        super().__init__(model_id, chat_template)
        from transformers import AutoModel, AutoTokenizer

        self.model = AutoModel.from_pretrained(model_id, torch_dtype="auto", use_flash_attn=True, trust_remote_code=True,
                                               device_map="auto").eval()
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True, use_fast=False)

    def _pixels(self, image):
        import torch
        import torchvision.transforms as T
        from torchvision.transforms.functional import InterpolationMode

        tf = T.Compose([T.Lambda(lambda im: im.convert("RGB") if im.mode != "RGB" else im),
                        T.Resize((448, 448), interpolation=InterpolationMode.BICUBIC), T.ToTensor(),
                        T.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD)])
        return torch.stack([tf(t) for t in dynamic_preprocess(image.convert("RGB"), image_size=448, use_thumbnail=True)])

    def generate(self, turns, p):
        import torch

        user = next(t for t in turns if t["role"] == "user")
        pv = [self._pixels(im).to(torch.bfloat16).cuda() for im in user["images"]]
        cfg = generation_kwargs(p)
        seed_everything(p.seed)
        text = self.model.chat(self.tokenizer, torch.cat(pv, dim=0) if pv else None, user["text"], generation_config=cfg)
        return GenOutput(text=text, raw=text)


# ---------------------------------------------------------------- Qwen3-VL
class QwenVL(Backend):
    """Qwen/Qwen3-VL (transformers' own classes; the MoE class for the A3B / A22B checkpoints)."""

    name = "qwen3-vl"

    def __init__(self, model_id: str, chat_template: str | None = None):
        super().__init__(model_id, chat_template)
        import torch
        from transformers import (
            AutoProcessor,
            Qwen3VLForConditionalGeneration,
            Qwen3VLMoeForConditionalGeneration,
        )

        cls = Qwen3VLMoeForConditionalGeneration if re.search(r"-A\d+B", model_id) else Qwen3VLForConditionalGeneration
        self.model = cls.from_pretrained(model_id, dtype=torch.bfloat16, attn_implementation="flash_attention_2",
                                         device_map="auto").eval()
        self.processor = AutoProcessor.from_pretrained(model_id)

    def _inputs(self, turns, p):
        msgs = [{"role": t["role"], "content": [*({"type": "image", "image": im} for im in t["images"]),
                                                {"type": "text", "text": t["text"]}]} for t in turns]
        kw = {"chat_template": self.chat_template} if self.chat_template else {}
        return self.processor.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True, return_dict=True,
                                                  return_tensors="pt", **kw, **p.template_kwargs).to(self.model.device)

    def render(self, turns, p):
        return self._inputs(turns, p).input_ids[0].tolist()

    def generate(self, turns, p):
        inputs = self._inputs(turns, p)
        n_in = inputs.input_ids.shape[-1]
        out = self._generate(dict(inputs), p, prompt_len=n_in)
        gen = out[0][n_in:]
        text = self.processor.batch_decode([gen], skip_special_tokens=p.skip_special_tokens,
                                           clean_up_tokenization_spaces=False)[0]
        return GenOutput(text=text, raw=text, input_tokens=n_in, output_tokens=len(gen),
                         finish_reason=_finish(len(gen), p.max_tokens))


BACKENDS = {b.name: b for b in (LLMjpVL, Sarashina, InternVL, QwenVL)}
# HF id prefixes (case-insensitive) -> backend; local checkpoints: --backend, else the config's architectures
ID_PREFIXES = (("llm-jp/llm-jp-4-vl", "llm-jp-vl"), ("sbintuitions/sarashina2.2-vision", "sarashina"),
               ("opengvlab/internvl3", "internvl"), ("qwen/qwen3-vl", "qwen3-vl"))
ARCHITECTURES = {"Qwen3VLForConditionalGeneration": "qwen3-vl", "Qwen3VLMoeForConditionalGeneration": "qwen3-vl",
                 "InternVLChatModel": "internvl"}


def backend_name(model_id: str, explicit: str | None = None, architectures: list[str] | None = None) -> str:
    if explicit:
        if explicit not in BACKENDS:
            raise ValueError(f"unknown --backend {explicit!r} (backends: {', '.join(BACKENDS)})")
        return explicit
    low = model_id.lower()
    for prefix, name in ID_PREFIXES:
        if low.startswith(prefix):
            return name
    for a in architectures or []:
        if a in ARCHITECTURES:
            return ARCHITECTURES[a]
    raise ValueError(f"{model_id}: no hf_server backend known for this model; pass --backend "
                     f"({', '.join(BACKENDS)})")
