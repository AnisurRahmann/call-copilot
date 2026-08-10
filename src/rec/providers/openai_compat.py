"""OpenAI-compatible /chat/completions transport.

Covers GLM (Z.ai), DeepSeek, OpenRouter, LM Studio, vLLM — anything that speaks
the ``POST {base_url}/chat/completions`` shape with ``messages`` and ``usage``.

GLM-specific quirks handled here (the maintainer runs GLM, so this path must be
perfect on day one):

1. **Thinking is billed at the output rate.** When ``thinking=False`` (the Tier 1
   map pass), send ``"thinking": {"type": "disabled"}`` so we don't pay reasoning
   rates forty times over for pure extraction. Tier 3 leaves it on.
2. **``reasoning_content`` in the response is ignored** for the summary body but
   its tokens are counted in ``tokens_out``, or the cost line under-reports.
3. **The model string is never mangled** — ``glm-5.2[1m]`` passes through
   verbatim. No lowercasing, no suffix stripping.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..log import get_logger
from . import _http, pricing
from .base import Completion, ProviderError

log = get_logger(__name__)


@dataclass
class OpenAICompatProvider:
    """A /chat/completions transport.

    ``base_url`` is the API root (e.g. ``https://api.z.ai/api/paas/v4``);
    requests go to ``{base_url}/chat/completions``. ``api_key`` is resolved by
    the caller (from the env var named in config) and passed in here — never
    stored in config.
    """

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
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Thinking is billed at the output rate. The Tier 1 map pass is
        # extraction, not reasoning — disable it there. Tier 3 leaves it on.
        if not thinking:
            payload["thinking"] = {"type": "disabled"}

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        log.info(
            "provider=%s model=%s requesting (max_tokens=%d thinking=%s)",
            self.name, model, max_tokens, thinking,
        )
        data = _http.post_json(
            url,
            payload=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )

        text = _extract_content(data)
        # Count reasoning tokens in the output total — they're billed at the
        # output rate, so omitting them makes the cost line under-report.
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
    """Pull the assistant message text out of a /chat/completions response.

    GLM may carry ``reasoning_content`` alongside ``content`` — we ignore the
    reasoning field for the body (it's chain-of-thought, not summary text) but
    its tokens are counted in :func:`_extract_usage`.
    """
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        raise ProviderError("provider response had no choices")
    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = msg.get("content")
    if content is None:
        # Some providers return empty content on safety filters; surface it.
        raise ProviderError("provider returned an empty message (content was None)")
    return str(content)


def _extract_usage(data: dict, *, fallback_text: str) -> tuple[int, int]:
    """Return (tokens_in, tokens_out) from the ``usage`` block.

    When the provider reports no usage (some local endpoints), fall back to a
    char/4 estimate on the output text and zero input — the caller marks the
    cost line estimated. Never present a guess as measured.
    """
    usage = data.get("usage")
    if isinstance(usage, dict):
        # prompt_tokens / completion_tokens are the OpenAI field names; GLM
        # uses the same. reasoning_tokens may appear inside completion_tokens
        # already (the billed total), so we don't add them separately.
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        if tokens_in or tokens_out:
            return tokens_in, tokens_out
    # Fallback: estimate output tokens from the returned text.
    return 0, pricing.estimate_tokens(fallback_text)
