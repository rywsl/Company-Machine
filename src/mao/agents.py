"""常驻 Agent 配置与邮箱（spec/system/agents.md）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from mao.base import canonical_bytes, canonical_json, parse_duration, utc_iso
from mao.errors import FactoryError

_LABEL = re.compile(r"^[^=\s]+=[^=\s]+$")
_LOG = __import__("logging").getLogger("mao.agents")
_RESIDENT_NAME = "foreman"
_TALK_OBLIGATION = "本轮有 human talk 消息时必须 talk_reply。"
_HANDOFF_LEGACY = (
    "请把仍在进行的事项、关键事实和下一步写进 /workspace/notebook/HANDOFF.md（覆盖旧文件），然后结束本轮。"
)
_HANDOFF_ASSET = (
    "请把仍在进行的事项写成 /workspace/notebook/HANDOFF.next.json，"
    "不要替换上一个可信交接。文件必须是完整交接 document，然后结束本轮。"
)
ASSET_FOREMAN_GUIDANCE = (
    "正式知识与临时记忆分开。notebook 只是便笺。"
    "用现有正常工作收集证据，经已授权契约晋升；未知条件保留为问题。"
)
_FOREMAN_CHARTER = (
    "你是这座工厂的厂长。\n"
    "/workspace/registry 是 active epoch 的 Registry 克隆。\n"
    "用 MCP 服务器 factory 的工具查询工厂。需要改结构时：在 /workspace/registry 提交 git commit，"
    "然后调用 changeset_propose，commit 填你的提交。\n"
    "处理完就退出。\n" + _TALK_OBLIGATION
)
_REMIND_MIN_S = 60.0
_REMIND_MAX_S = 30 * 24 * 3600.0


@dataclass(frozen=True)
class AgentConfig:
    name: str
    key: str
    runtime: str
    model: str
    charter: str
    policy: str
    lines: tuple[str, ...]
    schedule: tuple[tuple[float, str], ...]
    beans: tuple[tuple[str, str | None], ...]
    daily_usd: float
    turn_usd: float
    turn_timeout_s: float
    batch: int
    rotate_turns: int
    rotate_tokens: int
    max_turns: int
    digest: str


def agent_config(snapshot: Any, name: str, *, spec: Mapping[str, Any] | None = None) -> AgentConfig:
    keys = [key for key in snapshot.keys("Agent") if key.startswith(f"Agent:{name}@")]
    if not keys:
        raise FactoryError("AGENT_NOT_FOUND", "没有这个 Agent", agent=name)
    if len(keys) > 1:
        raise FactoryError("AGENT_INVALID", "同名 Agent 有多个版本", agent=name, reason="versions")
    key = keys[0]
    body = dict(spec) if spec is not None else dict(snapshot.spec(key))
    runtime_ref = str(body.get("runtime") or "")
    try:
        runtime_key = snapshot.key("OperatorRuntime", runtime_ref)
        runtime = snapshot.spec(runtime_key)
    except FactoryError as exc:
        raise FactoryError("AGENT_INVALID", "runtime 不合法", agent=name, reason="runtime") from exc
    if runtime.get("kind") != "agent-cli" or not (runtime.get("invoke") or {}).get("resume"):
        raise FactoryError("AGENT_INVALID", "runtime 不合法", agent=name, reason="runtime")
    allowed = list(runtime.get("models_allowed") or [])
    model = body.get("model")
    if model is None:
        if not allowed:
            raise FactoryError("AGENT_INVALID", "没有可用模型", agent=name, reason="model")
        model = allowed[0]
    elif allowed and model not in allowed:
        raise FactoryError("AGENT_INVALID", "模型不在允许列表", agent=name, reason="model")
    charter = body.get("charter")
    if not isinstance(charter, str) or not charter or len(charter) > 8000:
        raise FactoryError("AGENT_INVALID", "charter 不合法", agent=name, reason="charter")
    policy_ref = str(body.get("policy") or "")
    try:
        policy_key = snapshot.key("Policy", policy_ref)
        policy = snapshot.spec(policy_key)
    except FactoryError as exc:
        raise FactoryError("AGENT_INVALID", "policy 不合法", agent=name, reason="policy") from exc
    if policy.get("secrets") == "from-machine":
        raise FactoryError("AGENT_INVALID", "policy 不能从机器取秘密", agent=name, reason="policy")
    lines = tuple(snapshot.key("Line", item) for item in (body.get("lines") or []))
    schedule = _schedule(name, (body.get("subscribe") or {}).get("schedule") or [])
    beans = _beans(snapshot, name, (body.get("subscribe") or {}).get("beans") or [])
    daily, turn, timeout = _budget(name, body.get("budget"))
    batch, rotate_turns, rotate_tokens, max_turns = _context(name, body.get("context") or {})
    return AgentConfig(
        name,
        key,
        runtime_key,
        str(model),
        charter,
        policy_key,
        lines,
        schedule,
        beans,
        daily,
        turn,
        timeout,
        batch,
        rotate_turns,
        rotate_tokens,
        max_turns,
        hashlib.sha256(canonical_bytes(body)).hexdigest(),
    )


def _schedule(name: str, items: Any) -> tuple[tuple[float, str], ...]:
    if not isinstance(items, list):
        raise FactoryError("AGENT_INVALID", "schedule 不合法", agent=name, reason="schedule")
    found: list[tuple[float, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or "every" not in item:
            raise FactoryError("AGENT_INVALID", "schedule 不合法", agent=name, reason="schedule")
        try:
            seconds = parse_duration(str(item.get("every")))
        except FactoryError as exc:
            raise FactoryError("AGENT_INVALID", "schedule 不合法", agent=name, reason="schedule") from exc
        if seconds < 60:
            raise FactoryError("AGENT_INVALID", "schedule 少于 60 秒", agent=name, reason="schedule")
        found.append((float(seconds), str(item.get("note") or "")))
    return tuple(found)


def _beans(snapshot: Any, name: str, items: Any) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(items, list):
        raise FactoryError("AGENT_INVALID", "beans 不合法", agent=name, reason="label")
    found: list[tuple[str, str | None]] = []
    for item in items:
        if not isinstance(item, Mapping) or not item.get("type"):
            raise FactoryError("AGENT_INVALID", "beans 不合法", agent=name, reason="label")
        label = item.get("label")
        if label is not None and (not isinstance(label, str) or _LABEL.fullmatch(label) is None):
            raise FactoryError("AGENT_INVALID", "label 不是 K=V", agent=name, reason="label")
        found.append((snapshot.key("Type", str(item["type"])), None if label is None else str(label)))
    return tuple(found)


def _budget(name: str, body: Any) -> tuple[float, float, float]:
    if not isinstance(body, Mapping) or any(
        key not in body for key in ("daily_usd", "turn_usd", "turn_timeout")
    ):
        raise FactoryError("AGENT_INVALID", "budget 不完整", agent=name, reason="budget")
    daily = _positive_number(body.get("daily_usd"))
    turn = _positive_number(body.get("turn_usd"))
    if daily is None or turn is None:
        raise FactoryError("AGENT_INVALID", "budget 不是正数", agent=name, reason="budget")
    try:
        timeout = parse_duration(str(body.get("turn_timeout")))
    except FactoryError as exc:
        raise FactoryError("AGENT_INVALID", "turn_timeout 不合法", agent=name, reason="budget") from exc
    if timeout <= 0:
        raise FactoryError("AGENT_INVALID", "turn_timeout 不是正数", agent=name, reason="budget")
    return daily, turn, float(timeout)


def _context(name: str, body: Mapping[str, Any]) -> tuple[int, int, int, int]:
    defaults = {"batch": 20, "rotate_turns": 200, "rotate_tokens": 150000, "max_turns": 50}
    values: dict[str, int] = {}
    for key, default in defaults.items():
        raw = body.get(key, default) if isinstance(body, Mapping) else default
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise FactoryError("AGENT_INVALID", "context 不是正整数", agent=name, reason="context")
        values[key] = raw
    return values["batch"], values["rotate_turns"], values["rotate_tokens"], values["max_turns"]


def agent_names(snapshot: Any, root: Any) -> list[str]:
    names = []
    for key in snapshot.keys("Agent"):
        doc = snapshot.doc(key)
        lifecycle = (doc.get("metadata") or {}).get("lifecycle", "active")
        if lifecycle not in (None, "active"):
            continue
        names.append(str(doc["metadata"]["name"]))
    if resident_foreman(root) is not None:
        names.append(_RESIDENT_NAME)
    return sorted(set(names))


def resident_foreman(root: Any) -> Any:
    from pathlib import Path

    path = Path(root) / "factory.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    section = body.get("foreman") if isinstance(body, dict) else None
    if not isinstance(section, dict) or section.get("mode", "session") != "resident":
        return None
    from mao.supervisor import SupervisorConfig

    return SupervisorConfig.load(Path(root))


def foreman_agent(snapshot: Any, foreman: Any) -> AgentConfig:
    try:
        runtime_key = snapshot.key("OperatorRuntime", str(foreman.runtime))
        runtime = snapshot.spec(runtime_key)
    except FactoryError as exc:
        raise FactoryError("AGENT_INVALID", "runtime 不合法", agent=_RESIDENT_NAME, reason="runtime") from exc
    if not (runtime.get("invoke") or {}).get("resume"):
        raise FactoryError("AGENT_INVALID", "runtime 不合法", agent=_RESIDENT_NAME, reason="runtime")
    body = {
        "runtime": foreman.runtime,
        "model": foreman.model,
        "reviewers": list(foreman.reviewers),
        "models": dict(foreman.models),
        "image": foreman.image,
        "network": foreman.network,
        "session_timeout_s": foreman.session_timeout_s,
        "session_budget_usd": foreman.session_budget_usd,
        "daily_budget_usd": foreman.daily_budget_usd,
        "max_turns": foreman.max_turns,
        "max_attempts": foreman.max_attempts,
        "backoff_s": foreman.backoff_s,
        "env": dict(foreman.env),
        "mode": foreman.mode,
    }
    return AgentConfig(
        _RESIDENT_NAME,
        "",
        runtime_key,
        str(foreman.model),
        _FOREMAN_CHARTER,
        "",
        (),
        (),
        (),
        float(foreman.daily_budget_usd),
        float(foreman.session_budget_usd),
        float(foreman.session_timeout_s),
        20,
        200,
        150000,
        int(foreman.max_turns),
        hashlib.sha256(canonical_bytes(body)).hexdigest(),
    )


@dataclass(frozen=True)
class Message:
    id: str
    agent: str
    kind: str
    source: str
    body: dict[str, Any]
    state: str
    turn: str | None
    attempts: int
    not_before: str | None
    created_at: str
    enqueue_seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "kind": self.kind,
            "source": self.source,
            "body": dict(self.body),
            "state": self.state,
            "turn": self.turn,
            "attempts": self.attempts,
            "not_before": self.not_before,
            "created_at": self.created_at,
            "enqueue_seq": self.enqueue_seq,
        }


class Mailbox:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def post(
        self,
        agent: str,
        kind: str,
        source: str,
        body: Mapping[str, Any],
        *,
        not_before: str | None = None,
        delivery_source: str | None = None,
        delivery_kind: str | None = None,
        origin: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> Message:
        from mao.egress import SecretStore

        stored = SecretStore(self.ctx.root).redact_obj(dict(body))
        raw = canonical_json(stored).encode("utf-8")
        if len(raw) > 64 * 1024:
            raise FactoryError("AGENT_MESSAGE_TOO_LARGE", "消息过大", agent=agent, bytes=len(raw))
        now = utc_iso(self.ctx.clock.now())
        key_kind = delivery_kind
        key_source = delivery_source
        if kind == "schedule" and source.startswith("schedule:"):
            key_kind = key_kind or "schedule"
            if key_source is None:
                every = str(stored.get("every") or "")
                key_source = f"{agent}:{every}:{source}:{now}"
        if conn is None and key_kind and key_source:
            existing = self.ctx.db.query(
                "SELECT message_id FROM delivery_keys WHERE kind = ? AND source = ? AND target = ?",
                (key_kind, key_source, agent),
            )
            if existing:
                return self._load(str(existing[0]["message_id"]))
        message_id = self.ctx.ids.new("am")
        if conn is not None:
            return self._insert_message(
                conn,
                message_id,
                agent,
                kind,
                source,
                stored,
                not_before,
                now,
                key_kind,
                key_source,
                origin,
            )
        with self.ctx.db.tx() as conn:
            if key_kind and key_source:
                existing = conn.execute(
                    "SELECT message_id FROM delivery_keys WHERE kind = ? AND source = ? AND target = ?",
                    (key_kind, key_source, agent),
                ).fetchone()
                if existing is not None:
                    message_id = str(existing["message_id"])
                    return _message(
                        conn.execute(
                            "SELECT * FROM agent_messages WHERE id = ?",
                            (message_id,),
                        ).fetchone()
                    )
            seq = _alloc_enqueue_seq(conn)
            conn.execute(
                "INSERT INTO agents (name, state, updated_at) VALUES (?, 'active', ?) "
                "ON CONFLICT(name) DO NOTHING",
                (agent, now),
            )
            conn.execute(
                """INSERT INTO agent_messages (
                       id, agent, kind, source, body, state, turn, attempts, not_before,
                       created_at, updated_at, enqueue_seq
                   ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, 0, ?, ?, ?, ?)""",
                (message_id, agent, kind, source, canonical_json(stored), not_before, now, now, seq),
            )
            if key_kind and key_source:
                conn.execute(
                    "INSERT INTO delivery_keys (kind, source, target, message_id) VALUES (?, ?, ?, ?)",
                    (key_kind, key_source, agent, message_id),
                )
            if kind == "schedule" and source.startswith("schedule:"):
                index = int(source.split(":", 1)[1])
                conn.execute(
                    "INSERT INTO agent_schedule (agent, idx, last_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(agent, idx) DO UPDATE SET last_at = excluded.last_at",
                    (agent, index, now),
                )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'agent.message', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json({"agent": agent, "id": message_id, "kind": kind, "source": source}),
                ),
            )
            _insert_origin(conn, message_id, origin)
        return self._load(message_id)

    def _insert_message(
        self,
        conn: Any,
        message_id: str,
        agent: str,
        kind: str,
        source: str,
        stored: Mapping[str, Any],
        not_before: str | None,
        now: str,
        key_kind: str | None,
        key_source: str | None,
        origin: Mapping[str, Any] | None,
    ) -> Message:
        if key_kind and key_source:
            existing = conn.execute(
                "SELECT message_id FROM delivery_keys WHERE kind = ? AND source = ? AND target = ?",
                (key_kind, key_source, agent),
            ).fetchone()
            if existing is not None:
                return _message(
                    conn.execute(
                        "SELECT * FROM agent_messages WHERE id = ?",
                        (str(existing["message_id"]),),
                    ).fetchone()
                )
        seq = _alloc_enqueue_seq(conn)
        conn.execute(
            "INSERT INTO agents (name, state, updated_at) VALUES (?, 'active', ?) "
            "ON CONFLICT(name) DO NOTHING",
            (agent, now),
        )
        conn.execute(
            """INSERT INTO agent_messages (
                   id, agent, kind, source, body, state, turn, attempts, not_before,
                   created_at, updated_at, enqueue_seq
               ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, 0, ?, ?, ?, ?)""",
            (message_id, agent, kind, source, canonical_json(stored), not_before, now, now, seq),
        )
        if key_kind and key_source:
            conn.execute(
                "INSERT INTO delivery_keys (kind, source, target, message_id) VALUES (?, ?, ?, ?)",
                (key_kind, key_source, agent, message_id),
            )
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'agent.message', NULL, ?)",
            (
                self.ctx.ids.new("ev"),
                now,
                canonical_json({"agent": agent, "id": message_id, "kind": kind, "source": source}),
            ),
        )
        _insert_origin(conn, message_id, origin)
        return _message(conn.execute("SELECT * FROM agent_messages WHERE id = ?", (message_id,)).fetchone())

    def list(self, agent: str, *, state: str | None = None) -> list[Message]:
        sql = "SELECT * FROM agent_messages WHERE agent = ?"
        args: list[Any] = [agent]
        if state is not None:
            sql += " AND state = ?"
            args.append(state)
        sql += " ORDER BY enqueue_seq, id"
        return [_message(row) for row in self.ctx.db.query(sql, tuple(args))]

    def _load(self, message_id: str) -> Message:
        return _message(self.ctx.db.query("SELECT * FROM agent_messages WHERE id = ?", (message_id,))[0])


@dataclass(frozen=True)
class AgentsConfig:
    image: str
    network: str = "none"
    poll_s: float = 5.0
    env: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.env is None:
            object.__setattr__(self, "env", {})

    @classmethod
    def load(cls, root: Any) -> AgentsConfig:
        path = (
            root / "factory.json"
            if not isinstance(root, str)
            else __import__("pathlib").Path(root) / "factory.json"
        )
        from pathlib import Path

        path = Path(root) / "factory.json"
        if not path.is_file():
            raise FactoryError("AGENTS_CONFIG_MISSING", "缺少 factory.json", path=str(path))
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FactoryError("AGENTS_CONFIG_MISSING", "factory.json 无法解析", path=str(path)) from exc
        section = body.get("agents") if isinstance(body, dict) else None
        if not isinstance(section, dict):
            raise FactoryError("AGENTS_CONFIG_MISSING", "缺少 agents 段", path=str(path))
        image = section.get("image")
        if not isinstance(image, str) or not image:
            raise FactoryError("AGENTS_CONFIG_INVALID", "image 不合法", field="image")
        network = str(section.get("network") or "none")
        if network not in ("none", "egress"):
            raise FactoryError("AGENTS_CONFIG_INVALID", "network 不合法", field="network")
        poll = section.get("poll_s", 5)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or float(poll) <= 0:
            raise FactoryError("AGENTS_CONFIG_INVALID", "poll_s 不合法", field="poll_s")
        env = section.get("env") or {}
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
        ):
            raise FactoryError("AGENTS_CONFIG_INVALID", "env 不合法", field="env")
        banned = ("FACTORY_", "GIT_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
        if any(key.startswith(banned) for key in env):
            raise FactoryError("AGENTS_CONFIG_INVALID", "env 键不合法", field="env")
        return cls(image, network, float(poll), dict(env))


class AgentHost:
    def __init__(
        self,
        ctx: Any,
        epochs: Any,
        config: AgentsConfig,
        *,
        pipeline: Any = None,
        foreman: Any = None,
    ) -> None:
        self.ctx = ctx
        self.epochs = epochs
        self.config = config
        if pipeline is None:
            from mao.governance.changesets import ChangePipeline

            pipeline = ChangePipeline(ctx, epochs)
        self.pipeline = pipeline
        if foreman is None:
            foreman = resident_foreman(ctx.root)
        elif getattr(foreman, "mode", "session") != "resident":
            foreman = None
        self.foreman = foreman
        self._invalid: set[tuple[str, str, str]] = set()
        self._lease: Any = None

    def collect(self) -> list[str]:
        snapshot = self.epochs.materialize(self.epochs.current())
        created: list[str] = []
        created.extend(self._schedules(snapshot))
        created.extend(self._beans(snapshot))
        created.extend(self._wakes())
        return sorted(created)

    def run_once(self, name: str) -> Any:
        snapshot = self.epochs.materialize(self.epochs.current())
        try:
            cfg = self._config_for(snapshot, name)
        except FactoryError:
            return None
        self._ensure_row(name)
        row = self._agent_row(name)
        if str(row["state"]) == "paused":
            return None
        if self._budget_blocked(name, cfg):
            return None
        if int(row["rotate_pending"]):
            self._rotate(snapshot, cfg, row)
            row = self._agent_row(name)
            if str(row["state"]) == "paused":
                return None
        pending = self._pending(name, cfg.batch)
        if not pending:
            return None
        return self._work(snapshot, cfg, row, pending)

    def recover(self) -> list[str]:
        rows = self.ctx.db.query(
            "SELECT id, owner, lease_generation FROM agent_turns WHERE state = 'running' ORDER BY id"
        )
        found = []
        now = utc_iso(self.ctx.clock.now())
        store = None
        for row in rows:
            owner = None if row["owner"] is None else str(row["owner"])
            generation = None if row["lease_generation"] is None else int(row["lease_generation"])
            if owner and generation is not None:
                if store is None:
                    from mao.durable import LeaseStore

                    store = LeaseStore(self.ctx)
                held = store.valid("agents")
                if held is not None and held.owner == owner and held.generation == generation:
                    continue
            turn_id = str(row["id"])
            error = canonical_json(
                {"code": "AGENT_INTERRUPTED", "message": "轮次被中断", "detail": {"turn": turn_id}}
            )
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE agent_turns SET state = 'interrupted', error = ?, finished_at = ? WHERE id = ?",
                    (error, now, turn_id),
                )
                conn.execute(
                    "UPDATE agent_messages SET state = 'pending', turn = NULL WHERE turn = ? AND state = 'delivered'",
                    (turn_id,),
                )
            turn = self._turn(turn_id)
            if turn.kind == "work":
                from mao.learning import cancel_pending_takeover

                cancel_pending_takeover(self.ctx, turn.agent)
            self._event(
                "agent.turn.finished",
                {
                    "agent": turn.agent,
                    "turn": turn_id,
                    "state": "interrupted",
                    "cost_usd": 0,
                    "exit_code": None,
                    "code": "AGENT_INTERRUPTED",
                },
            )
            found.append(turn_id)
        return sorted(found)

    def turns(self, name: str) -> list[Any]:
        rows = self.ctx.db.query(
            "SELECT * FROM agent_turns WHERE agent = ? ORDER BY started_at, id",
            (name,),
        )
        return [_turn(row) for row in rows]

    def pause(self, name: str) -> None:
        self._ensure_row(name)
        self._set_state(name, "paused")

    def resume(self, name: str) -> None:
        self._ensure_row(name)
        self._set_state(name, "active")

    def tick(self) -> list[str]:
        self.collect()
        snapshot = self.epochs.materialize(self.epochs.current())
        found = []
        for name in self._valid(snapshot):
            if not self._pending(name, 1):
                continue
            turn = self.run_once(name)
            if turn is not None:
                found.append(turn.id)
        return found

    def serve(self, stop: Any) -> None:
        from mao.durable import LeaseKeeper, LeaseStore

        store = LeaseStore(self.ctx)
        token = store.acquire_fenced("agents", owner=self.ctx.owner)
        if token is None:
            raise FactoryError("AGENTS_ALREADY_RUNNING", "agents 已在运行")
        self._lease = token
        keeper = LeaseKeeper(store, "agents", owner=self.ctx.owner)
        keeper.token = token
        keeper.start()
        try:
            self.recover()
            while not stop.is_set() and not keeper.lost.is_set():
                try:
                    self.tick()
                except FactoryError as exc:
                    if exc.code == "LEASE_LOST":
                        keeper.lost.set()
                        break
                    _LOG.exception("agent tick 失败")
                except Exception:
                    _LOG.exception("agent tick 失败")
                if keeper.lost.is_set() or stop.is_set():
                    break
                remaining = self.config.poll_s
                while remaining > 0 and not stop.is_set() and not keeper.lost.is_set():
                    step = min(0.05, remaining)
                    if stop.wait(step):
                        break
                    remaining -= step
        finally:
            self._lease = None
            keeper.stop()
            if keeper.lost.is_set():
                self._event(
                    "agent.lease_lost",
                    {
                        "resource": token.resource,
                        "owner": token.owner,
                        "generation": token.generation,
                    },
                )
            else:
                store.release_fenced(token)

    def max_turn_timeout_s(self) -> float:
        try:
            snapshot = self.epochs.materialize(self.epochs.current())
        except FactoryError:
            return 0.0
        best = 0.0
        for name in agent_names(snapshot, self.ctx.root):
            try:
                best = max(best, self._config_for(snapshot, name).turn_timeout_s)
            except FactoryError:
                continue
        return best

    def close(self) -> None:
        self._sandboxes = {}

    def _schedules(self, snapshot: Any) -> list[str]:
        now = self.ctx.clock.now()
        found: list[str] = []
        for name in self._valid(snapshot):
            cfg = self._config_for(snapshot, name)
            for index, (seconds, note) in enumerate(cfg.schedule):
                rows = self.ctx.db.query(
                    "SELECT last_at FROM agent_schedule WHERE agent = ? AND idx = ?",
                    (name, index),
                )
                due = True
                if rows:
                    last = _parse(str(rows[0]["last_at"]))
                    due = now >= last + timedelta(seconds=seconds)
                if not due:
                    continue
                every = _every_text(snapshot, name, index)
                stamp = utc_iso(now)
                version = _agent_version(snapshot, name)
                message = Mailbox(self.ctx).post(
                    name,
                    "schedule",
                    f"schedule:{index}",
                    {"note": note, "every": every},
                    delivery_source=f"{name}:{version}:{index}:{stamp}",
                )
                found.append(message.id)
        return found

    def _beans(self, snapshot: Any) -> list[str]:
        rows = self.ctx.db.query("SELECT seq FROM cursors WHERE name = 'agents.beans'")
        cursor = int(rows[0]["seq"]) if rows else 0
        events = self.ctx.db.query(
            "SELECT seq, data FROM events WHERE kind = 'bean.sealed' AND seq > ? ORDER BY seq",
            (cursor,),
        )
        found: list[str] = []
        max_seq = cursor
        configs = []
        for name in self._valid(snapshot):
            configs.append(self._config_for(snapshot, name))
        for event in events:
            max_seq = max(max_seq, int(event["seq"]))
            data = json.loads(event["data"])
            bean_id = str(data.get("bean") or "")
            if not bean_id:
                continue
            bean = self.ctx.warehouse.get(bean_id)
            labels = self.ctx.warehouse.labels(bean_id)
            for cfg in configs:
                for index, (type_key, label) in enumerate(cfg.beans):
                    if not _type_matches(snapshot, bean.type, type_key):
                        continue
                    if label is not None:
                        key, value = label.split("=", 1)
                        if labels.get(key) != value:
                            continue
                    message = Mailbox(self.ctx).post(
                        cfg.name,
                        "bean",
                        f"bean:{index}",
                        {"bean": bean_id, "type": bean.type, "labels": dict(labels), "job": data.get("job")},
                    )
                    found.append(message.id)
        if events:
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "INSERT INTO cursors (name, seq) VALUES ('agents.beans', ?) "
                    "ON CONFLICT(name) DO UPDATE SET seq = excluded.seq",
                    (max_seq,),
                )
        return found

    def _config_for(self, snapshot: Any, name: str) -> AgentConfig:
        if self.foreman is not None and name == _RESIDENT_NAME:
            cfg = foreman_agent(snapshot, self.foreman)
            from mao.assets import assets_config

            if assets_config(self.ctx) is not None:
                cfg = replace(cfg, charter=cfg.charter + "\n" + ASSET_FOREMAN_GUIDANCE)
            return cfg
        return agent_config(snapshot, name)

    def _valid(self, snapshot: Any) -> list[str]:
        names = []
        for name in agent_names(snapshot, self.ctx.root):
            try:
                self._config_for(snapshot, name)
            except FactoryError as exc:
                mark = (name, exc.code, str(exc.detail.get("reason")))
                if mark not in self._invalid:
                    self._invalid.add(mark)
                    self._event(
                        "agent.invalid",
                        {"agent": name, "code": exc.code, "reason": exc.detail.get("reason")},
                    )
                continue
            names.append(name)
        return names

    def _work(self, snapshot: Any, cfg: AgentConfig, row: Any, pending: list[Message]) -> Any:
        from mao.operator import build_runtime_invocation, parse_transcript

        name = cfg.name
        session = None if row["session"] is None else str(row["session"])
        number = int(row["turns_in_conv"]) + 1
        turn_id = self.ctx.ids.new("at")
        now = utc_iso(self.ctx.clock.now())
        host = self.ctx.root / "agents" / name
        workspace = workspace_path(self.ctx, name)
        (workspace / "notebook").mkdir(parents=True, exist_ok=True)
        incoming = host / "in"
        _clear(incoming)
        payload = [item.to_json() for item in pending]
        (incoming / "messages.json").write_text(canonical_json(payload), encoding="utf-8")
        from mao.learning import mount_turn_context, record_use

        extra, mounted, handoff = mount_turn_context(self.ctx, self.epochs, name, incoming)
        task = _task_text(cfg, pending, session, workspace)
        if extra:
            task = task + "\n\n" + extra
        with self.ctx.db.tx() as conn:
            self._guard_lease(conn)
            lease = getattr(self, "_lease", None)
            conn.execute(
                """INSERT INTO agent_turns (
                       id, agent, conversation, number, kind, session, state, messages, started_at,
                       owner, lease_generation
                   ) VALUES (?, ?, ?, ?, 'work', ?, 'running', ?, ?, ?, ?)""",
                (
                    turn_id,
                    name,
                    int(row["conversation"]),
                    number,
                    session,
                    canonical_json([item.id for item in pending]),
                    now,
                    None if lease is None else lease.owner,
                    None if lease is None else lease.generation,
                ),
            )
            conn.execute(
                "UPDATE agent_messages SET state = 'delivered', turn = ?, updated_at = ? WHERE id = ?",
                (turn_id, now, pending[0].id),
            )
            for item in pending[1:]:
                conn.execute(
                    "UPDATE agent_messages SET state = 'delivered', turn = ?, updated_at = ? WHERE id = ?",
                    (turn_id, now, item.id),
                )
            record_use(
                self.ctx,
                snapshot,
                "turn",
                turn_id,
                subject=f"agent:{name}",
                fresh=session is None,
                handoff=handoff,
                assets=mounted,
            )
            _bind_turn_origin(conn, turn_id, pending)
        self._event(
            "agent.turn.started",
            {
                "agent": name,
                "turn": turn_id,
                "kind": "work",
                "conversation": int(row["conversation"]),
                "number": number,
                "messages": [item.id for item in pending],
            },
        )
        from mao.egress import register_job_cap

        register_job_cap(self.ctx.db, turn_id, "agent", cfg.turn_usd)
        from mao.objectives import note_turn_reserved

        note_turn_reserved(self.ctx, self.epochs, turn_id, cfg.turn_usd)
        runtime_ref = cfg.runtime.split(":", 1)[-1]
        invocation = build_runtime_invocation(
            snapshot,
            runtime_ref,
            task=task,
            contract=_CONTRACT,
            model=cfg.model,
            max_turns=cfg.max_turns,
            budget_usd=cfg.turn_usd,
            timeout_s=cfg.turn_timeout_s,
            session=session,
        )
        sandbox = self._sandbox(snapshot, name, cfg)
        logs = host / "turns" / turn_id
        logs.mkdir(parents=True, exist_ok=True)
        try:
            result, server = self._run_cli(snapshot, cfg, turn_id, number, invocation, sandbox, logs)
        except OSError as exc:
            return self._fail_open_turn(name, turn_id, "AGENT_RESULT_INVALID", str(exc))
        _store_turn_actions(self.ctx, turn_id, server.actions)
        summary = None
        try:
            summary = parse_transcript(logs / "transcript.raw", invocation.transcript or "stream-json")
        except FactoryError:
            summary = None
        wait_marker = _waiting_for_result(logs, code="SESSION_FAILED")
        leaked = self._archive(snapshot, logs)
        state, code, error = _turn_outcome(
            self.ctx,
            turn_id,
            result,
            summary,
            resume_required=_runtime_resumes(snapshot, cfg.runtime),
            agent=name,
        )
        defer_message = wait_marker and code == "SESSION_FAILED"
        if leaked and state == "ok":
            state, code = "failed", "SECRET_LEAK"
            error = {"code": "SECRET_LEAK", "message": "打码后仍像密钥", "detail": {}}
        exit_code = result.exit_code
        cost = 0.0 if summary is None else float(summary.cost_usd)
        ended = summary.session_id if summary is not None and summary.session_id else session
        finished = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            self._guard_lease(conn)
            conn.execute(
                """UPDATE agent_turns SET state = ?, session = ?, cost_usd = ?, input_tokens = ?,
                   exit_code = ?, error = ?, finished_at = ? WHERE id = ?""",
                (
                    state,
                    ended,
                    cost,
                    0 if summary is None else summary.input_tokens,
                    exit_code,
                    None if error is None else canonical_json(error),
                    finished,
                    turn_id,
                ),
            )
            if state == "ok":
                conn.execute(
                    "UPDATE agent_messages SET state = 'done', updated_at = ? WHERE turn = ?",
                    (finished, turn_id),
                )
                tokens = 0 if summary is None else summary.input_tokens
                rotate = 1 if number >= cfg.rotate_turns or tokens >= cfg.rotate_tokens else 0
                conn.execute(
                    "UPDATE agents SET session = ?, turns_in_conv = turns_in_conv + 1, last_tokens = ?, "
                    "rotate_pending = ?, updated_at = ? WHERE name = ?",
                    (ended, tokens, rotate, finished, name),
                )
            else:
                if defer_message:
                    # A foreman may intentionally yield while a dispatched job is
                    # still running.  That is a handled delivery, not a failed
                    # delivery: the run-result message is the durable wake-up
                    # for the next foreman turn.  Leaving the cycle message
                    # pending would re-run it before the child can finish and
                    # eventually consume the retry budget.
                    conn.execute(
                        "UPDATE agent_messages SET state = 'done', turn = NULL, "
                        "updated_at = ? WHERE turn = ?",
                        (finished, turn_id),
                    )
                else:
                    conn.execute(
                        "UPDATE agent_messages SET attempts = attempts + 1, updated_at = ? WHERE turn = ?",
                        (finished, turn_id),
                    )
                    conn.execute(
                        "UPDATE agent_messages SET state = 'pending', turn = NULL WHERE turn = ? AND attempts < 3",
                        (turn_id,),
                    )
                    conn.execute(
                        "UPDATE agent_messages SET state = 'dropped' WHERE turn = ? AND attempts >= 3",
                        (turn_id,),
                    )
        from mao.learning import cancel_pending_takeover, install_pending_takeover

        if state == "ok":
            install_pending_takeover(self.ctx, name)
        else:
            cancel_pending_takeover(self.ctx, name)
        dropped = self.ctx.db.query(
            "SELECT id FROM agent_messages WHERE turn = ? AND state = 'dropped'",
            (turn_id,),
        )
        if dropped:
            self._drop(name, [str(item["id"]) for item in dropped])
        self._event(
            "agent.turn.finished",
            {
                "agent": name,
                "turn": turn_id,
                "state": state,
                "cost_usd": cost,
                "exit_code": exit_code,
                "code": code,
            },
        )
        from mao.objectives import note_turn_settled

        note_turn_settled(self.ctx, turn_id, cost)
        self._settle_wakes(pending, state, error, server)
        if self.foreman is not None and name == _RESIDENT_NAME:
            from mao.talk import settle_foreman_turn

            replied = False if server is None else bool(server.outcome.get("talk_replied"))
            settle_foreman_turn(self.ctx, pending, state, replied)
        return self._turn(turn_id)

    def rotate(self, name: str) -> None:
        self._ensure_row(name)
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE agents SET rotate_pending = 1, updated_at = ? WHERE name = ?",
                (utc_iso(self.ctx.clock.now()), name),
            )

    def _rotate(self, snapshot: Any, cfg: AgentConfig, row: Any) -> None:
        from mao.assets import assets_config

        if assets_config(self.ctx) is None:
            self._rotate_legacy(snapshot, cfg, row)
            return
        self._rotate_assets(snapshot, cfg, row)

    def _rotate_legacy(self, snapshot: Any, cfg: AgentConfig, row: Any) -> None:
        session = None if row["session"] is None else str(row["session"])
        ok = True
        if session:
            ok = self._handoff(snapshot, cfg, row, session).state == "ok"
        now = utc_iso(self.ctx.clock.now())
        conversation = int(row["conversation"]) + 1
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE agents SET session = NULL, conversation = ?, turns_in_conv = 0, last_tokens = 0, "
                "rotate_pending = 0, updated_at = ? WHERE name = ?",
                (conversation, now, cfg.name),
            )
        self._event("agent.rotated", {"agent": cfg.name, "conversation": conversation, "ok": ok})

    def _rotate_assets(self, snapshot: Any, cfg: AgentConfig, row: Any) -> None:
        from mao.assets import capture_handoff

        session = None if row["session"] is None else str(row["session"])
        turn = self._handoff(snapshot, cfg, row, session) if session else None
        document = _read_handoff_next(self.ctx, cfg.name)
        if turn is not None and turn.state == "ok" and document is not None:
            try:
                expected = _handoff_head(self.ctx, cfg.name)
                now = utc_iso(self.ctx.clock.now())
                conversation = int(row["conversation"]) + 1
                with self.ctx.db.tx() as conn:
                    capture_handoff(self.ctx, cfg.name, document, expected=expected, by="system")
                    conn.execute(
                        "UPDATE agents SET session = NULL, conversation = ?, turns_in_conv = 0, last_tokens = 0, "
                        "rotate_pending = 0, state = 'active', updated_at = ? WHERE name = ?",
                        (conversation, now, cfg.name),
                    )
                self._event("agent.rotated", {"agent": cfg.name, "conversation": conversation, "ok": True})
                _read_handoff_next(self.ctx, cfg.name, unlink=True)
                return
            except FactoryError:
                pass
        self._block_handoff(cfg.name)

    def _block_handoff(self, name: str) -> None:
        from mao.inbox import Inbox, RequestKind

        now = utc_iso(self.ctx.clock.now())
        origin = f"asset-handoff:{name}"
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE agents SET state = 'paused', rotate_pending = 1, updated_at = ? WHERE name = ?",
                (now, name),
            )
        self._event("agent.handoff.blocked", {"agent": name, "state": "paused", "reason": "handoff"})
        existing = self.ctx.db.query(
            "SELECT id FROM human_requests WHERE origin = ? AND status = 'open'",
            (origin,),
        )
        if not existing:
            Inbox(self.ctx).open(
                RequestKind.QUESTION.value,
                subject=origin,
                origin=origin,
                summary="交接没有固化，岗位已暂停",
                payload={"agent": name},
            )

    def _handoff(self, snapshot: Any, cfg: AgentConfig, row: Any, session: str) -> Any:
        from mao.operator import build_runtime_invocation, parse_transcript

        name = cfg.name
        number = int(row["turns_in_conv"]) + 1
        turn_id = self.ctx.ids.new("at")
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO agent_turns (
                       id, agent, conversation, number, kind, session, state, messages, started_at
                   ) VALUES (?, ?, ?, ?, 'handoff', ?, 'running', '[]', ?)""",
                (turn_id, name, int(row["conversation"]), number, session, now),
            )
        self._event(
            "agent.turn.started",
            {
                "agent": name,
                "turn": turn_id,
                "kind": "handoff",
                "conversation": int(row["conversation"]),
                "number": number,
                "messages": [],
            },
        )
        from mao.egress import register_job_cap

        register_job_cap(self.ctx.db, turn_id, "agent", cfg.turn_usd)
        from mao.assets import assets_config

        task = _HANDOFF_LEGACY if assets_config(self.ctx) is None else _HANDOFF_ASSET
        runtime_ref = cfg.runtime.split(":", 1)[-1]
        invocation = build_runtime_invocation(
            snapshot,
            runtime_ref,
            task=task,
            contract=_CONTRACT,
            model=cfg.model,
            max_turns=cfg.max_turns,
            budget_usd=cfg.turn_usd,
            timeout_s=cfg.turn_timeout_s,
            session=session,
        )
        sandbox = self._sandbox(snapshot, name, cfg)
        logs = self.ctx.root / "agents" / name / "turns" / turn_id
        logs.mkdir(parents=True, exist_ok=True)
        try:
            result, _server = self._run_cli(snapshot, cfg, turn_id, number, invocation, sandbox, logs)
        except OSError as exc:
            return self._fail_open_turn(name, turn_id, "AGENT_RESULT_INVALID", str(exc))
        summary = None
        try:
            summary = parse_transcript(logs / "transcript.raw", invocation.transcript or "stream-json")
        except FactoryError:
            summary = None
        leaked = self._archive(snapshot, logs)
        state, code, error = _turn_outcome(
            self.ctx,
            turn_id,
            result,
            summary,
            resume_required=_runtime_resumes(snapshot, cfg.runtime),
            agent=name,
        )
        if leaked and state == "ok":
            state, code = "failed", "SECRET_LEAK"
            error = {"code": "SECRET_LEAK", "message": "打码后仍像密钥", "detail": {}}
        ended = summary.session_id if summary is not None and summary.session_id else session
        finished = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE agent_turns SET state = ?, session = ?, finished_at = ?, cost_usd = ?, error = ? WHERE id = ?",
                (
                    state,
                    ended,
                    finished,
                    0.0 if summary is None else summary.cost_usd,
                    None if error is None else canonical_json(error),
                    turn_id,
                ),
            )
        self._event(
            "agent.turn.finished",
            {
                "agent": name,
                "turn": turn_id,
                "state": state,
                "cost_usd": 0.0 if summary is None else summary.cost_usd,
                "exit_code": result.exit_code,
                "code": code,
            },
        )
        return self._turn(turn_id)

    def _wakes(self) -> list[str]:
        if self.foreman is None:
            return []
        found: list[str] = []
        while True:
            message_id = self._claim_wake()
            if message_id is None:
                break
            found.append(message_id)
        return found

    def _claim_wake(self) -> str | None:
        from mao.foreman import _wake

        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM wakes WHERE status = 'open' AND signal != 'review' "
                "AND (not_before IS NULL OR not_before <= ?) ORDER BY at, id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            wake_id = str(row["id"])
            claimed = conn.execute(
                "UPDATE wakes SET status = 'taken', by = 'agents', updated_at = ? "
                "WHERE id = ? AND status = 'open'",
                (now, wake_id),
            )
            changed = claimed.rowcount
            if changed < 0:
                changed = int(conn.execute("SELECT changes()").fetchone()[0])
            if changed != 1:
                return None
            fresh = conn.execute("SELECT * FROM wakes WHERE id = ?", (wake_id,)).fetchone()
            wake = _wake(fresh)
            message = Mailbox(self.ctx).post(
                _RESIDENT_NAME,
                "wake",
                "wake:" + wake.id,
                {"wake": _wake_fields(wake)},
                delivery_kind="wake",
                delivery_source=wake.id,
            )
            return message.id

    def _run_cli(
        self,
        snapshot: Any,
        cfg: AgentConfig,
        turn_id: str,
        number: int,
        invocation: Any,
        sandbox: Any,
        logs: Any,
    ) -> tuple[Any, Any]:
        from mao.ctl import CtlServer, McpCtlHandler
        from mao.mcp import McpServer, Session

        name = cfg.name
        builtin = self.foreman is not None and name == _RESIDENT_NAME
        role = "foreman" if builtin else "agent"
        runtime_ref = cfg.runtime.split(":", 1)[-1]
        host = self.ctx.root / "agents" / name
        source = self._prepare_foreman_clone(self.epochs.current()) if builtin else None
        invocation = self._expand_egress(invocation, sandbox, turn_id)
        argv, extra = self._prepare_ctl(
            snapshot, runtime_ref, host, turn_id, cfg, role, list(invocation.argv)
        )
        env = {str(key): str(value) for key, value in self.config.env.items()}
        if builtin:
            env.update({str(key): str(value) for key, value in self.foreman.env.items()})
        env.update(invocation.env)
        env.update(extra)
        env.update(
            {
                "FACTORY_JOB_ID": turn_id,
                "FACTORY_ATTEMPT": "1",
                "FACTORY_TURN": str(number),
                "FACTORY_ROLE": role,
                "FACTORY_AGENT": name,
                "FACTORY_ACTOR": runtime_ref,
            }
        )
        if builtin:
            env.update(
                {
                    "GIT_AUTHOR_NAME": runtime_ref,
                    "GIT_COMMITTER_NAME": runtime_ref,
                    "GIT_AUTHOR_EMAIL": f"{turn_id}@foreman.invalid",
                    "GIT_COMMITTER_EMAIL": f"{turn_id}@foreman.invalid",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": "/workspace/registry",
                }
            )
        if getattr(sandbox.spec, "network", "none") == "egress":
            from mao.egress import proxy_env

            docker = getattr(self.ctx.provider, "name", "") == "docker"
            env.update(proxy_env(self.ctx.proxies, docker=docker))
        lease = getattr(self, "_lease", None)
        bound = self.ctx.db.query(
            "SELECT objective_id, cycle_id, task_id FROM agent_turns WHERE id = ?",
            (turn_id,),
        )
        objective_id = (
            None if not bound or bound[0]["objective_id"] is None else str(bound[0]["objective_id"])
        )
        cycle_id = None if not bound or bound[0]["cycle_id"] is None else str(bound[0]["cycle_id"])
        task_id = None if not bound or bound[0]["task_id"] is None else str(bound[0]["task_id"])
        session = (
            Session(
                "foreman",
                runtime_ref,
                turn_id,
                turn_id,
                source,
                turn_id=turn_id,
                lease_owner=None if lease is None else lease.owner,
                lease_generation=None if lease is None else lease.generation,
                objective_id=objective_id,
                cycle_id=cycle_id,
                task_id=task_id,
            )
            if builtin
            else Session(
                "agent",
                runtime_ref,
                None,
                turn_id,
                None,
                name,
                turn_id=turn_id,
                lease_owner=None if lease is None else lease.owner,
                lease_generation=None if lease is None else lease.generation,
                objective_id=objective_id,
                cycle_id=cycle_id,
                task_id=task_id,
            )
        )
        server = McpServer(self.ctx, self.epochs, session, pipeline=self.pipeline)
        ctl = CtlServer(host / "ctl", McpCtlHandler(server), self.ctx.clock)
        ctl.start(interval_s=0.05)
        try:
            self._guard_lease()
            result = sandbox.exec(
                argv,
                timeout_s=cfg.turn_timeout_s,
                stdout=logs / "transcript.raw",
                stderr=logs / "stderr.raw",
                env=env,
                workdir="/workspace/registry" if builtin else "/workspace",
            )
        finally:
            ctl.stop()
        return result, server

    def _prepare_ctl(
        self,
        snapshot: Any,
        runtime_ref: str,
        host: Any,
        turn_id: str,
        cfg: AgentConfig,
        role: str,
        argv: list[str],
    ) -> tuple[list[str], dict[str, str]]:
        ctl = host / "ctl"
        _clear(ctl)
        (ctl / "requests").mkdir(parents=True, exist_ok=True)
        (ctl / "responses").mkdir(parents=True, exist_ok=True)
        (ctl / "job.json").write_text(
            canonical_json(
                {
                    "job_id": turn_id,
                    "attempt": 1,
                    "station": role,
                    "epoch": self.epochs.current(),
                    "inputs": {},
                    "params": {},
                    "budget_usd": cfg.turn_usd,
                    "timeout_s": cfg.turn_timeout_s,
                }
            ),
            encoding="utf-8",
        )
        runtime = snapshot.spec(snapshot.key("OperatorRuntime", runtime_ref))
        mcp = (runtime.get("invoke") or {}).get("mcp") or {}
        fmt = str(mcp.get("format") or "")
        extra: dict[str, str] = {}
        if fmt == "claude-json":
            if not mcp.get("flag"):
                raise FactoryError("INVOCATION_INVALID", "claude-json 缺少 flag", reason="mcp.flag")
            (ctl / "mcp.json").write_text(
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
            argv = [*argv, str(mcp["flag"]), "/factory/ctl/mcp.json"]
        elif fmt == "codex-toml":
            if mcp.get("flag"):
                raise FactoryError("INVOCATION_INVALID", "codex-toml 不能带 flag", reason="mcp.flag")
            codex_home = workspace_path(self.ctx, cfg.name) / ".codex"
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
            extra["CODEX_HOME"] = "/workspace/.codex"
        else:
            raise FactoryError("INVOCATION_INVALID", "invoke.mcp 格式不认识", reason=fmt or "missing")
        return argv, extra

    def _prepare_foreman_clone(self, epoch: str) -> Any:
        dest = workspace_path(self.ctx, _RESIDENT_NAME) / "registry"
        return clone_foreman_registry(self.ctx, dest, epoch)

    def _settle_wakes(
        self, pending: list[Message], state: str, error: dict[str, Any] | None, server: Any
    ) -> None:
        if self.foreman is None:
            return
        from mao.foreman import WakeQueue

        queue = WakeQueue(self.ctx)
        for item in pending:
            if item.kind != "wake":
                continue
            wake_id = str((item.body.get("wake") or {}).get("id") or "")
            if not wake_id:
                continue
            wake = queue.get(wake_id)
            if wake.status != "taken":
                continue
            if state == "ok":
                self._close_ok_wake(wake, server)
                continue
            rows = self.ctx.db.query("SELECT state FROM agent_messages WHERE id = ?", (item.id,))
            if rows and str(rows[0]["state"]) == "dropped":
                code = "SESSION_FAILED" if error is None else str(error.get("code") or "SESSION_FAILED")
                queue.fail(wake.id, code=code, by="agents")

    def _close_ok_wake(self, wake: Any, server: Any) -> None:
        from mao.foreman import ConversionStore, WakeQueue

        proposed = [] if server is None else list(server.outcome["proposed"])
        if wake.signal == "conversion" and not proposed:
            self._retry_wake(wake, "SESSION_NO_OUTCOME")
            return
        if wake.signal == "approval" and _approval_still_foreman(self, wake):
            self._retry_wake(wake, "APPROVAL_UNDECIDED")
            return
        if wake.signal == "conversion" and proposed:
            ConversionStore(self.ctx).mark(wake.subject, "in_progress", changeset_id=proposed[-1])
        WakeQueue(self.ctx).done(wake.id, by="agents")

    def _retry_wake(self, wake: Any, code: str) -> None:
        from mao.foreman import WakeQueue

        queue = WakeQueue(self.ctx)
        if wake.attempts + 1 >= int(self.foreman.max_attempts):
            queue.fail(wake.id, code=code, by="agents")
            return
        delay = float(self.foreman.backoff_s) * (2 ** int(wake.attempts))
        not_before = utc_iso(self.ctx.clock.now() + timedelta(seconds=delay))
        queue.reopen(wake.id, not_before=not_before, failed=True, by="agents")

    def _sandbox(self, snapshot: Any, name: str, cfg: AgentConfig) -> Any:
        held = getattr(self, "_sandboxes", None)
        if held is None:
            self._sandboxes = {}
            self._box_digest = {}
            held = self._sandboxes
        digests = self._box_digest
        image, network, plan = self._box_network(snapshot, name, cfg)
        workspace = workspace_path(self.ctx, name)
        digest = _box_digest(cfg, image, network, str(workspace))
        current = held.get(name)
        if current is not None and digests.get(name) == digest and current.alive():
            return current
        if current is not None:
            self._drop_box(name, current)
        spec = self._box_spec(snapshot, name, cfg, image, network)
        row = self._agent_row(name)
        stored = None if row["sandbox"] is None else str(row["sandbox"])
        sandbox = None
        if stored == digest:
            try:
                sandbox = self.ctx.provider.attach(spec)
            except FactoryError:
                sandbox = None
            if sandbox is not None and not sandbox.alive():
                sandbox = None
        if sandbox is None:
            spec = self._open_egress(name, spec, plan)
            sandbox = self.ctx.provider.open(spec)
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE agents SET sandbox = ?, updated_at = ? WHERE name = ?",
                    (digest, utc_iso(self.ctx.clock.now()), name),
                )
        held[name] = sandbox
        digests[name] = digest
        return sandbox

    def _drop_box(self, name: str, sandbox: Any) -> None:
        from mao.egress import close_egress

        try:
            sandbox.close()
        except FactoryError:
            _LOG.exception("关闭 Agent 沙箱失败")
        close_egress(self.ctx.db, f"agent-{name}")
        self._sandboxes.pop(name, None)
        self._box_digest.pop(name, None)

    def _box_network(self, snapshot: Any, name: str, cfg: AgentConfig) -> tuple[str, str, Any]:
        builtin = self.foreman is not None and name == _RESIDENT_NAME
        image = self.foreman.image if builtin else self.config.image
        network = self.foreman.network if builtin else self.config.network
        runtime = snapshot.spec(cfg.runtime)
        if _runtime_wants_proxy(runtime):
            network = "egress"
        plan = None
        if cfg.policy:
            from mao.egress import SecretStore, build_egress

            plan = build_egress(
                snapshot,
                cfg.key,
                SecretStore(self.ctx.root),
                station={
                    "policy": cfg.policy,
                    "role": "transform",
                    "operator": {"runtime": cfg.runtime.split(":", 1)[-1]},
                },
            )
            if plan.network == "egress":
                network = "egress"
        return image, network, plan

    def _box_spec(self, snapshot: Any, name: str, cfg: AgentConfig, image: str, network: str) -> Any:
        from mao.sandbox import Mount, SandboxSpec

        host = self.ctx.root / "agents" / name
        workspace = workspace_path(self.ctx, name)
        for folder in (host / "in", host / "ctl", workspace / "notebook"):
            folder.mkdir(parents=True, exist_ok=True)
        builtin = self.foreman is not None and name == _RESIDENT_NAME
        mounts = [
            Mount(str((host / "in").resolve()), "/factory/in", True),
            Mount(str((host / "ctl").resolve()), "/factory/ctl", False),
            Mount(str(workspace.resolve()), "/workspace", False),
            Mount(str((self.ctx.root / "tools" / "factory").resolve()), "/opt/factory", True),
        ]
        runtime = snapshot.spec(cfg.runtime)
        inject = runtime.get("inject") or {}
        volume_ref = inject.get("volume")
        if volume_ref and getattr(self.ctx.provider, "name", "") == "docker":
            hexpart = str(volume_ref).split("sha256:", 1)[-1]
            op_volume = "mao-op-" + hexpart[:12]
            self.ctx.provider.prepare_inject_volume(str(volume_ref), op_volume)
            mounts.append(Mount("volume:" + op_volume, str(inject.get("mount") or "/opt/operator"), True))
        return SandboxSpec(
            name=f"mao-agent-{name}",
            image=image,
            mounts=tuple(mounts),
            workdir="/workspace/registry" if builtin else "/workspace",
            env={"FACTORY_AGENT": name},
            network=network,
        )

    def _open_egress(self, name: str, spec: Any, plan: Any) -> Any:
        if plan is None or getattr(plan, "network", "none") != "egress":
            return spec
        from dataclasses import replace
        from secrets import token_hex

        from mao.egress import SecretStore, credentialed_env, proxy_env, register_check_egress

        token = token_hex(16)
        register_check_egress(self.ctx.db, f"agent-{name}", plan, token=token)
        store = SecretStore(self.ctx.root)
        env = {str(key): str(value) for key, value in spec.env.items()}
        env.update({str(key): str(value) for key, value in plan.env.items()})
        env.update(store.inject(plan.secret_names))
        self._ensure_proxies()
        docker = getattr(self.ctx.provider, "name", "") == "docker"
        env.update(credentialed_env(proxy_env(self.ctx.proxies, docker=docker), f"agent-{name}", token))
        return replace(spec, env=env, network="egress")

    def _expand_egress(self, invocation: Any, sandbox: Any, turn_id: str) -> Any:
        if getattr(sandbox.spec, "network", "none") != "egress":
            return invocation
        from mao.egress import proxy_env
        from mao.operator import expand_control_vars

        self._ensure_proxies()
        docker = getattr(self.ctx.provider, "name", "") == "docker"
        urls = proxy_env(self.ctx.proxies, docker=docker)
        return expand_control_vars(
            invocation,
            {
                "FACTORY_JOB_ID": turn_id,
                "FACTORY_ATTEMPT": "1",
                "MODEL_PROXY": urls["MODEL_PROXY"],
                "EGRESS_PROXY": urls["EGRESS_PROXY"],
            },
        )

    def _ensure_proxies(self) -> None:
        if getattr(self.ctx, "proxies", None) is not None:
            return
        from mao.egress import start_owned_proxies

        start_owned_proxies(self.ctx, model_port=0, timeout_s=60.0)

    def _archive(self, snapshot: Any, logs: Any) -> bool:
        from mao.egress import SecretStore

        transcript = _redact_file(self.ctx, snapshot, logs / "transcript.raw")
        stderr = _redact_file(self.ctx, snapshot, logs / "stderr.raw")
        store = SecretStore(self.ctx.root)
        leaked = store.redact(transcript) != transcript or store.redact(stderr) != stderr
        if not leaked:
            (logs / "transcript.jsonl").write_text(transcript, encoding="utf-8")
            (logs / "stderr.txt").write_text(stderr, encoding="utf-8")
        for raw in (logs / "transcript.raw", logs / "stderr.raw"):
            if raw.is_file():
                raw.unlink()
        return leaked

    def _budget_blocked(self, name: str, cfg: AgentConfig) -> bool:
        now = self.ctx.clock.now()
        cutoff = utc_iso(now - timedelta(hours=24))
        rows = self.ctx.db.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM agent_turns WHERE agent = ? AND started_at >= ?",
            (name, cutoff),
        )
        spent = float(rows[0]["spent"])
        row = self._agent_row(name)
        if spent >= cfg.daily_usd:
            if int(row["budget_exhausted"]) == 0:
                with self.ctx.db.tx() as conn:
                    conn.execute(
                        "UPDATE agents SET budget_exhausted = 1, updated_at = ? WHERE name = ?",
                        (utc_iso(now), name),
                    )
                self._event(
                    "agent.budget_exhausted",
                    {"agent": name, "spent_usd": spent, "daily_usd": cfg.daily_usd},
                )
            return True
        if int(row["budget_exhausted"]) == 1:
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE agents SET budget_exhausted = 0, updated_at = ? WHERE name = ?",
                    (utc_iso(now), name),
                )
        return False

    def _pending(self, name: str, batch: int) -> list[Message]:
        now = utc_iso(self.ctx.clock.now())
        rows = self.ctx.db.query(
            "SELECT * FROM agent_messages WHERE agent = ? AND state = 'pending' "
            "AND (not_before IS NULL OR not_before <= ?) ORDER BY enqueue_seq LIMIT ?",
            (name, now, batch),
        )
        return [_message(row) for row in rows]

    def _drop(self, name: str, message_ids: list[str]) -> None:
        from mao.inbox import Inbox

        request = Inbox(self.ctx).open(
            "question",
            subject=self.ctx.ids.new("aq"),
            origin="agent:" + name,
            summary=f"agent {name} 连续 3 轮失败，{len(message_ids)} 条消息已丢弃",
            payload={"options": ["知道了"], "messages": message_ids},
        )
        self._event("agent.dropped", {"agent": name, "messages": message_ids, "request": request.id})

    def _ensure_row(self, name: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO agents (name, state, updated_at) VALUES (?, 'active', ?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, now),
            )

    def _agent_row(self, name: str) -> Any:
        return self.ctx.db.query("SELECT * FROM agents WHERE name = ?", (name,))[0]

    def _set_state(self, name: str, state: str) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE agents SET state = ?, updated_at = ? WHERE name = ?",
                (state, utc_iso(self.ctx.clock.now()), name),
            )

    def _turn(self, turn_id: str) -> Any:
        return _turn(self.ctx.db.query("SELECT * FROM agent_turns WHERE id = ?", (turn_id,))[0])

    def _fail_open_turn(self, name: str, turn_id: str, code: str, reason: str) -> Any:
        finished = utc_iso(self.ctx.clock.now())
        error = {
            "code": code,
            "message": "轮次未能形成有效结果",
            "detail": {"agent": name, "turn": turn_id, "reason": reason[:120]},
        }
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE agent_turns SET state = 'failed', error = ?, finished_at = ? "
                "WHERE id = ? AND state = 'running'",
                (canonical_json(error), finished, turn_id),
            )
            conn.execute(
                "UPDATE agent_messages SET attempts = attempts + 1, updated_at = ? WHERE turn = ?",
                (finished, turn_id),
            )
            conn.execute(
                "UPDATE agent_messages SET state = 'pending', turn = NULL WHERE turn = ? AND attempts < 3",
                (turn_id,),
            )
            conn.execute(
                "UPDATE agent_messages SET state = 'dropped' WHERE turn = ? AND attempts >= 3",
                (turn_id,),
            )
        self._event("agent.turn.finished", {"agent": name, "turn": turn_id, "state": "failed", "code": code})
        return self._turn(turn_id)

    def _guard_lease(self, conn: Any | None = None) -> None:
        lease = getattr(self, "_lease", None)
        if lease is None:
            return
        from mao.durable import LeaseStore

        LeaseStore(self.ctx).guard(
            lease.resource,
            owner=lease.owner,
            generation=lease.generation,
            conn=conn,
        )

    def _event(self, kind: str, data: Mapping[str, Any]) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
                (self.ctx.ids.new("ev"), utc_iso(self.ctx.clock.now()), kind, canonical_json(dict(data))),
            )


