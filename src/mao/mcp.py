"""MCP 操作面。身份只来自 Session。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import mao
from mao.base import canonical_json
from mao.egress import SecretStore
from mao.errors import FactoryError
from mao.foreman import ConversionStore, ForemanQuery, ForemanSubmit
from mao.governance.changesets import ChangePipeline

PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

_SHARED = (
    "registry_get",
    "registry_list",
    "registry_diff",
    "epoch_list",
    "epoch_show",
    "bean_get",
    "bean_lineage",
    "job_get",
    "job_transcript",
    "dlq_list",
    "drift_list",
    "invariant_report",
    "metrics_query",
    "approval_list",
    "conversion_list",
    "changeset_status",
)
_FOREMAN = ("changeset_propose", "changeset_withdraw", "registry_check", "image_build")
_REVIEWER = ("review_submit",)


@dataclass(frozen=True)
class Session:
    role: str
    actor: str
    wake_id: str | None = None
    session_id: str | None = None
    source_repo: Path | None = None


class McpServer:
    def __init__(self, ctx: Any, epochs: Any, session: Session, *, pipeline: Any | None = None) -> None:
        if session.role not in ("foreman", "reviewer"):
            raise FactoryError("MCP_ACTOR_INVALID", "角色不合法", role=session.role, actor=session.actor)
        snapshot = epochs.materialize(epochs.current())
        try:
            snapshot.spec(snapshot.key("OperatorRuntime", session.actor))
        except FactoryError as exc:
            raise FactoryError(
                "MCP_ACTOR_INVALID", "actor 不是 OperatorRuntime", role=session.role, actor=session.actor
            ) from exc
        self.ctx = ctx
        self.epochs = epochs
        self.session = session
        self.pipeline = pipeline if pipeline is not None else ChangePipeline(ctx, epochs)
        if (session.session_id is None) != (session.source_repo is None):
            raise FactoryError("MCP_ACTOR_INVALID", "会话克隆不完整", role=session.role, actor=session.actor)
        self.initialized = False
        self._secrets = SecretStore(ctx.root)
        self._outcome: dict[str, list[str]] = {"proposed": [], "reviewed": []}

    @property
    def outcome(self) -> dict[str, list[str]]:
        return {"proposed": list(self._outcome["proposed"]), "reviewed": list(self._outcome["reviewed"])}

    def tools(self) -> list[dict[str, Any]]:
        names = list(_SHARED)
        if self.session.role == "foreman":
            names.extend(_FOREMAN)
        else:
            names.extend(_REVIEWER)
        return [_tool(name) for name in sorted(names)]

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _rpc_error(_rid(message), -32600, "invalid request")
        method = str(message.get("method"))
        rid = message.get("id", None)
        if "id" not in message:
            return None
        if not self.initialized and method not in ("initialize", "ping"):
            return _rpc_error(rid, -32600, "not initialized")
        try:
            if method == "initialize":
                self.initialized = True
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                version = str(params.get("protocolVersion") or "")
                if version not in PROTOCOL_VERSIONS:
                    version = PROTOCOL_VERSIONS[0]
                return _rpc_ok(
                    rid,
                    {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mao-factory", "version": mao.__version__},
                        "instructions": "工厂操作面。只读查询与 ChangeSet 提议；身份由会话决定。",
                    },
                )
            if method == "ping":
                return _rpc_ok(rid, {})
            if method == "tools/list":
                return _rpc_ok(rid, {"tools": self.tools()})
            if method == "tools/call":
                return _rpc_ok(rid, self._call(message.get("params")))
            return _rpc_error(rid, -32601, "method not found")
        except _Proto as exc:
            return _rpc_error(rid, exc.code, exc.message)
        except Exception as exc:
            return _rpc_error(rid, -32603, type(exc).__name__)

    def serve_stdio(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if line.strip() == "":
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(canonical_json(_rpc_error(None, -32700, "parse error")) + "\n")
                stdout.flush()
                continue
            reply = self.handle(message)
            if reply is not None:
                stdout.write(canonical_json(reply) + "\n")
                stdout.flush()

    def _call(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _Proto(-32602, "invalid params")
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        allowed = {tool["name"]: tool for tool in self.tools()}
        if not isinstance(name, str) or name not in allowed:
            raise _Proto(-32602, "unknown tool")
        schema = allowed[name]["inputSchema"]
        if not _matches(schema, arguments):
            raise _Proto(-32602, "invalid params")
        try:
            value = self._invoke(name, arguments)
            if hasattr(value, "to_json") and callable(value.to_json):
                value = value.to_json()
        except FactoryError as exc:
            body = self._secrets.redact_obj(exc.to_json())
            return {"content": [{"type": "text", "text": canonical_json(body)}], "isError": True}
        clean = self._secrets.redact_obj(value)
        return {
            "content": [{"type": "text", "text": canonical_json(clean)}],
            "structuredContent": {"result": clean},
            "isError": False,
        }

    def _invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        query = ForemanQuery(self.ctx, self.epochs)
        submit = ForemanSubmit(self.ctx, self.pipeline)
        if name == "registry_get":
            return query.registry_get(str(arguments["key"]))
        if name == "registry_list":
            return query.registry_list(str(arguments["kind"]))
        if name == "registry_diff":
            return query.registry_diff(
                str(arguments["base"]),
                str(arguments["commit"]),
                timeout_s=self.epochs.timeout_s,
            )
        if name == "epoch_list":
            return query.epoch_list()
        if name == "epoch_show":
            return query.epoch_show(arguments.get("commit"))
        if name == "bean_get":
            return query.bean_get(str(arguments["bean_id"]))
        if name == "bean_lineage":
            return query.bean_lineage(str(arguments["bean_id"]), str(arguments["direction"]))
        if name == "job_get":
            return query.job_get(str(arguments["job_id"]))
        if name == "job_transcript":
            return query.job_transcript(str(arguments["job_id"]))
        if name == "dlq_list":
            return query.dlq_list()
        if name == "drift_list":
            return query.drift_list()
        if name == "invariant_report":
            return query.invariant_report()
        if name == "metrics_query":
            return query.metrics_query(str(arguments["station"]), str(arguments["metric"]))
        if name == "approval_list":
            return query.approval_list()
        if name == "conversion_list":
            return ConversionStore(self.ctx).list(status=arguments.get("status"))
        if name == "changeset_status":
            return submit.status(str(arguments["changeset_id"]))
        if name == "changeset_propose":
            self._fetch_session()
            found = submit.propose(
                arguments["document"],
                str(arguments["commit"]),
                by=self.session.actor,
                timeout_s=self.epochs.timeout_s,
            )
            self._outcome["proposed"].append(found.id)
            return found
        if name == "changeset_withdraw":
            return submit.withdraw(str(arguments["changeset_id"]), by=self.session.actor)
        if name == "registry_check":
            return self._registry_check(str(arguments["commit"]))
        if name == "image_build":
            self._fetch_session()
            from mao.images import ImageBuilder, ImageConfig

            record = ImageBuilder(self.ctx, self.epochs, ImageConfig.load(self.ctx.root)).build(
                str(arguments["path"]),
                commit=None if arguments.get("commit") in (None, "") else str(arguments["commit"]),
                by=self.session.actor,
            )
            body = record.to_json()
            log_path = Path(self.ctx.root) / record.log
            tail = ""
            if log_path.is_file():
                tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-100:])
            body["log_tail"] = tail
            return body
        if name == "review_submit":
            found = submit.review(
                str(arguments["changeset_id"]),
                {
                    "verdict": arguments["verdict"],
                    "runtime": self.session.actor,
                    "note": arguments.get("note", ""),
                },
                by=self.session.actor,
            )
            self._outcome["reviewed"].append(str(arguments["changeset_id"]))
            return found
        raise _Proto(-32602, "unknown tool")

    def _fetch_session(self) -> None:
        if self.session.source_repo is None or self.session.session_id is None:
            return
        from mao.governance.git import git

        source = str(self.session.source_repo)
        git(
            self.epochs.registry,
            "-c",
            f"safe.directory={source}",
            "fetch",
            "--no-tags",
            "--quiet",
            source,
            f"+refs/heads/*:refs/foreman/{self.session.session_id}/heads/*",
            f"+HEAD:refs/foreman/{self.session.session_id}/HEAD",
            timeout_s=self.epochs.timeout_s,
        )

    def _registry_check(self, commit: str) -> dict[str, Any]:
        from mao.foreman import file_conversions
        from mao.snapshot import Snapshot

        self._fetch_session()
        dest = Path(self.ctx.root) / "tmp" / "registry-check" / commit
        if dest.exists():
            import shutil

            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        import io
        import tarfile

        from mao.governance.git import git_bytes

        blob = git_bytes(
            self.epochs.registry, "archive", "--format=tar", commit, timeout_s=self.epochs.timeout_s
        )
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            archive.extractall(dest, filter="data")
        try:
            snap = Snapshot.load(dest)
            diags = [item.to_json() for item in snap.result.diags]
            resolutions = list(snap.result.resolutions)
        except FactoryError as exc:
            if exc.code != "REGISTRY_INVALID":
                raise
            diags = list(exc.detail.get("diags") or [])
            resolutions = list(exc.detail.get("resolutions") or [])
        from mao.governance.git import git as git_text

        changed = git_text(
            self.epochs.registry,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit,
            timeout_s=self.epochs.timeout_s,
        )
        touched = {Path(line).stem for line in changed.splitlines() if line.strip()}
        scoped = [
            item
            for item in resolutions
            if str(item.get("line", "")).split(":", 1)[-1].split("@", 1)[0] in touched
        ]
        conversions = file_conversions(
            self.ctx,
            diags,
            scoped or resolutions,
            found_at={"commit": commit, "by": self.session.actor},
        )
        errors = [item for item in diags if item.get("severity") == "error"]
        return {"ok": not errors, "diags": diags, "conversions": conversions}


class _Proto(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


def _tool(name: str) -> dict[str, Any]:
    props, required = _SCHEMAS[name]
    return {
        "name": name,
        "description": name,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


def _matches(schema: dict[str, Any], arguments: dict[str, Any]) -> bool:
    if not isinstance(arguments, dict):
        return False
    props = schema["properties"]
    if any(key not in props for key in arguments):
        return False
    for name in schema["required"]:
        if name not in arguments:
            return False
    for name, value in arguments.items():
        spec = props[name]
        kind = spec.get("type")
        if kind == "string" and not isinstance(value, str):
            return False
        if kind == "object" and not isinstance(value, dict):
            return False
        if "enum" in spec and value not in spec["enum"]:
            return False
    return True


def _rpc_ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _rid(message: Any) -> Any:
    if isinstance(message, dict) and "id" in message:
        return message.get("id")
    return None


_SCHEMAS: dict[str, tuple[dict[str, Any], list[str]]] = {
    "registry_get": ({"key": {"type": "string"}}, ["key"]),
    "registry_list": ({"kind": {"type": "string"}}, ["kind"]),
    "registry_diff": ({"base": {"type": "string"}, "commit": {"type": "string"}}, ["base", "commit"]),
    "epoch_list": ({}, []),
    "epoch_show": ({"commit": {"type": "string"}}, []),
    "bean_get": ({"bean_id": {"type": "string"}}, ["bean_id"]),
    "bean_lineage": (
        {"bean_id": {"type": "string"}, "direction": {"type": "string", "enum": ["up", "down"]}},
        ["bean_id", "direction"],
    ),
    "job_get": ({"job_id": {"type": "string"}}, ["job_id"]),
    "job_transcript": ({"job_id": {"type": "string"}}, ["job_id"]),
    "dlq_list": ({}, []),
    "drift_list": ({}, []),
    "invariant_report": ({}, []),
    "metrics_query": ({"station": {"type": "string"}, "metric": {"type": "string"}}, ["station", "metric"]),
    "approval_list": ({}, []),
    "conversion_list": ({"status": {"type": "string"}}, []),
    "changeset_status": ({"changeset_id": {"type": "string"}}, ["changeset_id"]),
    "changeset_propose": (
        {"document": {"type": "object"}, "commit": {"type": "string"}},
        ["document", "commit"],
    ),
    "registry_check": ({"commit": {"type": "string"}}, ["commit"]),
    "changeset_withdraw": ({"changeset_id": {"type": "string"}}, ["changeset_id"]),
    "review_submit": (
        {
            "changeset_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["approve", "deny"]},
            "note": {"type": "string"},
        },
        ["changeset_id", "verdict", "note"],
    ),
    "image_build": ({"path": {"type": "string"}, "commit": {"type": "string"}}, ["path", "commit"]),
}
