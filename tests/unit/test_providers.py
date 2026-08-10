"""Tests for the provider registry — local detection, key resolution, URL safety.

The load-bearing security invariant: ``is_local_base_url`` must reflect the
ACTUAL resolved host, not a substring, so a remote host like
``localhost.evil.com`` cannot skip the network-consent prompt and silently
exfiltrate the transcript + key.
"""

from __future__ import annotations

import pytest

from rec.providers import NoProviderError, is_local_base_url, is_local_provider, make_provider

# ---- is_local_base_url: the consent-gate boundary -------------------------


@pytest.mark.parametrize("url,expected", [
    # True localhosts.
    ("http://localhost:11434", True),
    ("http://127.0.0.1:11434", True),
    ("http://localhost", True),
    ("https://127.0.0.1", True),
    # NOT local — these must NOT skip consent (the security blocker).
    ("http://localhost.evil.com/v1", False),
    ("http://evil-localhost.com/v1", False),
    ("http://127.0.0.1.nip.io/v1", False),
    ("http://evil.com/localhost", False),
    ("http://10.0.0.5", False),
    ("http://192.168.1.1", False),
    ("", False),
])
def test_is_local_base_url_checks_actual_host(url, expected):
    """A substring check would let localhost.evil.com skip consent. Parse instead."""
    assert is_local_base_url(url) is expected


def test_is_local_provider_ollama_always_local():
    assert is_local_provider("ollama", None) is True
    assert is_local_provider("ollama", "http://remote.host") is True


def test_is_local_provider_remote_base_not_local():
    assert is_local_provider("openai-compat", "http://api.example.com/v1") is False


# ---- scheme validation ----------------------------------------------------


def test_file_scheme_rejected():
    """A file:// base_url must not be accepted — urllib would read local files."""
    with pytest.raises(NoProviderError, match="http"):
        make_provider(name="openai-compat", base_url="file:///etc/passwd",
                      env={"OPENAI_API_KEY": "k"})


def test_http_and_https_accepted(monkeypatch):
    """http and https base URLs are valid (localhost dev servers use http)."""
    # These construct a provider without error (the key resolves from env).
    for scheme in ("http", "https"):
        p = make_provider(name="openai-compat",
                          base_url=f"{scheme}://api.example.com/v1",
                          env={"OPENAI_API_KEY": "k"})
        assert p.name == "openai-compat"


# ---- key resolution -------------------------------------------------------


def test_key_resolved_from_env_var_name():
    p = make_provider(name="openai-compat",
                      api_key_env="MY_KEY",
                      base_url="https://api.example.com/v1",
                      env={"MY_KEY": "secret-value"})
    assert p.api_key == "secret-value"


def test_missing_key_errors_with_var_name():
    with pytest.raises(NoProviderError, match="MY_KEY"):
        make_provider(name="openai-compat",
                      api_key_env="MY_KEY",
                      base_url="https://api.example.com/v1",
                      env={})


def test_ollama_needs_no_key():
    p = make_provider(name="ollama", env={})
    assert p.api_key == ""
