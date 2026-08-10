"""HTTP ``Range`` header parsing for the web UI's audio endpoint.

The stdlib ``http.server`` does not implement byte-range requests, but Safari's
``<audio>`` element sends ``Range: bytes=0-1`` first and refuses to play or seek
without a correct ``206 Partial Content`` response. This module turns a header
value into a decision the handler can act on:

- :class:`RangeSpec` with ``kind="none"``    → no Range header; serve ``200``.
- ``kind="single"`` (``start``/``end`` set)  → serve ``206`` for those bytes.
- ``kind="multi"``                           → fall back to ``200`` full body
  (multipart/byteranges is deliberately not implemented; it's rarely needed by
  ``<audio>`` and easy to get wrong).
- ``kind="invalid"``                         → respond ``416``.

The grammar handled: ``bytes=A-B``, ``bytes=A-`` (open-ended), ``bytes=-N``
(last N bytes, a suffix range). Anything that fails to parse or is unsatisfiable
(end out of bounds, suffix on a zero-length file) is ``invalid``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RangeSpec:
    """The outcome of parsing one ``Range`` header.

    ``start``/``end`` are inclusive byte offsets and only meaningful when
    ``kind == "single"``. ``end`` is always ``<= file_size - 1`` and
    ``start <= end`` for a single spec.
    """

    kind: str  # "none" | "single" | "multi" | "invalid"
    start: int = 0
    end: int = 0


def parse_range(header: str | None, file_size: int) -> RangeSpec:
    """Parse a ``Range`` header value against a known ``file_size``.

    ``file_size`` is the total size of the representation in bytes. A negative
    size is treated as unsatisfiable (``invalid``).
    """
    if header is None:
        return RangeSpec(kind="none")
    if file_size <= 0:
        # No bytes exist to satisfy any range.
        return RangeSpec(kind="invalid")

    header = header.strip()
    if not header.startswith("bytes="):
        return RangeSpec(kind="invalid")
    body = header[len("bytes="):].strip()
    if not body:
        return RangeSpec(kind="invalid")

    # A comma means multipart/byteranges — we don't implement it; the caller
    # serves the full body with 200.
    if "," in body:
        return RangeSpec(kind="multi")

    spec = body.strip()
    if "-" not in spec:
        return RangeSpec(kind="invalid")
    start_str, _, end_str = spec.partition("-")
    start_str, end_str = start_str.strip(), end_str.strip()

    # Suffix range: bytes=-N → last N bytes.
    if start_str == "":
        if not end_str.isdigit():
            return RangeSpec(kind="invalid")
        length = int(end_str)
        if length == 0:
            # bytes=-0 is unsatisfiable (no bytes).
            return RangeSpec(kind="invalid")
        start = max(0, file_size - length)
        return RangeSpec(kind="single", start=start, end=file_size - 1)

    # Open-ended or fully-bounded range: bytes=A- or bytes=A-B.
    if not start_str.isdigit():
        return RangeSpec(kind="invalid")
    start = int(start_str)
    if start >= file_size:
        # Start at or past EOF → unsatisfiable.
        return RangeSpec(kind="invalid")
    if end_str == "":
        return RangeSpec(kind="single", start=start, end=file_size - 1)
    if not end_str.isdigit():
        return RangeSpec(kind="invalid")
    end = int(end_str)
    if end < start:
        return RangeSpec(kind="invalid")
    # Clamp end to the last byte; clients may ask past EOF, which is legal and
    # resolves to the available tail.
    end = min(end, file_size - 1)
    return RangeSpec(kind="single", start=start, end=end)
