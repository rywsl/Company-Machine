"""产线运行计划：纯函数编译，不读数据库、不读时间。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from mao.base import canonical_bytes, digest_bytes
from mao.errors import FactoryError

_UNSUPPORTED = ("subline", "switch", "map", "until")


@dataclass(frozen=True)
class Binding:
    port: str
    source: Literal["input", "node"]
    node: str
    out_port: str

    def to_json(self) -> dict[str, str]:
        return {"port": self.port, "source": self.source, "node": self.node, "out_port": self.out_port}


@dataclass(frozen=True)
class PlanNode:
    id: str
    kind: Literal["station", "converter"]
    step: str
    station: str
    role: str
    params: dict[str, Any]
    templated: tuple[str, ...]
    inputs: tuple[Binding, ...]
    deps: tuple[str, ...]
    budget_usd: float
    implicit: bool
    resolution: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "step": self.step,
            "station": self.station,
            "role": self.role,
            "params": copy.deepcopy(self.params),
            "templated": list(self.templated),
            "deps": list(self.deps),
            "inputs": [item.to_json() for item in self.inputs],
            "budget_usd": self.budget_usd,
            "implicit": self.implicit,
            "resolution": copy.deepcopy(self.resolution),
        }


@dataclass(frozen=True)
class RunPlan:
    line: str
    epoch: str
    nodes: tuple[PlanNode, ...]
    outputs: dict[str, tuple[str, str]]
    keep: tuple[tuple[str, str], ...]
    inputs: dict[str, str]
    input_requires: dict[str, tuple[str, ...]]
    budget_cap_usd: float | None
    budget_items: tuple[tuple[str, float], ...]

    def node(self, node_id: str) -> PlanNode:
        for item in self.nodes:
            if item.id == node_id:
                return item
        raise FactoryError("NODE_NOT_FOUND", f"计划中没有节点 {node_id}", node=node_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "epoch": self.epoch,
            "inputs": dict(self.inputs),
            "input_requires": {name: list(reqs) for name, reqs in self.input_requires.items()},
            "budget_cap_usd": self.budget_cap_usd,
            "budget_items": [[item, usd] for item, usd in self.budget_items],
            "nodes": [node.to_json() for node in self.nodes],
            "outputs": {name: [node, port] for name, (node, port) in self.outputs.items()},
            "keep": [[node, port] for node, port in self.keep],
        }

    def digest(self) -> str:
        return digest_bytes(canonical_bytes(self.to_json()))


def compile_run_plan(snapshot: Any, line_ref: str) -> RunPlan:
    key = snapshot.key("Line", line_ref)
    spec = snapshot.spec(key)
    steps = spec.get("steps") or {}
    _reject_unsupported(key, steps)
    nodes = _build_nodes(snapshot, key, steps)
    ordered = _topo(nodes)
    items = tuple(
        sorted(
            (node.id if node.implicit else node.step, node.budget_usd)
            for node in ordered
            if "budget_usd" in (snapshot.spec(node.station).get("limits") or {})
        )
    )
    kernel = tuple(
        sorted((item, usd) for line, item, usd in snapshot.result.model.rows("cost_item") if line == key)
    )
    if list(items) != list(kernel):
        raise FactoryError(
            "RUN_PLAN_INVALID",
            "预算组成与内核 cost_item 不一致",
            line=key,
            got=[[item, usd] for item, usd in items],
            kernel=[[item, usd] for item, usd in kernel],
        )
    iface = spec.get("interface") or {}
    inputs, requires = _interface_inputs(iface.get("inputs") or {})
    budget = spec.get("budget") or {}
    cap = float(budget["cap_usd"]) if "cap_usd" in budget else None
    return RunPlan(
        key,
        snapshot.epoch,
        ordered,
        _outputs(iface.get("outputs") or {}),
        _keep(spec.get("keep") or []),
        inputs,
        requires,
        cap,
        items,
    )


def _reject_unsupported(line: str, steps: Mapping[str, Any]) -> None:
    for step in sorted(steps):
        body = steps[step]
        if not isinstance(body, dict):
            continue
        for construct in _UNSUPPORTED:
            if construct in body:
                raise FactoryError(
                    "LINE_UNSUPPORTED",
                    f"M2 不执行 {construct}",
                    step=step,
                    construct=construct,
                    line=line,
                )


def _build_nodes(snapshot: Any, line: str, steps: Mapping[str, Any]) -> dict[str, PlanNode]:
    nodes: dict[str, PlanNode] = {}
    for step, body in steps.items():
        if not isinstance(body, dict) or "station" not in body:
            raise FactoryError("RUN_PLAN_INVALID", f"步骤 {step} 不是工位步骤", line=line, step=step)
        station_key = snapshot.key("Station", body["station"])
        station = snapshot.spec(station_key)
        params = copy.deepcopy(body.get("params") or {})
        templated = tuple(
            sorted(name for name, value in params.items() if isinstance(value, str) and "{{" in value)
        )
        bindings: list[Binding] = []
        for port, expr in (body.get("in") or {}).items():
            origin = _parse_ref(line, step, port, expr)
            resolution = _resolution(snapshot, line, step, port)
            status = resolution.get("status")
            if status == "direct":
                bindings.append(origin)
                continue
            if status != "convert":
                raise FactoryError(
                    "RUN_PLAN_INVALID",
                    f"边 {step}.{port} 的解析状态不能执行",
                    line=line,
                    step=step,
                    port=port,
                    status=status,
                )
            chain = list(resolution.get("chain") or [])
            if len(chain) != len(set(chain)):
                raise FactoryError(
                    "RUN_PLAN_INVALID",
                    f"边 {step}.{port} 的转换链有重复",
                    line=line,
                    step=step,
                    port=port,
                )
            previous = origin
            last_id = ""
            for converter in chain:
                node_id = f"{step}.in.{port}~{converter}"
                conv_key = snapshot.key("Station", converter)
                conv = snapshot.spec(conv_key)
                _require_defaults(line, conv_key, conv)
                incoming = Binding("in", previous.source, previous.node, previous.out_port)
                nodes[node_id] = _node(
                    node_id,
                    "converter",
                    step,
                    conv_key,
                    conv,
                    {},
                    (),
                    (incoming,),
                    _budget(conv),
                    True,
                    copy.deepcopy(resolution),
                )
                last_id = node_id
                previous = Binding("in", "node", node_id, "out")
            bindings.append(Binding(port, "node", last_id, "out"))
        nodes[step] = _node(
            step,
            "station",
            step,
            station_key,
            station,
            params,
            templated,
            tuple(sorted(bindings, key=lambda item: item.port)),
            _budget(station),
            False,
            None,
        )
    return nodes


def _node(
    node_id: str,
    kind: Literal["station", "converter"],
    step: str,
    station: str,
    spec: Mapping[str, Any],
    params: dict[str, Any],
    templated: tuple[str, ...],
    inputs: tuple[Binding, ...],
    budget: float,
    implicit: bool,
    resolution: dict[str, Any] | None,
) -> PlanNode:
    deps = tuple(sorted({item.node for item in inputs if item.source == "node" and item.node}))
    return PlanNode(
        node_id,
        kind,
        step,
        station,
        str(spec.get("role") or ""),
        params,
        templated,
        tuple(sorted(inputs, key=lambda item: item.port)),
        deps,
        budget,
        implicit,
        resolution,
    )


def _parse_ref(line: str, step: str, port: str, expr: Any) -> Binding:
    text = str(expr)
    if text.startswith("$inputs."):
        return Binding(port, "input", "", text[len("$inputs.") :])
    if "." in text:
        node, out_port = text.split(".", 1)
        return Binding(port, "node", node, out_port)
    raise FactoryError("RUN_PLAN_INVALID", f"无法解析绑定 {text}", line=line, step=step, port=port)


def _resolution(snapshot: Any, line: str, step: str, port: str) -> dict[str, Any]:
    for row in snapshot.result.resolutions:
        if (row.get("line"), row.get("step"), row.get("port")) == (line, step, port):
            return row
    raise FactoryError(
        "RUN_PLAN_INVALID", f"找不到边 {step}.{port} 的解析结果", line=line, step=step, port=port
    )


def _require_defaults(line: str, station: str, spec: Mapping[str, Any]) -> None:
    for name, decl in (spec.get("params") or {}).items():
        if isinstance(decl, dict) and "default" not in decl:
            raise FactoryError(
                "RUN_PLAN_INVALID",
                f"转换工位 {station} 的参数 {name} 没有默认值",
                line=line,
                station=station,
                param=name,
            )


def _budget(spec: Mapping[str, Any]) -> float:
    return float((spec.get("limits") or {}).get("budget_usd", 0.0))


def _interface_inputs(declared: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    inputs: dict[str, str] = {}
    requires: dict[str, tuple[str, ...]] = {}
    for name, decl in declared.items():
        if isinstance(decl, dict):
            inputs[name] = str(decl.get("type", ""))
            requires[name] = tuple(str(item) for item in decl.get("requires") or [])
        else:
            inputs[name] = str(decl)
            requires[name] = ()
    return inputs, requires


def _outputs(declared: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for name, decl in declared.items():
        src = decl.get("from") if isinstance(decl, dict) else str(decl)
        node, port = str(src).split(".", 1)
        found[name] = (node, port)
    return found


def _keep(items: list[Any]) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in items:
        node, port = str(item).split(".", 1)
        pairs.append((node, port))
    return tuple(sorted(pairs))


def _topo(nodes: dict[str, PlanNode]) -> tuple[PlanNode, ...]:
    pending = dict(nodes)
    indegree = {node_id: 0 for node_id in pending}
    children: dict[str, list[str]] = {node_id: [] for node_id in pending}
    for node in pending.values():
        for dep in node.deps:
            if dep not in pending:
                raise FactoryError("RUN_PLAN_INVALID", f"节点 {node.id} 依赖不存在的 {dep}", node=node.id)
            indegree[node.id] += 1
            children[dep].append(node.id)
    ordered: list[PlanNode] = []
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    while ready:
        layer = ready
        ready = []
        for node_id in layer:
            ordered.append(pending[node_id])
            for child in children[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        ready.sort()
    if len(ordered) != len(pending):
        raise FactoryError("RUN_PLAN_INVALID", "运行计划有环")
    return tuple(ordered)
