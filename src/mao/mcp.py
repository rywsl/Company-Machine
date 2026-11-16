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
_FOREMAN = (
    "changeset_propose",
    "changeset_withdraw",
    "registry_check",
    "image_build",
    "escalate",
    "inbox_get",
    "inbox_list",
    "instance_list",
    "approval_get",
    "approval_decide",
    "approval_escalate",
    "secret_request",
    "secret_verify",
    "talk_propose_run",
    "talk_reply",
)
_REVIEWER = ("review_submit",)
_AGENT = ("run_start", "ask_human", "remind", "agent_send")
_ASSET_SHARED = (
    "asset_get",
    "asset_list",
    "asset_closure",
    "knowledge_heads",
    "knowledge_trial_get",
)
_ASSET_FOREMAN = (
    "knowledge_trial_open",
    "knowledge_evaluate",
    "knowledge_trial_cancel",
    "knowledge_promote",
    "knowledge_invalidate",
    "agent_takeover_prepare",
    "agent_takeover_finish",
    "agent_takeover_abort",
)


@dataclass(frozen=True)
class Session:
    role: str
    actor: str
    wake_id: str | None = None
    session_id: str | None = None
    source_repo: Path | None = None
    agent: str | None = None


class McpServer:
    def __init__(self, ctx: Any, epochs: Any, session: Session, *, pipeline: Any | None = None) -> None:
        if (session.role == "agent") != bool(session.agent):
            raise FactoryError("MCP_ACTOR_INVALID", "角色不合法", role=session.role, actor=session.actor)
        if session.role not in ("foreman", "reviewer", "agent"):
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
        if session.role != "agent" and (session.session_id is None) != (session.source_repo is None):
            raise FactoryError("MCP_ACTOR_INVALID", "会话克隆不完整", role=session.role, actor=session.actor)
        self.initialized = False
        self._secrets = SecretStore(ctx.root)
        self._outcome: dict[str, list[str]] = {"proposed": [], "reviewed": []}
        self.actions: list[dict[str, Any]] = []
        self._talk_replied = False

    @property
    def outcome(self) -> dict[str, Any]:
        return {
            "proposed": list(self._outcome["proposed"]),
            "reviewed": list(self._outcome["reviewed"]),
            "talk_replied": self._talk_replied,
        }

    def tools(self) -> list[dict[str, Any]]:
        from mao.agents import resident_foreman
        from mao.assets import assets_config

        names = list(_SHARED)
        enabled = assets_config(self.ctx) is not None
        if enabled:
            names.extend(_ASSET_SHARED)
        if self.session.role == "foreman":
            names.extend(_FOREMAN)
            if enabled:
                names.extend(_ASSET_FOREMAN)
                names.append("asset_put")
                if resident_foreman(self.ctx.root) is not None:
                    names.extend(("agent_send", "remind"))
        elif self.session.role == "reviewer":
            names.extend(_REVIEWER)
        else:
            names.extend(_AGENT)
            if enabled:
                names.append("asset_put")
        return [_tool(name) for name in sorted(set(names))]

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

    def _asset_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from mao.assets import AssetStore, assets_config
        from mao.learning import LearningStore

        if assets_config(self.ctx) is None or not name.startswith(
            ("asset_", "knowledge_", "agent_takeover_")
        ):
            return _ASSET_SKIP
        assets = AssetStore(self.ctx)
        learning = LearningStore(self.ctx, self.epochs)
        actor = str(self.session.actor)
        if name == "asset_get":
            return assets.get(str(arguments["asset_id"]))
        if name == "asset_list":
            return assets.list(kind=arguments.get("kind"), subject=arguments.get("subject"))
        if name == "asset_closure":
            return assets.closure(list(arguments["roots"]))
        if name == "asset_put":
            document = dict(arguments["document"])
            if self.session.role == "agent":
                subject = str((document.get("scope") or {}).get("subject") or "")
                if subject != f"agent:{self.session.agent}":
                    raise FactoryError("ASSET_INVALID", "只能登记本岗位资产", field="scope")
            return assets.put(document, by=actor)
        if name == "knowledge_heads":
            return learning.heads(str(arguments["subject"]))
        if name == "knowledge_trial_get":
            return learning.get_trial(str(arguments["trial_id"]))
        if name == "knowledge_trial_open":
            return learning.open_trial(
                str(arguments["target"]),
                str(arguments["contract"]),
                purpose=str(arguments["purpose"]),
                by=actor,
            )
        if name == "knowledge_evaluate":
            return learning.evaluate(str(arguments["trial_id"]), arguments["evidence"], by=actor)
        if name == "knowledge_trial_cancel":
            return learning.cancel_trial(
                str(arguments["trial_id"]), reason=str(arguments["reason"]), by=actor
            )
        if name == "knowledge_promote":
            expected = arguments.get("expected")
            return learning.promote(
                str(arguments["target"]),
                trial_id=arguments.get("trial_id"),
                expected=None if expected in (None, "none", "") else str(expected),
                by=actor,
            )
        if name == "knowledge_invalidate":
            return learning.invalidate(str(arguments["target"]), reason=str(arguments["reason"]), by=actor)
        if name == "agent_takeover_prepare":
            return learning.prepare_takeover(str(arguments["trial_id"]), by=actor)
        if name == "agent_takeover_finish":
            return learning.finish_takeover(str(arguments["trial_id"]), by=actor)
        if name == "agent_takeover_abort":
            return learning.abort_takeover(
                str(arguments["trial_id"]), reason=str(arguments["reason"]), by=actor
            )
        return _ASSET_SKIP

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
        if name in _AGENT or name in ("agent_send", "remind"):
            from mao.agents import agent_tool

            actor = str(self.session.agent or "")
            if self.session.role == "foreman" and not actor:
                actor = "foreman"
            value = agent_tool(self.ctx, self.epochs, actor, name, arguments)
            self.actions.append({"tool": name, "refs": _action_refs(value)})
            return value
        asset_value = self._asset_tool(name, arguments)
        if asset_value is not _ASSET_SKIP:
            return asset_value
        if name == "changeset_propose":
            self._fetch_session()
            found = submit.propose(
                arguments["document"],
                str(arguments["commit"]),
                by=self.session.actor,
                timeout_s=self.epochs.timeout_s,
            )
            self._outcome["proposed"].append(found.id)
            self._talk_card("changeset", found)
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
        if name == "approval_get":
            return self._approvals().view(str(arguments["approval_id"]))
        if name == "approval_decide":
            return self._approval_decide(arguments)
        if name == "approval_escalate":
            found = self._approvals().escalate(
                str(arguments["approval_id"]),
                by="foreman:" + self.session.actor,
                note=str(arguments.get("note") or ""),
            )
            return self._approvals().view(found.id)
        if name == "escalate":
            found = self._escalate(arguments)
            self._talk_card("escalate", found)
            return found
        if name == "inbox_get":
            from mao.inbox import Inbox

            return Inbox(self.ctx).get(str(arguments["request_id"]))
        if name == "inbox_list":
            from mao.inbox import Inbox

            status = arguments.get("status")
            return Inbox(self.ctx).list(status=None if status is None else str(status))
        if name == "instance_list":
            from mao.commands.common import to_jsonable
            from mao.instances import Instances

            snapshot = self.epochs.materialize(self.epochs.current())
            station = arguments.get("station")
            state = arguments.get("state")
            rows = Instances(self.ctx, snapshot).list(
                station=None if station in (None, "") else str(station),
                state=None if state in (None, "") else str(state),
            )
            return [to_jsonable(row) for row in rows]
        if name == "talk_propose_run":
            from mao.talk import propose_run

            return propose_run(
                self.ctx,
                str(arguments["line"]),
                arguments["inputs"],
                by=self.session.actor,
                turn_id=self.session.wake_id or "",
                sink_mode=str(arguments.get("sink_mode") or "execute"),
            )
        if name == "talk_reply":
            from mao.talk import talk_reply

            found = talk_reply(
                self.ctx,
                arguments["text"],
                by=self.session.actor,
                turn_id=self.session.wake_id or "",
                in_reply_to=arguments.get("in_reply_to"),
            )
            self._talk_replied = True
            return found
        if name == "secret_request":
            found = self._secret_request(arguments)
            self._talk_card("secret", found)
            return found
        if name == "secret_verify":
            return self._secret_verify(arguments)
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

    def _approvals(self) -> Any:
        from mao.approvals import Approvals
        from mao.governance.epochs import EpochStore

        snapshot = EpochStore(self.ctx).materialize(EpochStore(self.ctx).current())
        return Approvals(self.ctx, snapshot)

    def _approval_decide(self, arguments: dict[str, Any]) -> Any:
        approvals = self._approvals()
        current = approvals.get(str(arguments["approval_id"]))
        if current.route != "foreman":
            raise FactoryError(
                "FOREMAN_FORBIDDEN",
                "厂长不能决定这条审批",
                op="approval_decide",
            )
        approvals.apply_decision(
            current.id,
            str(arguments["decision"]),
            by="foreman:" + self.session.actor,
            note=str(arguments.get("note") or ""),
        )
        return approvals.view(current.id)

    def _secret_request(self, arguments: dict[str, Any]) -> Any:
        from mao.secrets import request_secret, station_snapshot

        station = str(arguments["station"])
        snapshot = station_snapshot(self.ctx, station) or self.epochs.materialize(self.epochs.current())
        return request_secret(
            self.ctx,
            snapshot,
            name=str(arguments["name"]),
            secret_class=str(arguments["secret_class"]),
            station=station,
            reason=str(arguments["reason"]),
            origin="foreman:" + self.session.actor,
        )

    def _secret_verify(self, arguments: dict[str, Any]) -> Any:
        from mao.egress import SecretStore
        from mao.secrets import direct_label, station_snapshot, verify_secret

        name = str(arguments["name"])
        station = str(arguments["station"])
        store = SecretStore(self.ctx.root)
        injected = store.inject([name])
        if name not in injected:
            raise FactoryError("SECRET_NOT_FOUND", "找不到秘密", name=name)
        snapshot = station_snapshot(self.ctx, station) or self.epochs.materialize(self.epochs.current())
        result = verify_secret(
            self.ctx,
            snapshot,
            station=station,
            name=name,
            value=injected[name],
            label=direct_label(self.ctx, name),
        )
        return {
            "name": name,
            "ok": result.ok,
            "code": result.code,
            "exit_code": result.exit_code,
        }

    def _escalate(self, arguments: dict[str, Any]) -> Any:
        from mao.inbox import Inbox

        options = arguments.get("options", [])
        if "options" in arguments and (
            not isinstance(options, list)
            or not 1 <= len(options) <= 10
            or any(not isinstance(item, str) for item in options)
        ):
            raise FactoryError("INBOX_REQUEST_INVALID", "选项不合法", reason="options")
        detail = arguments.get("detail", "")
        if not isinstance(detail, str):
            raise FactoryError("INBOX_REQUEST_INVALID", "说明不合法", reason="detail")
        request_id = self.ctx.ids.new("hr")
        return Inbox(self.ctx).open(
            "question",
            subject=request_id,
            origin="foreman:" + self.session.actor,
            summary=str(arguments["summary"]),
            payload={"detail": detail, "options": list(options)},
        )

    def _talk_card(self, kind: str, result: Any) -> None:
        if self.session.role != "foreman" or not self.session.wake_id:
            return
        try:
            from mao.supervisor import SupervisorConfig

            if SupervisorConfig.load(self.ctx.root).mode != "resident":
                return
        except FactoryError:
            return
        rows = self.ctx.db.query(
            "SELECT id FROM agent_turns WHERE id = ? AND agent = 'foreman' AND state = 'running'",
            (self.session.wake_id,),
        )
        if not rows:
            return
        from mao.talk import append_changeset_card, append_escalate_card, append_secret_card

        if kind == "changeset":
            append_changeset_card(self.ctx, result)
        elif kind == "escalate":
            append_escalate_card(self.ctx, result)
        elif kind == "secret":
            append_secret_card(self.ctx, result)

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


