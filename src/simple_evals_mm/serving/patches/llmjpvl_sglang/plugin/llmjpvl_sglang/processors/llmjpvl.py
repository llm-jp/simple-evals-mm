"""SGLang multimodal processor for LLM-jp-VL.

Reproduces llmjpvl/processing_llmjpvl.py (LLMjpVLProcessor.__call__): the number
of tiles per image is capped by the text-token budget
``(tokenizer.model_max_length - text_tokens) // num_images``, each image is split
with the InternVL dynamic_preprocess (closest aspect ratio, <= max_dynamic_patch
tiles + thumbnail) and normalized by the model's SiglipImageProcessor, and every
``<image>`` placeholder in the prompt becomes
``<|image_start|><|image_pad|> x (256 x tiles)<|image_end|>``.
"""

import logging
from typing import List

import torch
from PIL import Image

from sglang.srt.managers.schedule_batch import (
    Modality,
    MultimodalDataItem,
    MultimodalProcessorOutput,
)
from sglang.srt.multimodal.processors.base_processor import (
    BaseMultimodalProcessor,
    MultimodalSpecialTokens,
)

from llmjpvl_sglang.models.llmjpvl import LLMjpVLModel

logger = logging.getLogger(__name__)

IMAGE_PLACEHOLDER = "<image>"
IMG_START = "<|image_start|>"
IMG_END = "<|image_end|>"
IMG_CONTEXT = "<|image_pad|>"


# --- vendored from llmjpvl/processing_llmjpvl.py (identical tiling) -------------

from llmjpvl_sglang.prompt_fix import undo_decode_spaces as _undo_decode_spaces  # noqa: E402


def find_closest_aspect_ratio(aspect_ratio, target_ratios, width, height, image_size):
    best_ratio_diff = float("inf")
    best_ratio = (1, 1)
    area = width * height
    for ratio in target_ratios:
        target_aspect_ratio = ratio[0] / ratio[1]
        ratio_diff = abs(aspect_ratio - target_aspect_ratio)
        if ratio_diff < best_ratio_diff:
            best_ratio_diff = ratio_diff
            best_ratio = ratio
        elif ratio_diff == best_ratio_diff:
            if area > 0.5 * image_size * image_size * ratio[0] * ratio[1]:
                best_ratio = ratio
    return best_ratio


