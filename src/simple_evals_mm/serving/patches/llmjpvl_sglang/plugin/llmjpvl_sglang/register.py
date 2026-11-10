"""Registration of the LLM-jp-VL model for SGLang: the startup module of the `llmjpvl_sglang` serving patch.

`sem serve --patch llmjpvl_sglang` puts the plugin root on sys.path, sets SGLANG_EXTERNAL_MODEL_PACKAGE (patch.json
env) and imports this module in every engine process (the overlay sitecustomize), so the scheduler / tokenizer-manager
/ detokenizer subprocesses register it too, without modifying the sglang package:

- SGLANG_EXTERNAL_MODEL_PACKAGE -> sglang's ModelRegistry imports `llmjpvl_sglang.models`
  (EntryClass LLMjpVLModel = hf_config.architectures[0]).
- post-import hooks (sglang is NOT imported eagerly here):
  * sglang.srt.configs.model_config: add "LLMjpVLModel" to multimodal_model_archs
    (otherwise the model is treated as text-only and images are dropped);
  * sglang.srt.managers.multimodal_processor: register
    llmjpvl_sglang.processors (image tiling + <|image_pad|> expansion);
  * sglang.srt.entrypoints.openai.serving_chat: undo the spaces the llm-jp tokenizer's decode inserts after special
    tokens in multimodal prompts (llmjpvl_sglang.prompt_fix), and keep the images of a "string"-format chat template
    (the released model's): sglang 0.5.23 drops them, the HF path puts "<image>" per image before the text.

Ported from llm-jp/llmjpvl sglang_plugin (sitecustomize.py, commit 012710d, BSD-3-Clause).
"""

import importlib.abc
import importlib.machinery
import os
import sys

os.environ.setdefault("SGLANG_EXTERNAL_MODEL_PACKAGE", "llmjpvl_sglang.models")  # also set by patch.json env


def _on_model_config(module):
    archs = getattr(module, "multimodal_model_archs", None)
    if isinstance(archs, list) and "LLMjpVLModel" not in archs:
        archs.append("LLMjpVLModel")


def _on_multimodal_processor(module):
    try:
        module.import_processors("llmjpvl_sglang.processors")
    except Exception as e:  # pragma: no cover
        print(f"[llmjpvl_sglang] processor registration failed: {e}", file=sys.stderr)


def _string_format_with_images(orig):
    """sglang 0.5.23 drops every image part when the chat template's content format is "string" (the released
    llm-jp-4-vl template renders plain-string content). For a message with images: collect them as the "openai" format
    does, and give the template the content the HF LLMjpVLProcessor path uses: "<image>" per image first, then the text
    parts (joined with " " as sglang's string format does). Messages without images are unchanged."""

    def wrapped(msg_dict, content_format, image_data, *args, **kwargs):
        content = msg_dict.get("content")
        if content_format != "string" or not isinstance(content, list) or not any(
            isinstance(c, dict) and c.get("type") in ("image_url", "input_image") for c in content
        ):
            return orig(msg_dict, content_format, image_data, *args, **kwargs)
        parts = orig(msg_dict, "openai", image_data, *args, **kwargs)["content"]
        n_img = sum(1 for c in parts if c.get("type") == "image")
        texts = [c["text"] for c in parts if c.get("type") == "text"]
        new = {k: v for k, v in msg_dict.items() if v is not None}
        new["content"] = "<image>" * n_img + " ".join(texts)
        return new

    wrapped._llmjpvl_wrapped = True
    return wrapped


def _on_serving_chat(module):
    """Wrap OpenAIServingChat._apply_jinja_template: for multimodal models sglang decodes the
    encoded prompt back to text, and the llm-jp tokenizer's decode inserts a space after each
    special token (text-only requests included). Undo it so the re-encoded prompt equals the
    rendered one (llmjpvl_sglang.prompt_fix, 2026-10-20)."""
    pc = getattr(module, "process_content_for_template_format", None)
    if pc is not None and not getattr(pc, "_llmjpvl_wrapped", False):
        module.process_content_for_template_format = _string_format_with_images(pc)
    cls = getattr(module, "OpenAIServingChat", None)
    orig = getattr(cls, "_apply_jinja_template", None)
    if cls is None or orig is None or getattr(orig, "_llmjpvl_wrapped", False):
        return
    from llmjpvl_sglang.prompt_fix import undo_decode_spaces

    def wrapped(self, request, tools, is_multimodal):
        result = orig(self, request, tools, is_multimodal)
        prompt = getattr(result, "prompt", None)
        if isinstance(prompt, str) and prompt:
            fixed = undo_decode_spaces(prompt)
            if fixed != prompt:
                result.prompt = fixed
                if isinstance(getattr(result, "prompt_ids", None), str):
                    result.prompt_ids = fixed
        return result

    wrapped._llmjpvl_wrapped = True
    cls._apply_jinja_template = wrapped


_HOOKS = {
    "sglang.srt.configs.model_config": _on_model_config,
    "sglang.srt.managers.multimodal_processor": _on_multimodal_processor,
    "sglang.srt.entrypoints.openai.serving_chat": _on_serving_chat,
}


class _LoaderProxy(importlib.abc.Loader):
    def __init__(self, loader, callback):
        self._loader = loader
        self._callback = callback

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        self._callback(module)


class _PostImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        cb = _HOOKS.get(fullname)
        if cb is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _LoaderProxy(spec.loader, cb)
                return spec
        return None


if not any(isinstance(f, _PostImportFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _PostImportFinder())
