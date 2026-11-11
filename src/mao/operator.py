"""OperatorRuntime 调用渲染与 stream-json transcript 解析。不执行命令。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import parse_duration
from mao.brief import Brief, render_brief, resolve_station
from mao.errors import FactoryError
from mao.values import params_value, recipe_env, runtime_env

__all__ = [
    "Brief",
    "Invocation",
    "TranscriptSummary",
    "build_invocation",
    "parse_transcript",
    "render_brief",
]


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    env: dict[str, str]
    timeout_s: float
    transcript: str | None


@dataclass(frozen=True)
class TranscriptSummary:
    cost_usd: float
    turns: int | None
    is_error: bool
    result: str | None
    events: int
    malformed: int


def build_invocation(
    snapshot: Any,
    station_key: str,
    brief: Brief,
    *,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
) -> Invocation:
    resolved = resolve_station(snapshot, station_key)
    station_key = resolved.station_key
    params = params_value(snapshot, station_key, params)
    timeout_s = _timeout_seconds(resolved.station)
    kind = resolved.runtime.get("kind")
    if kind == "script":
        argv = _script_argv(snapshot, resolved, inputs, params)
        return Invocation(tuple(argv), {}, timeout_s, None)
    if kind == "agent-cli":
        return _agent_invocation(snapshot, resolved, brief, inputs, params, timeout_s)
    raise FactoryError(
        "INVOCATION_INVALID",
        f"未知的 runtime kind {kind!r}",
        station=station_key,
        reason=f"未知的 runtime kind {kind!r}",
    )


def parse_transcript(path: Path, fmt: str) -> TranscriptSummary:
    if fmt != "stream-json":
        raise FactoryError("TRANSCRIPT_FORMAT", "transcript 格式不是 stream-json", format=fmt)
    path = Path(path)
    if not path.is_file():
        raise FactoryError("TRANSCRIPT_MISSING", "transcript 文件不存在", path=str(path))
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    events = 0
    malformed = 0
    result_event: dict[str, Any] | None = None
    for line in text.splitlines():
        if line == "":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(obj, dict):
            malformed += 1
            continue
        events += 1
        if obj.get("type") == "result":
            result_event = obj
    if result_event is None:
        return TranscriptSummary(0.0, None, events + malformed > 0, None, events, malformed)
    return TranscriptSummary(
        _cost(result_event),
        _turns(result_event),
        _is_error(result_event),
        result_event.get("result") if isinstance(result_event.get("result"), str) else None,
        events,
        malformed,
    )


def _timeout_seconds(station: Mapping[str, Any]) -> float:
    limits = station.get("limits") or {}
    if "timeout" not in limits:
        return 600.0
    raw = limits["timeout"]
    seconds = parse_duration(raw)
    if seconds != int(seconds):
        raise FactoryError("TIMEOUT_NOT_INTEGRAL", "limits.timeout 不是整秒", timeout=raw)
    return float(seconds)


def _script_argv(
    snapshot: Any, resolved: Any, inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> list[str]:
    run = resolved.recipe.get("run")
    if not isinstance(run, list) or not run:
        raise FactoryError(
            "INVOCATION_INVALID",
            "script 工位的配方没有 run",
            station=resolved.station_key,
            reason="script 工位的配方没有 run",
        )
    env = recipe_env(inputs, params)
    argv: list[str] = []
    for index, item in enumerate(run):
        argv.append(
            _render_item(
                snapshot, item, f"{resolved.machine_key}/recipes.{resolved.recipe_name}.run[{index}]", env
            )
        )
    return argv


def _agent_invocation(
    snapshot: Any,
    resolved: Any,
    brief: Brief,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    timeout_s: float,
) -> Invocation:
    del inputs, params
    argv_spec = (resolved.runtime.get("invoke") or {}).get("argv")
    if not argv_spec:
        raise FactoryError(
            "INVOCATION_INVALID",
            "runtime 没有 invoke.argv",
            station=resolved.station_key,
            reason="runtime 没有 invoke.argv",
        )
    max_turns = (resolved.station.get("operator") or {}).get("max_turns", 1)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise FactoryError(
            "INVOCATION_INVALID",
            "max_turns 不合法",
            station=resolved.station_key,
            reason="max_turns 不合法",
        )
    limits = resolved.station.get("limits") or {}
    budget = float(limits.get("budget_usd", 0.0))
    model = (resolved.station.get("operator") or {}).get("model", "")
    if not isinstance(model, str):
        model = str(model)
    runtime_ref = resolved.runtime_key.split(":", 1)[1]
    _check_model(resolved, model)
    values = runtime_env(
        brief,
        {"max_turns": max_turns, "timeout_s": int(timeout_s), "budget_usd": budget},
        {"model": model, "runtime": runtime_ref},
    )
    argv = tuple(
        _render_item(snapshot, item, f"{resolved.runtime_key}/invoke.argv[{index}]", values)
        for index, item in enumerate(argv_spec)
    )
    env: dict[str, str] = {}
    for name in sorted(resolved.runtime.get("env") or {}):
        raw = resolved.runtime["env"][name]
        env[name] = _render_item(snapshot, raw, f"{resolved.runtime_key}/env.{name}", values)
    return Invocation(argv, env, timeout_s, _transcript_format(resolved.runtime))


def _check_model(resolved: Any, model: str) -> None:
    allowed = list(resolved.runtime.get("models_allowed") or [])
    if model and model not in allowed:
        raise FactoryError(
            "MODEL_NOT_ALLOWED",
            f"模型 {model} 不在 runtime 允许列表中",
            model=model,
            runtime=resolved.runtime_key,
        )
    if not model and allowed:
        raise FactoryError(
            "MODEL_NOT_ALLOWED",
            "工位没有指定模型",
            model=model,
            runtime=resolved.runtime_key,
        )


def _transcript_format(runtime: Mapping[str, Any]) -> str | None:
    if "transcript" not in runtime:
        return None
    transcript = runtime.get("transcript") or {}
    fmt = transcript.get("format") if isinstance(transcript, Mapping) else None
    if fmt == "stream-json":
        return "stream-json"
    raise FactoryError("TRANSCRIPT_FORMAT", "runtime transcript 格式不是 stream-json", format=fmt)


def _render_item(snapshot: Any, item: Any, subject: str, env: Mapping[str, Any]) -> str:
    text = item if isinstance(item, str) else str(item)
    if "{{" not in text:
        return text
    rendered = snapshot.compiled(subject).render(dict(env))
    if not isinstance(rendered, str):
        raise FactoryError(
            "INVOCATION_INVALID",
            f"模板 {subject} 的渲染结果不是字符串",
            station=subject,
            reason="渲染结果不是字符串",
        )
    return rendered


def _cost(event: Mapping[str, Any]) -> float:
    if "total_cost_usd" not in event:
        return 0.0
    value = event["total_cost_usd"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0.0
    return round(float(value), 6)


def _turns(event: Mapping[str, Any]) -> int | None:
    if "num_turns" not in event:
        return None
    value = event["num_turns"]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _is_error(event: Mapping[str, Any]) -> bool:
    if "is_error" in event:
        return bool(event["is_error"])
    return event.get("subtype") != "success"
