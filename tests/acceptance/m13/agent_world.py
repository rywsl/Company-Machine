"""M13 验收的夹具（spec/system/agents.md 第 7 章）。导入本模块不会加载 mao。

在 tests/fixtures/registry-m2 的副本上：
- fake-agent@1 带 invoke.mcp（同 m5 的 auto_world）与 invoke.resume；
- Agent scout@1：订阅定时（1h）、带标签 src=scout 的 note.text@1 Bean、入站路由 scout-in，可启动 note-trigger@1；
  batch 3，rotate_turns 3，rotate_tokens 1000；
- Agent helper@1：只收消息。
factory.json 带 governance、foreman、agents 段；agents.env 的 FAKE_AGENT_SCRIPT 指向 tmp 下的脚本文件。
CLI 是 tests/fakes/fake_agent.py，脚本按 FACTORY_TURN 分轮（by_turn）。时钟是 FakeClock。
"""

from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from tests.acceptance.m3.gov_world import FIXTURE, START, git
from tests.acceptance.m5.auto_world import IMAGE, _patch_runtimes, foreman_section

RESUME = (
    '    resume: [/opt/operator/bin/fake-agent, --resume, "{{ session.id }}", -p, "{{ brief.task }}",\n'
    '             --append-system-prompt, "{{ brief.factory_contract }}", --output-format, stream-json,\n'
    '             --max-turns, "{{ limits.max_turns }}", --model, "{{ operator.model }}"]\n'
)

SCOUT = """apiVersion: mao/v1
kind: Agent
metadata: {name: scout, version: 1}
spec:
  runtime: fake-agent@1
  model: fake-model-1
  charter: 你是侦察员 scout，负责收集笔记并整理。
  policy: agent-default@1
  lines: [note-trigger@1]
  subscribe:
    schedule:
      - {every: 1h, note: 整理笔记}
    beans:
      - {type: note.text@1, label: "src=scout"}
    ingress:
      scout-in: {auth: {kind: bearer, secret: SCOUT_TOKEN}}
  budget: {daily_usd: 1.0, turn_usd: 1.0, turn_timeout: 60s}
  context: {batch: 3, rotate_turns: 3, rotate_tokens: 1000, max_turns: 5}
"""

HELPER = """apiVersion: mao/v1
kind: Agent
metadata: {name: helper, version: 1}
spec:
  runtime: fake-agent@1
  model: fake-model-1
  charter: 你是 helper，只回答别的 agent 的问题。
  policy: agent-default@1
  budget: {daily_usd: 1.0, turn_usd: 1.0, turn_timeout: 60s}
"""

SCOUT_TOKEN = "scout-token-value-5d21"


def patch_registry(repo: Path) -> None:
    _patch_runtimes(repo)
    path = repo / "runtimes" / "fake-agent.yaml"
    text = path.read_text(encoding="utf-8")
    anchor = "    mcp: {format: claude-json, flag: --mcp-config}\n"
    if text.count(anchor) != 1:
        raise AssertionError("fake-agent.yaml layout changed")
    path.write_text(text.replace(anchor, RESUME + anchor, 1), encoding="utf-8", newline="\n")
    (repo / "agents").mkdir(exist_ok=True)
    (repo / "agents" / "scout.yaml").write_text(SCOUT, encoding="utf-8", newline="\n")
    (repo / "agents" / "helper.yaml").write_text(HELPER, encoding="utf-8", newline="\n")


