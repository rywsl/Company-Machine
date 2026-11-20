"""运行期值投影与求值环境。不 import 仓库。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from mao.errors import FactoryError


def bean_value(bean: Any) -> dict[str, Any]:
    return {
        "id": bean.id,
        "type": bean.type,
        "facts": copy.deepcopy(dict(bean.facts)),
        "meta": copy.deepcopy(dict(bean.meta)),
        "payload": [
            {"path": item.path, "digest": item.digest, "size": item.size, "media_type": item.media_type}
            for item in bean.payload
        ],
    }


def inputs_value(inputs: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in inputs.items():
        if isinstance(value, (list, tuple)):
            out[name] = [bean_value(item) for item in value]
        else:
            out[name] = bean_value(value)
    return out


def _matches(declared: str, value: Any) -> bool:
    if isinstance(value, bool):
        return declared == "bool"
    if declared == "string":
        return isinstance(value, str)
    if declared == "int":
        return isinstance(value, int)
    if declared == "float":
        return isinstance(value, float)
    if declared == "num":
        return isinstance(value, (int, float))
    if declared == "bool":
        return False
    return False


def params_value(snapshot: Any, station_key: str, given: Mapping[str, Any]) -> dict[str, Any]:
    key = snapshot.key("Station", station_key)
    decls = snapshot.spec(key).get("params") or {}
    problems: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    for name, decl in decls.items():
        if name in given:
            value = given[name]
        elif isinstance(decl, dict) and "default" in decl:
            value = decl["default"]
        else:
            problems.append({"name": name, "reason": "missing"})
            continue
        expected = decl.get("type") if isinstance(decl, dict) else None
        if not _matches(str(expected), value):
            try:
                shown: Any = value
                from mao.base import canonical_json

                canonical_json(value)
            except Exception:
                shown = repr(value)
            problems.append({"name": name, "reason": "type", "expected": expected, "value": shown})
            continue
        result[name] = value
    for name in given:
        if name not in decls:
            problems.append({"name": name, "reason": "unknown"})
    if problems:
        problems.sort(key=lambda item: item["name"])
        raise FactoryError("PARAM_INVALID", "工位参数不合法", station=key, problems=problems)
    return result


def property_env(bean: Any) -> dict[str, Any]:
    return {"bean": bean_value(bean)}


def accept_env(out: Any, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(out, (list, tuple)):
        projected: Any = [bean_value(item) for item in out]
    else:
        projected = bean_value(out)
    return {"out": projected, "inputs": inputs_value(inputs), "params": dict(params)}


def approval_env(
    phase: str, payload: Mapping[str, Any], inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> dict[str, Any]:
    if phase == "propose":
        root = "proposal"
    elif phase == "in_run":
        root = "request"
    else:
        raise ValueError(f"未知审批阶段 {phase}")
    return {root: dict(payload), "inputs": inputs_value(inputs), "params": dict(params)}


def recipe_env(inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    return {"inputs": inputs_value(inputs), "params": dict(params)}


def _pick(obj: Any, *names: str) -> dict[str, Any]:
    if all(hasattr(obj, name) for name in names):
        return {name: getattr(obj, name) for name in names}
    return {name: obj[name] for name in names}


def runtime_env(brief: Any, limits: Mapping[str, Any], operator: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "brief": _pick(brief, "task", "factory_contract"),
        "limits": _pick(limits, "max_turns", "timeout_s", "budget_usd"),
        "operator": _pick(operator, "model", "runtime"),
    }
