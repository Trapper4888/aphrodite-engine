from typing import List, Optional, Union

from loguru import logger

from aphrodite.common.config import ModelConfig
from aphrodite.common.utils import random_uuid
# yapf conflicts with isort
# yapf: disable
from aphrodite.endpoints.chat_utils import (load_chat_template,
                                            parse_chat_messages)
from aphrodite.endpoints.logger import RequestLogger
from aphrodite.endpoints.openai.protocol import (DetokenizeRequest,
                                                 DetokenizeResponse,
                                                 ErrorResponse,
                                                 TokenizeChatRequest,
                                                 TokenizeRequest,
                                                 TokenizeResponse)
# yapf: enable
from aphrodite.endpoints.openai.serving_engine import (LoRAModulePath,
                                                       OpenAIServing)
from aphrodite.engine.protocol import AsyncEngineClient


class OpenAIServingTokenization(OpenAIServing):

    def __init__(
        self,
        async_engine_client: AsyncEngineClient,
        model_config: ModelConfig,
        served_model_names: List[str],
        *,
        lora_modules: Optional[List[LoRAModulePath]],
        request_logger: Optional[RequestLogger],
        chat_template: Optional[str],
    ):
        super().__init__(async_engine_client=async_engine_client,
                         model_config=model_config,
                         served_model_names=served_model_names,
                         lora_modules=lora_modules,
                         prompt_adapters=None,
                         request_logger=request_logger)

        # If this is None we use the tokenizer's default chat template
        self.chat_template = load_chat_template(chat_template)

    async def create_tokenize(
        self,
        request: TokenizeRequest,
    ) -> Union[TokenizeResponse, ErrorResponse]:
        error_check_ret = await self._check_model(request)
        if error_check_ret is not None:
            return error_check_ret

        request_id = f"tokn-{random_uuid()}"

        (
            lora_request,
            prompt_adapter_request,
        ) = self._maybe_get_adapters(request)

        tokenizer = await self.async_engine_client.get_tokenizer(lora_request)

        if isinstance(request, TokenizeChatRequest):
            model_config = self.model_config

            conversation, mm_futures = parse_chat_messages(
                request.messages, model_config, tokenizer)

            if mm_futures:
                logger.warning(
                    "Multi-modal inputs are ignored during tokenization")

            prompt = tokenizer.apply_chat_template(
                add_generation_prompt=request.add_generation_prompt,
                conversation=conversation,
                tokenize=False,
                chat_template=self.chat_template)
            assert isinstance(prompt, str)
        else:
            prompt = request.prompt

        self._log_inputs(request_id,
                         prompt,
                         params=None,
                         lora_request=lora_request,
                         prompt_adapter_request=prompt_adapter_request)

        # Silently ignore prompt adapter since it does not affect tokenization

        prompt_input = self._tokenize_prompt_input(
            request,
            tokenizer,
            prompt,
            add_special_tokens=request.add_special_tokens,
        )
        input_ids = prompt_input["prompt_token_ids"]

        return TokenizeResponse(tokens=input_ids,
                                count=len(input_ids),
                                max_model_len=self.max_model_len)

    async def create_detokenize(
        self,
        request: DetokenizeRequest,
    ) -> Union[DetokenizeResponse, ErrorResponse]:
        error_check_ret = await self._check_model(request)
        if error_check_ret is not None:
            return error_check_ret

        request_id = f"tokn-{random_uuid()}"

        (
            lora_request,
            prompt_adapter_request,
        ) = self._maybe_get_adapters(request)

        tokenizer = await self.async_engine_client.get_tokenizer(lora_request)

        self._log_inputs(request_id,
                         request.tokens,
                         params=None,
                         lora_request=lora_request,
                         prompt_adapter_request=prompt_adapter_request)

        if prompt_adapter_request is not None:
            raise NotImplementedError("Prompt adapter is not supported "
                                      "for tokenization")

        prompt_input = self._tokenize_prompt_input(
            request,
            tokenizer,
            request.tokens,
        )
        input_text = prompt_input["prompt"]

        return DetokenizeResponse(prompt=input_text)
