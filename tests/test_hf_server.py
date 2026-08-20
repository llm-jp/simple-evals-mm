"""Unit tests for serving/hf_server.py protocol conversion (no GPU, no HTTP)."""

import base64
import io

import pytest
from PIL import Image

from simple_evals_mm.common import SamplerResponse
from simple_evals_mm.serving.hf_server import (
    build_openai_response,
    extract_images_and_text,
    load_image_from_data_url,
    openai_messages_to_sampler_messages,
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


class TestMessageRepacking:
    def test_repacks_through_sampler(self):
        class FakeSampler:
            def pack_message(self, images=None, instruction="", role="user"):
                return {"role": role, "images": images, "text": instruction}

        messages = [
            {"role": "system", "content": "be helpful"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                    {"type": "text", "text": "describe"},
                ],
            },
        ]
        packed = openai_messages_to_sampler_messages(FakeSampler(), messages)
        assert packed[0] == {"role": "system", "images": None, "text": "be helpful"}
        assert packed[1]["role"] == "user"
        assert len(packed[1]["images"]) == 1
        assert packed[1]["text"] == "describe"


class TestBuildResponse:
    def test_maps_sampler_response_fields(self):
        resp = SamplerResponse(
            response_text="Answer: C",
            reasoning="thought about it",
            raw="...",
            input_tokens=100,
            output_tokens=50,
            reasoning_tokens=30,
            finish_reason="length",
        )
        out = build_openai_response(resp, "some/model", "abc")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "Answer: C"
        assert msg["reasoning_content"] == "thought about it"
        assert out["choices"][0]["finish_reason"] == "length"
        u = out["usage"]
        assert u["prompt_tokens"] == 100 and u["completion_tokens"] == 50
        assert u["total_tokens"] == 150 and u["reasoning_tokens"] == 30
        assert out["model"] == "some/model"

    def test_no_reasoning_omits_field_and_defaults_finish(self):
        resp = SamplerResponse(response_text="B", raw="B")
        out = build_openai_response(resp, "m", "id")
        msg = out["choices"][0]["message"]
        assert "reasoning_content" not in msg
        assert out["choices"][0]["finish_reason"] == "stop"


class TestClientCompatibility:
    def test_sglang_sampler_parses_our_response(self):
        """The openai SDK objects SGLangSampler builds must expose exactly the
        fields our server emits (incl. extra fields via pydantic extra-allow)."""
        from openai.types.chat import ChatCompletion

        resp = SamplerResponse(
            response_text="A",
            reasoning="think",
            raw="...",
            input_tokens=10,
            output_tokens=5,
            reasoning_tokens=3,
            finish_reason="stop",
        )
        parsed = ChatCompletion.model_validate(
            build_openai_response(resp, "m", "id")
        )
        message = parsed.choices[0].message
        assert message.content == "A"
        assert (getattr(message, "reasoning_content", None) or "") == "think"
        assert parsed.usage.prompt_tokens == 10
        assert (getattr(parsed.usage, "reasoning_tokens", 0) or 0) == 3
        assert parsed.choices[0].finish_reason == "stop"
