"""The openai_responses API format: OpenAI's Responses API (providers openai / azure), ported from v0.4's
sampler/responses_sampler.py.

Request (as v0.4): `responses.create(model, input, max_output_tokens, reasoning={"effort": e})`, the developer system
message first, images as `input_image` data URLs (lossless PNG, detail "high"); no temperature (gpt-5 rejects it) unless
given explicitly. max_output_tokens is raised to the preset's effort floor (16384 at "high": reasoning tokens count
against it). Azure: the preset's deployment name (`api_model`), API version and key / endpoint variable names.

Response: output_text = the answer; reasoning summaries (requested with `--model-arg reasoning_summary=auto`) = the
reasoning; usage with output_tokens_details.reasoning_tokens and input_tokens_details.cached_tokens; status mapped to
finish_reason (completed -> stop, incomplete: max_output_tokens -> length, content_filter -> content_filter, other
-> length as v0.4); a refusal content part -> finish_reason content_filter (cause refused); function_call items ->
tool_calls.

Retry (v0.4 retried every non-400 error forever with min(2**n, 60) s): 400 fails at once, 401-404 fail at once, 429
jittered backoff up to max_rate_limit_tries, other errors min(2**n, 60) s up to max_tries, then provider_failed.
"""

from __future__ import annotations

import json

from simple_evals_mm import errors
from simple_evals_mm.models import Image, Message, Response, Text, ToolCall
from simple_evals_mm.samplers.base import retry_call
from simple_evals_mm.samplers.openai_chat import TRANSIENT_400, OpenAIChatSampler


def _tool(t: dict) -> dict:
    """A chat-completions tool definition -> the Responses API's flat function tool."""
    if t.get("type") == "function" and isinstance(t.get("function"), dict):
        f = t["function"]
        out = {"type": "function", "name": f["name"], "parameters": f.get("parameters", {"type": "object", "properties": {}})}
        if f.get("description"):
            out["description"] = f["description"]
        if "strict" in f:
            out["strict"] = f["strict"]
        return out
    return t


class OpenAIResponsesSampler(OpenAIChatSampler):
    api_format = "openai_responses"

    # ------------------------------------------------------------------ request
    def request_body(self) -> dict:
        s = self.spec
        body: dict = {"max_output_tokens": self.effective_max_tokens()}
        body.update(s.sampling)
        if s.effort is not None or s.reasoning_summary:
            reasoning: dict = {}
            if s.effort is not None:
                reasoning["effort"] = s.effort
            if s.reasoning_summary:
                reasoning["summary"] = s.reasoning_summary
            body["reasoning"] = reasoning
        return body

    def _image_part(self, p: Image) -> dict:  # type: ignore[override]
        chat = super()._image_part(p)["image_url"]
        part = {"type": "input_image", "image_url": chat["url"]}
        if chat.get("detail"):
            part["detail"] = chat["detail"]
        return part

    def to_api_messages(self, messages: list[Message]) -> list[dict]:
        s = self.spec
        out: list[dict] = []
        if s.system_message:
            out.append({"role": s.system_role, "content": [{"type": "input_text", "text": s.system_message}]})
        for m in messages:
            if m.role == "tool":
                out.append({"type": "function_call_output", "call_id": m.tool_call_id, "output": m.text})
                continue
            if m.role == "assistant":
                if m.text:
                    out.append({"role": "assistant", "content": m.text})
                for t in m.tool_calls or []:
                    out.append({"type": "function_call", "call_id": t.id, "name": t.name,
                                "arguments": t.arguments if isinstance(t.arguments, str) else json.dumps(t.arguments)})
                continue
            content = [{"type": "input_text", "text": p.text} if isinstance(p, Text) else self._image_part(p) for p in m.content]
            out.append({"role": m.role, "content": content})
        return out

    # ------------------------------------------------------------------ call
    def __call__(self, messages: list[Message], tools: list[dict] | None = None) -> Response:
        s = self.spec
        items = self.to_api_messages(messages)
        kwargs = self.request_body()
        if tools:
            kwargs["tools"] = [_tool(t) for t in tools]
        if s.extra_body:
            kwargs["extra_body"] = dict(s.extra_body)
        resp = retry_call(lambda: self.client.responses.create(model=s.request_model, input=items, **kwargs),
                          label=f"[{s.provider}] {s.model}", max_tries=s.max_tries,
                          max_rate_limit_tries=s.max_rate_limit_tries, classify=classify_openai_error)
        if getattr(resp, "status", None) == "failed":
            err = getattr(resp, "error", None)
            raise errors.SamplerError(f"response failed: {getattr(err, 'code', '')} {getattr(err, 'message', err)}",
                                      exc_type="ResponseFailed")
        return self._parse(resp)

    def _parse(self, resp) -> Response:  # type: ignore[override]
        texts, summaries, tool_calls, refused = [], [], [], False
        for item in getattr(resp, "output", None) or []:
            kind = getattr(item, "type", None)
            if kind == "message":
                for c in getattr(item, "content", None) or []:
                    ctype = getattr(c, "type", None)
                    if ctype == "output_text":
                        texts.append(c.text or "")
                    elif ctype == "refusal":
                        refused = True
            elif kind == "reasoning":
                summaries += [getattr(x, "text", "") or "" for x in getattr(item, "summary", None) or []]
                summaries += [getattr(x, "text", "") or "" for x in getattr(item, "content", None) or []]
            elif kind == "function_call":
                args = item.arguments
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    pass
                tool_calls.append(ToolCall(id=item.call_id, name=item.name, arguments=args))
        text = getattr(resp, "output_text", None)
        if text is None:
            text = "".join(texts)
        status = getattr(resp, "status", None) or ""
        if refused:
            finish = "content_filter"
        elif status == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
            finish = "content_filter" if reason == "content_filter" else "length"
        elif tool_calls:
            finish = "tool_calls"
        else:
            finish = {"completed": "stop"}.get(status, status)
        usage = getattr(resp, "usage", None)
        out_details = getattr(usage, "output_tokens_details", None) if usage else None
        in_details = getattr(usage, "input_tokens_details", None) if usage else None
        return Response(
            text=(text or "").strip(), reasoning="\n\n".join(x for x in summaries if x).strip(), tool_calls=tool_calls,
            input_tokens=(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
            output_tokens=(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
            reasoning_tokens=(getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0,
            cached_tokens=(getattr(in_details, "cached_tokens", 0) or 0) if in_details else 0,
            finish_reason=finish, model=getattr(resp, "model", None), raw=resp,
        )


def classify_openai_error(e: Exception) -> str:
    """'fail' (raise SamplerError now), 'rate' (rate limited) or 'retry' (transient) for an OpenAI SDK exception."""
    import openai

    if isinstance(e, openai.BadRequestError):
        return "retry" if any(sig in str(e) for sig in TRANSIENT_400) else "fail"
    if isinstance(e, openai.RateLimitError):
        return "rate"
    if isinstance(e, openai.APIStatusError) and e.status_code in (401, 402, 403, 404):
        return "fail"
    return "retry"
