"""Ollama transport — local, free, never triggers the network-consent prompt.

Hits ``http://localhost:11434/api/chat``. Cost is a true ``$0.0`` (via
:func:`pricing.zero_cost`), distinct from an unknown-model ``None``. Because the
base URL is ``localhost``, the CLI's consent gate treats this as local and never
asks before sending transcript text — nothing leaves the machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..log import get_logger
from . import _http, pricing
from .base import Completion, ProviderError

log = get_logger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"


@dataclass
class OllamaProvider:
    name: str
    base_url: str
    api_key: str = ""  # Ollama needs no key; field exists for uniform construction.

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: float = 300.0,
        thinking: bool = True,
    ) -> Completion:
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
        log.info("provider=%s model=%s requesting (max_tokens=%d)", self.name, model, max_tokens)
        data = _http.post_json(url, payload=payload, headers={}, timeout=timeout)

        text = _extract_content(data)
        tokens_in, tokens_out = _extract_usage(data, fallback_text=text)
        # Ollama is known-free — a true $0.0, not an unknown None.
        cost_usd = pricing.zero_cost()
        log.info(
            "provider=%s model=%s ok tokens_in=%d tokens_out=%d cost=$0.00 (local)",
            self.name, model, tokens_in, tokens_out,
        )
        return Completion(
            text=text,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )


def _extract_content(data: dict) -> str:
    msg = data.get("message")
    if not isinstance(msg, dict):
        raise ProviderError("ollama response had no message")
    content = msg.get("content")
    if content is None:
        raise ProviderError("ollama returned an empty message")
    return str(content)


def _extract_usage(data: dict, *, fallback_text: str) -> tuple[int, int]:
    # Ollama reports prompt_eval_count / eval_count when available.
    tokens_in = int(data.get("prompt_eval_count", 0) or 0)
    tokens_out = int(data.get("eval_count", 0) or 0)
    if tokens_in or tokens_out:
        return tokens_in, tokens_out
    return 0, pricing.estimate_tokens(fallback_text)
