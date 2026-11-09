"""M1-01 单元：mao.base（spec/system/foundation.md）。模块缺失时整文件跳过。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

pytest.importorskip("mao.base")

from mao.base import FakeClock, canonical_json, digest_bytes, parse_duration, utc_iso  # noqa: E402


def test_canonical_json_sorts_keys() -> None:
    assert canonical_json({"b": 1, "a": [1.5, "中"]}) == '{"a":[1.5,"中"],"b":1}'


def test_digest_bytes_empty() -> None:
    assert digest_bytes(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_utc_iso_truncates_milliseconds() -> None:
    dt = datetime(2026, 1, 2, 3, 4, 5, 123999, tzinfo=UTC)
    assert utc_iso(dt) == "2026-01-02T03:04:05.123Z"


def test_utc_iso_converts_offset() -> None:
    shifted = datetime(2026, 1, 2, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    assert utc_iso(shifted) == "2026-01-02T00:00:00.000Z"


def test_parse_duration_and_fake_clock() -> None:
    assert parse_duration("90m") == 5400.0
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert utc_iso(clock.now()) == "2026-01-01T00:00:00.000Z"
    clock.advance(1)
    assert utc_iso(clock.now()) == "2026-01-01T00:00:01.000Z"
