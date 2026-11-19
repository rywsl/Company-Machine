"""Read models for the local M18 web application."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mao.base import canonical_json, utc_iso
from mao.commands.common import governance_registry
from mao.errors import FactoryError
from mao.secrets import store_secret
from mao.status import factory_status, tail_events
from mao.talk import list_messages

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization|cookie)")
_SECRET_VALUE = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+")
_PATH_VALUE = re.compile(r"(?i)(?:[A-Z]:\\|/)(?:[^\s/\\]+[/\\])*[^\s/\\]+")
_ACTIVE_RUNS = {"PENDING", "RUNNING", "FAILING", "CANCELLING"}
_HARNESS_PROFILE_PATH = Path(".factory") / "harnesses" / "profiles.json"
_THINKING_EFFORTS = {"minimal", "low", "medium", "high", "max"}


class WebReadModel:
    """Project existing control-plane facts into the M18 UI shape."""

    def __init__(self, ctx: Any, snapshot: Any | None = None) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self._memo: dict[str, Any] = {}
        self._runs_cache: dict[str, list[dict[str, Any]]] = {}
        self._asset_sources_cache: dict[str, dict[str, Any]] | None = None

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
            "harness_profiles": self.harness_profiles(),
            "dependencies": self.dependencies(),
            "foreman": self.foreman(),
            "secrets": self.secret_resources(limit=50),
            "constitution": self.constitution(),
        }

    def status(self) -> dict[str, Any]:
        cached = self._memo.get("status")
        if cached is not None:
            return cached
        epochs, pipeline = self._governance()
        value = factory_status(self.ctx, epochs=epochs, pipeline=pipeline)
        self._memo["status"] = value
        return value

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
            "metadata": self._graph_provenance(),
        }

    def workarea_graph(self, workarea: str | None) -> dict[str, Any] | None:
        if not workarea:
            return None
        snapshot = self._require_snapshot()
        line_key = self._resolve_line_key(workarea)
        spec = snapshot.spec(line_key)
        metadata = snapshot.doc(line_key).get("metadata") or {}
        steps = spec.get("steps") if isinstance(spec, dict) else None
        if not isinstance(steps, dict):
            steps = {}
        records = self._workarea_step_records(snapshot, line_key, steps)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        line_name = str(metadata.get("name") or line_key)
        if records:
            primary = next((record for record in records if record["runtime"] != "未声明"), records[0])
            group_key = ("project", str(primary["runtime"]), "project")
            operator_id = f"{line_key}:aggregate:operator"
            machine_id = f"{line_key}:aggregate:machine"
            status = self._aggregate_status([record["status"] for record in records])
            operator_metadata = self._aggregate_operator_metadata(line_name, records, group_key)
            machine_metadata = self._aggregate_machine_metadata(snapshot, line_name, records, group_key)
            nodes.extend(
                (
                    {
                        "id": operator_id,
                        "type": "operator",
                        "group": line_key,
                        "label": "Operator",
                        "position": {"x": 190, "y": 150},
                        "status": status,
                        "metadata": operator_metadata,
                    },
                    {
                        "id": machine_id,
                        "type": "machine",
                        "group": line_key,
                        "label": "Machine",
                        "position": {"x": 470, "y": 150},
                        "status": status,
                        "metadata": machine_metadata,
                    },
                )
            )
            edges.append(
                {
                    "id": f"{operator_id}:to:{machine_id}",
                    "source": operator_id,
                    "target": machine_id,
                    "kind": "aggregate",
                    "condition": "Operator 管理完整项目 Machine",
                    "status": status,
                    "metadata": {
                        "steps": [record["step"] for record in records],
                        "stations": [record["station"] for record in records],
                        "projection": "web_read_model",
                    },
                }
            )
        return {
            "scope": "workarea",
            "id": line_key,
            "title": line_name,
            "nodes": nodes,
            "edges": edges,
            "metadata": self._graph_provenance(),
        }

    def _workarea_step_records(
        self, snapshot: Any, line_key: str, steps: dict[str, Any]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for step_name, raw_step in steps.items():
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
            machine_key = None
            machine_spec: dict[str, Any] = {}
            if machine_ref:
                try:
                    machine_key = snapshot.key("Machine", str(machine_ref))
                    machine_spec = snapshot.spec(machine_key)
                except FactoryError:
                    machine_key = str(machine_ref)
            records.append(
                {
                    "step": str(step_name),
                    "station": station_key,
                    "station_name": str(
                        (snapshot.doc(station_key).get("metadata") or {}).get("name") or station_key
                    ),
                    "role": str(station.get("role") or "transform"),
                    "runtime": self._short_ref(runtime_ref) or "未声明",
                    "model": self._safe_scalar(model),
                    "machine": machine_key or "未声明",
                    "machine_name": self._machine_name(machine_key),
                    "machine_form": str(machine_spec.get("form") or "未声明"),
                    "machine_spec": machine_spec,
                    "status": self._step_status(line_key, str(step_name)),
                    "inputs": self._port_types_for_details(station.get("inputs")),
                    "outputs": self._port_types_for_details(station.get("outputs")),
                    "resource_request": self._operator_request(line_key, str(step_name)),
                    "params": self._safe_obj(raw_step.get("params") or {}),
                }
            )
        return records

    @staticmethod
    def _workarea_group_key(record: dict[str, Any]) -> tuple[str, str, str]:
        role = str(record["role"])
        if role == "sink":
            capability = "delivery"
        elif role == "source":
            capability = "ingress"
        else:
            capability = str(record["machine_form"])
        return role, str(record["runtime"]), capability

    @staticmethod
    def _workarea_group_id(line_key: str, group_key: tuple[str, str, str]) -> str:
        slug = "-".join(re.sub(r"[^A-Za-z0-9_.-]+", "-", item).strip("-") or "unknown" for item in group_key)
        return f"{line_key}:aggregate:{slug}"

    def _aggregate_operator_metadata(
        self, line_name: str, records: list[dict[str, Any]], group_key: tuple[str, str, str]
    ) -> dict[str, Any]:
        _role, runtime, _capability = group_key
        status = self._aggregate_status([record["status"] for record in records])
        return {
            "project": line_name,
            "project_summary": f"{line_name} 项目由 Operator 负责整体运行",
            "recent_status": status,
            "harness": runtime,
            "operator_runtime": sorted({record["runtime"] for record in records}),
            "models": sorted({record["model"] for record in records if record["model"]}),
            "inputs": sorted({item for record in records for item in record["inputs"]}),
            "outputs": sorted({item for record in records for item in record["outputs"]}),
            "step_count": len(records),
            "internal_steps": self._internal_step_details(records),
            "roles": sorted({record["role"] for record in records}),
            "resource_requests": [
                record["resource_request"] for record in records if record["resource_request"]
            ],
            "delivery": "由 Foreman、Operator 和项目配置判断",
            "provenance": self._node_provenance(),
        }

    def _aggregate_machine_metadata(
        self, snapshot: Any, line_name: str, records: list[dict[str, Any]], group_key: tuple[str, str, str]
    ) -> dict[str, Any]:
        _role, _runtime, capability = group_key
        machine_keys = list(dict.fromkeys(str(record["machine"]) for record in records))
        machines = [
            self._machine_resource_details(snapshot, machine_key, records)
            for machine_key in machine_keys
            if machine_key != "未声明"
        ]
        status = self._aggregate_status([record["status"] for record in records])
        return {
            "project": line_name,
            "project_summary": f"{line_name} 的完整启动项目与交付能力",
            "recent_status": status,
            "machines": machines,
            "machine_count": len(machines),
            "inputs": sorted({item for record in records for item in record["inputs"]}),
            "outputs": sorted({item for record in records for item in record["outputs"]}),
            "repos": sorted({item["repo"] for item in machines if item.get("repo")}),
            "workflows": sorted({item["workflow"] for item in machines if item.get("workflow")}),
            "step_count": len(records),
            "internal_steps": self._internal_step_details(records),
            "roles": sorted({record["role"] for record in records}),
            "delivery": "由 Foreman、Operator 和项目配置判断",
            "capability": capability,
            "provenance": self._node_provenance(),
        }

    def _machine_resource_details(
        self, snapshot: Any, machine_key: str, records: list[dict[str, Any]]
    ) -> dict[str, Any]:
        matching = [record for record in records if record["machine"] == machine_key]
        try:
            spec = snapshot.spec(machine_key)
            metadata = snapshot.doc(machine_key).get("metadata") or {}
        except FactoryError:
            return {
                "id": machine_key,
                "name": self._machine_name(machine_key),
                "form": "未声明",
                "instance": None,
                "resources": {},
                "repo": None,
                "workflow": None,
                "recipes": [],
                "stations": sorted({record["station"] for record in matching}),
            }
        source = spec.get("source") or {}
        runtime = spec.get("runtime") or {}
        return {
            "id": machine_key,
            "name": str(metadata.get("name") or self._machine_name(machine_key)),
            "form": str(spec.get("form") or "未声明"),
            "instance": self._safe_scalar(runtime.get("instance")),
            "resources": self._safe_obj(runtime.get("resources") or {}),
            "repo": self._safe_scalar(source.get("repo")),
            "workflow": self._safe_scalar(source.get("workflow")),
            "recipes": sorted(str(item) for item in (spec.get("recipes") or {})),
            "stations": sorted({record["station"] for record in matching}),
        }

    @staticmethod
    def _internal_step_details(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": record["step"],
                "station": record["station_name"],
                "status": record["status"],
                "machine": record["machine_name"],
                "operator": record["runtime"],
                "inputs": record["inputs"],
                "outputs": record["outputs"],
                "params": record["params"],
            }
            for record in records
        ]

    @staticmethod
    def _aggregate_operator_label(group_key: tuple[str, str, str]) -> str:
        del group_key
        return "Operator"

    @staticmethod
    def _aggregate_machine_label(group_key: tuple[str, str, str]) -> str:
        del group_key
        return "Machine"

    @staticmethod
    def _role_label(role: str) -> str:
        return {
            "source": "接入",
            "transform": "处理",
            "converter": "转换",
            "planner": "规划",
            "sink": "交付",
        }.get(role, role)

    @staticmethod
    def _aggregate_status(statuses: list[str]) -> str:
        values = set(statuses)
        for candidate in ("exception", "running", "repairing", "validating", "waiting", "ready", "skipped"):
            if candidate in values:
                return candidate
        return "unknown"

    @staticmethod
    def _machine_name(machine_key: str | None) -> str:
        if not machine_key:
            return "未声明"
        return machine_key.split(":", 1)[-1].split("@", 1)[0]

    @staticmethod
    def _port_types_for_details(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        found: list[str] = []
        for name, declaration in value.items():
            if isinstance(declaration, dict) and declaration.get("type"):
                found.append(f"{name}: {declaration['type']}")
            elif isinstance(declaration, str):
                found.append(f"{name}: {declaration}")
        return sorted(found)

    def _graph_provenance(self) -> dict[str, Any]:
        status = self.status()
        foreman = self.foreman()
        return {
            "projection": "web_read_model.aggregate",
            "static_source": "registry",
            "runtime_source": "runs, run_nodes and wakes",
            "foreman": {
                "configured": bool(foreman.get("configured")),
                "online": bool((status.get("supervisor") or {}).get("online")),
                "graph_generated": False,
                "state_source": "supervisor status",
            },
        }

    @staticmethod
    def _node_provenance() -> dict[str, str]:
        return {
            "static_source": "registry Line, Station, Machine and OperatorRuntime",
            "runtime_source": "runs, run_nodes and wakes",
            "projection": "web_read_model.aggregate",
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
        cached = self._memo.get("harnesses")
        if cached is not None:
            return cached
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
        profiles = {
            item["runtime"]: item for item in self.harness_profiles() if item.get("role") == "operator"
        }
        profiles.update(
            {item["runtime"]: item for item in self.harness_profiles() if item["runtime"] not in profiles}
        )
        for item in found.values():
            runtime = self._runtime_name(item["id"])
            item["configuration"] = profiles.get(runtime)
        value = sorted(found.values(), key=lambda item: (str(item.get("name", "")), str(item["id"])))
        self._memo["harnesses"] = value
        return value

    def dependencies(self) -> dict[str, Any]:
        harnesses = []
        for item in self.harnesses():
            if item.get("kind") != "agent-cli":
                continue
            invoke = item.get("capabilities") or {}
            harnesses.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "version": item.get("version"),
                    "state": "ready" if item.get("available") else "missing",
                    "integrity": {
                        "runtime": True,
                        "entrypoint": bool(item.get("available")),
                        "tool_calls": bool(invoke.get("tool_calls")),
                    },
                }
            )
        return {
            "harnesses": harnesses,
            "podman": self._podman_dependency(),
        }

    @staticmethod
    def _podman_dependency() -> dict[str, Any]:
        executable = shutil.which("podman")
        if executable is None:
            return {"state": "missing", "version": None, "message": "未找到 Podman CLI", "provider": "podman"}
        try:
            result = subprocess.run(
                [executable, "version", "--format", "{{.Version}}"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"state": "offline", "version": None, "message": "Podman CLI 存在，但运行时不在线", "provider": "podman"}
        version = result.stdout.strip()
        if result.returncode != 0 or not version:
            return {"state": "offline", "version": None, "message": "Podman 运行时不在线", "provider": "podman"}
        return {"state": "online", "version": version, "message": "Podman 运行时在线", "provider": "podman"}

    @staticmethod
    def _docker_dependency() -> dict[str, Any]:
        """Compatibility projection for older clients during the cutover."""

        return WebReadModel._podman_dependency()

    def harness_profiles(self) -> list[dict[str, Any]]:
        cached = self._memo.get("harness_profiles")
        if cached is not None:
            return cached
        path = Path(self.ctx.root) / _HARNESS_PROFILE_PATH
        if not path.is_file():
            self._memo["harness_profiles"] = []
            return []
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._memo["harness_profiles"] = []
            return []
        raw_profiles = body.get("profiles") if isinstance(body, dict) else None
        if not isinstance(raw_profiles, list):
            self._memo["harness_profiles"] = []
            return []
        value = [self._public_harness_profile(item) for item in raw_profiles if isinstance(item, dict)]
        self._memo["harness_profiles"] = value
        return value

    def save_harness_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise FactoryError("HARNESS_CONFIG_INVALID", "Harness 配置必须是对象", field="config")
        runtime = self._runtime_name(payload.get("runtime"))
        if not runtime:
            raise FactoryError("HARNESS_CONFIG_INVALID", "缺少 runtime", field="runtime")
        snapshot = self._require_snapshot()
        runtime_key = snapshot.key("OperatorRuntime", runtime)
        runtime_spec = snapshot.spec(runtime_key)
        if runtime_spec.get("kind") != "agent-cli":
            raise FactoryError("HARNESS_CONFIG_INVALID", "runtime 不是 agent-cli", field="runtime")
        model = self._required_text(payload, "model")
        allowed = [str(item) for item in runtime_spec.get("models_allowed") or []]
        if allowed and model not in allowed:
            raise FactoryError("HARNESS_CONFIG_INVALID", "模型不在 runtime 允许列表中", field="model")
        base_url = self._required_text(payload, "base_url")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise FactoryError("HARNESS_CONFIG_INVALID", "base_url 必须是 HTTP URL", field="base_url")
        effort = str(payload.get("thinking_effort") or "medium")
        if effort not in _THINKING_EFFORTS:
            raise FactoryError("HARNESS_CONFIG_INVALID", "thinking_effort 不合法", field="thinking_effort")
        context = self._positive_int(payload.get("context"), "context")
        role = str(payload.get("role") or "operator")
        if role not in {"operator", "foreman"}:
            raise FactoryError("HARNESS_CONFIG_INVALID", "role 不合法", field="role")
        profile_id = self._profile_id(runtime, role)
        secret_name = f"HARNESS_{re.sub(r'[^A-Za-z0-9]+', '_', profile_id).upper()}_API_KEY"
        api_key = str(payload.get("api_key") or "")
        existing = next((item for item in self._stored_profiles() if item.get("id") == profile_id), None)
        if api_key:
            store_secret(self.ctx.root, secret_name, "model", api_key)
        elif existing and existing.get("api_key_secret"):
            secret_name = str(existing.get("api_key_secret") or secret_name)
        else:
            secret_name = ""
        profile = {
            "id": profile_id,
            "runtime": runtime,
            "role": role,
            "base_url": base_url,
            "model": model,
            "thinking_effort": effort,
            "context": context,
            "api_key_secret": secret_name,
            "image": str(payload.get("image") or ""),
            "network": str(payload.get("network") or "egress"),
            "session_timeout_s": self._positive_number(
                payload.get("session_timeout_s", 600), "session_timeout_s"
            ),
            "session_budget_usd": self._nonnegative_number(
                payload.get("session_budget_usd", 1), "session_budget_usd"
            ),
            "daily_budget_usd": self._nonnegative_number(
                payload.get("daily_budget_usd", 10), "daily_budget_usd"
            ),
            "max_turns": self._positive_int(payload.get("max_turns", 20), "max_turns"),
            "max_attempts": self._positive_int(payload.get("max_attempts", 2), "max_attempts"),
            "backoff_s": self._nonnegative_number(payload.get("backoff_s", 60), "backoff_s"),
            "mode": str(payload.get("mode") or "session"),
            "verification": {"state": "pending", "reason": "等待 Foreman 验证", "checked_at": None},
            "updated_at": utc_iso(self.ctx.clock.now()),
        }
        if profile["network"] not in {"none", "egress"}:
            raise FactoryError("HARNESS_CONFIG_INVALID", "network 不合法", field="network")
        if profile["mode"] not in {"session", "resident"}:
            raise FactoryError("HARNESS_CONFIG_INVALID", "mode 不合法", field="mode")
        profiles = [item for item in self._stored_profiles() if item.get("id") != profile_id]
        profiles.append(profile)
        self._write_profiles(profiles)
        if profile["role"] == "foreman":
            self._write_foreman_config(profile)
        return self._public_harness_profile(profile)

    def verify_harness_profile(self, profile_id: str) -> dict[str, Any]:
        profile = next((item for item in self._stored_profiles() if item.get("id") == profile_id), None)
        if profile is None:
            raise FactoryError("HARNESS_CONFIG_MISSING", "找不到 Harness 配置", profile=profile_id)
        runtime = self._runtime_name(profile.get("runtime"))
        snapshot = self._require_snapshot()
        runtime_key = snapshot.key("OperatorRuntime", runtime)
        runtime_spec = snapshot.spec(runtime_key)
        allowed = [str(item) for item in runtime_spec.get("models_allowed") or []]
        if allowed and profile.get("model") not in allowed:
            return self._update_verification(profile, "failed", "模型不在 runtime 允许列表中")
        if not profile.get("api_key_secret"):
            return self._update_verification(profile, "missing", "缺少 API key")
        if not profile.get("image"):
            return self._update_verification(profile, "missing", "缺少 harness image")
        foreman = self.foreman()
        if not foreman.get("configured"):
            return self._update_verification(
                profile, "agent_required", "请先配置 Foreman，由 agent 验证 harness"
            )
        from mao.talk import post_human

        message = post_human(
            self.ctx,
            f"请验证 Harness 配置 {profile_id}。请启动对应 harness，验证 CLI 能启动，并通过一次最小模型请求确认 base_url、model 和 API key 可用。结果写回 Web App 可读的运行记录。",
            by="human:web",
        )
        result = self._update_verification(profile, "requested", "已请求 Foreman 验证", request_id=message.id)
        result["request_id"] = message.id
        return result

    def _stored_profiles(self) -> list[dict[str, Any]]:
        path = Path(self.ctx.root) / _HARNESS_PROFILE_PATH
        if not path.is_file():
            return []
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        profiles = body.get("profiles") if isinstance(body, dict) else None
        return [item for item in profiles if isinstance(item, dict)] if isinstance(profiles, list) else []

    def _write_profiles(self, profiles: list[dict[str, Any]]) -> None:
        path = Path(self.ctx.root) / _HARNESS_PROFILE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json({"profiles": profiles}) + "\n", encoding="utf-8")

    def _write_foreman_config(self, profile: dict[str, Any]) -> None:
        path = Path(self.ctx.root) / "factory.json"
        body: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    body = loaded
            except (OSError, json.JSONDecodeError):
                body = {}
        section = dict(body.get("foreman") or {})
        section.update(
            {
                "runtime": profile["runtime"],
                "model": profile["model"],
                "reviewers": section.get("reviewers") or [],
                "models": {**dict(section.get("models") or {}), profile["runtime"]: profile["model"]},
                "image": profile["image"],
                "network": profile["network"],
                "session_timeout_s": profile["session_timeout_s"],
                "session_budget_usd": profile["session_budget_usd"],
                "daily_budget_usd": profile["daily_budget_usd"],
                "max_turns": profile["max_turns"],
                "max_attempts": profile["max_attempts"],
                "backoff_s": profile["backoff_s"],
                "mode": profile["mode"],
                "env": {
                    **dict(section.get("env") or {}),
                    "ANTHROPIC_BASE_URL": profile["base_url"],
                    "ANTHROPIC_API_KEY": f"secret://{profile['api_key_secret']}",
                    "MAO_THINKING_EFFORT": profile["thinking_effort"],
                    "MAO_CONTEXT": str(profile["context"]),
                },
            }
        )
        body["foreman"] = section
        path.write_text(canonical_json(body) + "\n", encoding="utf-8")

    def _update_verification(
        self, profile: dict[str, Any], state: str, reason: str, **extra: Any
    ) -> dict[str, Any]:
        profile = dict(profile)
        profile["verification"] = {
            "state": state,
            "reason": reason,
            "checked_at": utc_iso(self.ctx.clock.now()),
            **extra,
        }
        profiles = [item for item in self._stored_profiles() if item.get("id") != profile.get("id")]
        profiles.append(profile)
        self._write_profiles(profiles)
        return self._public_harness_profile(profile)

    @staticmethod
    def _public_harness_profile(profile: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in profile.items() if key != "api_key_secret"} | {
            "api_key_configured": bool(profile.get("api_key_secret"))
        }

    @staticmethod
    def _runtime_name(value: Any) -> str:
        return str(value or "").split(":", 1)[-1]

    @staticmethod
    def _profile_id(runtime: str, role: str) -> str:
        runtime_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", runtime).strip("-") or "harness"
        return f"{role}-{runtime_id}"

    @staticmethod
    def _required_text(payload: dict[str, Any], field: str) -> str:
        value = str(payload.get(field) or "").strip()
        if not value:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"缺少 {field}", field=field)
        return value

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是正整数", field=field)
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是正整数", field=field) from exc
        if result < 1:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是正整数", field=field)
        return result

    @staticmethod
    def _positive_number(value: Any, field: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是正数", field=field) from exc
        if result <= 0:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是正数", field=field)
        return result

    @staticmethod
    def _nonnegative_number(value: Any, field: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是非负数", field=field) from exc
        if result < 0 or result != result:
            raise FactoryError("HARNESS_CONFIG_INVALID", f"{field} 必须是非负数", field=field)
        return result

    def foreman(self) -> dict[str, Any]:
        """Expose the current Foreman Harness without exposing local credentials."""
        cached = self._memo.get("foreman")
        if cached is not None:
            return cached
        profile = next((item for item in self.harness_profiles() if item.get("role") == "foreman"), None)
        try:
            from mao.supervisor import SupervisorConfig

            config = SupervisorConfig.load(Path(self.ctx.root))
        except (FactoryError, OSError, ValueError, TypeError):
            value = {
                "configured": False,
                "setup_required": True,
                "runtime": None,
                "model": None,
                "mode": None,
                "profile": profile,
            }
            self._memo["foreman"] = value
            return value
        runtime = str(config.runtime)
        harness = next(
            (item for item in self.harnesses() if item["id"] == runtime or item["name"] == runtime), None
        )
        value = {
            "configured": True,
            "setup_required": False,
            "runtime": runtime,
            "model": self._safe_scalar(config.model),
            "mode": config.mode,
            "harness": harness,
            "profile": profile,
        }
        self._memo["foreman"] = value
        return value

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
        cached = self._runs_cache.get(line)
        if cached is not None:
            return cached
        rows = self.ctx.db.query(
            "SELECT id, line, state, created_at, updated_at, error FROM runs WHERE line IN (?, ?) ORDER BY created_at",
            (line, line.split(":", 1)[-1]),
        )
        value = [
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
        self._runs_cache[line] = value
        return value

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
        if self._asset_sources_cache is None:
            found: dict[str, dict[str, Any]] = {}
            rows = self.ctx.db.query(
                "SELECT execution_id, subject, assets FROM asset_uses ORDER BY execution_id DESC"
            )
            for row in rows:
                try:
                    assets = json.loads(row["assets"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(assets, dict):
                    asset_ids = assets.keys()
                elif isinstance(assets, (list, tuple, set)):
                    asset_ids = assets
                else:
                    continue
                for asset_id in asset_ids:
                    found.setdefault(str(asset_id), {"run": str(row["execution_id"]), "node": str(row["subject"])})
            self._asset_sources_cache = found
        return self._asset_sources_cache.get(bean_id)

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
