"""The google_generate_content API format: Gemini through the google-genai SDK, ported from v0.4's
sampler/gemini_sampler.py. Providers: `gemini` (AI Studio, GEMINI_API_KEY) and `vertex` (Vertex AI with application
default credentials, GEMINI_PROJECT_ID / GEMINI_LOCATION; v0.4 used it when GEMINI_API_KEY was unset).

Request (as v0.4): GenerateContentConfig(temperature, max_output_tokens, thinking_config=ThinkingConfig(
thinking_level=effort)); images as inline PNG bytes (lossless). max_output_tokens is raised to the preset's effort
floor (16384 at "high": thoughts count against it). Differences: every message is its own Content with its role
(user / model; v0.4 flattened all parts into one role-less Content, which the API reads as one user turn: the same
request for single-turn prompts), a system message becomes system_instruction, tools become function declarations.

Response: non-thought text parts = the answer, thought parts (with `--model-arg include_thoughts=true`) = the
reasoning; usage: prompt_token_count, output = candidates + thoughts tokens (Google bills thoughts as output),
reasoning = thoughts_token_count; finish MAX_TOKENS -> length, SAFETY / RECITATION / BLOCKLIST / PROHIBITED_CONTENT /
SPII / IMAGE_* -> content_filter (cause refused), a blocked prompt (no candidates, prompt_feedback.block_reason) ->
content_filter; function_call parts -> tool_calls.

Retry (v0.4: 429 / 500 / 503 / 504 and network errors retried forever with 2**n s): the same codes and network errors
(and the transient 400s of openai_chat) are retried with min(2**n, 60) s up to max_tries, other API errors fail.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from io import BytesIO

from simple_evals_mm.models import Image, Message, Response, Text, ToolCall
from simple_evals_mm.samplers.base import Sampler, retry_call
from simple_evals_mm.samplers.openai_chat import (
    TRANSIENT_400,
    _encode_file,
    _encode_pil,
)

REFUSAL_FINISH = {"safety", "recitation", "blocklist", "prohibited_content", "spii", "image_safety",
                  "image_prohibited_content", "image_recitation"}
RETRY_CODES = (429, 500, 503, 504)


def classify_google_error(e: Exception) -> str:
    from google.genai import errors as gerrors

    if isinstance(e, gerrors.APIError):
        if e.code == 429:
            return "rate"
        if e.code in RETRY_CODES or (e.code == 400 and any(sig in str(e) for sig in TRANSIENT_400)):
            return "retry"
        return "fail"
    return "retry"  # httpx / connection / OS errors (v0.4 retried these too)


class GeminiSampler(Sampler):
    api_format = "google_generate_content"

    def __init__(self, spec, base_url: str | None = None):
        super().__init__(spec)
        self._cli_base_url = base_url
        self._client = None
        self._client_lock = threading.Lock()

    @property
    def client(self):
        with self._client_lock:
            if self._client is None:
                self._client = self._make_client()
            return self._client

    def _make_client(self):
        from google import genai
        from google.genai import types

        s = self.spec
        url = s.base_url(self._cli_base_url) if s.base_url_source != "default" else None
        http = types.HttpOptions(base_url=url) if url else None
        if s.provider == "vertex":
            return genai.Client(vertexai=True, project=os.environ.get("GEMINI_PROJECT_ID"),
                                location=os.environ.get("GEMINI_LOCATION"), http_options=http)
        return genai.Client(api_key=s.api_key(), http_options=http)

    def preflight(self, timeout: float = 10.0) -> list[str]:
        """Key / project variables set (names only); AI Studio: its endpoint answers GET /v1beta/models."""
        import urllib.error
        import urllib.request

        s, problems = self.spec, []
        if s.provider == "vertex":
            return [f"{v} is not set (provider vertex)" for v in s.extra_vars if not os.environ.get(v)]
        if s.api_key_required and not os.environ.get(s.api_key_name or ""):
            problems.append(f"{s.api_key_name} is not set (provider {s.provider})")
        url = s.base_url(self._cli_base_url)
        if not url:
            return problems + [f"no base URL ({s.base_url_source} is empty)"]
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/v1beta/models", timeout=timeout):
                pass
        except urllib.error.HTTPError:
            pass  # the server answered (403 without a key)
        except Exception as e:  # noqa: BLE001
            problems.append(f"base URL from {s.base_url_source} is unreachable ({type(e).__name__})")
        return problems

    # ------------------------------------------------------------------ request
    def effective_max_tokens(self) -> int:
        return max(self.spec.max_tokens, self.spec.effective_effort_floor())

    def request_body(self) -> dict:
        """The GenerateContentConfig fields (recorded in the job config)."""
        s = self.spec
        body: dict = dict(s.sampling)
        body["max_output_tokens"] = self.effective_max_tokens()
        thinking: dict = {}
        if s.effort is not None:
            thinking["thinking_level"] = s.effort
        if s.include_thoughts is not None:
            thinking["include_thoughts"] = s.include_thoughts
        if thinking:
            body["thinking_config"] = thinking
        return body

    def config(self) -> dict:
        return {**super().config(), "request_body": self.request_body()}

    def _image_part(self, p: Image):
        from google.genai import types

        if p.path is not None:
            data = _encode_file(p.path, self.spec.max_image_px, self.spec.max_image_b64_mb)
        else:
            from PIL import Image as PILImage

            with PILImage.open(BytesIO(p.data)) as im:
                data = _encode_pil(im.convert("RGB"), self.spec.max_image_px, self.spec.max_image_b64_mb)
        return types.Part(inline_data=types.Blob(mime_type="image/png", data=base64.b64decode(data)))

    def to_contents(self, messages: list[Message]) -> tuple[list, str | None]:
        """(contents, system_instruction)."""
        from google.genai import types

        system, contents, names = [], [], {}
        for m in messages:
            if m.role in ("system", "developer"):
                system.append(m.text)
                continue
            if m.role == "tool":
                resp = types.FunctionResponse(id=m.tool_call_id, name=names.get(m.tool_call_id, ""), response={"result": m.text})
                contents.append(types.Content(role="user", parts=[types.Part(function_response=resp)]))
                continue
            parts = [types.Part(text=p.text) if isinstance(p, Text) else self._image_part(p) for p in m.content
                     if not (isinstance(p, Text) and not p.text and m.tool_calls)]
            for t in m.tool_calls or []:
                names[t.id] = t.name
                args = t.arguments if isinstance(t.arguments, dict) else json.loads(t.arguments or "{}")
                parts.append(types.Part(function_call=types.FunctionCall(id=t.id, name=t.name, args=args)))
            contents.append(types.Content(role="model" if m.role == "assistant" else "user", parts=parts))
        if self.spec.system_message:
            system.insert(0, self.spec.system_message)
        return contents, ("\n\n".join(system) if system else None)

    def build_config(self, system: str | None = None, tools: list[dict] | None = None):
        from google.genai import types

        body = self.request_body()
        thinking = body.pop("thinking_config", None)
        cfg = dict(body)
        if thinking:
            cfg["thinking_config"] = types.ThinkingConfig(**thinking)
        if system:
            cfg["system_instruction"] = system
        if tools:
            decls = []
            for t in tools:
                f = t.get("function", t)
                decls.append(types.FunctionDeclaration(name=f["name"], description=f.get("description"),
                                                       parameters_json_schema=f.get("parameters")))
            cfg["tools"] = [types.Tool(function_declarations=decls)]
        return types.GenerateContentConfig(**cfg)

    # ------------------------------------------------------------------ call
    def __call__(self, messages: list[Message], tools: list[dict] | None = None) -> Response:
        s = self.spec
        contents, system = self.to_contents(messages)
        config = self.build_config(system, tools)
        resp = retry_call(lambda: self.client.models.generate_content(model=s.request_model, contents=contents, config=config),
                          label=f"[{s.provider}] {s.model}", max_tries=s.max_tries,
                          max_rate_limit_tries=s.max_rate_limit_tries, classify=classify_google_error)
        return self._parse(resp)

    def _parse(self, resp) -> Response:
        texts, thoughts, tool_calls = [], [], []
        cands = getattr(resp, "candidates", None) or []
        finish = ""
        if cands:
            fr = cands[0].finish_reason
            finish = (fr.name if hasattr(fr, "name") else str(fr or "")).lower()
            content = getattr(cands[0], "content", None)
            for i, part in enumerate(getattr(content, "parts", None) or []):
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_calls.append(ToolCall(id=fc.id or f"call_{i}", name=fc.name, arguments=dict(fc.args or {})))
                elif part.text:
                    (thoughts if getattr(part, "thought", None) else texts).append(part.text)
        elif getattr(getattr(resp, "prompt_feedback", None), "block_reason", None):
            finish = "content_filter"  # the prompt itself was blocked
        if finish == "max_tokens":
            finish = "length"
        elif finish in REFUSAL_FINISH:
            finish = "content_filter"
        elif finish == "stop" and tool_calls:
            finish = "tool_calls"
        um = getattr(resp, "usage_metadata", None)
        in_tok = (um.prompt_token_count or 0) if um else 0
        think = (um.thoughts_token_count or 0) if um else 0
        out_tok = ((um.candidates_token_count or 0) + think) if um else 0
        return Response(
            text="".join(texts).strip(), reasoning="".join(thoughts).strip(), tool_calls=tool_calls,
            input_tokens=in_tok, output_tokens=out_tok, reasoning_tokens=think,
            cached_tokens=(getattr(um, "cached_content_token_count", 0) or 0) if um else 0,
            finish_reason=finish, model=getattr(resp, "model_version", None), raw=resp,
        )
