"""Google Gemini transport (generativelanguage endpoint).

Uses ``{base_url}/models/{model}:generateContent`` with an API key query param.
Base URL default: ``https://generativelanguage.googleapis.com/v1beta``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..log import get_logger
from . import _http, pricing
from .base import Completion, ProviderError

log = get_logger(__name__)


@dataclass
class GeminiProvider:
    name: str
    base_url: str
    api_key: str

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
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        url = (
            f"{self.base_url.rstrip('/')}/models/{model}:generateContent"
            f"?key={self.api_key}"
        )
        log.info("provider=%s model=%s requesting (max_tokens=%d)", self.name, model, max_tokens)
        data = _http.post_json(url, payload=payload, headers={}, timeout=timeout)

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
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ProviderError("gemini response had no candidates")
    content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
    parts = content.get("parts", []) if isinstance(content, dict) else []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
    if not text:
        raise ProviderError("gemini returned no text")
    return text


def _extract_usage(data: dict, *, fallback_text: str) -> tuple[int, int]:
    meta = data.get("usageMetadata")
    if isinstance(meta, dict):
        tokens_in = int(meta.get("promptTokenCount", 0))
        tokens_out = int(meta.get("candidatesTokenCount", 0))
        if tokens_in or tokens_out:
            return tokens_in, tokens_out
    return 0, pricing.estimate_tokens(fallback_text)