def dynamic_preprocess(
    image, min_num=1, max_num=12, image_size=512, use_thumbnail=False
):
    orig_width, orig_height = image.size
    aspect_ratio = orig_width / orig_height
    target_ratios = set(
        (i, j)
        for n in range(min_num, max_num + 1)
        for i in range(1, n + 1)
        for j in range(1, n + 1)
        if i * j <= max_num and i * j >= min_num
    )
    target_ratios = sorted(target_ratios, key=lambda x: x[0] * x[1])
    target_aspect_ratio = find_closest_aspect_ratio(
        aspect_ratio, target_ratios, orig_width, orig_height, image_size
    )
    target_width = image_size * target_aspect_ratio[0]
    target_height = image_size * target_aspect_ratio[1]
    blocks = target_aspect_ratio[0] * target_aspect_ratio[1]
    resized_img = image.resize((target_width, target_height))
    processed_images = []
    for i in range(blocks):
        box = (
            (i % (target_width // image_size)) * image_size,
            (i // (target_width // image_size)) * image_size,
            ((i % (target_width // image_size)) + 1) * image_size,
            ((i // (target_width // image_size)) + 1) * image_size,
        )
        processed_images.append(resized_img.crop(box))
    if use_thumbnail and len(processed_images) != 1:
        processed_images.append(image.resize((image_size, image_size)))
    return processed_images


class LLMjpVLImageProcessor(BaseMultimodalProcessor):
    models = [LLMjpVLModel]
    gpu_image_decode = False

    def __init__(self, hf_config, server_args, _processor, *args, **kwargs):
        super().__init__(hf_config, server_args, _processor, *args, **kwargs)
        self.hf_config = hf_config
        # _processor is LLMjpVLProcessor (AutoProcessor, remote code) when the
        # model dir has processor_config.json with auto_map; else a tokenizer.
        if hasattr(_processor, "tokenizer"):
            self.tokenizer = _processor.tokenizer
            self.image_processor = getattr(_processor, "image_processor", None)
        else:
            self.tokenizer = _processor
            self.image_processor = None
        if self.image_processor is None:
            from transformers import AutoImageProcessor

            self.image_processor = AutoImageProcessor.from_pretrained(
                server_args.model_path, trust_remote_code=True
            )
        image_size = (
            getattr(hf_config, "force_image_size", None)
            or hf_config.vision_config.image_size
        )
        patch_size = hf_config.vision_config.patch_size
        self.image_size = int(image_size)
        self.num_image_token = int(
            (image_size // patch_size) ** 2 * (hf_config.downsample_ratio**2)
        )
        self.min_dynamic_patch = int(getattr(hf_config, "min_dynamic_patch", 1))
        self.max_dynamic_patch = int(getattr(hf_config, "max_dynamic_patch", 12))
        self.use_thumbnail = bool(getattr(hf_config, "use_thumbnail", True))
        self.dynamic_image_size = bool(getattr(hf_config, "dynamic_image_size", True))
        # Same text-token budget as the HF processor (tokenizer.model_max_length,
        # 4096 in the released models); fall back to the server context length.
        mml = getattr(self.tokenizer, "model_max_length", None)
        ctx = getattr(server_args, "context_length", None) or getattr(
            hf_config.llm_config, "max_position_embeddings", 65536
        )
        self.budget_len = int(mml) if (mml and mml < 10**8) else int(ctx)

        tk = self.tokenizer
        self.img_start_id = tk.convert_tokens_to_ids(IMG_START)
        self.img_end_id = tk.convert_tokens_to_ids(IMG_END)
        self.img_context_id = tk.convert_tokens_to_ids(IMG_CONTEXT)
        assert all(
            isinstance(i, int) and i >= 0
            for i in (self.img_start_id, self.img_end_id, self.img_context_id)
        ), "image tokens missing from tokenizer"
        self.mm_tokens = MultimodalSpecialTokens(
            image_token=IMAGE_PLACEHOLDER, image_token_id=self.img_context_id
        ).build(_processor)
        logger.info(
            f"[LLMjpVL processor] image_size={self.image_size} tokens/tile={self.num_image_token} "
            f"max_tiles={self.max_dynamic_patch} budget_len={self.budget_len}"
        )

    @staticmethod
    def _ensure_placeholders(prompt: str, want: int) -> str:
        """If the chat template dropped image parts, put the missing <image>
        placeholders at the start of the last user message (HF processor behaviour)."""
        have = prompt.count(IMAGE_PLACEHOLDER)
        missing = want - have
        if missing <= 0:
            return prompt
        marker = "<|start|>user<|message|>"
        idx = prompt.rfind(marker)
        ins = IMAGE_PLACEHOLDER * missing
        if idx < 0:
            return ins + prompt
        pos = idx + len(marker)
        return prompt[:pos] + ins + prompt[pos:]

    async def process_mm_data_async(
        self, image_data, input_text, request_obj, **kwargs
    ):
        if isinstance(input_text, list):
            # pre-tokenized prompt: not supported for this model's image expansion
            raise ValueError(
                "LLMjpVL: pre-tokenized prompts with images are not supported"
            )
        prompt = _undo_decode_spaces(input_text or "")
        if image_data:
            prompt = self._ensure_placeholders(prompt, len(image_data))
        base_output = await self.load_mm_data(
            prompt=prompt,
            image_data=image_data,
            multimodal_tokens=self.mm_tokens,
            discard_alpha_channel=True,
        )
        text = base_output.input_text or prompt
        images = list(base_output.images or [])
        if image_data and not images:
            raise ValueError("LLMjpVL: image_data given but no images loaded")

        # tile budget from the text length (without placeholders), as in HF
        text_tokens = len(
            self.tokenizer.encode(
                text.replace(IMAGE_PLACEHOLDER, ""), add_special_tokens=False
            )
        )
        num_image = max(1, len(images))
        image_budget = self.budget_len - text_tokens
        max_num = (image_budget // num_image - 2) // self.num_image_token - 1
        max_num = max(1, min(self.max_dynamic_patch, max_num))

        num_patches_list: List[int] = []
        pv_list: List[torch.Tensor] = []
        for image in images:
            if not isinstance(image, Image.Image):
                raise ValueError(f"LLMjpVL: unsupported image type {type(image)}")
            image = image.convert("RGB")
            tiles = (
                dynamic_preprocess(
                    image,
                    min_num=self.min_dynamic_patch,
                    max_num=max_num,
                    image_size=self.image_size,
                    use_thumbnail=self.use_thumbnail,
                )
                if self.dynamic_image_size
                else [image]
            )
            pv = self.image_processor(
                images=tiles, return_tensors="pt"
            ).pixel_values  # (T,3,H,W) f32
            pv_list.append(pv)
            num_patches_list.append(int(pv.shape[0]))

        # expand <image> -> <|image_start|><|image_pad|>*(256*T)<|image_end|>, in order
        for n in num_patches_list:
            text = text.replace(
                IMAGE_PLACEHOLDER,
                IMG_START + IMG_CONTEXT * (self.num_image_token * n) + IMG_END,
                1,
            )
        input_ids_t = self.tokenizer(
            text, return_tensors="pt", add_special_tokens=False
        )["input_ids"].flatten()
        input_ids = input_ids_t.tolist()

        items = []
        if pv_list:
            offsets = self.get_mm_items_offset(
                input_ids=input_ids_t, mm_token_id=self.img_context_id
            )
            assert len(offsets) == len(pv_list), (
                f"LLMjpVL: {len(offsets)} image spans vs {len(pv_list)} images"
            )
            for pv, off in zip(pv_list, offsets):
                items.append(
                    MultimodalDataItem(
                        feature=pv, modality=Modality.IMAGE, offsets=[off]
                    )
                )

        return MultimodalProcessorOutput(
            input_ids=input_ids,
            mm_items=items,
            im_start_id=self.img_start_id,
            im_end_id=self.img_end_id,
            im_token_id=self.img_context_id,
        )
