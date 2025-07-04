# SPDX-FileCopyrightText: 2022-present deepset GmbH <info@deepset.ai>
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
import copy
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from haystack import logging
from haystack.dataclasses import (
    AsyncStreamingCallbackT,
    ChatMessage,
    ComponentInfo,
    StreamingChunk,
    SyncStreamingCallbackT,
)
from haystack.lazy_imports import LazyImport
from haystack.utils.auth import Secret
from haystack.utils.device import ComponentDevice

with LazyImport(message="Run 'pip install \"transformers[torch]\"'") as torch_import:
    import torch

with LazyImport(message="Run 'pip install \"huggingface_hub>=0.27.0\"'") as huggingface_hub_import:
    from huggingface_hub import HfApi, model_info
    from huggingface_hub.utils import RepositoryNotFoundError

logger = logging.getLogger(__name__)


class HFGenerationAPIType(Enum):
    """
    API type to use for Hugging Face API Generators.
    """

    # HF [Text Generation Inference (TGI)](https://github.com/huggingface/text-generation-inference).
    TEXT_GENERATION_INFERENCE = "text_generation_inference"

    # HF [Inference Endpoints](https://huggingface.co/inference-endpoints).
    INFERENCE_ENDPOINTS = "inference_endpoints"

    # HF [Serverless Inference API](https://huggingface.co/inference-api).
    SERVERLESS_INFERENCE_API = "serverless_inference_api"

    def __str__(self):
        return self.value

    @staticmethod
    def from_str(string: str) -> "HFGenerationAPIType":
        """
        Convert a string to a HFGenerationAPIType enum.

        :param string: The string to convert.
        :return: The corresponding HFGenerationAPIType enum.

        """
        enum_map = {e.value: e for e in HFGenerationAPIType}
        mode = enum_map.get(string)
        if mode is None:
            msg = f"Unknown Hugging Face API type '{string}'. Supported types are: {list(enum_map.keys())}"
            raise ValueError(msg)
        return mode


class HFEmbeddingAPIType(Enum):
    """
    API type to use for Hugging Face API Embedders.
    """

    # HF [Text Embeddings Inference (TEI)](https://github.com/huggingface/text-embeddings-inference).
    TEXT_EMBEDDINGS_INFERENCE = "text_embeddings_inference"

    # HF [Inference Endpoints](https://huggingface.co/inference-endpoints).
    INFERENCE_ENDPOINTS = "inference_endpoints"

    # HF [Serverless Inference API](https://huggingface.co/inference-api).
    SERVERLESS_INFERENCE_API = "serverless_inference_api"

    def __str__(self):
        return self.value

    @staticmethod
    def from_str(string: str) -> "HFEmbeddingAPIType":
        """
        Convert a string to a HFEmbeddingAPIType enum.

        :param string:
        :return: The corresponding HFEmbeddingAPIType enum.
        """
        enum_map = {e.value: e for e in HFEmbeddingAPIType}
        mode = enum_map.get(string)
        if mode is None:
            msg = f"Unknown Hugging Face API type '{string}'. Supported types are: {list(enum_map.keys())}"
            raise ValueError(msg)
        return mode


class HFModelType(Enum):
    EMBEDDING = 1
    GENERATION = 2


def serialize_hf_model_kwargs(kwargs: Dict[str, Any]) -> None:
    """
    Recursively serialize HuggingFace specific model keyword arguments in-place to make them JSON serializable.

    :param kwargs: The keyword arguments to serialize
    """
    torch_import.check()

    for k, v in kwargs.items():
        # torch.dtype
        if isinstance(v, torch.dtype):
            kwargs[k] = str(v)

        if isinstance(v, dict):
            serialize_hf_model_kwargs(v)


