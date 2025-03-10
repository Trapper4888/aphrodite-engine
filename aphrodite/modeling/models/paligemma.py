from typing import Iterable, List, Literal, Optional, Tuple, TypedDict

import torch
from loguru import logger
from torch import nn
from transformers import PaliGemmaConfig

from aphrodite.attention import AttentionMetadata
from aphrodite.common.config import CacheConfig, MultiModalConfig
from aphrodite.common.sequence import IntermediateTensors, SamplerOutput
from aphrodite.inputs import INPUT_REGISTRY, InputContext, LLMInputs
from aphrodite.modeling.layers.logits_processor import LogitsProcessor
from aphrodite.modeling.layers.sampler import Sampler
from aphrodite.modeling.model_loader.weight_utils import default_weight_loader
from aphrodite.modeling.models.gemma import GemmaModel
from aphrodite.modeling.sampling_metadata import SamplingMetadata
from aphrodite.multimodal import MULTIMODAL_REGISTRY
from aphrodite.multimodal.image import cached_get_tokenizer
from aphrodite.quantization.base_config import QuantizationConfig

from .interfaces import SupportsVision
from .siglip import (SiglipVisionModel, dummy_image_for_siglip,
                     dummy_seq_data_for_siglip, get_max_siglip_image_tokens)
from .utils import merge_vision_embeddings

_KEYS_TO_MODIFY_MAPPING = {
    "language_model.model": "language_model",
}


def get_max_paligemma_image_tokens(ctx: InputContext):
    hf_config = ctx.get_hf_config(PaliGemmaConfig)
    vision_config = hf_config.vision_config

    return get_max_siglip_image_tokens(vision_config)


def dummy_data_for_paligemma(ctx: InputContext, seq_len: int):
    hf_config = ctx.get_hf_config(PaliGemmaConfig)
    vision_config = hf_config.vision_config

    seq_data = dummy_seq_data_for_siglip(
        vision_config,
        seq_len,
        image_token_id=hf_config.image_token_index,
    )

    mm_data = dummy_image_for_siglip(vision_config)
    return seq_data, mm_data


def input_processor_for_paligemma(ctx: InputContext, llm_inputs: LLMInputs):

    """
    The correct prompt format needs to be:
    '<image>' * image_feature_size + '<bos>' + prompt + '\n'
    See https://github.com/huggingface/transformers/blob/25245ec26dc29bcf6102e1b4ddd0dfd02e720cf5/src/transformers/models/paligemma/processing_paligemma.py#L55
    """ # noqa

    multi_modal_data = llm_inputs.get("multi_modal_data")
    if multi_modal_data is None or "image" not in multi_modal_data:
        return llm_inputs

    model_config = ctx.model_config
    hf_config = ctx.get_hf_config(PaliGemmaConfig)

    tokenizer = cached_get_tokenizer(model_config.tokenizer)
    image_feature_size = hf_config.text_config.num_image_tokens
    image_token_str = tokenizer.decode(hf_config.image_token_index)
    bos_token = tokenizer.decode(hf_config.bos_token_id)
    image_token_str_pad = image_token_str * image_feature_size
    image_token_ids_pad = [hf_config.image_token_index] * image_feature_size

    orig_prompt = llm_inputs.get("prompt")
    orig_prompt_ids = llm_inputs.get("prompt_token_ids")

    if orig_prompt is not None and image_token_str in orig_prompt:
        logger.warning(
            f"The image token '{image_token_str}' was detected in the prompt "
            "and will be removed. Please follow the proper prompt format"
            " documented on HuggingFace.")
        orig_prompt = orig_prompt.replace(image_token_str, "")
        orig_prompt_ids.remove(hf_config.image_token_index)

    new_prompt = f"{image_token_str_pad}{bos_token}{orig_prompt}\n"
    new_token_ids = image_token_ids_pad + orig_prompt_ids + [108]  #newline

    # NOTE: Create a defensive copy of the original inputs
    return LLMInputs(prompt_token_ids=new_token_ids,
                     prompt=new_prompt,
                     multi_modal_data=multi_modal_data)


