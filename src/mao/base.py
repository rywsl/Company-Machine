"""规范 JSON、摘要、时钟与 ID。时间与随机数只允许出现在本文件。"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mao.errors import FactoryError

_CHUNK = 1024 * 1024
_PREFIX = re.compile(r"^[a-z]{1,8}$")
_DURATION = re.compile(r"^([0-9]+)(ms|s|m|h|d)$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _reject(path: str) -> None:
    raise FactoryError("JSON_INVALID", "规范 JSON 含不允许的值", path=path)


def _check(obj: Any, path: str) -> None:
    if obj is None or isinstance(obj, str) or isinstance(obj, bool):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _reject(path)
        return
    if isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            _check(item, f"{path}[{index}]")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                _reject(path)
            _check(value, f"{path}.{key}")
        return
    _reject(path)


def _as_json(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return [_as_json(item) for item in obj]
    if isinstance(obj, list):
        return [_as_json(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _as_json(value) for key, value in obj.items()}
    return obj


def canonical_json(obj: Any) -> str:
    _check(obj, "$")
    return json.dumps(
        _as_json(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("utc_iso 要求带时区的 datetime")
    d = dt.astimezone(UTC)
    return (
        f"{d.year:04d}-{d.month:02d}-{d.day:02d}T"
        f"{d.hour:02d}:{d.minute:02d}:{d.second:02d}.{d.microsecond // 1000:03d}Z"
    )


def parse_duration(text: str) -> float:
    if not isinstance(text, str):
        raise FactoryError("DURATION_INVALID", "时长必须是字符串", text=repr(text))
    match = _DURATION.fullmatch(text)
    if match is None:
        raise FactoryError("DURATION_INVALID", "时长字符串不合法", text=repr(text))
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    def __init__(self, start: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("FakeClock 起点必须带时区")
        self._start = start
        self._elapsed = 0.0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        with self._lock:
            return self._elapsed

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("不能把时钟往回拨")
        with self._lock:
            self._elapsed += seconds


def _check_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None:
        raise ValueError("ID 前缀必须匹配 ^[a-z]{1,8}$")


class RandomIds:
    def new(self, prefix: str) -> str:
        _check_prefix(prefix)
        suffix = base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()[:10]
        return f"{prefix}-{suffix}"


class CounterIds:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def new(self, prefix: str) -> str:
        _check_prefix(prefix)
        with self._lock:
            n = self._counts.get(prefix, 0) + 1
            self._counts[prefix] = n
        return f"{prefix}-{n:04d}"
