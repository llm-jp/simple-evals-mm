"""SGLang model for LLM-jp-VL (``architectures: ["LLMjpVLModel"]``).

Mirrors llmjpvl/modeling_llmjpvl.py: SigLIP2 vision tower (sglang SiglipVisionModel,
GELU-tanh like HF) -> pixel shuffle (downsample_ratio 0.5) -> mlp1 (LayerNorm,
Linear, GELU, Linear) -> Llama LM (sglang LlamaForCausalLM). Image features are
scattered onto the <|image_pad|> positions by sglang's general_mm_embed_routine.
Based on sglang/srt/models/internvl.py (same InternVL-style architecture) and
gemma3_mm.py (SigLIP tower + weight-name mapping).
"""

from typing import Iterable, List, Optional, Tuple

import torch
from torch import nn
from transformers import PretrainedConfig

from sglang.srt.layers.quantization.base_config import QuantizationConfig
from sglang.srt.managers.mm_utils import (
    MultiModalityDataPaddingPatternTokenPairs,
    general_mm_embed_routine,
)
from sglang.srt.managers.schedule_batch import (
    Modality,
    MultimodalDataItem,
    MultimodalInputs,
)
from sglang.srt.model_executor.forward_batch_info import ForwardBatch
from sglang.srt.model_loader.weight_utils import default_weight_loader
from sglang.srt.models.llama import LlamaForCausalLM
from sglang.srt.models.siglip import SiglipVisionModel
from sglang.srt.utils import add_prefix
from sglang.utils import logger


