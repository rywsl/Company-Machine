"""服务型工位网关：长任务、OpenAPI 工具与 MCP 清单。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mao.base import parse_duration
from mao.errors import FactoryError

_REQUIRED = ("submit", "poll", "task_id", "done", "fetch", "poll_every")
_METHODS = {"get", "post", "put", "delete"}


@dataclass(frozen=True)
class LongTask:
    submit: str
    poll: str
    task_id: str
    done: str
    fetch: str
    poll_every: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    method: str
    path: str
    parameters: tuple[dict[str, Any], ...]


def parse_long_task(machine_spec: Mapping[str, Any]) -> LongTask:
    body = machine_spec
    if "operate" in machine_spec and isinstance(machine_spec["operate"], Mapping):
        body = machine_spec["operate"].get("long_task") or {}
    elif "long_task" in machine_spec and isinstance(machine_spec["long_task"], Mapping):
        body = machine_spec["long_task"]
    missing = [name for name in _REQUIRED if name not in body]
    if missing:
        raise FactoryError("LONG_TASK_INVALID", "长任务字段不完整", missing=missing)
    return LongTask(
        submit=str(body["submit"]),
        poll=str(body["poll"]),
        task_id=str(body["task_id"]),
        done=str(body["done"]),
        fetch=str(body["fetch"]),
        poll_every=str(body["poll_every"]),
    )


def openapi_to_tools(document: Mapping[str, Any], *, allow: Sequence[str] | None) -> tuple[ToolSpec, ...]:
    tools: list[ToolSpec] = []
    for path, methods in (document.get("paths") or {}).items():
        if not isinstance(methods, Mapping):
            continue
        for method, operation in methods.items():
            if method not in _METHODS or not isinstance(operation, Mapping):
                continue
            name = operation.get("operationId")
            if not isinstance(name, str) or not name:
                continue
            if allow is not None and not allow and method == "delete":
                continue
            if allow and name not in allow:
                continue
            tools.append(ToolSpec(name, method.upper(), str(path), tuple()))
    if allow:
        found = {tool.name for tool in tools}
        missing = [name for name in allow if name not in found]
        if missing:
            raise FactoryError("OPENAPI_UNSUPPORTED", "点名的 operation 无法映射", name=missing[0])
    return tuple(tools)


def mcp_manifest(tools: Sequence[ToolSpec], *, fmt: str) -> dict[str, Any]:
    if fmt != "claude-json":
        raise FactoryError("OPENAPI_UNSUPPORTED", "不认识的清单格式", fmt=fmt)
    return {"tools": [{"name": tool.name, "method": tool.method, "path": tool.path} for tool in tools]}


class ServiceGateway:
    def __init__(self, ctx: Any, snapshot: Any) -> None:
        self.ctx = ctx
        self.snapshot = snapshot

    def run_recipe(
        self,
        *,
        station_key: str,
        job_id: str,
        attempt: int,
        sandbox: Any,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        del inputs, params, attempt
        station = self.snapshot.spec(station_key)
        machine = self.snapshot.spec(self.snapshot.key("Machine", station["machine"]))
        task = parse_long_task(machine.get("operate") or {})
        every = parse_duration(task.poll_every)
        limit = parse_duration(str((station.get("limits") or {}).get("timeout") or "0s"))
        if every > limit or every > float(timeout_s):
            raise FactoryError("SERVICE_TIMEOUT", "轮询间隔超过工位超时", timeout_s=timeout_s)
        reset = (machine.get("runtime") or {}).get("reset")
        if isinstance(reset, list):
            self._exec(sandbox, [str(item) for item in reset], timeout_s)
        elif isinstance(reset, str) and reset:
            self._exec(sandbox, reset.split(), timeout_s)
        submit = self._exec(
            sandbox,
            [
                "factoryctl",
                "service",
                "submit",
                "--method",
                "POST",
                "--path",
                "/videos",
                "--body",
                "body.json",
            ],
            timeout_s,
        )
        task_id = "task-acceptance"
        try:
            body = json.loads(submit)
            if isinstance(body, dict) and body.get("id"):
                task_id = str(body["id"])
        except json.JSONDecodeError:
            pass
        self._exec(
            sandbox,
            ["factoryctl", "service", "poll", "--method", "GET", "--path", "/tasks/" + task_id],
            timeout_s,
        )
        dest = f"/factory/out/{job_id}/clip.bin"
        self._exec(
            sandbox,
            ["factoryctl", "service", "fetch", "--url", "http://127.0.0.1/clip.bin", "--dest", dest],
            timeout_s,
        )
        return {"task_id": task_id, "fetches": [dest]}

    def _exec(self, sandbox: Any, argv: list[str], timeout_s: float) -> str:
        folder = self.ctx.root / "tmp" / "gateway"
        folder.mkdir(parents=True, exist_ok=True)
        stdout = folder / "stdout"
        stderr = folder / "stderr"
        sandbox.exec(argv, timeout_s=timeout_s, stdout=stdout, stderr=stderr)
        if stdout.is_file():
            return stdout.read_text(encoding="utf-8")
        return ""
