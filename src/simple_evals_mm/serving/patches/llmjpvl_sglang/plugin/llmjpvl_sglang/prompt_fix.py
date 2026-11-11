"""Undo the space that tokenizer.decode() inserts after special tokens.

sglang's OpenAI chat endpoint renders the Jinja template, encodes it and, for
multimodal MODELS (text-only requests included), decodes the ids back to text
(serving_chat._apply_jinja_template). The llm-jp tokenizer's decode inserts a
space after every special token ("<|start|> system<|message|> You are ..."), so
every request was re-encoded from an off-distribution prompt (+5 tokens on a
GPQA prompt, +6 on CC-OCR; CC-OCR ja 70.3 -> 51.7, GPQA answers agreeing with HF
on 88/198 items; found 2026-10-20). Both the mm processor (image requests) and a
sitecustomize wrapper around _apply_jinja_template (all requests) apply this.
"<|constrain|> json" keeps its real space (Harmony tool calls). No-op on text
that never went through decode.
"""

import re

_DECODE_SPACE_RE = re.compile(r"(<\|(?!constrain\|)[a-z_]+\|>) (?=\S)")


def undo_decode_spaces(text: str) -> str:
    return _DECODE_SPACE_RE.sub(r"\1", text)
