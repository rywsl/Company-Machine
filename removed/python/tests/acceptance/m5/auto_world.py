"""M5-06、M5-07 的治理夹具（spec/system/supervisor.md 第 7 章）。

在 gov_world 的基础上：
- 给 fake-agent@1 加 invoke.mcp（claude-json），值守据此写 MCP 配置；
- 新增 fake-reviewer@1（同一个 fake agent，名字不同），用于交叉复核；
- 写 factory.json 的 foreman 段，FAKE_AGENT_SCRIPT 指向 tmp 下的脚本文件。
只改测试自己的 Registry 副本，不改 tests/fixtures/registry-m2。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tests.acceptance.m3.gov_world import FIXTURE, START, World, changeset_doc, git

SCRIPT = "agent-script.json"
MCP_LINE = "    mcp: {format: claude-json, flag: --mcp-config}\n"
IMAGE = "mao-test/operator@sha256:" + "0" * 64
REVIEW_ACTION = {
    "do": "mcp",
    "calls": [
        {
            "name": "review_submit",
            "arguments": {
                "changeset_id": "@json:/factory/in/wake.json#subject",
                "verdict": "approve",
                "note": "fixtures cover accept and reject",
            },
        }
    ],
}


class PassResult:
    def __init__(self, n: int) -> None:
        self.id = f"ct-{n:04d}"
        self.kind = "fake"
        self.subject = "fake"
        self.verdict = "pass"
        self.detail: dict = {}
        self.at = "2026-11-10T00:00:00.000Z"


class PassContracts:
    def __init__(self) -> None:
        self.n = 0

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.n += 1
            return PassResult(self.n)

        return call


class PassShadow:
    def run(self, cs, *, candidate, active, rollout, golden_only):
        from mao.governance import ShadowReport

        return ShadowReport(ok=True, runs=1, spent_usd=0.0, regressions=[])


def foreman_section(script: Path, **overrides: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "runtime": "fake-agent@1",
        "model": "fake-model-1",
        "reviewers": ["fake-agent@1", "fake-reviewer@1"],
        "models": {"fake-agent@1": "fake-model-1", "fake-reviewer@1": "fake-model-1"},
        "image": IMAGE,
        "network": "none",
        "session_timeout_s": 120,
        "session_budget_usd": 1.0,
        "daily_budget_usd": 10.0,
        "max_turns": 5,
        "max_attempts": 2,
        "backoff_s": 60,
        "env": {"FAKE_AGENT_SCRIPT": str(script)},
    }
    section.update(overrides)
    return section


def write_script(tmp: Path, script: dict[str, Any]) -> Path:
    path = tmp / SCRIPT
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return path


def _patch_runtimes(repo: Path) -> None:
    path = repo / "runtimes" / "fake-agent.yaml"
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    anchor = "  env:\n"
    if text.count(anchor) != 1:
        raise AssertionError("fake-agent.yaml layout changed")
    text = text.replace(anchor, MCP_LINE + anchor, 1)
    path.write_text(text, encoding="utf-8", newline="\n")
    reviewer = text.replace(
        "metadata: {name: fake-agent, version: 1}", "metadata: {name: fake-reviewer, version: 1}", 1
    )
    if reviewer == text:
        raise AssertionError("fake-agent.yaml metadata changed")
    (repo / "runtimes" / "fake-reviewer.yaml").write_text(reviewer, encoding="utf-8", newline="\n")


def auto_world(tmp: Path, *, drop: tuple[str, ...] = (), **foreman: Any) -> World:
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.governance import EpochStore
    from mao.sandbox import LocalProvider
    from tests.fakes.fakelib import local_commands

    repo = tmp / "registry"
    shutil.copytree(FIXTURE, repo)
    _patch_runtimes(repo)
    for rel in drop:
        (repo / rel).unlink()
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial registry")
    base = git(repo, "rev-parse", "HEAD")

    script = tmp / SCRIPT
    if not script.exists():
        write_script(tmp, {"default": []})
    root = tmp / "factory"
    root.mkdir()
    config = {
        "governance": {"registry": str(repo), "git_timeout_s": 60},
        "foreman": foreman_section(script, **foreman),
    }
    (root / "factory.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    clock = FakeClock(START)
    provider = LocalProvider(root, commands=local_commands(), clock=clock)
    ctx = Context.open(root, provider=provider, clock=clock, ids=CounterIds())
    epochs = EpochStore(ctx)
    epochs.init(base, by="human:acceptance")
    return World(ctx=ctx, clock=clock, repo=repo, base=base, epochs=epochs, snapshot=epochs.materialize(base))


def pass_pipeline(w: World):
    from mao.governance import ChangePipeline

    fake = PassContracts()
    return ChangePipeline(
        w.ctx, w.epochs, contracts=lambda snapshot: fake, shadow=PassShadow(), rebuild=lambda r, s: r
    )


def supervisor(w: World, pipeline):
    from mao.supervisor import ForemanSupervisor, SupervisorConfig

    return ForemanSupervisor(w.ctx, w.epochs, SupervisorConfig.load(w.ctx.root), pipeline=pipeline)


def commit_actions(files: dict[str, str], message: str) -> list[dict[str, Any]]:
    """会话里的动作：把 files 写进克隆，提交，把 HEAD 存到 /workspace/head.txt。"""
    actions: list[dict[str, Any]] = [
        {"do": "write", "path": f"/workspace/registry/{rel}", "text": text} for rel, text in files.items()
    ]
    actions += [
        {"do": "run", "argv": ["git", "-C", "/workspace/registry", "add", "-A"]},
        {"do": "run", "argv": ["git", "-C", "/workspace/registry", "commit", "-q", "-m", message]},
        {
            "do": "run",
            "argv": ["git", "-C", "/workspace/registry", "rev-parse", "HEAD"],
            "save": "/workspace/head.txt",
        },
    ]
    return actions


def propose_action(
    changes: list[dict[str, Any]], *, rollout: dict[str, Any] | None = None, allow_error: bool = False
) -> dict[str, Any]:
    """会话里的动作：经 MCP 提议，base_epoch 取 job.json 的 epoch，commit 取 head.txt。"""
    document = changeset_doc("@json:/factory/ctl/job.json#epoch", changes, rollout=rollout)
    return {
        "do": "mcp",
        "calls": [
            {
                "name": "changeset_propose",
                "arguments": {"document": document, "commit": "@file:/workspace/head.txt"},
            }
        ],
        "save": "/workspace/propose.json",
        "allow_error": allow_error,
    }


def events(ctx: Any, kind: str) -> list[dict[str, Any]]:
    rows = ctx.db.query("SELECT data FROM events WHERE kind = ? ORDER BY rowid", (kind,))
    return [json.loads(row["data"]) for row in rows]


def session_dir(w: World, session_id: str) -> Path:
    return w.ctx.root / "foreman" / "sessions" / session_id
