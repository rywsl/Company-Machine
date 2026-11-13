"""动态产线编译与运行期展开。"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError
from mao.runplan import (
    Binding,
    PlanNode,
    RunPlan,
    _budget,
    _interface_inputs,
    _keep,
    _outputs,
    _require_defaults,
    _resolution,
    _topo,
    compile_run_plan,
)
from mao.values import bean_value
from mrl.errors import EvalError

_DYNAMIC = ("map", "switch", "until", "subline")
_SUBMITTABLE = frozenset({"station", "converter"})
_STATION_INSTANCE = re.compile(r"^([a-z][a-z0-9_]*)(#\d+|@\d+)$")
_CONVERTER_INSTANCE = re.compile(r"^([a-z][a-z0-9_]*)\.in\.([a-z][a-z0-9_]*)~(.+)(#\d+|@\d+)$")


@dataclass(frozen=True)
class _Instance:
    step: str
    suffix: str
    port: str | None = None
    converter: str | None = None


def compile_dynamic(snapshot: Any, line_ref: str) -> RunPlan:
    key = snapshot.key("Line", line_ref)
    spec = snapshot.spec(key)
    steps = spec.get("steps") or {}
    if not any(isinstance(body, dict) and any(name in body for name in _DYNAMIC) for body in steps.values()):
        return compile_run_plan(snapshot, line_ref)
    nodes: dict[str, PlanNode] = {}
    for step, body in steps.items():
        if not isinstance(body, dict):
            raise FactoryError("RUN_PLAN_INVALID", "步骤不是对象", line=key, reason="step-shape", step=step)
        if "map" in body and "until" in body:
            raise FactoryError(
                "RUN_PLAN_INVALID",
                "同一工位同时有 map 与 until，节点编号没有组合形式",
                line=key,
                reason="map-until",
                step=step,
            )
        extra = _switch_deps(steps, step)
        if "switch" in body:
            nodes[step] = _control(step, "switch", _switch_on_dep(body), ())
            continue
        if "subline" in body:
            nodes[step] = _subline_node(snapshot, key, step, body, extra)
            continue
        if "station" not in body:
            raise FactoryError(
                "RUN_PLAN_INVALID", "步骤没有工位或子产线", line=key, reason="step-shape", step=step
            )
        mapping = body.get("map") if isinstance(body.get("map"), dict) else None
        if mapping is not None and isinstance(mapping.get("over"), list):
            _compile_literal_map(snapshot, key, step, body, extra, nodes)
            continue
        if mapping is not None:
            nodes[step] = _control(step, "map", extra + _over_dep(mapping.get("over")), ())
            continue
        if "until" in body:
            nodes[step] = _control(step, "until", extra, ())
            continue
        nodes.update(_family(snapshot, key, step, body, "", extra))
    iface = spec.get("interface") or {}
    inputs, requires = _interface_inputs(iface.get("inputs") or {})
    budget = spec.get("budget") or {}
    cap = float(budget["cap_usd"]) if "cap_usd" in budget else None
    kernel = tuple(
        sorted((item, usd) for line, item, usd in snapshot.result.model.rows("cost_item") if line == key)
    )
    return RunPlan(
        key,
        snapshot.epoch,
        _topo(nodes),
        _outputs(iface.get("outputs") or {}),
        _keep(spec.get("keep") or []),
        inputs,
        requires,
        cap,
        kernel,
    )


class PlanCheck:
    def __init__(
        self, ok: bool, diags: tuple[dict[str, Any], ...], plan: RunPlan | None, feedback: str
    ) -> None:
        self.ok = ok
        self.diags = diags
        self.plan = plan
        self.feedback = feedback


_CONTEXTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


def bind_context(snapshot: Any, ctx: Any) -> None:
    """让 compile_plan 能打开转换需求与 epoch 侧车。签名里没有 ctx。"""
    _CONTEXTS[snapshot] = ctx


def compile_plan(snapshot: Any, document: Mapping[str, Any], *, planner_key: str, run: Any) -> PlanCheck:
    diags: list[dict[str, Any]] = []
    if not isinstance(document, Mapping) or document.get("kind") != "Plan":
        diags.append({"code": "PLAN_INVALID", "subject": "kind", "message": "kind 不是 Plan"})
        diags.append({"code": "E-SCHEMA", "subject": "kind", "message": "文档不符合 Plan"})
        return _plan_result(False, diags, None)
    for path, message in _plan_schema_errors(document):
        diags.append({"code": "E-SCHEMA", "subject": path or "plan", "message": message})
    metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    planner = snapshot.key("Station", planner_key)
    if "generated_by" in metadata and metadata.get("generated_by") != planner:
        diags.append(
            {
                "code": "PLAN_INVALID",
                "subject": "metadata.generated_by",
                "message": "generated_by 与 planner 不一致",
            }
        )
    if "run" in metadata and metadata.get("run") != run.id:
        diags.append({"code": "PLAN_INVALID", "subject": "metadata.run", "message": "run 与当前 run 不一致"})
    spec = document.get("spec") if isinstance(document.get("spec"), Mapping) else {}
    steps = spec.get("steps") if isinstance(spec.get("steps"), Mapping) else {}
    bounds = _plan_bounds(snapshot, planner)
    parent_inputs = _line_inputs(snapshot, run.line)
    resolved: dict[str, Any] = {}
    expansions: list[tuple[str, int]] = []
    expanded = 0
    depth = 1
    for name, body in steps.items():
        if not isinstance(body, dict):
            diags.append({"code": "E-SCHEMA", "subject": str(name), "message": "步骤不是对象"})
            continue
        for path, message in _step_schema_errors(str(name), body):
            diags.append({"code": "E-SCHEMA", "subject": path or str(name), "message": message})
        if "station" in body:
            ref = str(body["station"])
            try:
                station_key = snapshot.key("Station", ref)
                station = snapshot.spec(station_key)
            except FactoryError:
                diags.append(
                    {"code": "PLAN_INVALID", "subject": str(name), "message": "当前 epoch 没有这个工位"}
                )
                continue
            resolved[str(name)] = (station_key, station)
            _bounds_station(diags, bounds, str(name), ref, station_key, station)
            if station.get("role") == "planner":
                diags.append({"code": "PLAN_BOUNDS", "subject": str(name), "message": "Plan 不能再产出 Plan"})
        elif "subline" in body:
            ref = str(body["subline"])
            try:
                snapshot.spec(snapshot.key("Line", ref))
            except FactoryError:
                diags.append(
                    {"code": "PLAN_INVALID", "subject": str(name), "message": "当前 epoch 没有这条子产线"}
                )
                continue
            depth = max(depth, 2)
        count = _expansion(body)
        expansions.append((str(name), count))
        expanded += count
        _check_plan_ports(diags, str(name), body, steps, parent_inputs)
    if "max_steps" in bounds and expanded > int(bounds["max_steps"]):
        diags.append(
            {
                "code": "PLAN_BOUNDS",
                "subject": "steps",
                "message": f"展开后的步骤数 {expanded} 超过 max_steps {bounds['max_steps']}",
            }
        )
    if "max_depth" in bounds and depth > int(bounds["max_depth"]):
        diags.append(
            {
                "code": "PLAN_BOUNDS",
                "subject": "depth",
                "message": f"嵌套深度 {depth} 超过 max_depth {bounds['max_depth']}",
            }
        )
    for name, count in expansions:
        body = steps.get(name) or {}
        if not isinstance(body, dict) or "map" not in body or "max_fanout" not in bounds:
            continue
        limit = int(bounds["max_fanout"])
        declared = (body.get("map") or {}).get("max_items")
        if isinstance(declared, int):
            limit = min(limit, declared)
        if count > limit:
            diags.append(
                {
                    "code": "PLAN_BOUNDS",
                    "subject": name,
                    "message": f"map 展开 {count} 超过有效上限 {limit}",
                }
            )
    nodes, cost, edge_rows = _plan_nodes(snapshot, run, steps, parent_inputs, resolved, diags)
    if "budget_usd" in bounds and cost > float(bounds["budget_usd"]):
        diags.append(
            {
                "code": "PLAN_BUDGET",
                "subject": "budget",
                "message": f"子计划 {cost} 超过 plan_bounds.budget_usd {bounds['budget_usd']}",
            }
        )
    remaining = float(run.value()["budget"]["remaining_usd"])
    if cost > remaining:
        diags.append(
            {
                "code": "PLAN_BUDGET",
                "subject": "budget",
                "message": f"子计划 {cost} 超过 run 剩余 {remaining}",
            }
        )
    _apply_resolution_sidecar(snapshot, run, edge_rows)
    if diags:
        return _plan_result(False, diags, None)
    keep = []
    for item in spec.get("keep") or []:
        if isinstance(item, str) and "." in item:
            node, port = item.split(".", 1)
            keep.append((node, port))
    plan = RunPlan(
        line=run.line,
        epoch=snapshot.epoch,
        nodes=tuple(nodes),
        outputs={},
        keep=tuple(sorted(keep)),
        inputs={},
        input_requires={},
        budget_cap_usd=None,
        budget_items=tuple(),
    )
    return _plan_result(True, [], plan)


def _plan_result(ok: bool, diags: list[dict[str, Any]], plan: RunPlan | None) -> PlanCheck:
    feedback = "\n".join(f"- {item['code']}: {item['subject']} {item['message']}" for item in diags)
    return PlanCheck(ok, tuple(diags), plan, feedback)


def _plan_bounds(snapshot: Any, planner_key: str) -> dict[str, Any]:
    try:
        return dict(snapshot.spec(planner_key).get("plan_bounds") or {})
    except FactoryError:
        return {}


def _line_inputs(snapshot: Any, line_key: str) -> dict[str, Any]:
    try:
        spec = snapshot.spec(snapshot.key("Line", line_key))
    except FactoryError:
        return {}
    return dict((spec.get("interface") or {}).get("inputs") or {})


def _bounds_station(
    diags: list[dict[str, Any]],
    bounds: Mapping[str, Any],
    step: str,
    ref: str,
    key: str,
    station: Mapping[str, Any],
) -> None:
    if "allowed_stations" in bounds:
        allowed = {str(item) for item in bounds.get("allowed_stations") or []}
        if ref not in allowed and key not in allowed and key.split(":", 1)[-1] not in allowed:
            diags.append({"code": "PLAN_BOUNDS", "subject": step, "message": "工位不在 allowed_stations"})
    if station.get("role") == "sink" and "allowed_sinks" in bounds:
        allowed = {str(item) for item in bounds.get("allowed_sinks") or []}
        if ref not in allowed and key not in allowed and key.split(":", 1)[-1] not in allowed:
            diags.append({"code": "PLAN_BOUNDS", "subject": step, "message": "sink 不在 allowed_sinks"})


def _expansion(body: Mapping[str, Any]) -> int:
    count = 1
    mapping = body.get("map") if isinstance(body.get("map"), Mapping) else None
    if mapping is not None:
        over = mapping.get("over")
        if isinstance(over, list):
            count = len(over)
            limit = mapping.get("max_items")
            if mapping.get("overflow") == "truncate" and isinstance(limit, int):
                count = min(count, int(limit))
        elif isinstance(mapping.get("max_items"), int):
            count = int(mapping["max_items"])
    until = body.get("until") if isinstance(body.get("until"), Mapping) else None
    if until is not None and isinstance(until.get("max_iter"), int):
        count *= int(until["max_iter"])
    return count


def _check_plan_ports(
    diags: list[dict[str, Any]],
    step: str,
    body: Mapping[str, Any],
    steps: Mapping[str, Any],
    parent_inputs: Mapping[str, Any],
) -> None:
    bound = set()
    for port, expr in (body.get("in") or {}).items():
        text = _binding_text(expr)
        bound.add(str(port))
        if text == "$item":
            if "map" not in body:
                diags.append(
                    {"code": "E-PORT", "subject": f"{step}.{port}", "message": "$item 只能出现在 map 步骤"}
                )
            continue
        if text.startswith("$inputs."):
            name = text[len("$inputs.") :]
            if name not in parent_inputs:
                diags.append(
                    {"code": "E-PORT", "subject": f"{step}.{port}", "message": f"接口没有输入 {name}"}
                )
            continue
        if "." not in text:
            diags.append({"code": "E-PORT", "subject": f"{step}.{port}", "message": "绑定无法解析"})
            continue
        src, src_port = text.split(".", 1)
        if src not in steps:
            diags.append({"code": "E-PORT", "subject": f"{step}.{port}", "message": f"Plan 内没有步骤 {src}"})


def _required_ports(snapshot: Any, body: Mapping[str, Any]) -> list[str]:
    if "station" not in body:
        return []
    try:
        station = snapshot.spec(snapshot.key("Station", str(body["station"])))
    except FactoryError:
        return []
    required = []
    for port, decl in (station.get("inputs") or {}).items():
        optional = bool(decl.get("optional")) if isinstance(decl, dict) else False
        if not optional:
            required.append(str(port))
    return required


def _plan_nodes(
    snapshot: Any,
    run: Any,
    steps: Mapping[str, Any],
    parent_inputs: Mapping[str, Any],
    resolved: Mapping[str, Any],
    diags: list[dict[str, Any]],
) -> tuple[list[PlanNode], float, list[dict[str, Any]]]:
    nodes: list[PlanNode] = []
    cost = 0.0
    rows: list[dict[str, Any]] = []
    for name, body in steps.items():
        if not isinstance(body, dict) or str(name) not in resolved:
            continue
        station_key, station = resolved[str(name)]
        missing = [port for port in _required_ports(snapshot, body) if port not in (body.get("in") or {})]
        for port in missing:
            diags.append({"code": "E-PORT", "subject": f"{name}.{port}", "message": "必填输入未绑定"})
        bindings: list[Binding] = []
        family: list[PlanNode] = []
        for port, expr in (body.get("in") or {}).items():
            text = _binding_text(expr)
            origin = _plan_origin(str(name), str(port), text)
            if origin is None:
                continue
            src = _source_type(snapshot, run, steps, parent_inputs, resolved, text)
            dst = _input_type(station, str(port))
            if src is None or dst is None:
                continue
            try:
                result = _resolve_edge(snapshot, src[0], src[1], dst[0], dst[1])
            except FactoryError:
                raise
            except Exception as exc:
                raise FactoryError(
                    "RUN_PLAN_INVALID", "转换解析失败", line=getattr(run, "line", ""), reason="resolve"
                ) from exc
            status = result.get("status")
            chain = [str(item) for item in result.get("chain") or []]
            row = {
                "from": src[0],
                "guarantees": list(src[1]),
                "to": dst[0],
                "requires": list(dst[1]),
                "chain": chain,
                "origin": "runtime",
            }
            if status == "no-converter":
                diags.append(
                    {
                        "code": "E-NO-CONVERTER",
                        "subject": f"{name}.{port}",
                        "message": f"{src[0]} → {dst[0]}",
                    }
                )
                _open_conversion(snapshot, row, str(name), str(port))
                continue
            rows.append(row)
            if status == "convert" and chain:
                previous = origin
                last_id = ""
                for converter in chain:
                    conv_key = snapshot.key("Station", converter)
                    conv = snapshot.spec(conv_key)
                    node_id = f"{name}.in.{port}~{converter}"
                    family.append(
                        _station_node(
                            node_id,
                            "converter",
                            str(name),
                            conv_key,
                            conv,
                            {},
                            (),
                            (Binding("in", previous.source, previous.node, previous.out_port),),
                            (),
                            True,
                            result,
                        )
                    )
                    last_id = node_id
                    previous = Binding("in", "node", node_id, "out")
                bindings.append(Binding(str(port), "node", last_id, "out"))
            else:
                bindings.append(origin)
        params = copy.deepcopy(body.get("params") or {})
        templated = tuple(
            sorted(key for key, value in params.items() if isinstance(value, str) and "{{" in value)
        )
        nodes.extend(family)
        nodes.append(
            _station_node(
                str(name),
                "station",
                str(name),
                station_key,
                station,
                params,
                templated,
                tuple(bindings),
                (),
                False,
                None,
            )
        )
        cost += _station_budget(snapshot, body) * _expansion(body)
        cost += sum(node.budget_usd for node in family) * _expansion(body)
    return nodes, cost, rows


def _plan_origin(step: str, port: str, text: str) -> Binding | None:
    if text == "$item":
        return Binding(port, "item", "", "")
    if text.startswith("$inputs."):
        return Binding(port, "input", "", text[len("$inputs.") :])
    if "." in text:
        node, out_port = text.split(".", 1)
        return Binding(port, "node", node, out_port)
    return None


def _source_type(
    snapshot: Any,
    run: Any,
    steps: Mapping[str, Any],
    parent_inputs: Mapping[str, Any],
    resolved: Mapping[str, Any],
    text: str,
) -> tuple[str, list[str]] | None:
    del run, resolved
    if text.startswith("$inputs."):
        name = text[len("$inputs.") :]
        decl = parent_inputs.get(name)
        if not isinstance(decl, Mapping):
            return None
        return _type_id(str(decl.get("type") or "")), [str(item) for item in decl.get("requires") or []]
    if "." not in text or text == "$item":
        return None
    src, port = text.split(".", 1)
    body = steps.get(src)
    if not isinstance(body, dict) or "station" not in body:
        return None
    try:
        station = snapshot.spec(snapshot.key("Station", str(body["station"])))
    except FactoryError:
        return None
    decl = (station.get("outputs") or {}).get(port)
    if not isinstance(decl, Mapping):
        return None
    return _type_id(str(decl.get("type") or "")), [str(item) for item in decl.get("guarantees") or []]


def _input_type(station: Mapping[str, Any], port: str) -> tuple[str, list[str]] | None:
    decl = (station.get("inputs") or {}).get(port)
    if not isinstance(decl, Mapping):
        return None
    return _type_id(str(decl.get("type") or "")), [str(item) for item in decl.get("requires") or []]


def _type_id(text: str) -> str:
    from mrl.factory.texpr import parse_texpr

    try:
        return parse_texpr(text).id
    except ValueError:
        return text


def _resolve_edge(
    snapshot: Any, src: str, guarantees: Sequence[str], dst: str, requires: Sequence[str]
) -> dict[str, Any]:
    from mrl.factory.functions import resolve
    from mrl.values import from_py, to_py

    model = snapshot.result.model

    def one(name: str) -> Any:
        rows = model.rows(name)
        return rows[0][0] if rows else ()

    result = resolve(
        one("conv_lib"),
        one("sub_pairs"),
        one("always_pairs"),
        src,
        from_py(list(guarantees)),
        dst,
        from_py(list(requires)),
    )
    return dict(to_py(result))


def _open_conversion(snapshot: Any, row: Mapping[str, Any], step: str, port: str) -> None:
    ctx = _CONTEXTS.get(snapshot)
    if ctx is None:
        return
    from mao.foreman import ConversionStore

    ConversionStore(ctx).open_or_merge(
        from_type=str(row["from"]),
        guarantees=list(row["guarantees"]),
        to_type=str(row["to"]),
        requires=list(row["requires"]),
        found_at={"step": step, "port": port},
    )


def _resolution_key(row: Mapping[str, Any]) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    return (
        str(row.get("from")),
        tuple(sorted(str(item) for item in row.get("guarantees") or [])),
        str(row.get("to")),
        tuple(sorted(str(item) for item in row.get("requires") or [])),
    )


def _apply_resolution_sidecar(snapshot: Any, run: Any, rows: Sequence[Mapping[str, Any]]) -> None:
    ctx = _CONTEXTS.get(snapshot)
    if ctx is None:
        return
    path = Path(ctx.root) / "epochs" / str(run.epoch) / "resolution-runtime.json"
    existing: list[dict[str, Any]] = []
    raw = ""
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            raise FactoryError(
                "RUN_PLAN_INVALID", "运行期解析表不是数组", line=run.line, reason="resolution-runtime"
            )
        existing = [dict(item) for item in loaded if isinstance(item, Mapping)]
    precomputed = {_resolution_key(item) for item in snapshot.result.resolutions}
    for item in existing:
        if _resolution_key(item) in precomputed:
            raise FactoryError(
                "RUN_PLAN_INVALID", "侧车含有预计算键", line=run.line, reason="resolution-runtime"
            )
    known = {_resolution_key(item): [str(x) for x in item.get("chain") or []] for item in existing}
    fresh: list[dict[str, Any]] = []
    for row in rows:
        key = _resolution_key(row)
        chain = [str(item) for item in row.get("chain") or []]
        if key in known and known[key] != chain:
            raise FactoryError(
                "RUN_PLAN_INVALID", "侧车同键的链不同", line=run.line, reason="resolution-runtime"
            )
        prior = next((item for item in snapshot.result.resolutions if _resolution_key(item) == key), None)
        if prior is not None and [str(item) for item in prior.get("chain") or []] != chain:
            raise FactoryError(
                "RUN_PLAN_INVALID", "解析链与预计算表不一致", line=run.line, reason="precomputed"
            )
        if key in known or prior is not None:
            continue
        known[key] = chain
        fresh.append(dict(row))
    if not fresh:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = existing + fresh
    path.write_text(canonical_json(payload), encoding="utf-8")
    del raw


def prefix_nodes(nodes: Sequence[PlanNode], planner_id: str) -> list[PlanNode]:
    prefixed: list[PlanNode] = []
    for node in nodes:
        prefixed.append(
            PlanNode(
                f"{planner_id}/{node.id}",
                node.kind,
                node.step,
                node.station,
                node.role,
                dict(node.params),
                node.templated,
                tuple(
                    Binding(
                        item.port,
                        item.source,
                        f"{planner_id}/{item.node}" if item.source == "node" and item.node else item.node,
                        item.out_port,
                    )
                    for item in node.inputs
                ),
                tuple(sorted({planner_id} | {f"{planner_id}/{dep}" for dep in node.deps})),
                node.budget_usd,
                node.implicit,
                copy.deepcopy(node.resolution),
            )
        )
    return prefixed


def attached_spec(snapshot: Any, run: Any, node_id: str, nodes: Sequence[Any]) -> PlanNode | None:
    if "/" not in node_id:
        return None
    planner_id = node_id.split("/", 1)[0]
    planner = next((node for node in nodes if node.node_id == planner_id), None)
    if planner is None or not planner.job_id:
        return None
    ctx = _CONTEXTS.get(snapshot)
    if ctx is None:
        return None
    from mao.jobs import load_job

    job = load_job(ctx.db, planner.job_id)
    document = _bean_document(ctx, snapshot, job)
    if document is None:
        return None
    check = compile_plan(snapshot, document, planner_key=job.station, run=run)
    if not check.ok or check.plan is None:
        return None
    found = {node.id: node for node in prefix_nodes(check.plan.nodes, planner_id)}
    return found.get(node_id)


def plan_document_from_job(ctx: Any, snapshot: Any, job: Any) -> dict[str, Any] | None:
    return _bean_document(ctx, snapshot, job)


def _bean_document(ctx: Any, snapshot: Any, job: Any) -> dict[str, Any] | None:
    outputs = job.outputs or {}
    if len(outputs) != 1:
        return None
    ids = next(iter(outputs.values()))
    if not ids:
        return None
    bean = ctx.warehouse.get(ids[0])
    paths = _required_payload_paths(snapshot, bean.type)
    if len(paths) != 1:
        return None
    item = next((entry for entry in bean.payload if entry.path == paths[0]), None)
    if item is None:
        return None
    try:
        loaded = json.loads(ctx.warehouse.blob_path(item.digest).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _required_payload_paths(snapshot: Any, type_name: str) -> list[str]:
    candidates = [type_name]
    if "@" in type_name:
        name, version = type_name.rsplit("@", 1)
        candidates.append(f"{name}@{version.split('.', 1)[0]}")
    spec = None
    for tid in candidates:
        try:
            spec = snapshot.spec(snapshot.type_key(tid))
            break
        except FactoryError:
            continue
    if spec is None:
        return []
    found = []
    for rule in spec.get("payload") or []:
        if not isinstance(rule, Mapping) or not rule.get("required"):
            continue
        path = str(rule.get("path") or "")
        if path and "*" not in path:
            found.append(path)
    return found


def _plan_schema_errors(document: Mapping[str, Any]) -> list[tuple[str, str]]:
    from mrl.factory.schema import validate

    return validate(dict(document))


def _step_schema_errors(step: str, body: Mapping[str, Any]) -> list[tuple[str, str]]:
    from mrl.factory.schema import validate

    wrapped = {
        "apiVersion": "mao/v1",
        "kind": "Line",
        "metadata": {"name": "plan", "version": 1},
        "spec": {"steps": {step: dict(body)}},
    }
    prefix = f"spec.steps.{step}"
    return [
        (path, message)
        for path, message in validate(wrapped)
        if path == prefix or path.startswith(prefix + ".")
    ]


class DynamicRunner:
    def __init__(self, ctx: Any, snapshot: Any, runner: Any, lines: Any) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.runner = runner
        self.lines = lines

    def expand(self, run_id: str) -> list[str]:
        run = self.lines.get(run_id)
        if run.state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return []
        spec = self.snapshot.spec(run.line)
        steps = spec.get("steps") or {}
        if not any(
            isinstance(body, dict) and any(name in body for name in _DYNAMIC) for body in steps.values()
        ):
            return []
        created: list[str] = []
        with self.ctx.db.tx() as conn:
            self._switches(conn, run, steps, created)
            self._propagate_skips(conn, run)
            self._maps(conn, run, steps, created)
            self._untils(conn, run, steps, created)
            self._sublines(conn, run, steps)
        return sorted(set(created))

    def _switches(self, conn: Any, run: Any, steps: Mapping[str, Any], created: list[str]) -> None:
        del created
        nodes = self._by_id(run.id)
        for step, body in steps.items():
            if not isinstance(body, dict) or "switch" not in body:
                continue
            stored = nodes.get(step)
            if stored is None or stored.state != "WAITING":
                continue
            source = _source_ids(run, nodes, str(body["switch"].get("on") or ""))
            if source is None:
                continue
            chosen, failed = self._choose(run, step, body, source)
            if failed is not None:
                self._mark(conn, run.id, step, "FAILED", error=failed)
                continue
            if chosen is None:
                continue
            self._mark(conn, run.id, step, "DONE", outputs={})
            targets = _switch_targets(body)
            if chosen not in targets and chosen not in nodes:
                raise FactoryError(
                    "RUN_PLAN_INVALID",
                    "switch 选中的步骤不在计划里",
                    line=run.line,
                    reason="switch-target",
                    step=step,
                )
            for target in targets:
                if target == chosen or target not in nodes:
                    continue
                self._mark(conn, run.id, target, "SKIPPED")

    def _choose(
        self, run: Any, step: str, body: Mapping[str, Any], source: Sequence[str]
    ) -> tuple[str | None, dict[str, Any] | None]:
        bean = self._switch_bean(source)
        env = {"bean": bean}
        cases = list(body["switch"].get("cases") or [])
        for index, case in enumerate(cases):
            subject = f"{run.line}/steps.{step}.switch.cases[{index}]"
            try:
                ok = self.snapshot.compiled(subject).evaluate(env) is True
            except EvalError as exc:
                return None, _switch_error(step, index, exc.code)
            except FactoryError as exc:
                if exc.code != "SUBJECT_UNKNOWN":
                    raise
                return None, _switch_error(step, index, _mrl_code(exc))
            if ok:
                return str(case.get("goto") or ""), None
        return str(body["switch"].get("default") or ""), None

    def _switch_bean(self, source: Sequence[str]) -> Any:
        if len(source) == 1:
            return bean_value(self.ctx.warehouse.get(source[0]))
        return [bean_value(self.ctx.warehouse.get(item)) for item in source]

    def _maps(self, conn: Any, run: Any, steps: Mapping[str, Any], created: list[str]) -> None:
        for step, body in steps.items():
            if not isinstance(body, dict) or not isinstance(body.get("map"), dict):
                continue
            if "station" not in body:
                continue
            nodes = self._by_id(run.id)
            stored = nodes.get(step)
            if stored is None or stored.state in ("DONE", "FAILED", "SKIPPED"):
                self._complete_map(conn, run, step, body, nodes)
                continue
            mapping = body["map"]
            over = mapping.get("over")
            if isinstance(over, list):
                if not _instance_ids(nodes, step, "#"):
                    decision = map_fanout(
                        len(over),
                        mapping.get("max_items"),
                        mapping.get("overflow"),
                        _station_budget(self.snapshot, body),
                    )
                    if decision[0] == "reject":
                        self._mark(
                            conn,
                            run.id,
                            step,
                            "FAILED",
                            error=_overflow(step, len(over), mapping.get("max_items")),
                        )
                self._complete_map(conn, run, step, body, self._by_id(run.id))
                continue
            if not _deps_done(self.snapshot, run.line, step, nodes):
                continue
            ids = _source_ids(run, nodes, str(over))
            if ids is None:
                continue
            decision = map_fanout(
                len(ids),
                mapping.get("max_items"),
                mapping.get("overflow"),
                _station_budget(self.snapshot, body),
            )
            if decision[0] == "invalid":
                raise FactoryError(
                    "RUN_PLAN_INVALID",
                    "动态 map 没有 max_items，无法按内核倍数预留",
                    line=run.line,
                    reason="max-items",
                    step=step,
                )
            if decision[0] == "reject":
                self._mark(
                    conn,
                    run.id,
                    step,
                    "FAILED",
                    error=_overflow(step, len(ids), mapping.get("max_items")),
                )
                continue
            self._ensure_instances(conn, run, step, body, decision[1], "#", created)
            self._complete_map(conn, run, step, body, self._by_id(run.id))

    def _complete_map(
        self, conn: Any, run: Any, step: str, body: Mapping[str, Any], nodes: Mapping[str, Any]
    ) -> None:
        stored = nodes.get(step)
        if stored is None or stored.state != "WAITING":
            return
        instances = _instance_ids(nodes, step, "#")
        if not instances:
            return
        if any(nodes[item].state != "DONE" for item in instances):
            return
        station = self.snapshot.spec(self.snapshot.key("Station", str(body["station"])))
        outputs: dict[str, list[str]] = {}
        for port in station.get("outputs") or {}:
            ids: list[str] = []
            for item in instances:
                ids.extend(list(nodes[item].outputs.get(port) or []))
            outputs[port] = ids
        self._mark(conn, run.id, step, "DONE", outputs=outputs)

    def _untils(self, conn: Any, run: Any, steps: Mapping[str, Any], created: list[str]) -> None:
        for step, body in steps.items():
            if not isinstance(body, dict) or "until" not in body or "station" not in body:
                continue
            nodes = self._by_id(run.id)
            stored = nodes.get(step)
            if stored is None or stored.state != "WAITING":
                continue
            iterations = _instance_ids(nodes, step, "@")
            if not iterations:
                self._ensure_instances(conn, run, step, body, 1, "@", created, start=1)
                continue
            latest = iterations[-1]
            if nodes[latest].state != "DONE":
                continue
            index = int(latest.rsplit("@", 1)[1])
            nxt = f"{step}@{index + 1}"
            if nxt in nodes:
                continue
            if self._until_holds(run, step, body, nodes[latest], index):
                self._mark(conn, run.id, step, "DONE", outputs=dict(nodes[latest].outputs))
                continue
            limit = int(body["until"]["max_iter"])
            if index < limit:
                self._ensure_instances(conn, run, step, body, 1, "@", created, start=index + 1)
                continue
            self._mark(
                conn,
                run.id,
                step,
                "FAILED",
                error={
                    "code": "UNTIL_EXHAUSTED",
                    "message": "达到最大次数时条件仍不成立",
                    "detail": {"step": step, "max_iter": limit},
                },
            )

    def _until_holds(self, run: Any, step: str, body: Mapping[str, Any], iteration: Any, index: int) -> bool:
        station_key = self.snapshot.key("Station", str(body["station"]))
        station = self.snapshot.spec(station_key)
        gate: dict[str, Any] = {"attempt": index}
        out: dict[str, Any] = {}
        for port, ids in iteration.outputs.items():
            beans = [self.ctx.warehouse.get(item) for item in ids]
            declared = (station.get("outputs") or {}).get(port)
            type_expr = declared.get("type") if isinstance(declared, dict) else str(declared or "")
            if str(type_expr).startswith("list<") and str(type_expr).endswith(">"):
                out[port] = [bean_value(item) for item in beans]
            elif len(beans) == 1:
                out[port] = bean_value(beans[0])
            else:
                out[port] = [bean_value(item) for item in beans]
        subject = f"{run.line}/steps.{step}.until"
        try:
            return self.snapshot.compiled(subject).evaluate({"gate": gate, "out": out}) is True
        except EvalError as exc:
            raise FactoryError(
                "RUN_PLAN_INVALID",
                "until 条件求值失败",
                line=run.line,
                reason="until-eval",
                step=step,
                mrl=exc.code,
            ) from exc

    def _sublines(self, conn: Any, run: Any, steps: Mapping[str, Any]) -> None:
        for step, body in steps.items():
            if not isinstance(body, dict) or "subline" not in body:
                continue
            nodes = self._by_id(run.id)
            stored = nodes.get(step)
            if stored is None or stored.state != "WAITING":
                continue
            child = _find_child(self.lines, run.id, step)
            if child is None:
                inputs = _bound_inputs(run, nodes, body.get("in") or {})
                if inputs is None:
                    continue
                if not _deps_done(self.snapshot, run.line, step, nodes):
                    continue
                target = str(body["subline"])
                child_key = self.snapshot.key("Line", target)
                declared = (self.snapshot.spec(child_key).get("budget") or {}).get("cap_usd")
                cap = 0.0 if declared is None else float(declared)
                fresh = self.lines.get(run.id)
                if fresh.spent_usd + fresh.reserved_usd + cap > fresh.budget_cap_usd:
                    self._fail_budget(conn, fresh, step, "子产线预留会超出 run 预算")
                    return
                kwargs: dict[str, Any] = {
                    "trigger": {"on": "subline", "parent": run.id, "step": step},
                    "use_cache": fresh.use_cache,
                    "sink_mode": fresh.sink_mode,
                }
                if declared is not None:
                    kwargs["budget_usd"] = cap
                self.lines.start_run(target, inputs, **kwargs)
                now = utc_iso(self.ctx.clock.now())
                conn.execute(
                    "UPDATE run_nodes SET reserved_usd = ?, updated_at = ? WHERE run_id = ? AND node_id = ?",
                    (cap, now, run.id, step),
                )
                conn.execute(
                    "UPDATE runs SET reserved_usd = reserved_usd + ?, updated_at = ? WHERE id = ?",
                    (cap, now, run.id),
                )
                continue
            if child.state not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                continue
            now = utc_iso(self.ctx.clock.now())
            if child.state == "SUCCEEDED":
                self._mark(conn, run.id, step, "DONE", outputs=dict(child.outputs))
            else:
                self._mark(
                    conn,
                    run.id,
                    step,
                    "FAILED",
                    error={
                        "code": "SUBLINE_FAILED",
                        "message": "子产线没有成功",
                        "detail": {
                            "step": step,
                            "child": child.id,
                            "child_state": child.state.value,
                        },
                    },
                )
            conn.execute(
                """UPDATE runs SET reserved_usd = reserved_usd - ?, spent_usd = spent_usd + ?, updated_at = ?
                   WHERE id = ?""",
                (stored.reserved_usd, child.spent_usd, now, run.id),
            )
            conn.execute(
                "UPDATE run_nodes SET reserved_usd = 0, updated_at = ? WHERE run_id = ? AND node_id = ?",
                (now, run.id, step),
            )

    def _ensure_instances(
        self,
        conn: Any,
        run: Any,
        step: str,
        body: Mapping[str, Any],
        count: int,
        mark: str,
        created: list[str],
        *,
        start: int = 0,
    ) -> None:
        nodes = self._by_id(run.id)
        for offset in range(count):
            index = start + offset
            suffix = f"{mark}{index}"
            station_id = f"{step}{suffix}"
            if station_id in nodes:
                continue
            family = _family(
                self.snapshot, run.line, step, body, suffix, _extra_deps(self.snapshot, run.line, step)
            )
            ordinal = _next_ord(conn, run.id)
            now = utc_iso(self.ctx.clock.now())
            for node_id in family:
                if node_id in nodes:
                    continue
                conn.execute(
                    """INSERT INTO run_nodes
                       (run_id, node_id, ord, state, outputs, reserved_usd, updated_at)
                       VALUES (?, ?, ?, 'WAITING', '{}', 0, ?)""",
                    (run_id_of(run), node_id, ordinal, now),
                )
                ordinal += 1
                created.append(node_id)
                nodes[node_id] = stored_placeholder(node_id)

    def _propagate_skips(self, conn: Any, run: Any) -> None:
        plan = compile_dynamic(self.snapshot, run.line)
        specs = {node.id: node for node in plan.nodes}
        changed = True
        while changed:
            changed = False
            nodes = self._by_id(run.id)
            skipped = {node_id for node_id, node in nodes.items() if node.state == "SKIPPED"}
            for node_id, spec in specs.items():
                stored = nodes.get(node_id)
                if stored is None or stored.state != "WAITING" or not spec.deps:
                    continue
                if all(dep in skipped for dep in spec.deps):
                    self._mark(conn, run.id, node_id, "SKIPPED")
                    changed = True
            nodes = self._by_id(run.id)
            skipped = {node_id for node_id, node in nodes.items() if node.state == "SKIPPED"}
            for node_id, stored in nodes.items():
                parsed = _parse_instance(node_id)
                if parsed is None or stored.state != "WAITING" or parsed.step not in skipped:
                    continue
                self._mark(conn, run.id, node_id, "SKIPPED")
                changed = True

    def _mark(
        self,
        conn: Any,
        run_id: str,
        node_id: str,
        state: str,
        *,
        outputs: Mapping[str, Sequence[str]] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        now = utc_iso(self.ctx.clock.now())
        if state == "DONE":
            cursor = conn.execute(
                """UPDATE run_nodes SET state = 'DONE', outputs = ?, error = NULL, updated_at = ?
                   WHERE run_id = ? AND node_id = ? AND state = 'WAITING'""",
                (
                    canonical_json({key: list(value) for key, value in (outputs or {}).items()}),
                    now,
                    run_id,
                    node_id,
                ),
            )
        elif state == "FAILED":
            cursor = conn.execute(
                """UPDATE run_nodes SET state = 'FAILED', error = ?, updated_at = ?
                   WHERE run_id = ? AND node_id = ? AND state = 'WAITING'""",
                (canonical_json(dict(error or {})), now, run_id, node_id),
            )
        else:
            cursor = conn.execute(
                """UPDATE run_nodes SET state = 'SKIPPED', error = NULL, updated_at = ?
                   WHERE run_id = ? AND node_id = ? AND state = 'WAITING'""",
                (now, run_id, node_id),
            )
        if cursor.rowcount:
            self._event(conn, run_id, "run.node", {"node": node_id, "from": "WAITING", "to": state})

    def _fail_budget(self, conn: Any, run: Any, node: str, message: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        error = {"code": "RUN_BUDGET_EXHAUSTED", "message": message, "node": node}
        conn.execute(
            "UPDATE runs SET state = 'FAILING', error = ?, updated_at = ? WHERE id = ?",
            (canonical_json(error), now, run.id),
        )
        previous = run.state.value if hasattr(run.state, "value") else str(run.state)
        self._event(conn, run.id, "run.state", {"from": previous, "to": "FAILING", "reason": "budget"})

    def _event(self, conn: Any, run_id: str, kind: str, data: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data, run_id) VALUES (?, ?, ?, NULL, ?, ?)",
            (
                self.ctx.ids.new("ev"),
                utc_iso(self.ctx.clock.now()),
                kind,
                canonical_json(dict(data)),
                run_id,
            ),
        )

    def _by_id(self, run_id: str) -> dict[str, Any]:
        return {node.node_id: node for node in self.lines.nodes(run_id)}


def node_spec(snapshot: Any, line_key: str, node_id: str) -> PlanNode | None:
    parsed = _parse_instance(node_id)
    if parsed is None:
        return None
    key = snapshot.key("Line", line_key)
    body = (snapshot.spec(key).get("steps") or {}).get(parsed.step)
    if not isinstance(body, dict) or "station" not in body:
        return None
    return _family(
        snapshot, key, parsed.step, body, parsed.suffix, _extra_deps(snapshot, key, parsed.step)
    ).get(node_id)


def parallel_limit(snapshot: Any, line_key: str, node_id: str) -> int | None:
    parsed = _parse_instance(node_id)
    if parsed is None or parsed.converter is not None or not parsed.suffix.startswith("#"):
        return None
    key = snapshot.key("Line", line_key)
    body = (snapshot.spec(key).get("steps") or {}).get(parsed.step) or {}
    mapping = body.get("map") if isinstance(body, dict) else None
    if not isinstance(mapping, dict):
        return None
    return int(mapping.get("max_parallel", 1))


def until_upfront(snapshot: Any, line_key: str, node_id: str, nodes: Sequence[Any]) -> float | None:
    parsed = _parse_instance(node_id)
    if parsed is None or parsed.converter is not None or not parsed.suffix.startswith("@"):
        return None
    for node in nodes:
        other = _parse_instance(node.node_id)
        if (
            other is not None
            and other.step == parsed.step
            and other.converter is None
            and other.suffix.startswith("@")
            and node.job_id
        ):
            return None
    key = snapshot.key("Line", line_key)
    body = (snapshot.spec(key).get("steps") or {}).get(parsed.step) or {}
    if not isinstance(body, dict) or "until" not in body:
        return None
    return _station_budget(snapshot, body) * int(body["until"]["max_iter"])


def template_extra(
    snapshot: Any, warehouse: Any, run: Any, node_id: str, nodes: Sequence[Any]
) -> dict[str, Any]:
    parsed = _parse_instance(node_id)
    if parsed is None or parsed.converter is not None or not parsed.suffix.startswith("#"):
        return {}
    key = snapshot.key("Line", run.line)
    body = (snapshot.spec(key).get("steps") or {}).get(parsed.step) or {}
    mapping = body.get("map") if isinstance(body, dict) else None
    if not isinstance(mapping, dict) or "as" not in mapping:
        return {}
    over = mapping.get("over")
    index = int(parsed.suffix[1:])
    if isinstance(over, list):
        if index >= len(over):
            return {}
        return {str(mapping["as"]): over[index]}
    bean_id = item_bean_id(snapshot, run, node_id, nodes)
    if not bean_id:
        return {}
    return {str(mapping["as"]): bean_value(warehouse.get(bean_id))}


def item_bean_id(snapshot: Any, run: Any, node_id: str, nodes: Sequence[Any]) -> str | None:
    parsed = _parse_instance(node_id)
    if parsed is None or not parsed.suffix.startswith("#"):
        return None
    key = snapshot.key("Line", run.line)
    body = (snapshot.spec(key).get("steps") or {}).get(parsed.step) or {}
    mapping = body.get("map") if isinstance(body, dict) else None
    if not isinstance(mapping, dict) or isinstance(mapping.get("over"), list):
        return None
    ids = _source_ids(run, {node.node_id: node for node in nodes}, str(mapping.get("over") or ""))
    index = int(parsed.suffix[1:])
    if ids is None or index >= len(ids):
        return None
    return ids[index]


def map_fanout(length: int, max_items: Any, overflow: Any, budget: float) -> tuple[str, int]:
    """返回 (expand|reject|invalid, 展开个数)。上限只来自工位写出的 max_items。"""
    if max_items is None:
        if budget > 0:
            return ("invalid", 0)
        return ("expand", length)
    limit = int(max_items)
    if length <= limit:
        return ("expand", length)
    if overflow == "truncate":
        return ("expand", limit)
    return ("reject", 0)


def _compile_literal_map(
    snapshot: Any,
    key: str,
    step: str,
    body: Mapping[str, Any],
    extra: tuple[str, ...],
    nodes: dict[str, PlanNode],
) -> None:
    mapping = body["map"]
    over = list(mapping["over"])
    limit = mapping.get("max_items")
    overflow = mapping.get("overflow")
    decision = map_fanout(len(over), limit, overflow, _station_budget(snapshot, body))
    if decision[0] == "reject":
        nodes[step] = _control(step, "map", extra, ())
        return
    if decision[0] == "invalid":
        raise FactoryError(
            "RUN_PLAN_INVALID",
            "字面 map 没有 max_items，无法按内核倍数预留",
            line=key,
            reason="max-items",
            step=step,
        )
    count = decision[1]
    if not (isinstance(limit, int) and len(over) > limit):
        _check_multiplicity(snapshot, key, step, count)
    for index in range(count):
        nodes.update(_family(snapshot, key, step, body, f"#{index}", extra))
    nodes[step] = _control(step, "aggregate", tuple(f"{step}#{index}" for index in range(count)), ())


def _family(
    snapshot: Any,
    key: str,
    step: str,
    body: Mapping[str, Any],
    suffix: str,
    extra: tuple[str, ...],
) -> dict[str, PlanNode]:
    station_key = snapshot.key("Station", str(body["station"]))
    station = snapshot.spec(station_key)
    params = copy.deepcopy(body.get("params") or {})
    templated = tuple(
        sorted(name for name, value in params.items() if isinstance(value, str) and "{{" in value)
    )
    nodes: dict[str, PlanNode] = {}
    bindings: list[Binding] = []
    for port, expr in (body.get("in") or {}).items():
        origin = _origin(key, step, port, expr)
        if origin.source == "item":
            previous = origin
        else:
            previous = origin
        try:
            resolution = _resolution(snapshot, key, step, port)
        except FactoryError:
            if origin.source == "item":
                raise
            resolution = {"status": "direct"}
        status = resolution.get("status")
        if status != "convert":
            if status != "direct" and not _line_has_error(snapshot, key):
                raise FactoryError(
                    "RUN_PLAN_INVALID",
                    "边的解析状态不能执行",
                    line=key,
                    reason="resolution",
                    step=step,
                    port=port,
                    status=status,
                )
            bindings.append(origin)
            continue
        chain = list(resolution.get("chain") or [])
        last_id = ""
        incoming: Binding = previous
        for converter in chain:
            node_id = f"{step}.in.{port}~{converter}{suffix}"
            conv_key = snapshot.key("Station", converter)
            conv = snapshot.spec(conv_key)
            _require_defaults(key, conv_key, conv)
            nodes[node_id] = _station_node(
                node_id,
                "converter",
                step,
                conv_key,
                conv,
                {},
                (),
                (Binding("in", incoming.source, incoming.node, incoming.out_port),),
                extra,
                True,
                copy.deepcopy(resolution),
            )
            last_id = node_id
            incoming = Binding("in", "node", node_id, "out")
        bindings.append(Binding(port, "node", last_id, "out"))
    nodes[f"{step}{suffix}"] = _station_node(
        f"{step}{suffix}",
        "station",
        step,
        station_key,
        station,
        params,
        templated,
        tuple(bindings),
        extra,
        False,
        None,
    )
    return nodes


def _subline_node(
    snapshot: Any, key: str, step: str, body: Mapping[str, Any], extra: tuple[str, ...]
) -> PlanNode:
    deps = set(extra)
    for expr in (body.get("in") or {}).values():
        text = _binding_text(expr)
        if "." in text and not text.startswith("$"):
            deps.add(text.split(".", 1)[0])
    return _control(step, "subline", tuple(sorted(deps)), ())


def _control(step: str, kind: str, deps: tuple[str, ...], inputs: tuple[Binding, ...]) -> PlanNode:
    return PlanNode(
        step,
        kind,  # type: ignore[arg-type]
        step,
        "",
        "",
        {},
        (),
        inputs,
        tuple(sorted(set(deps))),
        0.0,
        False,
        None,
    )


def _station_node(
    node_id: str,
    kind: str,
    step: str,
    station: str,
    spec: Mapping[str, Any],
    params: dict[str, Any],
    templated: tuple[str, ...],
    inputs: tuple[Binding, ...],
    extra: tuple[str, ...],
    implicit: bool,
    resolution: dict[str, Any] | None,
) -> PlanNode:
    deps = {item.node for item in inputs if item.source == "node" and item.node}
    deps.update(extra)
    return PlanNode(
        node_id,
        kind,  # type: ignore[arg-type]
        step,
        station,
        str(spec.get("role") or ""),
        params,
        templated,
        tuple(sorted(inputs, key=lambda item: item.port)),
        tuple(sorted(deps)),
        _budget(spec),
        implicit,
        resolution,
    )


def _origin(line: str, step: str, port: str, expr: Any) -> Binding:
    text = _binding_text(expr)
    if text == "$item":
        return Binding(port, "item", "", "")
    if text.startswith("$inputs."):
        return Binding(port, "input", "", text[len("$inputs.") :])
    if "." in text:
        node, out_port = text.split(".", 1)
        return Binding(port, "node", node, out_port)
    raise FactoryError("RUN_PLAN_INVALID", "无法解析绑定", line=line, reason="binding", step=step, port=port)


def _binding_text(expr: Any) -> str:
    if isinstance(expr, dict):
        return str(expr.get("from") or "")
    return str(expr)


def _switch_deps(steps: Mapping[str, Any], step: str) -> tuple[str, ...]:
    found: list[str] = []
    for name, body in steps.items():
        if not isinstance(body, dict) or "switch" not in body:
            continue
        if step in _switch_targets(body):
            found.append(name)
    return tuple(sorted(set(found)))


def _switch_targets(body: Mapping[str, Any]) -> list[str]:
    switch = body.get("switch") or {}
    targets = [str(case.get("goto")) for case in switch.get("cases") or [] if isinstance(case, dict)]
    if switch.get("default") is not None:
        targets.append(str(switch["default"]))
    return targets


def _switch_on_dep(body: Mapping[str, Any]) -> tuple[str, ...]:
    return _over_dep((body.get("switch") or {}).get("on"))


def _over_dep(over: Any) -> tuple[str, ...]:
    if isinstance(over, str) and "." in over and not over.startswith("$"):
        return (over.split(".", 1)[0],)
    return ()


def _extra_deps(snapshot: Any, line_key: str, step: str) -> tuple[str, ...]:
    key = snapshot.key("Line", line_key)
    steps = snapshot.spec(key).get("steps") or {}
    body = steps.get(step) or {}
    mapping = body.get("map") if isinstance(body, dict) else None
    over = mapping.get("over") if isinstance(mapping, dict) else None
    return tuple(sorted(set(_switch_deps(steps, step) + _over_dep(over))))


def _deps_done(snapshot: Any, line_key: str, step: str, nodes: Mapping[str, Any]) -> bool:
    key = snapshot.key("Line", line_key)
    plan_nodes = {node.id: node for node in compile_dynamic(snapshot, key).nodes}
    spec = plan_nodes.get(step)
    if spec is None:
        return True
    return all(nodes.get(dep) is not None and nodes[dep].state == "DONE" for dep in spec.deps)


def _source_ids(run: Any, nodes: Mapping[str, Any], text: str) -> list[str] | None:
    if text.startswith("$inputs."):
        return list(run.inputs.get(text[len("$inputs.") :]) or [])
    if text == "$item" or "." not in text:
        return None
    step, port = text.split(".", 1)
    node = nodes.get(step)
    if node is None or node.state != "DONE":
        return None
    return list(node.outputs.get(port) or [])


def _bound_inputs(
    run: Any, nodes: Mapping[str, Any], bindings: Mapping[str, Any]
) -> dict[str, list[str]] | None:
    found: dict[str, list[str]] = {}
    for port, expr in bindings.items():
        ids = _source_ids(run, nodes, _binding_text(expr))
        if ids is None:
            return None
        found[str(port)] = ids
    return found


def _parse_instance(node_id: str) -> _Instance | None:
    match = _CONVERTER_INSTANCE.fullmatch(node_id)
    if match is not None:
        return _Instance(match.group(1), match.group(4), match.group(2), match.group(3))
    match = _STATION_INSTANCE.fullmatch(node_id)
    if match is not None:
        return _Instance(match.group(1), match.group(2))
    return None


def _instance_ids(nodes: Mapping[str, Any], step: str, mark: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for node_id in nodes:
        parsed = _parse_instance(node_id)
        if (
            parsed is None
            or parsed.converter is not None
            or parsed.step != step
            or not parsed.suffix.startswith(mark)
        ):
            continue
        found.append((int(parsed.suffix[1:]), node_id))
    found.sort()
    return [node_id for _index, node_id in found]


def _station_budget(snapshot: Any, body: Mapping[str, Any]) -> float:
    if "station" not in body:
        return 0.0
    return _budget(snapshot.spec(snapshot.key("Station", str(body["station"]))))


def _line_has_error(snapshot: Any, key: str) -> bool:
    return any(
        diag.severity == "error" and str(diag.subject).startswith(key) for diag in snapshot.result.diags
    )


def _check_multiplicity(snapshot: Any, key: str, step: str, count: int) -> None:
    rows = [
        int(number)
        for line, name, number in snapshot.result.model.rows("multiplicity")
        if line == key and name == step
    ]
    if rows and rows[0] != count:
        raise FactoryError(
            "RUN_PLAN_INVALID",
            "实例个数与内核倍数不一致",
            line=key,
            reason="multiplicity",
            step=step,
        )


def _overflow(step: str, length: int, max_items: Any) -> dict[str, Any]:
    return {
        "code": "DYNAMIC_OVERFLOW",
        "message": "动态列表超过声明的 max_items",
        "detail": {"step": step, "length": length, "max_items": max_items},
    }


def _switch_error(step: str, index: int, mrl: str) -> dict[str, Any]:
    return {
        "code": "SWITCH_EVAL",
        "message": "switch 条件求值失败",
        "detail": {"step": step, "index": index, "mrl": mrl},
    }


def _mrl_code(exc: FactoryError) -> str:
    if exc.code.startswith("MRL-"):
        return exc.code
    for token in str(exc).replace(":", " ").split():
        if token.startswith("MRL-"):
            return token
    return exc.code


def _find_child(lines: Any, parent_id: str, step: str) -> Any:
    for run in lines.list():
        trig = run.trigger or {}
        if trig.get("on") == "subline" and trig.get("parent") == parent_id and trig.get("step") == step:
            return run
    return None


def _next_ord(conn: Any, run_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(ord), -1) + 1 AS n FROM run_nodes WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["n"])


def run_id_of(run: Any) -> str:
    return str(run.id)


def stored_placeholder(node_id: str) -> Any:
    return type("Node", (), {"node_id": node_id, "state": "WAITING", "outputs": {}})()


def _node(node_id: str, kind: str, step: str, body: dict[str, Any], snapshot: Any) -> PlanNode:
    station = ""
    if body.get("station"):
        station = snapshot.key("Station", str(body["station"]))
    return PlanNode(
        id=node_id,
        kind=kind,  # type: ignore[arg-type]
        step=step,
        station=station,
        role="transform",
        params={},
        templated=tuple(),
        inputs=tuple(),
        deps=tuple(),
        budget_usd=0.0,
        implicit=False,
        resolution=None,
    )
