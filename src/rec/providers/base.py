"""Provider abstraction for summarisation.

Defines the types every transport (GLM/OpenAI-compat, Anthropic-compat, Gemini,
Ollama) implements. Nothing here makes a network call — that lives in the
transport modules. Nothing here is imported by the read-only MCP server.

Design notes:
  - ``cost_usd`` is ``None`` (not ``0.0``) when the model isn't in the price
    table. Printing ``$0.00`` for a model we can't price would be a lie; the cost
    line says "cost unknown" instead. Only Ollama/local gets a true zero.
  - Transports report ``tokens_in``/``tokens_out`` from the provider's ``usage``
    block when present. When a provider reports nothing (some local endpoints),
    the caller falls back to a char/4 estimate and marks the cost line estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Completion:
    """One model completion.

    ``cost_usd`` is ``None`` when the model isn't priced (unknown), distinct
    from ``0.0`` (Ollama / known-free). Callers must not collapse the two.
    """

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None


@runtime_checkable
class Provider(Protocol):
    """A single transport's completion call.

    Implementations are constructed by a factory registered in the package
    registry (see :mod:`rec.providers.__init__`), not instantiated directly by
    callers. ``complete`` is synchronous: one JSON POST, no streaming, no async.
    """

    name: str
    base_url: str

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
    ) -> Completion: ...


class ProviderError(Exception):
    """A provider returned an error (non-retriable, or exhausted retries).

    ``status_code`` is the HTTP status (or ``None`` for a transport-level failure
    like a timeout after all retries). ``message`` is the provider's own message
    where available — rendered as one human line by ``cli.py``, never a
    traceback.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
