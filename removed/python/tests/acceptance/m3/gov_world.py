"""M3 验收测试的建库辅助（spec/system/governance.md 第 7 章）。导入本模块不会加载 mao。

- make_registry：把 tests/fixtures/registry-m2 复制成新的 Git 仓库（main 分支，初始提交）；
- commit_change：在给定父提交上做一次提交，**不移动 main**（模拟厂长在自己的分支上工作）；
- open_world：写 factory.json、打开 Context、EpochStore.init 初始提交；
- 若干返回补丁文件的函数，供各测试组合。所有补丁都保持 Registry 零 error（gov_world 自检）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "registry-m2"
START = datetime(2026, 11, 10, tzinfo=UTC)
HUMAN_ENV = {
    "GIT_AUTHOR_NAME": "human",
    "GIT_AUTHOR_EMAIL": "human@example.invalid",
    "GIT_COMMITTER_NAME": "human",
    "GIT_COMMITTER_EMAIL": "human@example.invalid",
}

if not FIXTURE.is_dir():
    raise FileNotFoundError(f"registry-m2 fixture is missing: {FIXTURE}")


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **HUMAN_ENV}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    return proc.stdout.strip()


def fixture_text(rel: str) -> str:
    return (FIXTURE / rel).read_text(encoding="utf-8")


def make_registry(dest: Path) -> tuple[Path, str]:
    repo = dest / "registry"
    shutil.copytree(FIXTURE, repo)
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial registry")
    return repo, git(repo, "rev-parse", "HEAD")


_counter = {"n": 0}


def commit_change(
    repo: Path, files: dict[str, str | None], *, parent: str | None = None, message: str = "change"
) -> str:
    """在 parent（缺省 main）上提交 files（值为 None 表示删除），返回新 commit。main 不动。"""
    parent = parent or git(repo, "rev-parse", "refs/heads/main")
    git(repo, "checkout", "-q", "--detach", parent)
    try:
        for rel, text in files.items():
            path = repo / rel
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        new = git(repo, "rev-parse", "HEAD")
        _counter["n"] += 1
        git(repo, "update-ref", f"refs/proposals/p{_counter['n']}", new)
    finally:
        git(repo, "checkout", "-q", "-f", "main")
    return new


@dataclass
class World:
    ctx: Any
    clock: Any
    repo: Path
    base: str
    epochs: Any
    snapshot: Any


def write_factory_json(root: Path, repo: Path, **governance: Any) -> None:
    config = {"governance": {"registry": str(repo), "git_timeout_s": 60, **governance}}
    (root / "factory.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


def open_world(tmp: Path, *, init: bool = True, **governance: Any) -> World:
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.governance import EpochStore
    from mao.sandbox import LocalProvider
    from tests.fakes.fakelib import local_commands

    repo, base = make_registry(tmp)
    root = tmp / "factory"
    root.mkdir()
    write_factory_json(root, repo, **governance)
    clock = FakeClock(START)
    provider = LocalProvider(root, commands=local_commands(), clock=clock)
    ctx = Context.open(root, provider=provider, clock=clock, ids=CounterIds())
    epochs = EpochStore(ctx)
    snapshot = None
    if init:
        epochs.init(base, by="human:acceptance")
        snapshot = epochs.materialize(base)
    return World(ctx=ctx, clock=clock, repo=repo, base=base, epochs=epochs, snapshot=snapshot)


# ---------------------------------------------------------------- ChangeSet 文档

ROLLOUT = {
    "shadow": {"runs": 1, "budget_usd": 1.0},
    "canary": {"percent": 50, "min_runs": 2},
    "observe": "1h",
    "slo": {"success_rate": ">= 0.9", "gate_reject_rate": "<= 0.2", "cost_per_run_usd": "<= 10"},
}


def changeset_doc(
    base: str, changes: list[dict[str, Any]], *, rollout: dict[str, Any] | None = None, **spec_extra: Any
) -> dict[str, Any]:
    return {
        "apiVersion": "mao/v1",
        "kind": "ChangeSet",
        "metadata": {"name": "cs-draft", "proposer": "acceptance"},
        "spec": {
            "base_epoch": base,
            "intent": "验收测试",
            "changes": changes,
            "rollout": rollout or ROLLOUT,
            **spec_extra,
        },
    }


def upsert(kind: str, name: str) -> dict[str, Any]:
    return {"op": "upsert", "kind": kind, "name": name}


# ---------------------------------------------------------------- Registry 补丁


def _bump(doc_text: str, old_version: str, new_version: str) -> str:
    out, n = re.subn(
        r"version: " + re.escape(old_version) + r"\}", "version: " + new_version + "}", doc_text, count=1
    )
    if n != 1:
        raise AssertionError(f"version {old_version} not found")
    return out


def _append_doc(rel: str, new_doc: str) -> dict[str, str]:
    return {rel: fixture_text(rel).rstrip("\n") + "\n---\n" + new_doc.lstrip("\n")}


def upper_v2_limits() -> dict[str, str]:
    """新增 upper@2，只改 limits（T1）。"""
    doc = _bump(fixture_text("stations/upper.yaml"), "1", "2")
    doc = doc.replace("timeout: 60s", "timeout: 90s", 1)
    return _append_doc("stations/upper.yaml", doc)


def toolbox_v2_image() -> dict[str, str]:
    """新增 toolbox@2，只换镜像（T2）。"""
    text = fixture_text("machines/toolbox/machine.yaml")
    doc = _bump(text, "1", "2").replace("09b42648636a8405", "19b42648636a8405", 1)
    return _append_doc("machines/toolbox/machine.yaml", doc)


def publish_local_v3() -> dict[str, str]:
    """新增 sink publish-local@3，只改 limits（sink → T3）。"""
    doc = _bump(fixture_text("stations/publish-local.yaml"), "1", "3").replace(
        "timeout: 60s", "timeout: 90s", 1
    )
    return _append_doc("stations/publish-local.yaml", doc)


def upper_v1_in_place() -> dict[str, str]:
    """原地改 upper@1（V3_IMMUTABLE）。"""
    return {
        "stations/upper.yaml": fixture_text("stations/upper.yaml").replace("timeout: 60s", "timeout: 90s", 1)
    }


def note_text_v1_1_incompatible() -> dict[str, str]:
    """新增 note.text@1.1，必需 payload 从 note.txt 改成 text.txt（V3_INCOMPATIBLE）。"""
    doc = fixture_text("types/note.yaml").replace('version: "1.0"', 'version: "1.1"', 1)
    doc = doc.replace(
        "{path: note.txt, media_type: text/plain, required: true}",
        "{path: text.txt, media_type: text/plain, required: true}",
        1,
    )
    return _append_doc("types/note.yaml", doc)


def constitution_edit() -> dict[str, str]:
    return {
        "constitution/invariants.mrl": fixture_text("constitution/invariants.mrl") + "// 厂长想放宽规则\n"
    }


def broken_station() -> dict[str, str]:
    """引用不存在的机器（REGISTRY_INVALID）。"""
    doc = fixture_text("stations/upper.yaml").replace("machine: toolbox@1", "machine: nope@1", 1)
    return {"stations/upper.yaml": doc}


# ---------------------------------------------------------------- 运行记录


def insert_run(
    ctx: Any,
    run_id: str,
    *,
    epoch: str,
    state: str,
    created_at: str,
    spent_usd: float = 0.0,
    plan: str = "{}",
    line: str = "Line:note-trigger@1",
) -> None:
    ctx.db.query(
        "INSERT INTO runs (id, line, epoch, state, plan, plan_digest, inputs, budget_cap_usd, spent_usd, "
        "trigger, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '{}', 5.0, ?, 'acceptance', ?, ?)",
        (run_id, line, epoch, state, plan, "sha256:" + "0" * 64, spent_usd, created_at, created_at),
    )


def insert_job(ctx: Any, job_id: str, *, run_id: str, rejected: bool, at: str) -> None:
    ctx.db.query(
        "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
        "VALUES (?, 'Station:upper@1', 'x', 'SEALED', 1, '{}', '{}', '{}', ?, ?)",
        (job_id, at, at),
    )
    ctx.db.query(
        "INSERT INTO run_node_jobs (run_id, node_id, job_id, created_at) VALUES (?, 'upper', ?, ?)",
        (run_id, job_id, at),
    )
    if rejected:
        data = json.dumps({"from": "GATING", "to": "REJECTED", "attempt": 1, "reason": "gate"})
        ctx.db.query(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'job.state', ?, ?)",
            ("ev-acc-" + job_id, at, job_id, data),
        )
