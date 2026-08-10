"""Shared stdlib HTTP for provider transports.

One JSON POST per call, no streaming, no async, no new dependency. Retries are
3 attempts with backoff 1s/4s/10s, ONLY on 429/5xx/timeout — a 401 or 400 is
surfaced immediately with the provider's message (retrying a bad key or a
malformed request is a waste and hides the real cause).

This module never logs request/response bodies — only status codes, attempt
counts, and model names. Transcript and summary text pass through unchanged.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .base import ProviderError

# Backoff schedule for retriable failures (429/5xx/timeout). 3 attempts total.
_RETRY_BACKOFFS = (1.0, 4.0, 10.0)
_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


def post_json(
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float = 300.0,
) -> dict[str, Any]:
    """POST ``payload`` as JSON to ``url`` and return the parsed JSON response.

    Retries 429/5xx/timeout per the backoff schedule; surfaces 401/400/other as
    :class:`ProviderError` immediately. ``headers`` must include auth.
    """
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Accept": "application/json", **headers}

    last_error: ProviderError | None = None
    for attempt, backoff in enumerate(_RETRY_BACKOFFS):
        try:
            req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # urlopen raises HTTPError for non-2xx; a 2xx reaches here.
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code
            msg = _safe_read_error(e)
            if status in _RETRIABLE_STATUS and attempt < len(_RETRY_BACKOFFS) - 1:
                last_error = ProviderError(msg, status_code=status)
                time.sleep(backoff)
                continue
            # Non-retriable (401/400) or out of retries.
            raise ProviderError(_human_http_error(status, msg), status_code=status)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Network/timeout — retriable.
            last_error = ProviderError(f"network error: {e}", status_code=None)
            if attempt < len(_RETRY_BACKOFFS) - 1:
                time.sleep(backoff)
                continue
            raise last_error from e

    # Exhausted retries on a retriable status.
    assert last_error is not None
    raise ProviderError(
        f"provider returned {last_error.status_code} after {len(_RETRY_BACKOFFS)} attempts: "
        f"{last_error.message}",
        status_code=last_error.status_code,
    )


def _safe_read_error(e: urllib.error.HTTPError) -> str:
    """Best-effort extraction of the provider's error message from an HTTPError."""
    try:
        raw = e.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover — defensive
        return str(e)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw[:500] or str(e)
    # Common shapes: {"error": {"message": ...}}, {"error": "..."}, {"message": ...}
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and "message" in err:
            return str(err["message"])
        if isinstance(err, str):
            return err
        if "message" in data:
            return str(data["message"])
    return raw[:500] or str(e)


def _human_http_error(status: int, msg: str) -> str:
    """Render an HTTP error as one human line, with auth-specific guidance."""
    if status in (401, 403):
        return (
            f"provider rejected the API key (HTTP {status}). "
            f"Check the env var named in your config — {msg}"
        )
    return f"provider error (HTTP {status}): {msg}"
