"""服务型工位网关：长任务、OpenAPI 工具与 MCP 清单。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from mao.base import canonical_json, parse_duration, utc_iso
from mao.errors import FactoryError
from mao.values import recipe_env

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
        station = self.snapshot.spec(station_key)
        machine_key = self.snapshot.key("Machine", station["machine"])
        machine = self.snapshot.spec(machine_key)
        task = parse_long_task(machine)
        recipe_name = str(station.get("recipe") or "")
        recipe = (machine.get("recipes") or {}).get(recipe_name) or {}
        outputs = recipe.get("outputs") or {}
        if len(outputs) != 1 or any(
            isinstance(decl, Mapping) and "harvest" in decl for decl in outputs.values()
        ):
            raise FactoryError(
                "LONG_TASK_INVALID",
                "服务型配方必须恰好有一个输出端口，且不写 harvest",
                machine=machine_key,
            )
        port, decl = next(iter(outputs.items()))
        declared = (station.get("limits") or {}).get("timeout")
        if not declared:
            raise FactoryError("LONG_TASK_INVALID", "工位没有超时", machine=machine_key)
        limit = parse_duration(str(declared))
        every = parse_duration(task.poll_every)
        if every > limit or every > float(timeout_s):
            raise FactoryError(
                "SERVICE_TIMEOUT",
                "轮询间隔超过工位超时",
                job=job_id,
                polls=0,
                timeout_s=float(timeout_s),
            )
        done_expr, fetch_expr = _compile_task(task)
        runtime = machine.get("runtime") or {}
        service = runtime.get("service") or {}
        service_name = str(service.get("name") or "")
        reset = runtime.get("reset")
        if isinstance(reset, list):
            self._exec(sandbox, job_id, [str(item) for item in reset], timeout_s)
        elif isinstance(reset, str) and reset:
            if '"' in reset or "'" in reset:
                raise FactoryError("LONG_TASK_INVALID", "reset 字符串含引号", machine=machine_key)
            self._exec(sandbox, job_id, reset.split(), timeout_s)
        self._health(sandbox, job_id, machine, timeout_s)
        env = recipe_env(self._input_beans(inputs), params)
        body = _render_value(self.snapshot, machine_key, recipe_name, recipe.get("call") or {}, "call", env)
        body_virtual = "/factory/ctl/service-body.json"
        _write_virtual(sandbox, body_virtual, json.dumps(body, ensure_ascii=False).encode("utf-8"))
        method, path = _split_request(task.submit)
        submit = self._exec(
            sandbox,
            job_id,
            ["factoryctl", "service", "submit", "--method", method, "--path", path, "--body", body_virtual],
            timeout_s,
        )
        try:
            submitted = json.loads(submit)
        except json.JSONDecodeError as exc:
            raise FactoryError(
                "SERVICE_FAILED", "submit 响应不是 JSON", job=job_id, reason="submit-json"
            ) from exc
        if not isinstance(submitted, dict):
            raise FactoryError("SERVICE_FAILED", "submit 响应不是对象", job=job_id, reason="submit-shape")
        task_id = _pointer(submitted, task.task_id)
        if not isinstance(task_id, str) or not task_id:
            raise FactoryError("SERVICE_FAILED", "任务号不是字符串", job=job_id, reason="task-id")
        self._event(
            job_id, "service.submit", {"job": job_id, "task_id": task_id, "method": method, "path": path}
        )
        poll_method, poll_path = _split_request(task.poll)
        poll_path = poll_path.replace("{task_id}", quote(task_id, safe=""))
        self._phase(sandbox, "poll", task_id, 0, [])
        started = self.ctx.clock.monotonic()
        polls = 0
        response: dict[str, Any] = {}
        while True:
            if self.ctx.clock.monotonic() - started > float(timeout_s):
                raise FactoryError(
                    "SERVICE_TIMEOUT",
                    "轮询超过工位超时",
                    job=job_id,
                    polls=polls,
                    timeout_s=float(timeout_s),
                )
            raw = self._exec(
                sandbox,
                job_id,
                ["factoryctl", "service", "poll", "--method", poll_method, "--path", poll_path],
                timeout_s,
            )
            polls += 1
            self._event(job_id, "service.poll", {"job": job_id, "polls": polls})
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FactoryError(
                    "SERVICE_FAILED", "poll 响应不是 JSON", job=job_id, reason="poll-json"
                ) from exc
            if not isinstance(response, dict):
                raise FactoryError("SERVICE_FAILED", "poll 响应不是对象", job=job_id, reason="poll-shape")
            self._phase(sandbox, "poll", task_id, polls, [])
            try:
                finished = done_expr.evaluate({"response": response}) is True
            except Exception as exc:
                raise FactoryError("SERVICE_FAILED", "done 求值失败", job=job_id, reason="done") from exc
            if finished:
                break
            self.ctx.clock.sleep(every)
        try:
            fetched = fetch_expr.evaluate({"response": response})
        except Exception as exc:
            raise FactoryError("SERVICE_FAILED", "fetch 求值失败", job=job_id, reason="fetch") from exc
        urls = [str(item) for item in fetched] if isinstance(fetched, (list, tuple)) else None
        if urls is None:
            raise FactoryError("SERVICE_FAILED", "fetch 结果不是列表", job=job_id, reason="fetch-shape")
        type_expr = decl.get("type") if isinstance(decl, Mapping) else str(decl)
        listed = str(type_expr).startswith("list<") and str(type_expr).endswith(">")
        if not listed and len(urls) != 1:
            raise FactoryError(
                "SERVICE_FAILED", "非列表端口取回的项数不是 1", job=job_id, reason="fetch-count"
            )
        if any(not _fetch_allowed(item, service_name) for item in urls):
            raise FactoryError("SERVICE_FAILED", "取回地址不在服务主机内", job=job_id, reason="fetch-host")
        place = _place_as(self.snapshot, str(type_expr))
        dests: list[str] = []
        for index, url in enumerate(urls):
            name = place if place != "file" else _last_segment(url)
            if listed:
                dest = f"/factory/out/{port}/{index}/{name}"
            else:
                dest = f"/factory/out/{port}/{name}"
            self._exec(
                sandbox,
                job_id,
                ["factoryctl", "service", "fetch", "--url", url, "--dest", dest],
                timeout_s,
            )
            dests.append(dest)
        self._event(job_id, "service.fetch", {"job": job_id, "count": len(dests)})
        self._phase(sandbox, "done", task_id, polls, dests)
        del attempt
        return {"task_id": task_id, "fetches": dests}

    def _input_beans(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        loaded: dict[str, Any] = {}
        for name, value in inputs.items():
            if isinstance(value, str):
                loaded[name] = self.ctx.warehouse.get(value)
            elif isinstance(value, (list, tuple)) and value and all(isinstance(item, str) for item in value):
                loaded[name] = [self.ctx.warehouse.get(item) for item in value]
            else:
                loaded[name] = value
        return loaded

    def _health(self, sandbox: Any, job_id: str, machine: Mapping[str, Any], timeout_s: float) -> None:
        runtime = machine.get("runtime") or {}
        service = runtime.get("service") or {}
        port = service.get("port")
        operate = machine.get("operate") or {}
        openapi = str(operate.get("openapi") or "/openapi.json")
        path = urlsplit(openapi).path or openapi
        if not str(path).startswith("/"):
            path = "/" + str(path)
        url = f"http://127.0.0.1:{port}{path}"
        out = "/factory/ctl/openapi.json"
        _write_virtual(sandbox, out, b"")
        try:
            self._exec(
                sandbox,
                job_id,
                ["factoryctl", "service", "openapi", "--url", url, "--out", out],
                timeout_s,
            )
        except FactoryError as exc:
            raise FactoryError("SERVICE_UNHEALTHY", "服务健康检查失败", url=url) from exc

    def _phase(self, sandbox: Any, phase: str, task_id: str, polls: int, fetches: Sequence[str]) -> None:
        _write_virtual(
            sandbox,
            "/factory/ctl/service.json",
            canonical_json(
                {
                    "phase": phase,
                    "task_id": task_id,
                    "polls": polls,
                    "last_status": None,
                    "fetches": list(fetches),
                }
            ).encode("utf-8"),
        )

    def _event(self, job_id: str, kind: str, data: Mapping[str, Any]) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
                (
                    self.ctx.ids.new("ev"),
                    utc_iso(self.ctx.clock.now()),
                    kind,
                    job_id,
                    canonical_json(dict(data)),
                ),
            )

    def _exec(self, sandbox: Any, job_id: str, argv: list[str], timeout_s: float) -> str:
        folder = self.ctx.root / "tmp" / "gateway"
        folder.mkdir(parents=True, exist_ok=True)
        stdout = folder / f"{job_id}.stdout"
        stderr = folder / f"{job_id}.stderr"
        result = sandbox.exec(argv, timeout_s=timeout_s, stdout=stdout, stderr=stderr)
        text = stdout.read_text(encoding="utf-8") if stdout.is_file() else ""
        if getattr(result, "timed_out", False) or getattr(result, "exit_code", 1) not in (0, None):
            raise FactoryError("SERVICE_FAILED", "factoryctl 失败", job=job_id, reason="ctl")
        return text


def _compile_task(task: LongTask) -> tuple[Any, Any]:
    from mrl.errors import StaticError
    from mrl.expr import compile_expr
    from mrl.types import BOOL, DYN, STRING, ListT, MapT

    env = {"response": MapT(DYN)}
    try:
        done = compile_expr(task.done, env, expect=BOOL)
        fetch = compile_expr(task.fetch, env, expect=ListT(STRING))
    except StaticError as exc:
        raise FactoryError("LONG_TASK_INVALID", "长任务表达式不能编译", mrl=exc.code) from exc
    return done, fetch


def _split_request(text: str) -> tuple[str, str]:
    parts = str(text).split(None, 1)
    if len(parts) != 2 or not parts[0] or not parts[1].startswith("/"):
        raise FactoryError("LONG_TASK_INVALID", "请求必须是方法加路径", missing=["submit"])
    return parts[0].upper(), parts[1]


def _pointer(document: Mapping[str, Any], pointer: str) -> Any:
    if pointer in ("", "/"):
        return document
    if not pointer.startswith("/"):
        return None
    current: Any = document
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _render_value(
    snapshot: Any, machine_key: str, recipe: str, value: Any, path: str, env: Mapping[str, Any]
) -> Any:
    if isinstance(value, str) and "{{" in value:
        return snapshot.compiled(f"{machine_key}/recipes.{recipe}.{path}").render(env)
    if isinstance(value, Mapping):
        return {
            key: _render_value(snapshot, machine_key, recipe, item, f"{path}.{key}", env)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _render_value(snapshot, machine_key, recipe, item, f"{path}[{index}]", env)
            for index, item in enumerate(value)
        ]
    return value


def _place_as(snapshot: Any, type_expr: str) -> str:
    expr = type_expr[5:-1] if type_expr.startswith("list<") and type_expr.endswith(">") else type_expr
    try:
        from mao.dynamic import _required_payload_paths

        paths = _required_payload_paths(snapshot, expr)
    except FactoryError:
        return "file"
    if len(paths) == 1:
        return paths[0].rsplit("/", 1)[-1]
    return "file"


def _last_segment(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name or "file"


def _fetch_allowed(url: str, service_name: str) -> bool:
    if url.startswith("/workspace/") or url == "/workspace":
        return True
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return False
    return parts.hostname == "127.0.0.1" or (bool(service_name) and parts.hostname == service_name)


def _host_path(sandbox: Any, virtual: str) -> Path:
    mounts = tuple(getattr(sandbox.spec, "mounts", ()) or ())
    ranked = sorted(mounts, key=lambda item: len(item.target), reverse=True)
    for mount in ranked:
        target = mount.target.rstrip("/") or "/"
        if virtual == target or virtual.startswith(target + "/"):
            rest = virtual[len(target) :].lstrip("/")
            return Path(mount.source) / rest
    raise FactoryError("SERVICE_FAILED", "沙箱没有这个挂载", reason="mount")


def _write_virtual(sandbox: Any, virtual: str, data: bytes) -> None:
    path = _host_path(sandbox, virtual)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
