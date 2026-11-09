"""JSON Schema 校验（V1 的结构部分）。schemas 以 spec/schemas/ 为唯一来源。"""

from __future__ import annotations

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


def validate(data: Any) -> list[tuple[str, str]]:
    """返回 [(JSON 路径, 消息)]；空列表表示通过。"""
    if not isinstance(data, dict):
        return [("", "文档必须是一个映射")]
    kind = data.get("kind")
    if kind not in KIND_SCHEMA:
        return [("kind", f"未知的 kind {kind!r}，可用：{', '.join(KIND_SCHEMA)}")]
    v = _validators(str(schemas_dir()))[kind]
    errors = sorted(v.iter_errors(data), key=lambda e: (json_path(e.absolute_path), e.message))
    return [(json_path(e.absolute_path), e.message) for e in errors[:MAX_ERRORS_PER_DOC]]