class World:
    def __init__(self, tmp: Path, *, foreman: dict[str, Any] | None = None, record: bool = False) -> None:
        from mao.base import CounterIds, FakeClock
        from mao.context import Context
        from mao.governance import EpochStore
        from mao.sandbox import LocalProvider
        from tests.fakes.fakelib import local_commands

        self.tmp = tmp
        self.repo = tmp / "registry"
        shutil.copytree(FIXTURE, self.repo)
        patch_registry(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "checkout", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial registry")
        self.base = git(self.repo, "rev-parse", "HEAD")

        self.script_path = tmp / "agent-script.json"
        self.script({"default": []})
        self.root = tmp / "factory"
        self.root.mkdir()
        config: dict[str, Any] = {
            "governance": {"registry": str(self.repo), "git_timeout_s": 60},
            "foreman": foreman_section(self.script_path, **(foreman or {})),
            "agents": {
                "image": IMAGE,
                "network": "none",
                "poll_s": 1,
                "env": {"FAKE_AGENT_SCRIPT": str(self.script_path)},
            },
        }
        self.config = config
        (self.root / "factory.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.put_secret("SCOUT_TOKEN", "other", SCOUT_TOKEN)

        self.clock = FakeClock(START)
        self.opened: list[Any] = []
        opened = self.opened

        class Recording(LocalProvider):
            def open(self, spec):  # noqa: ANN001, ANN201 — 记录每个 SandboxSpec
                opened.append(spec)
                return super().open(spec)

            def attach(self, spec):  # noqa: ANN001, ANN201 — 接上已有沙箱也算一次
                opened.append(spec)
                return super().attach(spec)

        provider = (Recording if record else LocalProvider)(
            self.root, commands=local_commands(), clock=self.clock
        )
        self.ctx = Context.open(self.root, provider=provider, clock=self.clock, ids=CounterIds())
        self.epochs = EpochStore(self.ctx)
        self.epochs.init(self.base, by="human:acceptance")
        self.snapshot = self.epochs.materialize(self.base)

    # ------------------------------------------------------------ 配置

    def script(self, body: dict[str, Any]) -> None:
        self.script_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def put_secret(self, name: str, klass: str, value: str) -> None:
        folder = self.root / "secrets"
        (folder / "values").mkdir(parents=True, exist_ok=True)
        manifest = folder / "manifest.json"
        body = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        body[name] = klass
        manifest.write_text(json.dumps(body), encoding="utf-8")
        (folder / "values" / name).write_text(value, encoding="utf-8", newline="")

    # ------------------------------------------------------------ 组件

    def pipeline(self):
        from tests.acceptance.m5.auto_world import pass_pipeline

        return pass_pipeline(self)

    def host(self, pipeline: Any = None):
        from mao.agents import AgentHost, AgentsConfig

        return AgentHost(
            self.ctx, self.epochs, AgentsConfig.load(self.root), pipeline=pipeline or self.pipeline()
        )

    def supervisor(self):
        from mao.supervisor import ForemanSupervisor, SupervisorConfig

        return ForemanSupervisor(
            self.ctx, self.epochs, SupervisorConfig.load(self.root), pipeline=self.pipeline()
        )

    def post(self, agent: str, text: str, **kw: Any):
        from mao.agents import Mailbox

        return Mailbox(self.ctx).post(agent, "message", "human:acceptance", {"text": text}, **kw)

    def messages(self, agent: str, state: str | None = None) -> list[Any]:
        from mao.agents import Mailbox

        return Mailbox(self.ctx).list(agent, state=state)

    def workspace(self, agent: str = "scout") -> Path:
        return self.root / "agents" / agent / "workspace"

    def read(self, rel: str, agent: str = "scout") -> str:
        return (self.workspace(agent) / rel).read_text(encoding="utf-8")

    def events(self, kind: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT data FROM events WHERE kind = ? ORDER BY seq", (kind,))
        return [json.loads(row["data"]) for row in rows]

    def rows(self, sql: str, args: tuple = ()) -> list[Any]:
        return list(self.ctx.db.query(sql, args))

    def note(self, text: str = "hello factory\n") -> str:
        from mao.warehouse import BeanDraft

        type_id = self.snapshot.type_key("note.text@1").split(":", 1)[1]
        incoming = self.root / "incoming" / self.ctx.ids.new("in")
        incoming.mkdir(parents=True)
        (incoming / "note.txt").write_text(text, encoding="utf-8")
        bean = self.ctx.warehouse.add(
            BeanDraft(type=type_id, meta={}, files={"note.txt": incoming / "note.txt"})
        )
        facts = {
            "chars": len(text),
            "lines": len(text.splitlines()),
            "has_lower": any(c.islower() for c in text),
        }
        self.ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest="sha256:" + "0" * 64)
        return bean.id

    def sealed_note(self, text: str = "hello factory\n") -> str:
        """经真实 job 封存一个 note（写 bean.sealed 事件），返回产出的 Bean id。"""
        from mao.runner import open_runner

        runner = open_runner(self.ctx, self.snapshot)
        job = runner.run(
            runner.submit(self.snapshot.key("Station", "upper@1"), {"note": self.note(text)}, {}).id
        )
        assert job.state.value == "SEALED", job.error
        found = job.outputs["note"]
        return found[0] if isinstance(found, list) else found

    def mcp(self, name: str, arguments: dict[str, Any], *, agent: str = "scout") -> dict[str, Any]:
        """经 McpServer.handle 以 agent 角色调用工具，返回原始的 JSON-RPC 回复。"""
        from mao.mcp import McpServer, Session

        server = McpServer(
            self.ctx,
            self.epochs,
            Session(role="agent", actor="fake-agent@1", agent=agent),
            pipeline=self.pipeline(),
        )
        init = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        }
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
        return server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def cli(self, *args: str) -> tuple[int, Any, str]:
        from mao.cli import main
        from tests.fakes.fakelib import local_commands

        mapping = self.tmp / "commands.json"
        mapping.write_text(json.dumps(local_commands()), encoding="utf-8")
        base = ["--root", str(self.root), "--provider", "local", "--local-commands", str(mapping), "--json"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main([*base, *args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        text = out.getvalue()
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = text
        return code, body, err.getvalue()


def tool_result(reply: dict[str, Any]) -> tuple[Any, Any]:
    """把 tools/call 的回复拆成 (结果, 错误对象)。"""
    assert "result" in reply, reply
    result = reply["result"]
    if result.get("isError"):
        return None, json.loads(result["content"][0]["text"])
    return result["structuredContent"]["result"], None
