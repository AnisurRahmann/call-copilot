"""Anthropic-compatible /v1/messages transport.

Covers Anthropic itself and Z.ai's Anthropic-format endpoint
(``https://api.z.ai/api/anthropic``).

Note for the ``glm-anthropic`` preset: that Z.ai endpoint takes a **Coding Plan
key**, which is a *different credential* from the standard ``ZAI_API_KEY``.
Mixing them produces a 401 that looks like a bad key — the factory's error path
calls this out.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..log import get_logger
from . import _http, pricing
from .base import Completion, ProviderError

log = get_logger(__name__)


@dataclass
class AnthropicCompatProvider:
    """A /v1/messages transport.

    ``base_url`` is the API root; requests go to ``{base_url}/v1/messages``.
    Auth is the ``x-api-key`` header plus the required ``anthropic-version``.
    """

    name: str
    base_url: str
    api_key: str
    anthropic_version: str = "2023-06-01"

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
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        url = f"{self.base_url.rstrip('/')}/v1/messages"
        log.info(
            "provider=%s model=%s requesting (max_tokens=%d thinking=%s)",
            self.name, model, max_tokens, thinking,
        )
        data = _http.post_json(
            url,
            payload=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
            },
            timeout=timeout,
        )

        text = _extract_content(data)
        tokens_in, tokens_out = _extract_usage(data, fallback_text=text)
        cost_usd = pricing.cost(model, tokens_in, tokens_out)
        log.info(
            "provider=%s model=%s ok tokens_in=%d tokens_out=%d cost=%s",
            self.name, model, tokens_in, tokens_out,
            "unknown" if cost_usd is None else f"${cost_usd:.6f}",
        )
        return Completion(
            text=text,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )


def _extract_content(data: dict) -> str:
    """Pull text out of an Anthropic /v1/messages response (content blocks)."""
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise ProviderError("provider response had no content blocks")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    text = "".join(parts)
    if not text:
        raise ProviderError("provider returned no text content blocks")
    return text


def _extract_usage(data: dict, *, fallback_text: str) -> tuple[int, int]:
    usage = data.get("usage")
    if isinstance(usage, dict):
        tokens_in = int(usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))
        if tokens_in or tokens_out:
            return tokens_in, tokens_out
    return 0, pricing.estimate_tokens(fallback_text)
