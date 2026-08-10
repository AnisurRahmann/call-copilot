"""Provider registry: name → transport factory.

The single place that turns a config block (``summarize.provider``,
``summarize.api_key_env``, ``summarize.base_url``) into a live
:class:`~rec.providers.base.Provider`. Presets (``glm``, ``glm-anthropic``,
``anthropic``, ``gemini``, ``deepseek``, ``ollama``, ``openai-compat``) bake in
the right base URL so the user doesn't type one.

Key resolution (env vars only — config stores the *name*, never the key):
  ``--api-key-env`` flag → ``summarize.api_key_env`` → the preset's default env
  var order. None set → :class:`NoProviderError` naming the exact variable.

This module is imported by ``rec.summarize`` and ``rec.cli``. It is **never**
imported by the read-only MCP server (pinned by a guard test).
"""

from __future__ import annotations

import os
from collections.abc import Callable

from .anthropic_compat import AnthropicCompatProvider
from .base import Provider
from .gemini import GeminiProvider
from .ollama import DEFAULT_BASE_URL as OLLAMA_DEFAULT_BASE_URL
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider


class NoProviderError(Exception):
    """No usable provider — either unconfigured or no API key in the environment.

    The message names the exact env var to export and the config field, so a bare
    install gets a one-line setup instruction rather than a traceback.
    """


# A factory takes (base_url, api_key) and returns a Provider. base_url may be
# overridden by config; api_key is resolved by the caller (from the env var).
ProviderFactory = Callable[[str, str], Provider]


# Preset base URLs. Null/empty base_url in config → the preset default is used.
_PRESETS: dict[str, dict] = {
    "glm": {
        "transport": "openai-compat",
        "default_base_url": "https://api.z.ai/api/paas/v4",
        "key_env_order": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"),
        "key_help": "export ZAI_API_KEY=... (get one at https://z.ai)",
    },
    "glm-anthropic": {
        "transport": "anthropic-compat",
        "default_base_url": "https://api.z.ai/api/anthropic",
        # NOTE: this endpoint takes a Coding Plan key, NOT the standard ZAI_API_KEY.
        # A standard key here produces a 401 that looks like a bad key.
        "key_env_order": ("ZAI_CODING_PLAN_KEY", "ZAI_API_KEY"),
        "key_help": (
            "export ZAI_CODING_PLAN_KEY=... — the Z.ai Anthropic endpoint takes "
            "a Coding Plan key, which is a DIFFERENT credential from the standard "
            "ZAI_API_KEY. Mixing them produces a 401 that looks like a bad key."
        ),
    },
    "anthropic": {
        "transport": "anthropic-compat",
        "default_base_url": "https://api.anthropic.com",
        "key_env_order": ("ANTHROPIC_API_KEY",),
        "key_help": "export ANTHROPIC_API_KEY=...",
    },
    "gemini": {
        "transport": "gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "key_env_order": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "key_help": "export GEMINI_API_KEY=...",
    },
    "deepseek": {
        "transport": "openai-compat",
        "default_base_url": "https://api.deepseek.com/v1",
        "key_env_order": ("DEEPSEEK_API_KEY",),
        "key_help": "export DEEPSEEK_API_KEY=...",
    },
    "ollama": {
        "transport": "ollama",
        "default_base_url": OLLAMA_DEFAULT_BASE_URL,
        "key_env_order": (),  # local — no key needed
        "key_help": "run `ollama serve` locally (no API key needed)",
    },
    # A generic OpenAI-compatible endpoint (LM Studio, vLLM, OpenRouter, ...).
    "openai-compat": {
        "transport": "openai-compat",
        "default_base_url": "http://localhost:1234/v1",
        "key_env_order": ("OPENAI_API_KEY", "OPENAI_COMPAT_API_KEY"),
        "key_help": "set --api-key-env (or summarize.api_key_env) to the env var holding your key",
    },
}


def known_presets() -> list[str]:
    """The provider names a config block may set."""
    return sorted(_PRESETS)


