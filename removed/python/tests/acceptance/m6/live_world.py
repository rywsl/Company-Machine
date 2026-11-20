"""M6-03 的现场夹具（spec/system/live.md 4.2）：Docker、真实 CLI、脚本化的模型上游。

在 registry-m2 的副本上：
- 三个 M1 测试镜像的占位 digest 换成本机镜像 ID；
- 复制 spec/runtimes/ 下的两份参考 runtime，注入卷换成现场镜像，模型换成 fake-opus / fake-codex；
- 新增机器 bench@1、策略 agent-live@1、工位 writer-live@1。
守护进程在 start 时建 mao-egress 网络并持有两个代理；fake_llm 在宿主上监听。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.acceptance.m3.gov_world import FIXTURE, START, World, git
from tests.acceptance.m5.auto_world import PassContracts, PassShadow
from tests.fakes.fake_llm import FakeLlm
from tests.fakes.fakelib import PLACEHOLDERS, REPO, docker_image_ids

LIVE_NAMES = ("live-bench", "claude-code", "codex")
SECRET = "LIVE-MODEL-SECRET-0123456789"
ZERO = "0" * 64
TERMINAL = {"SEALED", "FAILED", "DEAD", "CANCELLED"}

BENCH = """# 现场测试的 A 形态机器：live-bench 镜像（glibc、git、python3）。
apiVersion: mao/v1
kind: Machine
metadata: {name: bench, version: 1}
spec:
  form: workspace
  runtime:
    image: mao-test/live-bench@sha256:@@BENCH@@
    workdir: /workspace
    instance: per_job
    resources: {cpus: 1, memory: 2048Mi}
  needs: {egress: none}
  recipes:
    write-note:
      inputs:
        task: {type: agent.task@1}
      outputs:
        note: {type: note.text@1, harvest: "drafts/*.txt"}
      task_template: |
        写一篇至少两行的短文，放进 /workspace/drafts/ 目录。
"""

POLICY = """apiVersion: mao/v1
kind: Policy
metadata: {name: agent-live, version: 1}
spec:
  applies_to: [transform]
  egress: {none: true}
  secrets: []
  resources: {cpus: 2, memory: 2048Mi}