_CONTRACT = "身份由会话决定。不要冒充别人。只能经 MCP 操作工厂。秘密不进笔记与输出。一轮处理完就结束。"


def agent_tool(
    ctx: Any,
    epochs: Any,
    name: str,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    origin: Mapping[str, Any] | None = None,
) -> Any:
    snapshot = epochs.materialize(epochs.current())
    if name not in agent_names(snapshot, ctx.root):
        raise FactoryError("AGENT_NOT_FOUND", "没有这个 Agent", agent=name)
    if tool == "run_start":
        return _run_start(ctx, snapshot, name, arguments)
    if tool == "ask_human":
        return _ask_human(ctx, name, arguments)
    if tool == "remind":
        return _remind(ctx, name, arguments)
    if tool == "agent_send":
        return _agent_send(ctx, snapshot, name, arguments, origin=origin)
    raise FactoryError("AGENT_TOOL_INVALID", "工具不存在", tool=tool, reason="tool")


def _run_start(ctx: Any, snapshot: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    line = str(arguments.get("line") or "")
    inputs = arguments.get("inputs")
    if not isinstance(inputs, Mapping) or not _inputs_ok(inputs):
        raise FactoryError("AGENT_TOOL_INVALID", "inputs 不合法", tool="run_start", reason="inputs")
    try:
        cfg = agent_config(snapshot, name)
    except FactoryError as exc:
        raise FactoryError("AGENT_LINE_FORBIDDEN", "产线不在允许列表", agent=name, line=line) from exc
    try:
        line_key = snapshot.key("Line", line)
        snapshot.doc(line_key)
    except FactoryError as exc:
        raise FactoryError("AGENT_LINE_FORBIDDEN", "产线不存在", agent=name, line=line) from exc
    if line_key not in cfg.lines:
        raise FactoryError("AGENT_LINE_FORBIDDEN", "产线不在允许列表", agent=name, line=line)
    from mao.runs import open_line_runner

    trigger: dict[str, Any] = {"on": "agent", "agent": name}
    if arguments.get("cycle_id") and arguments.get("task_id"):
        trigger["cycle_id"] = str(arguments["cycle_id"])
        trigger["task_id"] = str(arguments["task_id"])
    started = open_line_runner(ctx, snapshot).start_run(
        line,
        dict(inputs),
        trigger=trigger,
    )
    return {"id": started.id, "state": started.state.value}


def _inputs_ok(inputs: Mapping[str, Any]) -> bool:
    for value in inputs.values():
        if isinstance(value, str):
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            continue
        return False
    return True


def _ask_human(ctx: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from mao.inbox import Inbox

    options = arguments.get("options")
    if options is not None and (
        not isinstance(options, list)
        or len(options) > 10
        or any(not isinstance(item, str) for item in options)
    ):
        raise FactoryError("AGENT_TOOL_INVALID", "options 不合法", tool="ask_human", reason="options")
    detail = arguments.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise FactoryError("AGENT_TOOL_INVALID", "detail 不合法", tool="ask_human", reason="detail")
    payload: dict[str, Any] = {"detail": "" if detail is None else detail, "options": list(options or [])}
    request = Inbox(ctx).open(
        "question",
        subject=ctx.ids.new("aq"),
        origin="agent:" + name,
        summary=str(arguments.get("summary") or ""),
        payload=payload,
    )
    return request.to_json()


def _remind(ctx: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    seconds = parse_duration(str(arguments.get("after")))
    if seconds < _REMIND_MIN_S or seconds > _REMIND_MAX_S:
        raise FactoryError("AGENT_TOOL_INVALID", "after 超出范围", tool="remind", reason="after")
    not_before = utc_iso(ctx.clock.now() + timedelta(seconds=seconds))
    message = Mailbox(ctx).post(
        name,
        "remind",
        "agent:" + name,
        {"note": arguments.get("note")},
        not_before=not_before,
    )
    return message.to_json()


def _agent_send(
    ctx: Any,
    snapshot: Any,
    name: str,
    arguments: Mapping[str, Any],
    *,
    origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target = str(arguments.get("to") or "")
    if target not in agent_names(snapshot, ctx.root):
        raise FactoryError("AGENT_NOT_FOUND", "没有这个 Agent", agent=target)
    message = Mailbox(ctx).post(
        target,
        "message",
        "agent:" + name,
        {"text": arguments.get("text")},
        origin=origin,
    )
    return message.to_json()


def _wake_fields(wake: Any) -> dict[str, Any]:
    return {
        "id": wake.id,
        "signal": wake.signal,
        "subject": wake.subject,
        "payload": dict(wake.payload),
        "status": wake.status,
        "at": wake.at,
        "attempts": wake.attempts,
        "not_before": wake.not_before,
    }


def _box_digest(cfg: AgentConfig, image: str, network: str, workspace: str = "") -> str:
    return hashlib.sha256(
        canonical_json(
            {"config": cfg.digest, "image": image, "network": network, "workspace": workspace}
        ).encode("utf-8")
    ).hexdigest()


def _store_turn_actions(ctx: Any, turn_id: str, actions: list[dict[str, Any]]) -> None:
    if not actions:
        return
    with ctx.db.tx() as conn:
        row = conn.execute(
            "SELECT actions FROM asset_uses WHERE kind = 'turn' AND execution_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE asset_uses SET actions = ? WHERE kind = 'turn' AND execution_id = ?",
            (canonical_json(actions), turn_id),
        )


def _handoff_head(ctx: Any, name: str) -> str | None:
    rows = ctx.db.query("SELECT asset_id FROM asset_heads WHERE slot = ?", (f"handoff:{name}",))
    return None if not rows else str(rows[0]["asset_id"])


def _read_handoff_next(ctx: Any, name: str, *, unlink: bool = False) -> dict[str, Any] | None:
    path = workspace_path(ctx, name) / "notebook" / "HANDOFF.next.json"
    if unlink:
        path.unlink(missing_ok=True)
        return None
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def clone_foreman_registry(ctx: Any, dest: Path, epoch: str) -> Path:
    """把 active epoch 的 Registry 克隆到已解析工作区，供 /workspace/registry 挂载。"""
    import shutil

    from mao.governance.epochs import EpochStore
    from mao.governance.git import git

    epochs = EpochStore(ctx)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    registry = epochs.registry
    timeout = epochs.timeout_s
    if not (dest / ".git").exists():
        if dest.exists():
            shutil.rmtree(dest)
        git(
            registry,
            "clone",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.filemode=false",
            "--quiet",
            "--no-hardlinks",
            str(registry),
            str(dest),
            timeout_s=timeout,
        )
        git(
            dest,
            "fetch",
            "--quiet",
            str(registry),
            "+refs/factory/*:refs/factory/*",
            "+refs/changesets/*:refs/changesets/*",
            timeout_s=timeout,
        )
        git(
            dest,
            "-c",
            "core.autocrlf=false",
            "checkout",
            "--quiet",
            "--detach",
            epoch,
            timeout_s=timeout,
        )
    else:
        git(
            dest,
            "fetch",
            "--quiet",
            str(registry),
            "+refs/factory/*:refs/factory/*",
            "+refs/changesets/*:refs/changesets/*",
            timeout_s=timeout,
        )
    return dest


def workspace_path(ctx: Any, name: str) -> Path:
    rows = ctx.db.query("SELECT current_path FROM agent_workspaces WHERE agent = ?", (name,))
    if rows and rows[0]["current_path"]:
        return Path(ctx.root) / str(rows[0]["current_path"])
    return Path(ctx.root) / "agents" / name / "workspace"


def _runtime_wants_proxy(runtime: Mapping[str, Any]) -> bool:
    invoke = runtime.get("invoke") or {}
    texts: list[str] = []
    for key in ("argv", "resume"):
        for item in invoke.get(key) or []:
            texts.append(item if isinstance(item, str) else str(item))
    for value in (runtime.get("env") or {}).values():
        texts.append(value if isinstance(value, str) else str(value))
    return any("${MODEL_PROXY}" in text for text in texts)


def _redact_file(ctx: Any, snapshot: Any, path: Any) -> str:
    from mao.foreman import redact

    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "".join(redact(ctx, snapshot, line) for line in text.splitlines(keepends=True))


def _runtime_resumes(snapshot: Any, runtime: str) -> bool:
    ref = runtime.split(":", 1)[-1]
    try:
        spec = snapshot.spec(snapshot.key("OperatorRuntime", ref))
    except FactoryError:
        return False
    invoke = spec.get("invoke") if isinstance(spec, Mapping) else None
    return bool(isinstance(invoke, Mapping) and invoke.get("resume"))


def _waiting_for_result(logs: Path, *, code: str) -> bool:
    """Recognize the explicit foreman wait signal without treating it as success.

    The turn remains failed for audit purposes, while its delivery is deferred
    until the separately delivered run result wakes the foreman.  Only the
    structured, exact marker emitted by the in-repo witness agent qualifies;
    arbitrary model prose cannot suppress retry accounting.
    """
    if code != "SESSION_FAILED":
        return False
    path = logs / "transcript.raw"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return '"result": "wait-for-result"' in text or '"result":"wait-for-result"' in text


def _turn_outcome(
    ctx: Any,
    turn_id: str,
    result: Any,
    summary: Any,
    *,
    resume_required: bool = False,
    agent: str = "",
) -> tuple[str, str, dict[str, Any] | None]:
    exit_code = None if result is None else result.exit_code
    if result is not None and result.timed_out:
        error = {"code": "SESSION_TIMEOUT", "message": "SESSION_TIMEOUT", "detail": {"exit_code": exit_code}}
        return "timeout", "SESSION_TIMEOUT", error
    rows = ctx.db.query("SELECT code FROM budget_stops WHERE job_id = ?", (turn_id,))
    if rows:
        code = str(rows[0]["code"])
        return "failed", code, {"code": code, "message": code, "detail": {"exit_code": exit_code}}
    failed = result is not None and (exit_code not in (0, None) or (summary is not None and summary.is_error))
    if failed:
        error = {"code": "SESSION_FAILED", "message": "SESSION_FAILED", "detail": {"exit_code": exit_code}}
        return "failed", "SESSION_FAILED", error
    has_result = summary is not None and bool(getattr(summary, "has_result", False))
    session_id = None if summary is None else summary.session_id
    if not has_result or (resume_required and not session_id):
        reason = "session" if has_result else "result"
        error = {
            "code": "AGENT_RESULT_INVALID",
            "message": "缺少结构化结果或会话",
            "detail": {"agent": agent, "turn": turn_id, "reason": reason},
        }
        return "failed", "AGENT_RESULT_INVALID", error
    return "ok", "", None


def _approval_still_foreman(host: AgentHost, wake: Any) -> bool:
    from mao.approvals import Approvals

    snapshot = host.epochs.materialize(host.epochs.current())
    try:
        approval = Approvals(host.ctx, snapshot).get(wake.subject)
    except FactoryError:
        return False
    return approval.status.value == "pending" and approval.route == "foreman"


@dataclass(frozen=True)
class TurnRecord:
    id: str
    agent: str
    conversation: int
    number: int
    kind: str
    session: str | None
    state: str
    messages: tuple[str, ...]
    cost_usd: float
    input_tokens: int
    exit_code: int | None
    error: dict[str, Any] | None
    started_at: str
    finished_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "conversation": self.conversation,
            "number": self.number,
            "kind": self.kind,
            "session": self.session,
            "state": self.state,
            "messages": list(self.messages),
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "exit_code": self.exit_code,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _turn(row: Any) -> TurnRecord:
    error = json.loads(row["error"]) if row["error"] else None
    return TurnRecord(
        str(row["id"]),
        str(row["agent"]),
        int(row["conversation"]),
        int(row["number"]),
        str(row["kind"]),
        None if row["session"] is None else str(row["session"]),
        str(row["state"]),
        tuple(json.loads(row["messages"])),
        float(row["cost_usd"]),
        int(row["input_tokens"]),
        None if row["exit_code"] is None else int(row["exit_code"]),
        error if isinstance(error, dict) else None,
        str(row["started_at"]),
        None if row["finished_at"] is None else str(row["finished_at"]),
    )


def _task_text(cfg: AgentConfig, messages: list[Message], session: str | None, workspace: Any) -> str:
    from pathlib import Path

    parts = []
    if not session:
        parts.append(cfg.charter)
        if (Path(workspace) / "notebook" / "HANDOFF.md").is_file():
            parts.append("上一段对话的交接在 /workspace/notebook/HANDOFF.md，先读它。")
    lines = [
        f"你是常驻 agent {cfg.name}。以下是自上一轮以来的 {len(messages)} 条新消息，按时间排列；完整 JSON 也在 /factory/in/messages.json。",
        "处理完就结束本轮，不要等待新消息。需要以后再看的事，用 remind 预约，或写进 /workspace/notebook/。",
        "",
    ]
    for index, message in enumerate(messages, 1):
        lines.append(f"[{index}] {message.kind} {message.source} {message.created_at} {message.id}")
        lines.append(canonical_json(message.body))
    parts.append("\n".join(lines))
    if cfg.name == _RESIDENT_NAME and any(message.kind == "talk" for message in messages):
        parts.append(_TALK_OBLIGATION)
    return "\n\n".join(parts)


def _clear(path: Any) -> None:
    import shutil
    from pathlib import Path

    folder = Path(path)
    folder.mkdir(parents=True, exist_ok=True)
    for child in list(folder.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def list_turns(ctx: Any, name: str) -> list[TurnRecord]:
    rows = ctx.db.query(
        "SELECT * FROM agent_turns WHERE agent = ? ORDER BY started_at, id",
        (name,),
    )
    return [_turn(row) for row in rows]


def agent_status(ctx: Any, name: str, *, detail: bool = False) -> dict[str, Any]:
    rows = ctx.db.query("SELECT * FROM agents WHERE name = ?", (name,))
    pending = ctx.db.query(
        "SELECT COUNT(*) AS n FROM agent_messages WHERE agent = ? AND state = 'pending'",
        (name,),
    )
    if rows:
        row = rows[0]
        body = {
            "name": name,
            "state": str(row["state"]),
            "session": None if row["session"] is None else str(row["session"]),
            "pending": int(pending[0]["n"]),
            "conversation": int(row["conversation"]),
        }
    else:
        body = {
            "name": name,
            "state": "active",
            "session": None,
            "pending": int(pending[0]["n"]),
            "conversation": 1,
        }
    return body


def _bind_turn_origin(conn: Any, turn_id: str, pending: list[Message]) -> None:
    if not pending:
        return
    row = conn.execute(
        """SELECT objective_id, cycle_id, task_id FROM execution_origins
           WHERE kind = 'agent_message' AND execution_id = ?""",
        (pending[0].id,),
    ).fetchone()
    if row is None or row["objective_id"] is None:
        return
    conn.execute(
        "UPDATE agent_turns SET objective_id = ?, cycle_id = ?, task_id = ? WHERE id = ?",
        (row["objective_id"], row["cycle_id"], row["task_id"], turn_id),
    )
    conn.execute(
        """INSERT INTO execution_origins
           (kind, execution_id, objective_id, cycle_id, task_id, parent_kind, parent_id)
           VALUES ('turn', ?, ?, ?, ?, 'agent_message', ?)""",
        (turn_id, row["objective_id"], row["cycle_id"], row["task_id"], pending[0].id),
    )


def _insert_origin(conn: Any, message_id: str, origin: Mapping[str, Any] | None) -> None:
    if not origin:
        return
    conn.execute(
        """INSERT INTO execution_origins
           (kind, execution_id, objective_id, cycle_id, task_id, parent_kind, parent_id)
           VALUES ('agent_message', ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            origin["objective_id"],
            origin["cycle_id"],
            origin.get("task_id"),
            origin["parent_kind"],
            origin["parent_id"],
        ),
    )


def _message(row: Any) -> Message:
    return Message(
        str(row["id"]),
        str(row["agent"]),
        str(row["kind"]),
        str(row["source"]),
        json.loads(row["body"]),
        str(row["state"]),
        None if row["turn"] is None else str(row["turn"]),
        int(row["attempts"]),
        None if row["not_before"] is None else str(row["not_before"]),
        str(row["created_at"]),
        0 if "enqueue_seq" not in row.keys() or row["enqueue_seq"] is None else int(row["enqueue_seq"]),
    )


def _alloc_enqueue_seq(conn: Any) -> int:
    row = conn.execute("SELECT value FROM delivery_counters WHERE name = 'enqueue_seq'").fetchone()
    current = 0 if row is None else int(row["value"])
    nxt = current + 1
    conn.execute(
        "INSERT INTO delivery_counters (name, value) VALUES ('enqueue_seq', ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (nxt,),
    )
    return nxt


def _agent_version(snapshot: Any, name: str) -> str:
    keys = [key for key in snapshot.keys("Agent") if key.startswith(f"Agent:{name}@")]
    return keys[0] if keys else name


def _parse(stamp: str) -> Any:
    from datetime import datetime

    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    return datetime.fromisoformat(text)


def _every_text(snapshot: Any, name: str, index: int) -> str:
    spec = snapshot.spec(f"Agent:{name}@1") if f"Agent:{name}@1" in snapshot.keys("Agent") else {}
    keys = [key for key in snapshot.keys("Agent") if key.startswith(f"Agent:{name}@")]
    spec = snapshot.spec(keys[0]) if keys else spec
    items = ((spec.get("subscribe") or {}).get("schedule")) or []
    if index < len(items) and isinstance(items[index], Mapping):
        return str(items[index].get("every") or "")
    return ""


def _major(type_id: str) -> str:
    if "@" not in type_id:
        return type_id
    name, version = type_id.rsplit("@", 1)
    return f"{name}@{version.split('.', 1)[0]}"


def _type_matches(snapshot: Any, bean_type: str, type_key: str) -> bool:
    wanted = _major(type_key.split(":", 1)[-1])
    got = _major(bean_type)
    if got == wanted or bean_type == wanted:
        return True
    return snapshot.is_subtype(got, wanted) or snapshot.is_subtype(bean_type, wanted)


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        return None
    return float(value)