def is_local_base_url(base_url: str) -> bool:
    """True if a base URL points at localhost — those never prompt for consent.

    Parses the URL and checks the *actual* hostname, not a substring (a substring
    check would let ``http://localhost.evil.com`` or ``http://127.0.0.1.nip.io``
    skip the consent prompt and exfiltrate the transcript + key to an attacker).
    """
    if not base_url:
        return False
    from urllib.parse import urlparse
    try:
        host = (urlparse(base_url).hostname or "").lower().strip("[]")
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def is_local_provider(name: str, base_url: str | None) -> bool:
    """True if this provider is local (Ollama or any localhost base URL)."""
    if name == "ollama":
        return True
    preset = _PRESETS.get(name)
    eff_base = base_url or (preset["default_base_url"] if preset else "")
    return is_local_base_url(eff_base)


def _resolve_api_key(
    *, name: str, api_key_env: str | None, env: dict[str, str]
) -> str:
    """Resolve the API key from the environment per the preset's order.

    ``api_key_env`` (from config or --api-key-env) wins; otherwise the preset's
    ``key_env_order`` is tried in order. Raises :class:`NoProviderError` if none
    are set, naming the exact variable. Local providers (no key needed) get "".
    """
    preset = _PRESETS.get(name)
    if name == "ollama":
        return ""

    # Explicit env-var name in config/flag → that one only.
    if api_key_env:
        val = env.get(api_key_env, "").strip()
        if val:
            return val
        raise NoProviderError(
            f"The API key env var {api_key_env!r} (set in your summarize config or "
            f"--api-key-env) is not set in the environment. Export it and try again."
        )

    order = preset["key_env_order"] if preset else ()
    for var in order:
        val = env.get(var, "").strip()
        if val:
            return val
    if order:
        raise NoProviderError(
            f"No API key found. Set one of: {', '.join(order)}. "
            f"({preset['key_help'] if preset else 'see your provider docs'}) "
            f"Or set `summarize.provider` and `summarize.api_key_env` in your config "
            f"(run `rec setup` first if you have no config)."
        )
    # No key order and not local — unknown provider with no key path.
    raise NoProviderError(
        f"Provider {name!r} is not a known preset and has no api_key_env set. "
        f"Known presets: {', '.join(known_presets())}."
    )


def make_provider(
    *,
    name: str,
    api_key_env: str | None = None,
    base_url: str | None = None,
    env: dict[str, str] | None = None,
) -> Provider:
    """Construct a Provider from a config block.

    ``env`` defaults to the real ``os.environ``; tests inject a fake. Raises
    :class:`NoProviderError` if no key is resolvable.
    """
    env = env if env is not None else dict(os.environ)
    preset = _PRESETS.get(name)
    transport = preset["transport"] if preset else "openai-compat"
    eff_base_url = (base_url or (preset["default_base_url"] if preset else "")).strip()

    # Reject non-http(s) base URLs. A file:// URL could be read by urllib, and
    # the transcript/key should never reach anything but an http(s) endpoint.
    if eff_base_url:
        from urllib.parse import urlparse
        scheme = (urlparse(eff_base_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise NoProviderError(
                f"Provider {name!r} base_url must be http(s), got {eff_base_url!r} "
                f"(scheme {scheme!r})."
            )

    api_key = _resolve_api_key(name=name, api_key_env=api_key_env, env=env)

    if transport == "openai-compat":
        if not eff_base_url:
            raise NoProviderError(
                f"Provider {name!r} needs a base_url (set summarize.base_url in config)."
            )
        return OpenAICompatProvider(name=name, base_url=eff_base_url, api_key=api_key)
    if transport == "anthropic-compat":
        return AnthropicCompatProvider(name=name, base_url=eff_base_url, api_key=api_key)
    if transport == "gemini":
        return GeminiProvider(name=name, base_url=eff_base_url, api_key=api_key)
    if transport == "ollama":
        return OllamaProvider(name=name, base_url=eff_base_url or OLLAMA_DEFAULT_BASE_URL)
    raise NoProviderError(f"Unknown transport {transport!r} for provider {name!r}.")


def consent_host(*, name: str, base_url: str | None) -> str:
    """The host shown in the one-time network-consent prompt.

    A short, user-recognisable host derived from the base URL. Local providers
    never reach the prompt, so this is only called for network providers.
    """
    preset = _PRESETS.get(name)
    eff = (base_url or (preset["default_base_url"] if preset else "")).strip()
    # Strip scheme + path for a readable host.
    no_scheme = eff.split("://", 1)[-1] if "://" in eff else eff
    return no_scheme.split("/", 1)[0] or eff or name
