"""厂长值守：取唤醒票并启动一次 agent CLI。"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.ctl import CtlServer, McpCtlHandler
from mao.durable import LeaseKeeper, LeaseStore
from mao.egress import register_job_cap
from mao.errors import FactoryError
from mao.foreman import WakeQueue, redact
from mao.governance.git import git
from mao.mcp import McpServer, Session
from mao.operator import build_runtime_invocation, expand_control_vars, parse_transcript
from mao.sandbox import Mount, SandboxSpec

_FIELDS = (
    "runtime",
    "model",
    "reviewers",
    "models",
    "image",
    "network",
    "session_timeout_s",
    "session_budget_usd",
    "daily_budget_usd",
    "max_turns",
    "max_attempts",
    "backoff_s",
    "env",
)
_ENV_PREFIXES = ("FACTORY_", "GIT_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


@dataclass(frozen=True)
class SupervisorConfig:
    runtime: str
    model: str
    reviewers: tuple[str, ...]
    models: Mapping[str, str]
    image: str
    network: str
    session_timeout_s: float
    session_budget_usd: float
    daily_budget_usd: float
    max_turns: int
    max_attempts: int
    backoff_s: float
    env: Mapping[str, str]

    @classmethod
    def load(cls, root: Path) -> SupervisorConfig:
        path = Path(root) / "factory.json"
        if not path.is_file():
            raise FactoryError("SUPERVISOR_CONFIG_MISSING", "缺少 factory.json", path=str(path))
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FactoryError("SUPERVISOR_CONFIG_MISSING", "factory.json 无法解析", path=str(path)) from exc
        section = body.get("foreman") if isinstance(body, dict) else None
        if not isinstance(section, dict):
            raise FactoryError("SUPERVISOR_CONFIG_MISSING", "缺少 foreman 段", path=str(path))
        for name in _FIELDS:
            if name not in section:
                raise FactoryError("SUPERVISOR_CONFIG_INVALID", f"缺少 foreman.{name}", field=name)
        env = {str(key): str(value) for key, value in dict(section.get("env") or {}).items()}
        for name in env:
            if name.startswith(_ENV_PREFIXES):
                raise FactoryError("SUPERVISOR_CONFIG_INVALID", "环境变量不允许覆盖", field="env")
        if str(section.get("network")) not in {"none", "egress"}:
            raise FactoryError("SUPERVISOR_CONFIG_INVALID", "network 不合法", field="network")
        models = {str(key): str(value) for key, value in dict(section["models"]).items()}
        reviewers = tuple(str(item) for item in section["reviewers"])
        for ref in reviewers:
            if ref not in models:
                raise FactoryError("SUPERVISOR_CONFIG_INVALID", "缺少模型", field=f"models.{ref}")
        return cls(
            runtime=str(section["runtime"]),
            model=str(section["model"]),
            reviewers=reviewers,
            models=models,
            image=str(section["image"]),
            network=str(section["network"]),
            session_timeout_s=float(section["session_timeout_s"]),
            session_budget_usd=float(section["session_budget_usd"]),
            daily_budget_usd=float(section["daily_budget_usd"]),
            max_turns=int(section["max_turns"]),
            max_attempts=int(section["max_attempts"]),
            backoff_s=float(section["backoff_s"]),
            env=env,
        )


@dataclass(frozen=True)
class SessionRecord:
    id: str
    wake_id: str
    role: str
    runtime: str
    actor: str
    model: str
    epoch: str
    state: str
    exit_code: int | None
    cost_usd: float
    turns: int | None
    proposed: tuple[str, ...]
    reviewed: tuple[str, ...]
    error: dict[str, Any] | None
    started_at: str
    ended_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "wake_id": self.wake_id,
            "role": self.role,
            "runtime": self.runtime,
            "actor": self.actor,
            "model": self.model,
            "epoch": self.epoch,
            "state": self.state,
            "exit_code": self.exit_code,
            "cost_usd": self.cost_usd,
            "turns": self.turns,
            "proposed": list(self.proposed),
            "reviewed": list(self.reviewed),
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class ForemanSupervisor:
    def __init__(
        self, ctx: Any, epochs: Any, config: SupervisorConfig, *, pipeline: Any | None = None
    ) -> None:
        self.ctx = ctx
        self.epochs = epochs
        self.config = _validate(config)
        if pipeline is None:
            from mao.governance.changesets import ChangePipeline

            pipeline = ChangePipeline(ctx, epochs)
        self.pipeline = pipeline
        self._started = False
        self._keeper: LeaseKeeper | None = None
        self._budget_noted = False

    def start(self) -> list[str]:
        store = LeaseStore(self.ctx)
        if not store.acquire("supervisor", owner=self.ctx.owner):
            holder = store.holder("supervisor")
            raise FactoryError(
                "SUPERVISOR_ALREADY_RUNNING",
                "值守已在运行",
                owner="" if holder is None else holder.owner,
                expires_at="" if holder is None else holder.expires_at,
            )
        self._keeper = LeaseKeeper(store, "supervisor", owner=self.ctx.owner)
        self._keeper.start()
        recovered = self._recover()
        self._started = True
        return recovered

    def run_once(self) -> SessionRecord | None:
        if not self._started:
            raise FactoryError("SUPERVISOR_NOT_STARTED", "值守尚未 start")
        if self._over_budget():
            return None
        wake = WakeQueue(self.ctx).next(by="supervisor")
        if wake is None:
            return None
        role = "reviewer" if wake.signal == "review" else "foreman"
        epoch = self.epochs.current()
        snapshot = self.epochs.materialize(epoch)
        if role == "reviewer":
            actor = self._reviewer(wake)
            if actor is None:
                return None
        else:
            actor = self.config.runtime
        model = self.config.models.get(actor, self.config.model)
        return self._session(wake, role, actor, model, epoch, snapshot)

    def sessions(self, *, state: str | None = None) -> list[SessionRecord]:
        return list_sessions(self.ctx, state=state)

    def shutdown(self) -> None:
        if self._keeper is not None:
            self._keeper.stop()
            self._keeper = None
        LeaseStore(self.ctx).release("supervisor", owner=self.ctx.owner)
        self._started = False

    def serve(self, stop: threading.Event, *, idle_s: float = 5.0) -> None:
        if not self._started:
            self.start()
        try:
            while not stop.is_set():
                if self._keeper is not None and self._keeper.lost.is_set():
                    break
                try:
                    record = self.run_once()
                except Exception:
                    logging.getLogger("mao").exception("值守会话失败")
                    self._note_session_exception()
                    if stop.wait(idle_s):
                        break
                    continue
                if record is None and stop.wait(idle_s):
                    break
                if self._keeper is not None and self._keeper.lost.is_set():
                    break
        finally:
            self.shutdown()

    def _ensure_proxies(self) -> None:
        if getattr(self.ctx, "proxies", None) is not None:
            return
        from mao.egress import start_owned_proxies

        start_owned_proxies(self.ctx, model_port=0, timeout_s=60.0)

    def _note_session_exception(self) -> None:
        rows = self.ctx.db.query(
            "SELECT id, wake_id, role FROM foreman_sessions WHERE state = 'running' ORDER BY id"
        )
        for row in rows:
            error = {"code": "SESSION_FAILED", "detail": {}}
            now = utc_iso(self.ctx.clock.now())
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE foreman_sessions SET state = 'failed', error = ?, ended_at = ? WHERE id = ?",
                    (canonical_json(error), now, row["id"]),
                )
            wake = WakeQueue(self.ctx).get(str(row["wake_id"]))
            if wake.status == "taken":
                self._close_wake(wake, str(row["role"]), "failed", error, {"proposed": [], "reviewed": []})
        running = {
            str(row["wake_id"])
            for row in self.ctx.db.query("SELECT wake_id FROM foreman_sessions WHERE state = 'running'")
        }
        for row in self.ctx.db.query("SELECT id FROM wakes WHERE status = 'taken' ORDER BY id"):
            if str(row["id"]) in running:
                continue
            wake = WakeQueue(self.ctx).get(str(row["id"]))
            delay = self.config.backoff_s * (2**wake.attempts)
            not_before = utc_iso(self.ctx.clock.now() + timedelta(seconds=delay))
            WakeQueue(self.ctx).reopen(wake.id, not_before=not_before, failed=True, by="supervisor")

    def _over_budget(self) -> bool:
        cutoff = utc_iso(self.ctx.clock.now() - timedelta(hours=24))
        rows = self.ctx.db.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM foreman_sessions WHERE started_at >= ?",
            (cutoff,),
        )
        spent = float(rows[0]["spent"] or 0)
        if spent < self.config.daily_budget_usd:
            self._budget_noted = False
            return False
        if not self._budget_noted:
            _event(
                self.ctx,
                "foreman.budget_exhausted",
                {"spent_usd": spent, "daily_budget_usd": self.config.daily_budget_usd},
            )
            self._budget_noted = True
        return True

    def _reviewer(self, wake: Any) -> str | None:
        try:
            cs = self.pipeline.get(wake.subject)
        except FactoryError:
            WakeQueue(self.ctx).done(wake.id, by="supervisor")
            return None
        if (
            cs.state not in {"verified", "shadow", "canary"}
            or len(self.pipeline.approvals(cs.id)) >= cs.reviews_required
        ):
            WakeQueue(self.ctx).done(wake.id, by="supervisor")
            return None
        tried: list[str] = []
        for ref in self.config.reviewers:
            tried.append(ref)
            if ref == cs.proposer or ref == str(cs.proposer_runtime).split(":", 1)[-1]:
                continue
            if any(item.endswith(ref) or item == ref for item in self.pipeline.approvals(cs.id)):
                continue
            return ref
        WakeQueue(self.ctx).fail(wake.id, code="REVIEWERS_EXHAUSTED", by="supervisor")
        return None

    def _session(
        self, wake: Any, role: str, actor: str, model: str, epoch: str, snapshot: Any
    ) -> SessionRecord:
        session_id = self.ctx.ids.new("fs")
        runtime_key = snapshot.key("OperatorRuntime", actor)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO foreman_sessions (id, wake_id, role, runtime, actor, model, epoch, state, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (session_id, wake.id, role, runtime_key, actor, model, epoch, now),
            )
        _event(
            self.ctx,
            "foreman.session.started",
            {"session": session_id, "wake": wake.id, "role": role, "actor": actor, "epoch": epoch},
        )
        register_job_cap(self.ctx.db, session_id, "foreman", self.config.session_budget_usd)
        folder = Path(self.ctx.root) / "foreman" / "sessions" / session_id
        for name in ("in", "ctl", "workspace", "logs"):
            (folder / name).mkdir(parents=True, exist_ok=True)
        (folder / "ctl" / "requests").mkdir(exist_ok=True)
        (folder / "ctl" / "responses").mkdir(exist_ok=True)
        self._clone(folder / "workspace" / "registry", epoch)
        brief = _brief(role, wake, epoch)
        (folder / "in" / "brief.md").write_text(brief, encoding="utf-8")
        (folder / "in" / "wake.json").write_text(
            canonical_json(
                {
                    "id": wake.id,
                    "signal": wake.signal,
                    "subject": wake.subject,
                    "payload": wake.payload,
                    "attempts": wake.attempts,
                    "epoch": epoch,
                    "session": session_id,
                    "role": role,
                }
            ),
            encoding="utf-8",
        )
        (folder / "ctl" / "job.json").write_text(
            canonical_json(
                {
                    "job_id": session_id,
                    "attempt": wake.attempts + 1,
                    "station": role,
                    "epoch": epoch,
                    "inputs": {},
                    "params": {},
                    "budget_usd": self.config.session_budget_usd,
                    "timeout_s": self.config.session_timeout_s,
                }
            ),
            encoding="utf-8",
        )
        runtime = snapshot.spec(runtime_key)
        mcp = (runtime.get("invoke") or {}).get("mcp") or {}
        mcp_format = str(mcp.get("format") or "")
        if mcp_format == "claude-json":
            if not mcp.get("flag"):
                raise FactoryError("INVOCATION_INVALID", "claude-json 缺少 flag", reason="mcp.flag")
            (folder / "ctl" / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "factory": {
                                "command": "python3",
                                "args": ["/opt/factory/bin/factoryctl", "mcp"],
                                "env": {"FACTORY_CTL": "/factory/ctl"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
        elif mcp_format == "codex-toml":
            if mcp.get("flag"):
                raise FactoryError("INVOCATION_INVALID", "codex-toml 不能带 flag", reason="mcp.flag")
            codex_home = folder / "workspace" / ".codex"
            codex_home.mkdir(parents=True, exist_ok=True)
            (codex_home / "config.toml").write_text(
                "\n".join(
                    [
                        "[mcp_servers.factory]",
                        'command = "python3"',
                        'args = ["/opt/factory/bin/factoryctl", "mcp"]',
                        'env = { FACTORY_CTL = "/factory/ctl" }',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        else:
            raise FactoryError("INVOCATION_INVALID", "invoke.mcp 格式不认识", reason=mcp_format or "missing")
        task, contract = brief.split("\n\n", 1) if "\n\n" in brief else (brief, brief)
        inv = build_runtime_invocation(
            snapshot,
            actor,
            task=task,
            contract=contract,
            model=model,
            max_turns=self.config.max_turns,
            budget_usd=self.config.session_budget_usd,
            timeout_s=self.config.session_timeout_s,
        )
        argv = list(inv.argv)
        if mcp_format == "claude-json":
            argv.extend([str(mcp["flag"]), "/factory/ctl/mcp.json"])
        env = dict(inv.env)
        env.update(self.config.env)
        env.update(
            {
                "FACTORY_ACTOR": actor,
                "FACTORY_ROLE": role,
                "FACTORY_JOB_ID": session_id,
                "FACTORY_ATTEMPT": str(wake.attempts + 1),
                "FACTORY_WAKE": wake.id,
                "GIT_AUTHOR_NAME": actor,
                "GIT_COMMITTER_NAME": actor,
                "GIT_AUTHOR_EMAIL": f"{session_id}@foreman.invalid",
                "GIT_COMMITTER_EMAIL": f"{session_id}@foreman.invalid",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "/workspace/registry",
            }
        )
        if mcp_format == "codex-toml":
            env["CODEX_HOME"] = "/workspace/.codex"
        network = _session_network(self.config.network, runtime)
        if network == "egress":
            self._ensure_proxies()
            from mao.egress import proxy_env

            docker = getattr(self.ctx.provider, "name", "") == "docker"
            urls = proxy_env(self.ctx.proxies, docker=docker)
            env.update(urls)
            inv = expand_control_vars(
                inv,
                {
                    "FACTORY_JOB_ID": session_id,
                    "FACTORY_ATTEMPT": str(wake.attempts + 1),
                    "MODEL_PROXY": urls["MODEL_PROXY"],
                    "EGRESS_PROXY": urls["EGRESS_PROXY"],
                },
            )
            argv = list(inv.argv)
            if mcp_format == "claude-json":
                argv.extend([str(mcp["flag"]), "/factory/ctl/mcp.json"])
            env.update(inv.env)
        server = McpServer(
            self.ctx,
            self.epochs,
            Session(role, actor, wake.id, session_id, folder / "workspace" / "registry"),
            pipeline=self.pipeline,
        )
        ctl = CtlServer(folder / "ctl", McpCtlHandler(server), self.ctx.clock)
        ctl.start(interval_s=0.05)
        raw = folder / "logs" / "transcript.raw"
        err = folder / "logs" / "stderr.raw"
        mounts = [
            Mount(str((folder / "in").resolve()), "/factory/in", True),
            Mount(str((folder / "ctl").resolve()), "/factory/ctl", False),
            Mount(str((folder / "workspace").resolve()), "/workspace", False),
            Mount(str((self.ctx.root / "tools" / "factory").resolve()), "/opt/factory", True),
        ]
        inject = runtime.get("inject") or {}
        volume_ref = inject.get("volume")
        if volume_ref and getattr(self.ctx.provider, "name", "") == "docker":
            hexpart = str(volume_ref).split("sha256:", 1)[-1]
            op_volume = "mao-op-" + hexpart[:12]
            self.ctx.provider.prepare_inject_volume(str(volume_ref), op_volume)
            mounts.append(Mount("volume:" + op_volume, str(inject.get("mount") or "/opt/operator"), True))
        spec = SandboxSpec(
            name=f"mao-{session_id}-1",
            image=self.config.image,
            mounts=tuple(mounts),
            workdir="/workspace/registry",
            env=env,
            network=network,
        )
        git_bin = shutil.which("git")
        commands = getattr(self.ctx.provider, "commands", None)
        if git_bin and isinstance(commands, dict):
            commands.setdefault("git", [git_bin])
        sandbox = self.ctx.provider.open(spec)
        try:
            result = sandbox.exec(
                argv, timeout_s=self.config.session_timeout_s, stdout=raw, stderr=err, env=env
            )
        finally:
            ctl.stop()
            sandbox.close()
        summary = parse_transcript(raw, inv.transcript or "stream-json") if raw.is_file() else None
        archived = redact(
            self.ctx, snapshot, raw.read_text(encoding="utf-8", errors="replace") if raw.is_file() else ""
        )
        (folder / "logs" / "transcript.jsonl").write_text(archived, encoding="utf-8")
        stderr_text = redact(
            self.ctx, snapshot, err.read_text(encoding="utf-8", errors="replace") if err.is_file() else ""
        )
        (folder / "logs" / "stderr.txt").write_text(stderr_text, encoding="utf-8")
        raw.unlink(missing_ok=True)
        err.unlink(missing_ok=True)
        state, error, exit_code = _judge(wake, role, result, summary, server.outcome)
        cost = summary.cost_usd if summary is not None else 0.0
        turns = summary.turns if summary is not None else None
        ended = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE foreman_sessions SET state = ?, exit_code = ?, cost_usd = ?, turns = ?, outcome = ?, "
                "error = ?, ended_at = ? WHERE id = ?",
                (
                    state,
                    exit_code,
                    cost,
                    turns,
                    canonical_json(
                        {
                            "proposed": list(server.outcome["proposed"]),
                            "reviewed": list(server.outcome["reviewed"]),
                        }
                    ),
                    None if error is None else canonical_json(error),
                    ended,
                    session_id,
                ),
            )
        _event(
            self.ctx,
            "foreman.session.finished",
            {
                "session": session_id,
                "state": state,
                "exit_code": exit_code,
                "cost_usd": cost,
                "proposed": list(server.outcome["proposed"]),
                "reviewed": list(server.outcome["reviewed"]),
                "code": None if error is None else error.get("code"),
            },
        )
        self._close_wake(wake, role, state, error, server.outcome)
        return (
            self.sessions()[0]
            if False
            else _record(self.ctx.db.query("SELECT * FROM foreman_sessions WHERE id = ?", (session_id,))[0])
        )

    def _close_wake(
        self, wake: Any, role: str, state: str, error: dict[str, Any] | None, outcome: dict[str, list[str]]
    ) -> None:
        queue = WakeQueue(self.ctx)
        if state == "ok" and role == "foreman":
            if wake.signal == "conversion" and outcome["proposed"]:
                from mao.foreman import ConversionStore

                ConversionStore(self.ctx).mark(
                    wake.subject, "in_progress", changeset_id=outcome["proposed"][-1]
                )
            queue.done(wake.id, by="supervisor")
            return
        if state == "ok" and role == "reviewer":
            queue.done(wake.id, by="supervisor")
            return
        code = "SESSION_FAILED" if error is None else str(error.get("code"))
        if wake.attempts + 1 >= self.config.max_attempts:
            queue.fail(wake.id, code=code, by="supervisor")
            return
        delay = self.config.backoff_s * (2**wake.attempts)
        not_before = utc_iso(self.ctx.clock.now() + timedelta(seconds=delay))
        queue.reopen(wake.id, not_before=not_before, failed=True, by="supervisor")

    def _clone(self, dest: Path, epoch: str) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        git(
            self.epochs.registry,
            "clone",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.filemode=false",
            "--quiet",
            "--no-hardlinks",
            str(self.epochs.registry),
            str(dest),
            timeout_s=self.epochs.timeout_s,
        )
        git(
            dest,
            "fetch",
            "--quiet",
            str(self.epochs.registry),
            "+refs/factory/*:refs/factory/*",
            "+refs/changesets/*:refs/changesets/*",
            timeout_s=self.epochs.timeout_s,
        )
        git(
            dest,
            "-c",
            "core.autocrlf=false",
            "checkout",
            "--quiet",
            "--detach",
            epoch,
            timeout_s=self.epochs.timeout_s,
        )
        git(
            dest,
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.filemode=false",
            "reset",
            "--hard",
            "--quiet",
            timeout_s=self.epochs.timeout_s,
        )

    def _recover(self) -> list[str]:
        rows = self.ctx.db.query("SELECT id FROM foreman_sessions WHERE state = 'running' ORDER BY id")
        found = [str(row["id"]) for row in rows]
        for session_id in found:
            self.ctx.provider.reap(session_id, 1)
            row = self.ctx.db.query("SELECT * FROM foreman_sessions WHERE id = ?", (session_id,))[0]
            error = {"code": "CONTROL_PLANE_RESTART", "detail": {}}
            now = utc_iso(self.ctx.clock.now())
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE foreman_sessions SET state = 'failed', error = ?, ended_at = ? WHERE id = ?",
                    (canonical_json(error), now, session_id),
                )
            wake = WakeQueue(self.ctx).get(str(row["wake_id"]))
            if wake.status == "taken":
                self._close_wake(wake, str(row["role"]), "failed", error, {"proposed": [], "reviewed": []})
        taken = self.ctx.db.query("SELECT id FROM wakes WHERE status = 'taken' ORDER BY id")
        for row in taken:
            WakeQueue(self.ctx).reopen(str(row["id"]), not_before=None, failed=False, by="supervisor")
        return found


def _references_model_proxy(runtime: Mapping[str, Any]) -> bool:
    texts: list[str] = []
    for item in (runtime.get("invoke") or {}).get("argv") or []:
        texts.append(item if isinstance(item, str) else str(item))
    for value in (runtime.get("env") or {}).values():
        texts.append(value if isinstance(value, str) else str(value))
    return any("${MODEL_PROXY}" in text for text in texts)


def _session_network(config_network: str, runtime: Mapping[str, Any]) -> str:
    if _references_model_proxy(runtime):
        return "egress"
    return config_network


def _validate(config: SupervisorConfig) -> SupervisorConfig:
    if config.network not in {"none", "egress"}:
        raise FactoryError("SUPERVISOR_CONFIG_INVALID", "network 不合法", field="network")
    for name, value in config.env.items():
        if name.startswith(_ENV_PREFIXES):
            raise FactoryError("SUPERVISOR_CONFIG_INVALID", "环境变量不允许覆盖", field="env")
        del value
    for ref in config.reviewers:
        if ref not in config.models:
            raise FactoryError("SUPERVISOR_CONFIG_INVALID", "缺少模型", field=f"models.{ref}")
    return config


def _brief(role: str, wake: Any, epoch: str) -> str:
    if role == "reviewer":
        task = (
            f"你是复核者。ChangeSet {wake.subject} 等待复核。\n"
            "用 changeset_status、registry_diff 查看它，然后调用 review_submit，verdict 为 approve 或 deny，note 写理由。\n"
            "复核完就退出。"
        )
    else:
        task = (
            f"你是这座工厂的厂长。本次会话处理唤醒票 {wake.id}。\n"
            f"信号：{wake.signal}。对象：{wake.subject}。载荷：{canonical_json(wake.payload)}。\n"
            f"/workspace/registry 是 active epoch {epoch} 的 Registry 克隆。\n"
            "用 MCP 服务器 factory 的工具查询工厂。需要改结构时：在 /workspace/registry 提交 git commit，"
            f"然后调用 changeset_propose，base_epoch 填 {epoch}，commit 填你的提交。\n"
            "处理完就退出。"
        )
    contract = (
        "工厂契约：\n"
        "1. 只通过 MCP 服务器 factory 操作工厂。没有合入、推进、回滚的工具，不要尝试绕过。\n"
        "2. 不要修改 constitution/ 下的文件。宪法只由人修改。\n"
        "3. 不要读取或输出密钥。\n"
        "4. 你的身份由会话决定，参数里不要冒充别人。"
    )
    return task + "\n\n" + contract


def _judge(
    wake: Any, role: str, result: Any, summary: Any, outcome: dict[str, list[str]]
) -> tuple[str, dict[str, Any] | None, int | None]:
    exit_code = None if result is None else result.exit_code
    if result is not None and result.timed_out:
        return "timeout", {"code": "SESSION_TIMEOUT", "detail": {}}, exit_code
    failed = (exit_code not in (None, 0)) or (summary is not None and summary.is_error)
    if failed:
        return "failed", {"code": "SESSION_FAILED", "detail": {"exit_code": exit_code}}, exit_code
    if role == "foreman" and wake.signal == "conversion" and not outcome["proposed"]:
        return (
            "failed",
            {"code": "SESSION_NO_OUTCOME", "detail": {"signal": wake.signal, "subject": wake.subject}},
            exit_code,
        )
    if role == "reviewer" and wake.subject not in outcome["reviewed"]:
        return (
            "failed",
            {"code": "SESSION_NO_OUTCOME", "detail": {"signal": wake.signal, "subject": wake.subject}},
            exit_code,
        )
    return "ok", None, exit_code


def list_sessions(ctx: Any, *, state: str | None = None) -> list[SessionRecord]:
    if state is None:
        rows = ctx.db.query("SELECT * FROM foreman_sessions ORDER BY id")
    else:
        rows = ctx.db.query("SELECT * FROM foreman_sessions WHERE state = ? ORDER BY id", (state,))
    return [_record(row) for row in rows]


def _record(row: Any) -> SessionRecord:
    outcome = json.loads(row["outcome"] or "{}")
    error = None if row["error"] is None else json.loads(row["error"])
    return SessionRecord(
        str(row["id"]),
        str(row["wake_id"]),
        str(row["role"]),
        str(row["runtime"]),
        str(row["actor"]),
        str(row["model"]),
        str(row["epoch"]),
        str(row["state"]),
        None if row["exit_code"] is None else int(row["exit_code"]),
        float(row["cost_usd"] or 0),
        None if row["turns"] is None else int(row["turns"]),
        tuple(outcome.get("proposed") or []),
        tuple(outcome.get("reviewed") or []),
        error,
        str(row["started_at"]),
        None if row["ended_at"] is None else str(row["ended_at"]),
    )


def _event(ctx: Any, kind: str, data: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
            (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(data)),
        )