class LLMjpVLModel(nn.Module):
    # tiles per vision-tower call (a request can carry hundreds of tiles)
    vit_tile_chunk: int = 32

    def __init__(
        self,
        config: PretrainedConfig,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        self.config = config
        self.quant_config = quant_config
        image_size = getattr(config, "force_image_size", None) or config.vision_config.image_size
        patch_size = config.vision_config.patch_size
        self.select_layer = getattr(config, "select_layer", -1)
        if self.select_layer != -1:
            raise NotImplementedError("LLMjpVLModel sglang port supports select_layer == -1 only")
        self.downsample_ratio = config.downsample_ratio
        self.num_image_token = int((image_size // patch_size) ** 2 * (self.downsample_ratio**2))
        self.ps_version = getattr(config, "ps_version", "v2")
        if self.ps_version != "v2":
            raise NotImplementedError("ps_version v1 not supported")
        logger.info(f"[LLMjpVL] num_image_token per tile: {self.num_image_token}")

        self.vision_backbone = SiglipVisionModel(
            config.vision_config,
            quant_config=quant_config,
            prefix=add_prefix("vision_backbone", prefix),
        )
        # sglang's SiglipMLP defaults to QuickGELU; HF SigLIP2 uses gelu_pytorch_tanh.
        act = getattr(config.vision_config, "hidden_act", "gelu_pytorch_tanh")
        if act in ("gelu_pytorch_tanh", "gelu_new", "gelu_fast"):
            for layer in self.vision_backbone.vision_model.encoder.layers:
                layer.mlp.act = nn.GELU(approximate="tanh")
        elif act == "gelu":
            for layer in self.vision_backbone.vision_model.encoder.layers:
                layer.mlp.act = nn.GELU()

        llm_arch = config.llm_config.architectures[0]
        if llm_arch != "LlamaForCausalLM":
            raise NotImplementedError(f"{llm_arch} is not supported by the LLMjpVL sglang port")
        self.language_model = LlamaForCausalLM(
            config=config.llm_config,
            quant_config=quant_config,
            prefix=add_prefix("language_model", prefix),
        )

        vit_hidden_size = config.vision_config.hidden_size
        llm_hidden_size = config.llm_config.hidden_size
        proj_in = vit_hidden_size * int(1 / self.downsample_ratio) ** 2
        self.mlp1 = nn.Sequential(
            nn.LayerNorm(proj_in),
            nn.Linear(proj_in, llm_hidden_size),
            nn.GELU(),
            nn.Linear(llm_hidden_size, llm_hidden_size),
        )

        self.data_embedding_funcs = {Modality.IMAGE: self.get_image_feature}
        # some sglang code paths look for `.model` on the multimodal wrapper
        self.model = self.language_model.model

    # ---- vision -----------------------------------------------------------
    def pixel_shuffle(self, x: torch.Tensor, scale_factor: float = 0.5) -> torch.Tensor:
        n, w, h, c = x.size()
        x = x.view(n, w, int(h * scale_factor), int(c / scale_factor))
        x = x.permute(0, 2, 1, 3).contiguous()
        x = x.view(n, int(h * scale_factor), int(w * scale_factor), int(c / (scale_factor * scale_factor)))
        x = x.permute(0, 2, 1, 3).contiguous()
        return x

    def extract_feature(self, pixel_values: torch.Tensor) -> torch.Tensor:
        """(T, 3, H, W) tiles -> (T, num_image_token, llm_hidden)"""
        outs = []
        dtype = self.mlp1[1].weight.dtype
        for start in range(0, pixel_values.shape[0], self.vit_tile_chunk):
            pv = pixel_values[start : start + self.vit_tile_chunk].to(
                device=self.vision_backbone.device, dtype=dtype
            )
            vit_embeds = self.vision_backbone(pv)  # (t, 1024, 1152), post-layernormed
            h = w = int(vit_embeds.shape[1] ** 0.5)
            vit_embeds = vit_embeds.reshape(vit_embeds.shape[0], h, w, -1)
            vit_embeds = self.pixel_shuffle(vit_embeds, self.downsample_ratio)
            vit_embeds = vit_embeds.reshape(vit_embeds.shape[0], -1, vit_embeds.shape[-1])
            outs.append(self.mlp1(vit_embeds))
        return torch.cat(outs, dim=0)

    def get_image_feature(self, items: List[MultimodalDataItem]) -> torch.Tensor:
        feats = []
        for item in items:
            f = item.feature
            if f.dim() == 4:  # raw tiles
                feats.append(f)
            else:  # precomputed embeddings
                return torch.cat([it.feature for it in items]).to(self.language_model.model.embed_tokens.weight.device)
        pixel_values = torch.cat(feats, dim=0)
        return self.extract_feature(pixel_values)

    # ---- forward ----------------------------------------------------------
    @torch.no_grad()
    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        forward_batch: ForwardBatch,
        input_embeds: torch.Tensor = None,
    ) -> torch.Tensor:
        return general_mm_embed_routine(
            input_ids=input_ids,
            forward_batch=forward_batch,
            language_model=self.language_model,
            multimodal_model=self,
            data_embedding_funcs=self.data_embedding_funcs,
            positions=positions,
        )

    def pad_input_ids(self, input_ids: List[int], mm_inputs: MultimodalInputs):
        # <|image_start|> ... <|image_end|>: the context tokens in between get the
        # per-item hash (radix-cache key), like InternVL.
        helper = MultiModalityDataPaddingPatternTokenPairs([(mm_inputs.im_start_id, mm_inputs.im_end_id)])
        return helper.pad_input_tokens(input_ids, mm_inputs)

    # ---- weights ----------------------------------------------------------
    def load_weights(self, weights: Iterable[Tuple[str, torch.Tensor]]):
        vis_stacked = [
            (".qkv_proj", ".q_proj", "q"),
            (".qkv_proj", ".k_proj", "k"),
            (".qkv_proj", ".v_proj", "v"),
        ]
        params = dict(self.named_parameters())
        lm_weights = []
        n_vis = n_proj = 0
        for name, w in weights:
            if name.startswith("language_model."):
                lm_weights.append((name[len("language_model."):], w))
                continue
            if name.startswith("vision_backbone."):
                if ".vision_model.head." in name:  # SigLIP attention-pooling head: unused
                    continue
                name = name.replace(".self_attn.out_proj", ".self_attn.proj")
                for pn, wn, sid in vis_stacked:
                    if wn in name:
                        name = name.replace(wn, pn)
                        param = params[name]
                        param.weight_loader(param, w, sid)
                        break
                else:
                    param = params[name]
                    getattr(param, "weight_loader", default_weight_loader)(param, w)
                n_vis += 1
                continue
            if name.startswith("mlp1."):
                param = params[name]
                default_weight_loader(param, w)
                n_proj += 1
                continue
            logger.warning(f"[LLMjpVL] unexpected weight skipped: {name}")
        self.language_model.load_weights(lm_weights)
        logger.info(f"[LLMjpVL] loaded {n_vis} vision, {n_proj} projector, {len(lm_weights)} LM tensors")

    # speculative-decoding helpers expected by some sglang paths
    def get_embed_and_head(self):
        return self.language_model.model.embed_tokens.weight, self.language_model.lm_head.weight

    def set_embed_and_head(self, embed, head):
        del self.language_model.model.embed_tokens.weight
        del self.language_model.lm_head.weight
        self.language_model.model.embed_tokens.weight = embed
        self.language_model.lm_head.weight = head


EntryClass = LLMjpVLModel