class PaliGemmaMultiModalProjector(nn.Module):

    def __init__(self, vision_hidden_size: int, projection_dim: int):
        super().__init__()

        self.linear = nn.Linear(vision_hidden_size, projection_dim, bias=True)

    def forward(self, image_features: torch.Tensor) -> torch.Tensor:
        hidden_states = self.linear(image_features)
        return hidden_states


class PaliGemmaImagePixelInputs(TypedDict):
    type: Literal["pixel_values"]
    data: torch.Tensor
    """Shape: (batch_size, num_channels, height, width)"""


PaliGemmaImageInputs = PaliGemmaImagePixelInputs


@MULTIMODAL_REGISTRY.register_image_input_mapper()
@MULTIMODAL_REGISTRY.register_max_image_tokens(get_max_paligemma_image_tokens)
@INPUT_REGISTRY.register_dummy_data(dummy_data_for_paligemma)
@INPUT_REGISTRY.register_input_processor(input_processor_for_paligemma)
class PaliGemmaForConditionalGeneration(nn.Module, SupportsVision):

    def __init__(self,
                 config: PaliGemmaConfig,
                 multimodal_config: MultiModalConfig,
                 cache_config: Optional[CacheConfig] = None,
                 quant_config: Optional[QuantizationConfig] = None) -> None:
        super().__init__()

        self.config = config
        self.multimodal_config = multimodal_config

        # TODO: Port over SiglipVisionModel & TP
        self.vision_tower = SiglipVisionModel(config.vision_config)
        self.multi_modal_projector = PaliGemmaMultiModalProjector(
            vision_hidden_size=config.vision_config.hidden_size,
            projection_dim=config.vision_config.projection_dim)

        self.quant_config = quant_config
        self.language_model = GemmaModel(config.text_config, cache_config,
                                         quant_config)
        self.unpadded_vocab_size = config.text_config.vocab_size
        logit_scale = getattr(config, "logit_scale", 1.0)
        self.logits_processor = LogitsProcessor(self.unpadded_vocab_size,
                                                config.vocab_size, logit_scale)
        self.sampler = Sampler()

    def _validate_pixel_values(self, data: torch.Tensor) -> torch.Tensor:
        h = w = self.config.vision_config.image_size
        expected_dims = (3, h, w)
        actual_dims = tuple(data.shape[1:])

        if actual_dims != expected_dims:
            expected_expr = ("batch_size", *map(str, expected_dims))
            raise ValueError(
                f"The expected shape of pixel values is {expected_expr}. "
                f"You supplied {tuple(data.shape)}.")

        return data

    def _parse_and_validate_image_input(
            self, **kwargs: object) -> Optional[PaliGemmaImageInputs]:
        pixel_values = kwargs.pop("pixel_values", None)

        if pixel_values is None:
            return None

        if not isinstance(pixel_values, torch.Tensor):
            raise ValueError("Incorrect type of pixel values. "
                             f"Got type: {type(pixel_values)}")

        return PaliGemmaImagePixelInputs(
            type="pixel_values",
            data=self._validate_pixel_values(pixel_values),
        )

    def _image_pixels_to_features(
        self,
        vision_tower: SiglipVisionModel,
        pixel_values: torch.Tensor,
    ) -> torch.Tensor:

        target_dtype = vision_tower.get_input_embeddings().weight.dtype
        image_features = vision_tower(pixel_values.to(dtype=target_dtype))

        return image_features

    def _process_image_pixels(
        self,
        inputs: PaliGemmaImagePixelInputs,
    ) -> torch.Tensor:
        assert self.vision_tower is not None

        pixel_values = inputs["data"]

        return self._image_pixels_to_features(
            self.vision_tower,
            pixel_values,
        )

    def _process_image_input(
        self,
        image_input: PaliGemmaImageInputs,
    ) -> torch.Tensor:

        assert self.vision_tower is not None
        image_features = self._process_image_pixels(image_input, )

        return self.multi_modal_projector(image_features)

    def forward(self,
                input_ids: torch.Tensor,
                positions: torch.Tensor,
                kv_caches: List[torch.Tensor],
                attn_metadata: AttentionMetadata,
                intermediate_tensors: Optional[IntermediateTensors] = None,
                **kwargs: object) -> SamplerOutput:

        parsed_image_input = self._parse_and_validate_image_input(**kwargs)

        if parsed_image_input is not None:
            vision_embeddings = self._process_image_input(parsed_image_input)
            # https://github.com/huggingface/transformers/blob/main/src/transformers/models/paligemma/modeling_paligemma.py#L294 # noqa
            vision_embeddings = vision_embeddings * (self.config.hidden_size**
                                                     -0.5)

            inputs_embeds = self.language_model.get_input_embeddings(input_ids)

            inputs_embeds = merge_vision_embeddings(
                input_ids, inputs_embeds, vision_embeddings,
                self.config.image_token_index)

            input_ids = None
        else:
            inputs_embeds = None

        hidden_states = self.language_model(input_ids,
                                            positions,
                                            kv_caches,
                                            attn_metadata,
                                            None,
                                            inputs_embeds=inputs_embeds)

        return hidden_states

    # Copied from vllm/modeling/models/gemma.py
    def compute_logits(self, hidden_states: torch.Tensor,
                       sampling_metadata: SamplingMetadata) -> torch.Tensor:
        logits = self.logits_processor(self.language_model.embed_tokens,
                                       hidden_states, sampling_metadata)
        return logits

    # Copied from vllm/modeling/models/gemma.py
    def sample(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(logits, sampling_metadata)
        return next_tokens

    # Adapted from vllm/modeling/models/gemma.py
    def load_weights(self, weights: Iterable[Tuple[str, torch.Tensor]]):
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ("qkv_proj", "q_proj", "q"),
            ("qkv_proj", "k_proj", "k"),
            ("qkv_proj", "v_proj", "v"),
            ("gate_up_proj", "gate_proj", 0),
            ("gate_up_proj", "up_proj", 1),
        ]
        params_dict = dict(self.named_parameters())
        loaded_params = set()
        for name, loaded_weight in weights:
            for key_to_modify, new_key in _KEYS_TO_MODIFY_MAPPING.items():
                if key_to_modify in name:
                    name = name.replace(key_to_modify, new_key)
            use_default_weight_loading = False
            if "vision" in name:
                if self.vision_tower is not None:
                    # We only do sharding for language model and
                    # not vision model for now.
                    use_default_weight_loading = True
            else:
                for (param_name, shard_name,
                     shard_id) in stacked_params_mapping:
                    if shard_name not in name:
                        continue
                    name = name.replace(shard_name, param_name)
                    # Skip loading extra bias for GPTQ models.
                    if name.endswith(".bias") and name not in params_dict:
                        continue
                    param = params_dict[name]
                    weight_loader = param.weight_loader
                    weight_loader(param, loaded_weight, shard_id)
                    break
                else:
                    # lm_head is not used in vllm as it is tied with
                    # embed_token. To prevent errors, skip loading
                    # lm_head.weight.
                    if "lm_head.weight" in name:
                        continue
                    # Skip loading extra bias for GPTQ models.
                    if name.endswith(".bias") and name not in params_dict:
                        continue
                    use_default_weight_loading = True

            if use_default_weight_loading:
                param = params_dict[name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                weight_loader(param, loaded_weight)

            loaded_params.add(name)

        unloaded_params = params_dict.keys() - loaded_params
        if unloaded_params:
            logger.warning(
                "Some weights are not initialized from checkpoints: "
                f"{unloaded_params}")
