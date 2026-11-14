"""JSON Schema 校验（V1 的结构部分）。schemas 以 spec/schemas/ 为唯一来源。"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .paths import schemas_dir

BASE = "https://mao.local/schemas/"

KIND_SCHEMA = {
    "Type": "type.schema.json",
    "Machine": "machine.schema.json",
    "OperatorRuntime": "operator-runtime.schema.json",
    "Station": "station.schema.json",
    "Line": "line.schema.json",
    "Policy": "policy.schema.json",
    "Plan": "records.schema.json#/$defs/plan",
    "ChangeSet": "records.schema.json#/$defs/changeset",
    "ConversionRequest": "records.schema.json#/$defs/conversionRequest",
    "GoldenSet": "records.schema.json#/$defs/goldenSet",
    "Baseline": "records.schema.json#/$defs/baseline",
}

REGISTRY_KINDS = ("Type", "Machine", "OperatorRuntime", "Station", "Line", "Policy", "GoldenSet", "Baseline")
MAX_ERRORS_PER_DOC = 20


@lru_cache(maxsize=4)
def _validators(directory: str) -> dict[str, Draft202012Validator]:
    resources = []
    for p in sorted(Path(directory).glob("*.schema.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        resources.append((doc["$id"], Resource.from_contents(doc)))
    registry = Registry().with_resources(resources)
    out = {}
    for kind, ref in KIND_SCHEMA.items():
        out[kind] = Draft202012Validator({"$ref": BASE + ref}, registry=registry)
    return out


def json_path(parts: Any) -> str:
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out


# 同一份文档在一致性用例里会被反复校验。键含 schema 目录与文件字节，避免换 MRL_SPEC_DIR 后命中旧结果。
_VALIDATED: dict[tuple[str, str, str, str], tuple[tuple[str, str], ...]] = {}
_VALIDATED_MAX = 4096
_SCHEMA_BYTES: dict[str, tuple[tuple[tuple[str, int, int], ...], str]] = {}


def _schema_bytes(directory: str) -> str:
    root = Path(directory)
    files = sorted(root.glob("*.schema.json")) if root.is_dir() else []
    stamp = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in files)
    cached = _SCHEMA_BYTES.get(directory)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    hasher = hashlib.sha256()
    hasher.update(directory.encode("utf-8"))
    for path in files:
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
    digest = hasher.hexdigest()
    _SCHEMA_BYTES[directory] = (stamp, digest)
    return digest


def _fingerprint(data: dict[str, Any]) -> str | None:
    try:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


def validate(data: Any) -> list[tuple[str, str]]:
    """返回 [(JSON 路径, 消息)]；空列表表示通过。"""
    if not isinstance(data, dict):
        return [("", "文档必须是一个映射")]
    kind = data.get("kind")
    if kind not in KIND_SCHEMA:
        return [("kind", f"未知的 kind {kind!r}，可用：{', '.join(KIND_SCHEMA)}")]
    directory = str(schemas_dir().resolve())
    blob = _fingerprint(data)
    identity = _schema_bytes(directory)
    if blob is not None:
        hit = _VALIDATED.get((directory, identity, kind, blob))
        if hit is not None:
            return list(hit)
    v = _validators(directory)[kind]
    errors = sorted(v.iter_errors(data), key=lambda e: (json_path(e.absolute_path), e.message))
    out = [(json_path(e.absolute_path), e.message) for e in errors[:MAX_ERRORS_PER_DOC]]
    if blob is not None:
        if len(_VALIDATED) >= _VALIDATED_MAX:
            _VALIDATED.clear()
        _VALIDATED[(directory, identity, kind, blob)] = tuple(out)
    return out
