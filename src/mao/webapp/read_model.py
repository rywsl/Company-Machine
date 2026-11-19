"""Read models for the local M18 web application."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mao.commands.common import governance_registry
from mao.errors import FactoryError
from mao.status import factory_status, tail_events
from mao.talk import list_messages

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization|cookie)")
_SECRET_VALUE = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+")
_PATH_VALUE = re.compile(r"(?i)(?:[A-Z]:\\|/)(?:[^\s/\\]+[/\\])*[^\s/\\]+")
_ACTIVE_RUNS = {"PENDING", "RUNNING", "FAILING", "CANCELLING"}


class WebReadModel:
    """Project existing control-plane facts into the M18 UI shape."""

    def __init__(self, ctx: Any, snapshot: Any | None = None) -> None:
        self.ctx = ctx
        self.snapshot = snapshot

    def bootstrap(self) -> dict[str, Any]:
        status = self.status()
        factory_graph = self.factory_graph()
        first_workarea = factory_graph["nodes"][0]["id"] if factory_graph["nodes"] else None
        workarea_graph = self.workarea_graph(first_workarea) if first_workarea else None
        return {
            "version": "m18-web-1",
            "connection": self._connection(status),
            "status": status,
            "graph": {"factory": factory_graph, "workarea": workarea_graph},
            "assets": self.assets(limit=8),
            "logs": self.logs(limit=24),
            "talk": self.talk(limit=24),
            "harnesses": self.harnesses(),
            "foreman": self.foreman(),
            "secrets": self.secret_resources(limit=50),
            "constitution": self.constitution(),
        }

    def status(self) -> dict[str, Any]:
        epochs, pipeline = self._governance()
        return factory_status(self.ctx, epochs=epochs, pipeline=pipeline)

    def factory_graph(self) -> dict[str, Any]:
        snapshot = self._require_snapshot()
        lines = []
        for key in snapshot.keys("Line"):
            doc = snapshot.doc(key)
            spec = doc.get("spec") or {}
            metadata = doc.get("metadata") or {}
            line_name = str(metadata.get("name") or key.split(":", 1)[-1].rsplit("@", 1)[0])
            runs = self._runs_for_line(key)
            status = self._line_status(runs)
            lines.append(
                {
                    "id": key,
                    "type": "workarea",
                    "group": "factory",
                    "label": line_name,
                    "position": {"x": 180 + (len(lines) % 3) * 300, "y": 170 + (len(lines) // 3) * 210},
                    "status": status,
                    "metadata": {
                        "line": key,
                        "brief": self._line_brief(spec),
                        "active": sum(1 for run in runs if run["state"] in _ACTIVE_RUNS),
                        "runs": len(runs),
                        "latest": runs[-1]["id"] if runs else None,
                        "exception": sum(1 for run in runs if run["state"] in {"FAILED", "FAILING"}),
                    },
                }
            )
        return {
            "scope": "factory",
            "id": "factory",
            "title": "工厂",
            "nodes": lines,
            "edges": self._factory_edges(lines),
        }

    def workarea_graph(self, workarea: str | None) -> dict[str, Any] | None:
        if not workarea:
            return None
        snapshot = self._require_snapshot()
        line_key = self._resolve_line_key(workarea)
        spec = snapshot.spec(line_key)
        metadata = snapshot.doc(line_key).get("metadata") or {}
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        step_nodes: dict[str, tuple[str, str]] = {}
        steps = spec.get("steps") if isinstance(spec, dict) else None
        if not isinstance(steps, dict):
            steps = {}
        for index, (step_name, raw_step) in enumerate(steps.items()):
            if not isinstance(raw_step, dict) or not raw_step.get("station"):
                continue
            station_ref = str(raw_step["station"])
            station_key = snapshot.key("Station", station_ref)
            station = snapshot.spec(station_key)
            operator_ref = station.get("operator") or {}
            if isinstance(operator_ref, str):
                runtime_ref = operator_ref
                model = None
            else:
                runtime_ref = operator_ref.get("runtime")
                model = operator_ref.get("model")
            machine_ref = station.get("machine")
            operator_id = f"{line_key}:{step_name}:operator"
            machine_id = f"{line_key}:{step_name}:machine"
            step_nodes[str(step_name)] = (operator_id, machine_id)
            operator_meta = {
                "step": str(step_name),
                "station": station_key,
                "harness": self._short_ref(runtime_ref),
                "model": self._safe_scalar(model),
                "project": str(station.get("role") or "project operator"),
                "inputs": self._port_names(station.get("inputs")),
                "outputs": self._port_names(station.get("outputs")),
                "resource_request": self._operator_request(line_key, str(step_name)),
            }
            machine_meta = self._machine_metadata(snapshot, machine_ref, station_key)
            node_status = self._step_status(line_key, str(step_name))
            x = 190 + (index % 3) * 310
            y = 150 + (index // 3) * 205
            nodes.append(
                {
                    "id": operator_id,
                    "type": "operator",
                    "group": line_key,
                    "label": f"{step_name} Operator",
                    "position": {"x": x, "y": y},
                    "status": node_status,
                    "metadata": operator_meta,
                }
            )
            nodes.append(
                {
                    "id": machine_id,
                    "type": "machine",
                    "group": line_key,
                    "label": self._machine_label(snapshot, machine_ref),
                    "position": {"x": x + 150, "y": y},
                    "status": node_status,
                    "metadata": machine_meta,
                }
            )
            edges.append(
                {
                    "id": f"{operator_id}:to:{machine_id}",
                    "source": operator_id,
                    "target": machine_id,
                    "kind": "material",
                    "condition": "Operator 交付项目执行",
                    "status": node_status,
                    "metadata": {"station": station_key},
                }
            )
        for step_name, raw_step in steps.items():
            if step_name not in step_nodes or not isinstance(raw_step, dict):
                continue
            target_operator, _ = step_nodes[step_name]
            for source in self._binding_sources(raw_step.get("in")):
                source_step = source.split(".", 1)[0]
                source_nodes = step_nodes.get(source_step)
                if source_nodes is None:
                    continue
                _, source_machine = source_nodes
                condition = self._condition_summary(raw_step)
                edges.append(
                    {
                        "id": f"{source_machine}:to:{target_operator}:{source}",
                        "source": source_machine,
                        "target": target_operator,
                        "kind": "condition" if condition else "material",
                        "condition": condition or f"输入绑定 {source}",
                        "status": "unknown",
                        "metadata": {"binding": source},
                    }
                )
        return {
            "scope": "workarea",
            "id": line_key,
            "title": str(metadata.get("name") or line_key),
            "nodes": nodes,
            "edges": edges,
        }

    def assets(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        try:
            found = self.ctx.warehouse.list(sealed_only=False, include_expired=True)
        except Exception:
            return []
        result = []
        for bean in found[-limit:]:
            result.append(
                {
                    "id": bean.id,
                    "type": bean.type,
                    "sealed": bool(bean.sealed),
                    "expired": bean.expired_at is not None,
                    "created_at": bean.created_at,
                    "meta": self._safe_obj(bean.meta),
                    "source": self._asset_source(bean.id),
                }
            )
        return result

    def logs(self, *, limit: int = 100, since: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        try:
            return tail_events(self.ctx, since=since, limit=limit)
        except FactoryError:
            return []

    def talk(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        result = []
        for item in list_messages(self.ctx)[-limit:]:
            body = item.to_json()
            body["body"] = self.safe_text(body.get("body"), 1200)
            body["card"] = self._safe_obj(body.get("card"))
            body.pop("mailbox_id", None)
            result.append(body)
        return result

    def harnesses(self) -> list[dict[str, Any]]:
        """Return discovered Harness adapters without closing the adapter set.

        Registry OperatorRuntime records remain the source of truth for installed
        runtimes. A local JSON adapter directory is also accepted so a local
        deployment can add a Harness without changing this Web App.
        """
        found: dict[str, dict[str, Any]] = {}
        snapshot = self.snapshot
        if snapshot is not None:
            for key in snapshot.keys("OperatorRuntime"):
                doc = snapshot.doc(key)
                metadata = doc.get("metadata") or {}
                spec = doc.get("spec") or {}
                invoke = spec.get("invoke") or {}
                kind = str(spec.get("kind") or "")
                found[key] = {
                    "id": key,
                    "name": str(metadata.get("name") or key),
                    "version": metadata.get("version"),
                    "kind": kind,
                    "source": "registry",
                    "roles": ["operator", "foreman"] if kind == "agent-cli" else ["operator"],
                    "available": bool(invoke or kind == "script"),
                    "capabilities": {
                        "continuous_session": bool((invoke or {}).get("resume")),
                        "tool_calls": bool((invoke or {}).get("mcp")),
                        "recovery": bool((invoke or {}).get("resume")),
                    },
                }
        for candidate in self._local_harnesses():
            found[candidate["id"]] = candidate
        return sorted(found.values(), key=lambda item: (str(item.get("name", "")), str(item["id"])))

    def foreman(self) -> dict[str, Any]:
        """Expose the current Foreman Harness without exposing local credentials."""
        try:
            from mao.supervisor import SupervisorConfig

            config = SupervisorConfig.load(Path(self.ctx.root))
        except (FactoryError, OSError, ValueError, TypeError):
            return {"configured": False, "runtime": None, "model": None, "mode": None}
        runtime = str(config.runtime)
        harness = next(
            (item for item in self.harnesses() if item["id"] == runtime or item["name"] == runtime), None
        )
        return {
            "configured": True,
            "runtime": runtime,
            "model": self._safe_scalar(config.model),
            "mode": config.mode,
            "harness": harness,
        }

    def secret_resources(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Project secret names and bindings while keeping every value out of the UI."""
        limit = max(1, min(int(limit), 200))
        configured: dict[str, str] = {}
        try:
            from mao.egress import SecretStore

            store = SecretStore(Path(self.ctx.root))
            configured = {name: store.secret_class(name) for name in store.names()}
        except (FactoryError, OSError, TypeError, ValueError):
            configured = {}

        bindings: dict[str, dict[str, set[str]]] = {}
        snapshot = self.snapshot
        if snapshot is not None:
            for key in snapshot.keys("Machine"):
                try:
                    spec = snapshot.spec(key)
                except FactoryError:
                    continue
                needs = spec.get("needs") or {}
                names = set(str(item) for item in needs.get("secrets") or [])
                checks = needs.get("secret_checks") or {}
                if isinstance(checks, dict):
                    names.update(str(item) for item in checks)
                for name in names:
                    item = bindings.setdefault(name, {"machines": set(), "stations": set()})
                    item["machines"].add(str(key))
            for key in snapshot.keys("Station"):
                try:
                    station = snapshot.spec(key)
                except FactoryError:
                    continue
                machine = station.get("machine")
                if not machine:
                    continue
                machine_name = str(machine)
                machine_key = None
                try:
                    machine_key = snapshot.key("Machine", machine_name)
                except FactoryError:
                    pass
                names = set()
                if machine_key and machine_key in snapshot.keys("Machine"):
                    try:
                        needs = snapshot.spec(machine_key).get("needs") or {}
                        names.update(str(item) for item in needs.get("secrets") or [])
                        checks = needs.get("secret_checks") or {}
                        if isinstance(checks, dict):
                            names.update(str(item) for item in checks)
                    except FactoryError:
                        pass
                for name in names:
                    bindings.setdefault(name, {"machines": set(), "stations": set()})["stations"].add(
                        str(key)
                    )

        names = sorted(set(configured) | set(bindings))[:limit]
        last_used = self._secret_last_used(names)
        requests = self._secret_requests(names)
        result = []
        for name in names:
            binding = bindings.get(name, {"machines": set(), "stations": set()})
            kind = configured.get(name) or "unconfigured"
            result.append(
                {
                    "id": name,
                    "key": name,
                    "masked_value": "••••••••" if name in configured else "未配置",
                    "configured": name in configured,
                    "usage_scope": kind,
                    "bound_objects": sorted(binding["machines"] | binding["stations"]),
                    "last_used": last_used.get(name),
                    "expires_at": None,
                    "expired": False,
                    "status": "configured" if name in configured else "missing",
                    "request": requests.get(name),
                    "agent_generated_checks": [
                        "存在性",
                        "权限",
                        "与当前 Machine 匹配",
                        "过期状态",
                        "当前 Agent 使用范围",
                    ],
                }
            )
        return result

    def _local_harnesses(self) -> list[dict[str, Any]]:
        roots = (Path(self.ctx.root) / ".factory" / "harnesses", Path(self.ctx.root) / "harnesses")
        result = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.json")):
                try:
                    body = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(body, list):
                    candidates = body
                elif isinstance(body, dict) and isinstance(body.get("harnesses"), list):
                    candidates = body["harnesses"]
                else:
                    candidates = [body]
                for item in candidates:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    identifier = str(item["id"])
                    roles = item.get("roles") or ["operator", "foreman"]
                    if isinstance(roles, str):
                        roles = [roles]
                    result.append(
                        {
                            "id": identifier,
                            "name": str(item.get("name") or identifier),
                            "version": self._safe_scalar(item.get("version")),
                            "kind": str(item.get("kind") or "custom"),
                            "source": "local",
                            "roles": [str(role) for role in roles],
                            "available": bool(item.get("available", True)),
                            "capabilities": self._safe_obj(item.get("capabilities") or {}),
                        }
                    )
        return result

    def _secret_last_used(self, names: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        if not names:
            return found
        try:
            rows = self.ctx.db.query("SELECT plan, at FROM egress_plans ORDER BY at DESC LIMIT 200")
        except Exception:
            return found
        for row in rows:
            try:
                plan = json.loads(row["plan"])
            except (TypeError, json.JSONDecodeError):
                continue
            for name in names:
                if name not in found and self._contains_value(plan, name):
                    found[name] = str(row["at"])
        return found

    def _secret_requests(self, names: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if not names:
            return found
        try:
            rows = self.ctx.db.query(
                "SELECT id, signal, subject, payload, status, at FROM wakes ORDER BY at DESC LIMIT 200"
            )
        except Exception:
            return found
        for row in rows:
            payload = row["payload"]
            try:
                payload = json.loads(payload) if isinstance(payload, str) else payload
            except json.JSONDecodeError:
                payload = {}
            for name in names:
                if name in found or (
                    name not in str(row["subject"]) and not self._contains_value(payload, name)
                ):
                    continue
                found[name] = {
                    "id": str(row["id"]),
                    "signal": str(row["signal"]),
                    "status": str(row["status"]),
                    "at": str(row["at"]),
                }
        return found

    @staticmethod
    def _contains_value(value: Any, wanted: str) -> bool:
        if isinstance(value, dict):
            return any(
                WebReadModel._contains_value(key, wanted) or WebReadModel._contains_value(item, wanted)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(WebReadModel._contains_value(item, wanted) for item in value)
        return str(value) == wanted

    def constitution(self) -> dict[str, Any]:
        epochs, pipeline = self._governance()
        if epochs is None:
            return {"available": False, "status": "unconfigured", "version": None, "recent_change": None}
        pointers = epochs.pointers()
        current = pointers.get("active") or epochs.current()
        safe = pipeline.safe_mode() if pipeline is not None else None
        return {
            "available": True,
            "version": current,
            "active": pointers.get("active"),
            "candidate": pointers.get("candidate"),
            "previous": pointers.get("previous"),
            "status": "conflict" if safe and safe.get("on") else "effective",
            "recent_change": self._recent_change(),
            "conflict": safe,
        }

    def _require_snapshot(self) -> Any:
        if self.snapshot is None:
            raise FactoryError("REGISTRY_NOT_CONFIGURED", "Web App 没有可用 Registry")
        return self.snapshot

    def _resolve_line_key(self, value: str) -> str:
        snapshot = self._require_snapshot()
        if value in snapshot.keys("Line"):
            return value
        matches = [key for key in snapshot.keys("Line") if key.split(":", 1)[-1].split("@", 1)[0] == value]
        if len(matches) != 1:
            raise FactoryError("LINE_NOT_FOUND", "找不到厂区", workarea=value)
        return matches[0]

    def _governance(self) -> tuple[Any | None, Any | None]:
        if governance_registry(self.ctx) is None:
            return None, None
        from mao.governance.changesets import ChangePipeline
        from mao.governance.epochs import EpochStore

        epochs = EpochStore(self.ctx)
        return epochs, ChangePipeline(self.ctx, epochs)

    def _connection(self, status: dict[str, Any]) -> dict[str, Any]:
        if status["serve"]["online"] and status["supervisor"]["online"]:
            return {"state": "connected", "message": "本地自治服务在线"}
        if status["serve"]["online"]:
            return {"state": "degraded", "message": "服务在线，厂长状态待恢复"}
        return {"state": "exception", "message": "本地 serve 未运行"}

    def _runs_for_line(self, line: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.query(
            "SELECT id, line, state, created_at, updated_at, error FROM runs WHERE line IN (?, ?) ORDER BY created_at",
            (line, line.split(":", 1)[-1]),
        )
        return [
            {
                "id": str(row["id"]),
                "line": str(row["line"]),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "error": self._safe_obj(json.loads(row["error"]) if row["error"] else None),
            }
            for row in rows
        ]

    def _line_status(self, runs: list[dict[str, Any]]) -> str:
        if not runs:
            return "unknown"
        state = runs[-1]["state"]
        if state in _ACTIVE_RUNS:
            return "running"
        if state in {"FAILED", "FAILING"}:
            return "exception"
        if state == "SUCCEEDED":
            return "ready"
        return state.lower()

    def _step_status(self, line: str, step: str) -> str:
        runs = self._runs_for_line(line)
        if not runs:
            return "unknown"
        latest = runs[-1]
        rows = self.ctx.db.query(
            "SELECT state FROM run_nodes WHERE run_id = ? AND node_id = ?",
            (latest["id"], step),
        )
        if not rows:
            return self._line_status(runs)
        state = str(rows[0]["state"])
        return {
            "WAITING": "waiting",
            "SUBMITTED": "running",
            "DONE": "ready",
            "FAILED": "exception",
            "SKIPPED": "skipped",
        }.get(state, "unknown")

    def _factory_edges(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot = self._require_snapshot()
        inputs: dict[str, set[str]] = {}
        outputs: dict[str, set[str]] = {}
        for node in nodes:
            spec = snapshot.spec(node["id"])
            inputs[node["id"]] = self._port_types(spec.get("interface", {}).get("inputs"))
            outputs[node["id"]] = self._line_outputs(snapshot, spec)
        edges = []
        for source in nodes:
            for target in nodes:
                if source["id"] == target["id"]:
                    continue
                shared = sorted(outputs[source["id"]].intersection(inputs[target["id"]]))
                if not shared:
                    continue
                edges.append(
                    {
                        "id": f"{source['id']}:to:{target['id']}",
                        "source": source["id"],
                        "target": target["id"],
                        "kind": "material",
                        "condition": f"类型为 {shared[0]}",
                        "status": "unknown",
                        "metadata": {"types": shared},
                    }
                )
        return edges

    def _line_outputs(self, snapshot: Any, line_spec: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        steps = line_spec.get("steps") if isinstance(line_spec, dict) else None
        if not isinstance(steps, dict):
            return result
        for raw in steps.values():
            if not isinstance(raw, dict) or not raw.get("station"):
                continue
            try:
                station = snapshot.spec(snapshot.key("Station", str(raw["station"])))
            except FactoryError:
                continue
            for port in (station.get("outputs") or {}).values():
                if isinstance(port, dict) and port.get("type"):
                    result.add(str(port["type"]))
        return result

    def _line_brief(self, spec: dict[str, Any]) -> str:
        steps = spec.get("steps") if isinstance(spec, dict) else None
        return f"{len(steps) if isinstance(steps, dict) else 0} 个执行步骤"

    def _machine_metadata(self, snapshot: Any, machine_ref: Any, station_key: str) -> dict[str, Any]:
        if not machine_ref:
            return {"station": station_key, "project": "未声明 Machine", "delivery": "由 Foreman 判断"}
        key = snapshot.key("Machine", str(machine_ref))
        spec = snapshot.spec(key)
        source = spec.get("source") or {}
        recipes = spec.get("recipes") or {}
        recipe = next(iter(recipes.values()), {}) if isinstance(recipes, dict) else {}
        return {
            "station": station_key,
            "machine": key,
            "project": key.split(":", 1)[-1],
            "repo": self._safe_scalar(source.get("repo")),
            "workflow": self._safe_scalar(source.get("workflow")),
            "inputs": self._port_names(recipe.get("inputs")),
            "outputs": self._port_names(recipe.get("outputs")),
            "delivery": "由 Foreman 根据项目配置判断",
        }

    @staticmethod
    def _machine_label(snapshot: Any, machine_ref: Any) -> str:
        del snapshot
        if not machine_ref:
            return "Machine"
        return str(machine_ref).split("@", 1)[0]

    def _operator_request(self, line: str, step: str) -> dict[str, Any] | None:
        rows = self.ctx.db.query(
            "SELECT id, signal, subject, payload, status, at FROM wakes WHERE subject LIKE ? ORDER BY at DESC LIMIT 1",
            (f"%{step}%",),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "signal": str(row["signal"]),
            "created_at": str(row["at"]),
            "line": line,
        }

    def _asset_source(self, bean_id: str) -> dict[str, Any] | None:
        rows = self.ctx.db.query(
            "SELECT execution_id, subject, assets FROM asset_uses ORDER BY execution_id DESC"
        )
        for row in rows:
            try:
                assets = json.loads(row["assets"])
            except (TypeError, json.JSONDecodeError):
                continue
            if bean_id not in assets:
                continue
            return {"run": str(row["execution_id"]), "node": str(row["subject"])}
        return None

    def _recent_change(self) -> dict[str, Any] | None:
        rows = self.ctx.db.query(
            "SELECT id, state, created_at FROM changesets ORDER BY created_at DESC LIMIT 1"
        )
        if not rows:
            return None
        return {"id": str(rows[0]["id"]), "state": str(rows[0]["state"]), "at": str(rows[0]["created_at"])}

    @staticmethod
    def _port_types(value: Any) -> set[str]:
        if not isinstance(value, dict):
            return set()
        found = set()
        for port in value.values():
            if isinstance(port, dict) and port.get("type"):
                found.add(str(port["type"]))
        return found

    @staticmethod
    def _port_names(value: Any) -> list[str]:
        return sorted(str(key) for key in value) if isinstance(value, dict) else []

    @staticmethod
    def _binding_sources(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for item in value.values():
                if isinstance(item, str) and "." in item and not item.startswith("$"):
                    found.append(item)
        return found

    @staticmethod
    def _condition_summary(step: dict[str, Any]) -> str | None:
        for key in ("when", "condition", "switch", "until", "map"):
            value = step.get(key)
            if value is not None:
                return str(value)[:160]
        return None

    @staticmethod
    def _short_ref(value: Any) -> str | None:
        if not value:
            return None
        return str(value).split(":", 1)[-1]

    @staticmethod
    def _safe_scalar(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return WebReadModel.safe_text(value, 240) if isinstance(value, str) else value
        return None

    @classmethod
    def safe_text(cls, value: Any, limit: int) -> str:
        text = "" if value is None else str(value)
        text = _SECRET_VALUE.sub(lambda match: match.group(1) + "=[REDACTED]", text)
        if "://" not in text:
            text = _PATH_VALUE.sub("[LOCAL_PATH]", text)
        return text[:limit]

    @classmethod
    def _safe_obj(cls, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = str(key)
                result[name] = "[REDACTED]" if _SECRET_KEY.search(name) else cls._safe_obj(item)
            return result
        if isinstance(value, list):
            return [cls._safe_obj(item) for item in value]
        if isinstance(value, str):
            return cls.safe_text(value, 500)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)