"""

WRITER = """apiVersion: mao/v1
kind: Station
metadata: {name: writer-live, version: 1}
spec:
  role: transform
  machine: bench@1
  recipe: write-note
  operator: {runtime: claude-code@1, model: fake-opus, max_turns: 8}
  inputs:  {task: {type: agent.task@1, requires: [has_steps]}}
  outputs:
    note:
      type: note.text@1
      guarantees: [non_empty]
      accept: ["out.facts.lines >= 2"]
  limits: {timeout: 600s, budget_usd: 1.0, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: agent-live@1
"""


class MissingLiveImages(AssertionError):
    pass


def live_image_ids(docker: str = "docker") -> dict[str, str]:
    ids: dict[str, str] = {}
    missing: list[str] = []
    for name in LIVE_NAMES:
        proc = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", f"mao-test/{name}:dev"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        value = proc.stdout.strip()
        if proc.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            missing.append(f"mao-test/{name}:dev")
        else:
            ids[name] = value
    if missing:
        raise MissingLiveImages(
            "缺少现场镜像 " + "、".join(missing) + "。先固定 tools/live_versions.json，再运行 "
            "uv run python tools/build_live_images.py（spec/system/live.md 4.1）"
        )
    return ids


def image_labels(name: str, docker: str = "docker") -> dict[str, str]:
    proc = subprocess.run(
        [docker, "image", "inspect", "--format", "{{json .Config.Labels}}", f"mao-test/{name}:dev"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(proc.stdout) or {}


def _replace(text: str, old: str, new: str) -> str:
    if old not in text:
        raise AssertionError(f"reference runtime layout changed: {old!r}")
    return text.replace(old, new)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _registry(tmp: Path, drop: tuple[str, ...]) -> tuple[Path, str, dict[str, str]]:
    images = docker_image_ids()
    live = live_image_ids()
    repo = tmp / "registry"
    shutil.copytree(FIXTURE, repo)
    for path in sorted(repo.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        new = text
        for name, image_id in images.items():
            new = new.replace(PLACEHOLDERS[name], image_id.removeprefix("sha256:"))
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
    runtimes = REPO / "spec" / "runtimes"
    claude = (runtimes / "claude-code.yaml").read_text(encoding="utf-8")
    claude = _replace(
        claude,
        "mao-live/claude-code@sha256:" + ZERO,
        "mao-test/claude-code@sha256:" + live["claude-code"][7:],
    )
    claude = _replace(
        claude, "models_allowed: [claude-opus-5-5, claude-sonnet-5-5]", "models_allowed: [fake-opus]"
    )
    codex = (runtimes / "codex.yaml").read_text(encoding="utf-8")
    codex = _replace(codex, "mao-live/codex@sha256:" + ZERO, "mao-test/codex@sha256:" + live["codex"][7:])
    codex = _replace(codex, "models_allowed: [gpt-5-codex]", "models_allowed: [fake-codex]")
    _write(repo / "runtimes" / "claude-code.yaml", claude)
    _write(repo / "runtimes" / "codex.yaml", codex)
    _write(repo / "machines" / "bench" / "machine.yaml", BENCH.replace("@@BENCH@@", live["live-bench"][7:]))
    _write(repo / "policies" / "agent-live.yaml", POLICY)
    _write(repo / "stations" / "writer-live.yaml", WRITER)
    for rel in drop:
        (repo / rel).unlink()
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "live registry")
    return repo, git(repo, "rev-parse", "HEAD"), live


@dataclass
class LiveWorld:
    w: World
    llm: FakeLlm
    pipeline: Any
    daemon: Any

    def supervisor(self):
        from mao.supervisor import ForemanSupervisor, SupervisorConfig

        return ForemanSupervisor(
            self.w.ctx, self.w.epochs, SupervisorConfig.load(self.w.ctx.root), pipeline=self.pipeline
        )

    def until(self, done, *, supervisor=None, limit: int = 30) -> bool:
        for _ in range(limit):
            if done():
                return True
            self.daemon.tick()
            if supervisor is not None:
                supervisor.run_once()
        return done()

    def close(self) -> None:
        try:
            self.daemon.shutdown(timeout_s=60)
        finally:
            self.llm.close()


def live_world(tmp: Path, script: dict[str, Any], *, drop: tuple[str, ...] = (), **foreman: Any) -> LiveWorld:
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.daemon import Daemon, InlineJobExecutor, ServeConfig
    from mao.governance import ChangePipeline, EpochStore
    from mao.jobs import load_job
    from mao.runner import Runner
    from mao.sandbox import DockerProvider

    repo, base, live = _registry(tmp, drop)
    script_path = tmp / "llm.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    llm = FakeLlm(script_path)
    llm_port = llm.start()
    anthropic = f"http://127.0.0.1:{llm_port}"
    root = tmp / "factory"
    root.mkdir()
    _write(
        root / "egress" / "upstreams.json",
        json.dumps(
            {
                "fake-opus": {"url": anthropic, "secret": "MODEL_TOKEN", "header": "x-api-key"},
                "fake-codex": {"url": anthropic + "/v1", "secret": "MODEL_TOKEN", "header": "authorization"},
                "*": {"url": anthropic, "secret": "MODEL_TOKEN", "header": "x-api-key"},
            }
        ),
    )
    _write(
        root / "egress" / "prices.json",
        json.dumps(
            {
                "fake-opus": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
                "fake-codex": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
                "*": {"input_per_mtok": 1.0, "output_per_mtok": 1.0},
            }
        ),
    )
    _write(root / "secrets" / "manifest.json", json.dumps({"MODEL_TOKEN": "model"}))
    _write(root / "secrets" / "values" / "MODEL_TOKEN", SECRET)
    _write(
        root / "scheduler.json",
        json.dumps(
            {
                "pools": {"cpu": None, "memory_mb": None, "gpu": None},
                "compartments": [],
                "breakers": [],
                "wip": [],
                "rates": [],
                "run_cap": None,
                "toxic_failures": None,
                "tick_s": None,
            }
        ),
    )
    section = {
        "runtime": "claude-code@1",
        "model": "fake-opus",
        "reviewers": ["codex@1"],
        "models": {"claude-code@1": "fake-opus", "codex@1": "fake-codex"},
        "image": "mao-test/live-bench@sha256:" + live["live-bench"][7:],
        "network": "egress",
        "session_timeout_s": 900,
        "session_budget_usd": 5.0,
        "daily_budget_usd": 100.0,
        "max_turns": 20,
        "max_attempts": 1,
        "backoff_s": 60,
        "env": {},
    }
    section.update(foreman)
    config = {
        "governance": {"registry": str(repo), "git_timeout_s": 120},
        "serve": {
            "tick_s": 0.0,
            "max_parallel_jobs": 2,
            "audit_every_s": None,
            "canary_every_s": None,
            "docker_timeout_s": 120.0,
            "supervise": False,
            "model_port": 0,
            # Docker Desktop 的出网转发容器（hardening.md 5.4）：用已固定 digest、带 node 的现场基础镜像
            "forwarder_image": json.loads(
                (REPO / "tools" / "live_versions.json").read_text(encoding="utf-8")
            )["base"],
        },
        "foreman": section,
    }
    _write(root / "factory.json", json.dumps(config, ensure_ascii=False))
    clock = FakeClock(START)
    ctx = Context.open(root, provider=DockerProvider(root), clock=clock, ids=CounterIds())
    epochs = EpochStore(ctx)
    epochs.init(base, by="human:acceptance")
    fake = PassContracts()
    pipeline = ChangePipeline(
        ctx, epochs, contracts=lambda snapshot: fake, shadow=PassShadow(), rebuild=lambda r, s: r
    )

    def runner_for(job_id: str):
        return Runner(ctx, epochs.materialize(load_job(ctx.db, job_id).epoch))

    daemon = Daemon(
        ctx, ServeConfig.load(root), executor=InlineJobExecutor(runner_for), epochs=epochs, pipeline=pipeline
    )
    daemon.start()
    world = World(
        ctx=ctx, clock=clock, repo=repo, base=base, epochs=epochs, snapshot=epochs.materialize(base)
    )
    return LiveWorld(w=world, llm=llm, pipeline=pipeline, daemon=daemon)


def write_and_commit(rel: str, text: str, message: str) -> str:
    """Bash 命令：在克隆里写文件、提交，只输出新提交的 sha。"""
    body = text if text.endswith("\n") else text + "\n"
    return (
        f"cd /workspace/registry && cat > {rel} <<'MAO_EOF'\n{body}MAO_EOF\n"
        f"git add -A && git commit -q -m '{message}' && git rev-parse HEAD"
    )
