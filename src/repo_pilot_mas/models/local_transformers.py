"""Lazy local Hugging Face Transformers adapter for Qwen3."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.models.base import GenerationConfig, Message, ModelAdapter, RawGeneration


class LocalTransformersAdapter(ModelAdapter):
    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "cuda:0",
        dtype: str = "bfloat16",
        raw_log_dir: str | Path | None = None,
    ) -> None:
        resolved = Path(model_path).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("model_path must be a directory")
        super().__init__(resolved.name, raw_log_dir=raw_log_dir)
        self.model_path = resolved
        self.device = device
        self.dtype = dtype
        self._tokenizer: Any = None
        self._model: Any = None

    def prepare(self) -> None:
        """Load this model before concurrent dispatch to avoid lazy-import races."""

        self._load()

    def close(self) -> None:
        """Deterministically release model tensors instead of waiting for object GC."""

        model = self._model
        self._model = None
        self._tokenizer = None
        del model
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            return

    def _generate_once(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
    ) -> RawGeneration:
        self._load()
        import torch

        chat = [message.to_dict() for message in messages]
        try:
            input_ids = self._tokenizer.apply_chat_template(
                chat,
                tokenize=True,
                add_generation_prompt=True,
                enable_thinking=False,
                return_tensors="pt",
            )
        except TypeError:
            input_ids = self._tokenizer.apply_chat_template(
                chat,
                tokenize=True,
                add_generation_prompt=True,
                return_tensors="pt",
            )
        input_ids = input_ids.to(self.device)
        attention_mask = torch.ones_like(input_ids)
        generation_kwargs: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "max_new_tokens": config.max_output_tokens,
            "max_time": config.timeout_seconds,
            "pad_token_id": self._tokenizer.eos_token_id,
        }
        if config.temperature > 0:
            generation_kwargs.update(do_sample=True, temperature=config.temperature)
        else:
            generation_kwargs.update(
                do_sample=False,
                temperature=None,
                top_k=None,
                top_p=None,
            )
        with torch.inference_mode():
            output = self._model.generate(**generation_kwargs)
        generated = output[0, input_ids.shape[-1] :]
        text = self._tokenizer.decode(generated, skip_special_tokens=True)
        return RawGeneration(
            text=text,
            input_tokens=int(input_ids.shape[-1]),
            output_tokens=int(generated.shape[-1]),
        )

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        dtype = getattr(torch, self.dtype, None)
        if dtype is None:
            raise ValueError(f"unsupported torch dtype: {self.dtype}")
        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA is required by the configured local model device")
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_path,
            local_files_only=True,
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            torch_dtype=dtype,
            local_files_only=True,
            low_cpu_mem_usage=True,
        ).to(self.device)
        self._model.eval()