def deserialize_hf_model_kwargs(kwargs: Dict[str, Any]) -> None:
    """
    Recursively deserialize HuggingFace specific model keyword arguments in-place to make them JSON serializable.

    :param kwargs: The keyword arguments to deserialize
    """
    torch_import.check()

    for k, v in kwargs.items():
        # torch.dtype
        if isinstance(v, str) and v.startswith("torch."):
            dtype_str = v.split(".")[1]
            dtype = getattr(torch, dtype_str, None)
            if dtype is not None and isinstance(dtype, torch.dtype):
                kwargs[k] = dtype

        if isinstance(v, dict):
            deserialize_hf_model_kwargs(v)


def resolve_hf_device_map(device: Optional[ComponentDevice], model_kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Update `model_kwargs` to include the keyword argument `device_map`.

    This method is useful you want to force loading a transformers model when using `AutoModel.from_pretrained` to
    use `device_map`.

    We handle the edge case where `device` and `device_map` is specified by ignoring the `device` parameter and printing
    a warning.

    :param device: The device on which the model is loaded. If `None`, the default device is automatically
        selected.
    :param model_kwargs: Additional HF keyword arguments passed to `AutoModel.from_pretrained`.
        For details on what kwargs you can pass, see the model's documentation.
    """
    model_kwargs = copy.copy(model_kwargs) or {}
    if model_kwargs.get("device_map"):
        if device is not None:
            logger.warning(
                "The parameters `device` and `device_map` from `model_kwargs` are both provided. "
                "Ignoring `device` and using `device_map`."
            )
        # Resolve device if device_map is provided in model_kwargs
        device_map = model_kwargs["device_map"]
    else:
        device_map = ComponentDevice.resolve_device(device).to_hf()

    # Set up device_map which allows quantized loading and multi device inference
    # requires accelerate which is always installed when using `pip install transformers[torch]`
    model_kwargs["device_map"] = device_map

    return model_kwargs


def resolve_hf_pipeline_kwargs(  # pylint: disable=too-many-positional-arguments
    huggingface_pipeline_kwargs: Dict[str, Any],
    model: str,
    task: Optional[str],
    supported_tasks: List[str],
    device: Optional[ComponentDevice],
    token: Optional[Secret],
) -> Dict[str, Any]:
    """
    Resolve the HuggingFace pipeline keyword arguments based on explicit user inputs.

    :param huggingface_pipeline_kwargs: Dictionary containing keyword arguments used to initialize a
        Hugging Face pipeline.
    :param model: The name or path of a Hugging Face model for on the HuggingFace Hub.
    :param task: The task for the Hugging Face pipeline.
    :param supported_tasks: The list of supported tasks to check the task of the model against. If the task of the model
        is not present within this list then a ValueError is thrown.
    :param device: The device on which the model is loaded. If `None`, the default device is automatically
        selected. If a device/device map is specified in `huggingface_pipeline_kwargs`, it overrides this parameter.
    :param token: The token to use as HTTP bearer authorization for remote files.
        If the token is also specified in the `huggingface_pipeline_kwargs`, this parameter will be ignored.
    """
    huggingface_hub_import.check()

    token = token.resolve_value() if token else None
    # check if the huggingface_pipeline_kwargs contain the essential parameters
    # otherwise, populate them with values from other init parameters
    huggingface_pipeline_kwargs.setdefault("model", model)
    huggingface_pipeline_kwargs.setdefault("token", token)

    device = ComponentDevice.resolve_device(device)
    device.update_hf_kwargs(huggingface_pipeline_kwargs, overwrite=False)

    # task identification and validation
    task = task or huggingface_pipeline_kwargs.get("task")
    if task is None and isinstance(huggingface_pipeline_kwargs["model"], str):
        task = model_info(huggingface_pipeline_kwargs["model"], token=huggingface_pipeline_kwargs["token"]).pipeline_tag

    if task not in supported_tasks:
        raise ValueError(f"Task '{task}' is not supported. The supported tasks are: {', '.join(supported_tasks)}.")
    huggingface_pipeline_kwargs["task"] = task
    return huggingface_pipeline_kwargs


def check_valid_model(model_id: str, model_type: HFModelType, token: Optional[Secret]) -> None:
    """
    Check if the provided model ID corresponds to a valid model on HuggingFace Hub.

    Also check if the model is an embedding or generation model.

    :param model_id: A string representing the HuggingFace model ID.
    :param model_type: the model type, HFModelType.EMBEDDING or HFModelType.GENERATION
    :param token: The optional authentication token.
    :raises ValueError: If the model is not found or is not a embedding model.
    """
    huggingface_hub_import.check()

    api = HfApi()
    try:
        model_info = api.model_info(model_id, token=token.resolve_value() if token else None)
    except RepositoryNotFoundError as e:
        raise ValueError(
            f"Model {model_id} not found on HuggingFace Hub. Please provide a valid HuggingFace model_id."
        ) from e

    if model_type == HFModelType.EMBEDDING:
        allowed_model = model_info.pipeline_tag in ["sentence-similarity", "feature-extraction"]
        error_msg = f"Model {model_id} is not a embedding model. Please provide a embedding model."
    elif model_type == HFModelType.GENERATION:
        allowed_model = model_info.pipeline_tag in ["text-generation", "text2text-generation", "image-text-to-text"]
        error_msg = f"Model {model_id} is not a text generation model. Please provide a text generation model."
    else:
        allowed_model = False
        error_msg = f"Unknown model type for {model_id}"

    if not allowed_model:
        raise ValueError(error_msg)


def convert_message_to_hf_format(message: ChatMessage) -> Dict[str, Any]:
    """
    Convert a message to the format expected by Hugging Face.
    """
    text_contents = message.texts
    tool_calls = message.tool_calls
    tool_call_results = message.tool_call_results

    if not text_contents and not tool_calls and not tool_call_results:
        raise ValueError("A `ChatMessage` must contain at least one `TextContent`, `ToolCall`, or `ToolCallResult`.")
    if len(text_contents) + len(tool_call_results) > 1:
        raise ValueError("A `ChatMessage` can only contain one `TextContent` or one `ToolCallResult`.")

    # HF always expects a content field, even if it is empty
    hf_msg: Dict[str, Any] = {"role": message._role.value, "content": ""}

    if tool_call_results:
        result = tool_call_results[0]
        hf_msg["content"] = result.result
        if tc_id := result.origin.id:
            hf_msg["tool_call_id"] = tc_id
        # HF does not provide a way to communicate errors in tool invocations, so we ignore the error field
        return hf_msg

    if text_contents:
        hf_msg["content"] = text_contents[0]
    if tool_calls:
        hf_tool_calls = []
        for tc in tool_calls:
            hf_tool_call = {"type": "function", "function": {"name": tc.tool_name, "arguments": tc.arguments}}
            if tc.id is not None:
                hf_tool_call["id"] = tc.id
            hf_tool_calls.append(hf_tool_call)
        hf_msg["tool_calls"] = hf_tool_calls

    return hf_msg


with LazyImport(message="Run 'pip install \"transformers[torch]\"'") as transformers_import:
    from transformers import StoppingCriteria, TextStreamer
    from transformers.tokenization_utils import PreTrainedTokenizer
    from transformers.tokenization_utils_fast import PreTrainedTokenizerFast

    torch_import.check()
    transformers_import.check()

    class StopWordsCriteria(StoppingCriteria):
        """
        Stops text generation in HuggingFace generators if any one of the stop words is generated.

        Note: When a stop word is encountered, the generation of new text is stopped.
        However, if the stop word is in the prompt itself, it can stop generating new text
        prematurely after the first token. This is particularly important for LLMs designed
        for dialogue generation. For these models, like for example mosaicml/mpt-7b-chat,
        the output includes both the new text and the original prompt. Therefore, it's important
        to make sure your prompt has no stop words.
        """

        def __init__(
            self,
            tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
            stop_words: List[str],
            device: Union[str, torch.device] = "cpu",
        ):
            super().__init__()
            # check if tokenizer is a valid tokenizer
            if not isinstance(tokenizer, (PreTrainedTokenizer, PreTrainedTokenizerFast)):
                raise ValueError(
                    f"Invalid tokenizer provided for StopWordsCriteria - {tokenizer}. "
                    f"Please provide a valid tokenizer from the HuggingFace Transformers library."
                )
            if not tokenizer.pad_token:
                if tokenizer.eos_token:
                    tokenizer.pad_token = tokenizer.eos_token
                else:
                    tokenizer.add_special_tokens({"pad_token": "[PAD]"})
            encoded_stop_words = tokenizer(stop_words, add_special_tokens=False, padding=True, return_tensors="pt")
            self.stop_ids = encoded_stop_words.input_ids.to(device)

        def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs: Any) -> bool:
            """Check if any of the stop words are generated in the current text generation step."""
            for stop_id in self.stop_ids:
                found_stop_word = self.is_stop_word_found(input_ids, stop_id)
                if found_stop_word:
                    return True
            return False

        @staticmethod
        def is_stop_word_found(generated_text_ids: torch.Tensor, stop_id: torch.Tensor) -> bool:
            """
            Performs phrase matching.

            Checks if a sequence of stop tokens appears in a continuous or sequential order within the generated text.
            """
            generated_text_ids = generated_text_ids[-1]
            len_generated_text_ids = generated_text_ids.size(0)
            len_stop_id = stop_id.size(0)
            result = all(generated_text_ids[len_generated_text_ids - len_stop_id :].eq(stop_id))
            return result

    class HFTokenStreamingHandler(TextStreamer):
        """
        Streaming handler for HuggingFaceLocalGenerator and HuggingFaceLocalChatGenerator.

        Note: This is a helper class for HuggingFaceLocalGenerator & HuggingFaceLocalChatGenerator enabling streaming
        of generated text via Haystack SyncStreamingCallbackT callbacks.

        Do not use this class directly.
        """

        def __init__(
            self,
            tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
            stream_handler: SyncStreamingCallbackT,
            stop_words: Optional[List[str]] = None,
            component_info: Optional[ComponentInfo] = None,
        ):
            super().__init__(tokenizer=tokenizer, skip_prompt=True)  # type: ignore
            self.token_handler = stream_handler
            self.stop_words = stop_words or []
            self.component_info = component_info
            self._call_counter = 0

        def on_finalized_text(self, word: str, stream_end: bool = False) -> None:
            """Callback function for handling the generated text."""
            self._call_counter += 1
            word_to_send = word + "\n" if stream_end else word
            if word_to_send.strip() not in self.stop_words:
                self.token_handler(
                    StreamingChunk(
                        content=word_to_send, index=0, start=self._call_counter == 1, component_info=self.component_info
                    )
                )

    class AsyncHFTokenStreamingHandler(TextStreamer):
        """
        Async streaming handler for HuggingFaceLocalGenerator and HuggingFaceLocalChatGenerator.

        Note: This is a helper class for HuggingFaceLocalGenerator & HuggingFaceLocalChatGenerator enabling
        async streaming of generated text via Haystack Callable[StreamingChunk, Awaitable[None]] callbacks.

        Do not use this class directly.
        """

        def __init__(
            self,
            tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
            stream_handler: AsyncStreamingCallbackT,
            stop_words: Optional[List[str]] = None,
            component_info: Optional[ComponentInfo] = None,
        ):
            super().__init__(tokenizer=tokenizer, skip_prompt=True)  # type: ignore
            self.token_handler = stream_handler
            self.stop_words = stop_words or []
            self.component_info = component_info
            self._queue: asyncio.Queue[StreamingChunk] = asyncio.Queue()

        def on_finalized_text(self, word: str, stream_end: bool = False) -> None:
            """Synchronous callback that puts chunks in a queue."""
            word_to_send = word + "\n" if stream_end else word
            if word_to_send.strip() not in self.stop_words:
                self._queue.put_nowait(StreamingChunk(content=word_to_send, component_info=self.component_info))

        async def process_queue(self) -> None:
            """Process the queue of streaming chunks."""
            while True:
                try:
                    chunk = await self._queue.get()
                    await self.token_handler(chunk)
                    self._queue.task_done()
                except asyncio.CancelledError:
                    break
