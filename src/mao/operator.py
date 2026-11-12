"""OperatorRuntime 调用渲染与 stream-json transcript 解析。不执行命令。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import parse_duration
from mao.brief import Brief, render_brief, resolve_station
from mao.errors import FactoryError
from mao.values import params_value, recipe_env, runtime_env

__all__ = [
    "CONTROL_VARS",
    "Brief",
    "Invocation",
    "TranscriptSummary",
    "build_invocation",
    "build_runtime_invocation",
    "expand_control_vars",
    "parse_transcript",
    "render_brief",
]

CONTROL_VARS: tuple[str, ...] = ("FACTORY_JOB_ID", "FACTORY_ATTEMPT", "MODEL_PROXY", "EGRESS_PROXY")
_CONTROL_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


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
        return _checked(Invocation(tuple(argv), {}, timeout_s, None))
    if kind == "agent-cli":
        return _checked(_agent_invocation(snapshot, resolved, brief, inputs, params, timeout_s))
    raise FactoryError(
        "INVOCATION_INVALID",
        f"未知的 runtime kind {kind!r}",
        station=station_key,
        reason=f"未知的 runtime kind {kind!r}",
    )


def build_runtime_invocation(
    snapshot: Any,
    runtime: str,
    *,
    task: str,
    contract: str,
    model: str,
    max_turns: int,
    budget_usd: float,
    timeout_s: float,
) -> Invocation:
    runtime_key = snapshot.key("OperatorRuntime", runtime)
    spec = snapshot.spec(runtime_key)
    if spec.get("kind") != "agent-cli":
        raise FactoryError(
            "INVOCATION_INVALID",
            "会话需要 agent-cli runtime",
            reason="会话需要 agent-cli runtime",
        )
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or float(timeout_s) != int(timeout_s)
    ):
        raise FactoryError("TIMEOUT_NOT_INTEGRAL", "timeout_s 不是整秒", timeout=timeout_s)
    timeout = float(int(timeout_s))
    runtime_ref = runtime_key.split(":", 1)[1]
    return _checked(
        _render_agent(
            snapshot,
            runtime_key,
            spec,
            Brief(task, contract, ""),
            model,
            max_turns,
            float(budget_usd),
            timeout,
            runtime_ref,
            station=runtime_key,
        )
    )


def expand_control_vars(inv: Invocation, values: Mapping[str, str]) -> Invocation:
    def replace(text: str) -> str:
        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in values:
                raise FactoryError(
                    "EGRESS_PROXY_UNAVAILABLE",
                    f"控制面变量 {name} 没有值",
                    var=name,
                )
            return str(values[name])

        return _CONTROL_VAR.sub(one, text)

    return Invocation(
        tuple(replace(item) for item in inv.argv),
        {key: replace(value) for key, value in inv.env.items()},
        inv.timeout_s,
        inv.transcript,
    )


def parse_transcript(path: Path, fmt: str) -> TranscriptSummary:
    if fmt == "codex-jsonl":
        return _parse_codex(path)
    if fmt != "stream-json":
        raise FactoryError("TRANSCRIPT_FORMAT", "transcript 格式不是 stream-json 或 codex-jsonl", format=fmt)
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
    max_turns = (resolved.station.get("operator") or {}).get("max_turns", 1)
    limits = resolved.station.get("limits") or {}
    budget = float(limits.get("budget_usd", 0.0))
    model = (resolved.station.get("operator") or {}).get("model", "")
    if not isinstance(model, str):
        model = str(model)
    runtime_ref = resolved.runtime_key.split(":", 1)[1]
    return _render_agent(
        snapshot,
        resolved.runtime_key,
        resolved.runtime,
        brief,
        model,
        max_turns,
        budget,
        timeout_s,
        runtime_ref,
        station=resolved.station_key,
    )


def _render_agent(
    snapshot: Any,
    runtime_key: str,
    runtime: Mapping[str, Any],
    brief: Brief,
    model: str,
    max_turns: Any,
    budget: float,
    timeout_s: float,
    runtime_ref: str,
    *,
    station: str,
) -> Invocation:
    argv_spec = (runtime.get("invoke") or {}).get("argv")
    if not argv_spec:
        raise FactoryError(
            "INVOCATION_INVALID",
            "runtime 没有 invoke.argv",
            station=station,
            reason="runtime 没有 invoke.argv",
        )
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise FactoryError(
            "INVOCATION_INVALID",
            "max_turns 不合法",
            station=station,
            reason="max_turns 不合法",
        )
    _check_model(runtime_key, runtime, model)
    values = runtime_env(
        brief,
        {"max_turns": max_turns, "timeout_s": int(timeout_s), "budget_usd": budget},
        {"model": model, "runtime": runtime_ref},
    )
    argv = tuple(
        _render_item(snapshot, item, f"{runtime_key}/invoke.argv[{index}]", values)
        for index, item in enumerate(argv_spec)
    )
    env: dict[str, str] = {}
    for name in sorted(runtime.get("env") or {}):
        raw = runtime["env"][name]
        env[name] = _render_item(snapshot, raw, f"{runtime_key}/env.{name}", values)
    return Invocation(argv, env, timeout_s, _transcript_format(runtime))


def _check_model(runtime_key: str, runtime: Mapping[str, Any], model: str) -> None:
    allowed = list(runtime.get("models_allowed") or [])
    if model and model not in allowed:
        raise FactoryError(
            "MODEL_NOT_ALLOWED",
            f"模型 {model} 不在 runtime 允许列表中",
            model=model,
            runtime=runtime_key,
        )
    if not model and allowed:
        raise FactoryError(
            "MODEL_NOT_ALLOWED",
            "工位没有指定模型",
            model=model,
            runtime=runtime_key,
        )


def _transcript_format(runtime: Mapping[str, Any]) -> str | None:
    if "transcript" not in runtime:
        return None
    transcript = runtime.get("transcript") or {}
    fmt = transcript.get("format") if isinstance(transcript, Mapping) else None
    if fmt in ("stream-json", "codex-jsonl"):
        return str(fmt)
    raise FactoryError(
        "TRANSCRIPT_FORMAT", "runtime transcript 格式不是 stream-json 或 codex-jsonl", format=fmt
    )


def _checked(inv: Invocation) -> Invocation:
    for text in (*inv.argv, *inv.env.values()):
        for match in _CONTROL_VAR.finditer(text):
            name = match.group(1)
            if name not in CONTROL_VARS:
                raise FactoryError(
                    "INVOCATION_INVALID",
                    f"未知控制面变量 {name}",
                    var=name,
                    reason=f"未知控制面变量 {name}",
                )
    return inv


def _parse_codex(path: Path) -> TranscriptSummary:
    path = Path(path)
    if not path.is_file():
        raise FactoryError("TRANSCRIPT_MISSING", "transcript 文件不存在", path=str(path))
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    events = 0
    malformed = 0
    turns = 0
    failed = False
    result: str | None = None
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
        kind = obj.get("type")
        if kind == "turn.completed":
            turns += 1
        elif kind in ("turn.failed", "error"):
            failed = True
        elif kind == "item.completed":
            item = obj.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                result = item["text"]
    return TranscriptSummary(0.0, turns, failed or turns == 0, result, events, malformed)


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