_ASSET_SKIP = object()


class _Proto(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


def _action_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return []
    ref = value.get("id")
    if isinstance(ref, str) and ref:
        return [{"kind": "object", "id": ref}]
    return []


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
    "asset_get": ({"asset_id": {"type": "string"}}, ["asset_id"]),
    "asset_list": ({"kind": {"type": "string"}, "subject": {"type": "string"}}, []),
    "asset_closure": ({"roots": {"type": "array"}}, ["roots"]),
    "asset_put": ({"document": {"type": "object"}}, ["document"]),
    "knowledge_heads": ({"subject": {"type": "string"}}, ["subject"]),
    "knowledge_trial_get": ({"trial_id": {"type": "string"}}, ["trial_id"]),
    "knowledge_trial_open": (
        {
            "target": {"type": "string"},
            "contract": {"type": "string"},
            "purpose": {"type": "string", "enum": ["knowledge", "takeover", "release"]},
        },
        ["target", "contract", "purpose"],
    ),
    "knowledge_evaluate": (
        {"trial_id": {"type": "string"}, "evidence": {"type": "object"}},
        ["trial_id", "evidence"],
    ),
    "knowledge_trial_cancel": (
        {"trial_id": {"type": "string"}, "reason": {"type": "string"}},
        ["trial_id", "reason"],
    ),
    "knowledge_promote": (
        {"target": {"type": "string"}, "trial_id": {"type": "string"}, "expected": {}},
        ["target"],
    ),
    "knowledge_invalidate": (
        {"target": {"type": "string"}, "reason": {"type": "string"}},
        ["target", "reason"],
    ),
    "agent_takeover_prepare": ({"trial_id": {"type": "string"}}, ["trial_id"]),
    "agent_takeover_finish": ({"trial_id": {"type": "string"}}, ["trial_id"]),
    "agent_takeover_abort": (
        {"trial_id": {"type": "string"}, "reason": {"type": "string"}},
        ["trial_id", "reason"],
    ),
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
    "escalate": (
        {
            "summary": {"type": "string"},
            "detail": {"type": "string"},
            "options": {"type": "array"},
        },
        ["summary"],
    ),
    "inbox_get": ({"request_id": {"type": "string"}}, ["request_id"]),
    "inbox_list": ({"status": {"type": "string"}}, []),
    "instance_list": ({"station": {"type": "string"}, "state": {"type": "string"}}, []),
    "approval_get": ({"approval_id": {"type": "string"}}, ["approval_id"]),
    "approval_decide": (
        {
            "approval_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["approve", "deny"]},
            "note": {"type": "string"},
        },
        ["approval_id", "decision"],
    ),
    "approval_escalate": (
        {"approval_id": {"type": "string"}, "note": {"type": "string"}},
        ["approval_id"],
    ),
    "secret_request": (
        {
            "name": {"type": "string"},
            "secret_class": {"type": "string"},
            "station": {"type": "string"},
            "reason": {"type": "string"},
        },
        ["name", "secret_class", "station", "reason"],
    ),
    "secret_verify": (
        {"name": {"type": "string"}, "station": {"type": "string"}},
        ["name", "station"],
    ),
    "talk_reply": (
        {"text": {"type": "string"}, "in_reply_to": {"type": "string"}},
        ["text"],
    ),
    "talk_propose_run": (
        {
            "line": {"type": "string"},
            "inputs": {"type": "object"},
            "sink_mode": {"type": "string"},
        },
        ["line", "inputs"],
    ),
    "run_start": (
        {"line": {"type": "string"}, "inputs": {"type": "object"}},
        ["line", "inputs"],
    ),
    "ask_human": (
        {
            "summary": {"type": "string"},
            "detail": {"type": "string"},
            "options": {"type": "array"},
        },
        ["summary"],
    ),
    "remind": (
        {"after": {"type": "string"}, "note": {"type": "string"}},
        ["after", "note"],
    ),
    "agent_send": (
        {"to": {"type": "string"}, "text": {"type": "string"}},
        ["to", "text"],
    ),
}
