"""Unit tests for rec.web.ranges — pure, no network, no rec imports."""

from __future__ import annotations

from rec.web.ranges import RangeSpec, parse_range

# A representative small file for the bounded-range cases.
SIZE = 1000


def test_none_header_means_serve_full_body():
    assert parse_range(None, SIZE) == RangeSpec(kind="none")


def test_open_ended_from_start():
    # bytes=0- → whole file.
    spec = parse_range("bytes=0-", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (0, SIZE - 1)


def test_two_byte_range_from_start():
    # The classic Safari opener: bytes=0-1.
    spec = parse_range("bytes=0-1", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (0, 1)


def test_bounded_mid_range():
    spec = parse_range("bytes=100-199", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (100, 199)


def test_suffix_range_last_n_bytes():
    spec = parse_range("bytes=-500", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (500, 999)


def test_suffix_larger_than_file_clamps_to_zero():
    # bytes=-2000 on a 1000-byte file → the whole file.
    spec = parse_range("bytes=-2000", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (0, 999)


def test_suffix_zero_is_unsatisfiable():
    assert parse_range("bytes=-0", SIZE).kind == "invalid"


def test_open_ended_from_middle():
    spec = parse_range("bytes=800-", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (800, 999)


def test_end_past_eof_clamps_to_last_byte():
    # bytes=950-2000 → 950-999 (legal; not a 416).
    spec = parse_range("bytes=950-2000", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (950, 999)


def test_start_at_eof_is_unsatisfiable():
    # bytes=1000- on a 1000-byte file (0-indexed EOF) → 416.
    assert parse_range("bytes=1000-", SIZE).kind == "invalid"


def test_start_past_eof_is_unsatisfiable():
    assert parse_range("bytes=1500-", SIZE).kind == "invalid"


def test_malformed_no_bytes_prefix():
    assert parse_range("0-1", SIZE).kind == "invalid"


def test_malformed_non_numeric():
    assert parse_range("bytes=abc-def", SIZE).kind == "invalid"


def test_malformed_reversed_range():
    assert parse_range("bytes=200-100", SIZE).kind == "invalid"


def test_malformed_empty():
    assert parse_range("bytes=", SIZE).kind == "invalid"


def test_malformed_no_dash():
    assert parse_range("bytes=999", SIZE).kind == "invalid"


def test_multi_range_falls_back_to_full_body():
    # We deliberately don't implement multipart/byteranges.
    spec = parse_range("bytes=0-10,20-30", SIZE)
    assert spec.kind == "multi"


def test_multi_range_with_spaces():
    spec = parse_range("bytes=0-10, 20-30", SIZE)
    assert spec.kind == "multi"


def test_zero_byte_file_is_unsatisfiable():
    assert parse_range("bytes=0-", 0).kind == "invalid"
    assert parse_range(None, 0).kind == "none"


def test_negative_size_treated_as_unsatisfiable():
    assert parse_range("bytes=0-", -1).kind == "invalid"


def test_header_is_stripped_before_parsing():
    # Leading/trailing whitespace should not defeat a valid header.
    spec = parse_range("  bytes=0-1  ", SIZE)
    assert spec.kind == "single"
    assert (spec.start, spec.end) == (0, 1)


def test_unit_other_than_bytes_is_invalid():
    # RFC 7233 allows other range units; we only speak bytes.
    assert parse_range("items=0-1", SIZE).kind == "invalid"
