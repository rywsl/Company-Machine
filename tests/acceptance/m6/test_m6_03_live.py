"""M6-03 验收：真实 CLI 在 Docker 里的端到端（spec/system/live.md 第 7 章）。

需要 Docker、M1 测试镜像与现场镜像。镜像缺失时失败并提示构建命令，不跳过。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("mao.supervisor")
pytest.importorskip("mao.daemon")

from tests.acceptance.m1.m1_exec_helpers import read_payload  # noqa: E402
from tests.acceptance.m2.world import seal_input  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    ROLLOUT,
    changeset_doc,
    commit_change,
    fixture_text,
    git,
    upper_v2_limits,
    upsert,
)
from tests.acceptance.m6.live_world import (  # noqa: E402
    SECRET,
    TERMINAL,
    image_labels,
    live_image_ids,
    live_world,
    write_and_commit,
)
from tests.fakes.fakelib import REPO  # noqa: E402

pytestmark = pytest.mark.docker

EPOCH_RE = "@prompt:active epoch ([0-9a-f]{40})"
CS_RE = "@prompt:ChangeSet (cs-[0-9]+)"
DRAFT = "mkdir -p /workspace/drafts && printf '第一行\\n第二行\\n' > /workspace/drafts/note.txt"
CANARY = dict(ROLLOUT, canary={"percent": 100, "min_runs": 1})


def _anthropic(turns: list, **extra) -> dict:
    return {
        "anthropic": {
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "main_models": ["fake-opus"],
            "turns": turns,
            "fallback": "ok",
            **extra,
        }
    }


def _bash(command: str) -> dict:
    return {"tool": {"name": "Bash"}, "input": {"command": command, "description": "acceptance"}}


def _job(lw, station: str):
    from mao.jobs import load_job
    from mao.runner import Runner

    task = seal_input(lw.w.ctx, lw.w.snapshot, "agent.task@1")
    job = Runner(lw.w.ctx, lw.w.snapshot).submit(lw.w.snapshot.key("Station", station), {"task": task}, {})
    assert lw.until(lambda: load_job(lw.w.ctx.db, job.id).state.value in TERMINAL, limit=5)
    return load_job(lw.w.ctx.db, job.id)


def test_live_images_pinned() -> None:
    versions = json.loads((REPO / "tools" / "live_versions.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", versions["base"]), versions["base"]
    for key in ("claude_code", "codex"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", versions[key]), versions[key]
    live_image_ids()
    assert image_labels("claude-code")["mao.live.claude_code"] == versions["claude_code"]
    assert image_labels("codex")["mao.live.codex"] == versions["codex"]
    for name in ("live-bench", "claude-code", "codex"):
        assert image_labels(name)["mao.live.base"] == versions["base"]


def test_claude_station_job(tmp_path: Path) -> None:
    lw = live_world(tmp_path, _anthropic([[_bash(DRAFT)], [{"text": "写好了。"}]]))
    try:
        done = _job(lw, "writer-live@1")
        assert done.state.value == "SEALED", done.error
        [out] = done.outputs["note"]
        assert read_payload(lw.w.ctx, out, "note.txt") == "第一行\n第二行\n"
        main = [item for item in lw.llm.requests if item["model"] == "fake-opus"]
        assert main
        assert all(item["headers"].get("x-api-key") == SECRET for item in main)
        row = lw.w.ctx.db.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS spent FROM model_usage WHERE job_id = ?",
            (done.id,),
        )[0]
        assert row["n"] > 0
        assert abs(done.cost_usd - row["spent"]) < 1e-9
    finally:
        lw.close()


def test_sandbox_isolation(tmp_path: Path) -> None:
    probe = (
        "env; python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 5)\" "
        "|| echo DIRECT-BLOCKED"
    )
    lw = live_world(tmp_path, _anthropic([[_bash(probe)], [_bash(DRAFT)], [{"text": "完成。"}]]))
    try:
        done = _job(lw, "writer-live@1")
        assert done.state.value == "SEALED", done.error
        seen = [item for item in lw.llm.requests if item["model"] == "fake-opus" and item["turn"] == 1]
        assert seen, lw.llm.requests
        observed = seen[-1]["last_result"]
        assert "ANTHROPIC_API_KEY=factory-proxy" in observed
        assert f"X-Factory-Job: {done.id}" in observed
        assert "DIRECT-BLOCKED" in observed
        assert SECRET not in observed
    finally:
        lw.close()


def test_budget_stop_real_cli(tmp_path: Path) -> None:
    script = _anthropic([[_bash(DRAFT)], [{"text": "完成。"}]])
    script["anthropic"]["usage"] = {"input_tokens": 10_000_000, "output_tokens": 10}
    lw = live_world(tmp_path, script)
    try:
        done = _job(lw, "writer-live@1")
        assert done.state.value == "FAILED"
        assert done.error["code"] == "BUDGET_EXCEEDED"
        assert not done.outputs.get("note")
    finally:
        lw.close()


def test_claude_foreman_session(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore, WakeQueue

    [(rel, text)] = upper_v2_limits().items()
    turns = [
        [_bash(write_and_commit(rel, text, "upper@2: longer timeout"))],
        [
            {
                "tool": {"suffix": "changeset_propose"},
                "input": {
                    "document": changeset_doc(EPOCH_RE, [upsert("Station", "upper")]),
                    "commit": "@last_result",
                },
            }
        ],
        [{"text": "已提议。"}],
    ]
    lw = live_world(tmp_path, _anthropic(turns))
    try:
        request = ConversionStore(lw.w.ctx).open_or_merge(
            from_type="note.raw@1",
            guarantees=[],
            to_type="note.text@1",
            requires=[],
            found_at={"line": "live"},
        )
        WakeQueue(lw.w.ctx).file("conversion", request.id, {"id": request.id})
        sup = lw.supervisor()
        sup.start()
        record = sup.run_once()
        sup.shutdown()
        assert record.state == "ok", record.error
        assert record.actor == "claude-code@1"
        [cs_id] = record.proposed
        cs = lw.pipeline.get(cs_id)
        assert cs.proposer == "claude-code@1"
        assert cs.proposer_runtime == "OperatorRuntime:claude-code@1"
        assert git(lw.w.repo, "log", "-1", "--format=%an", cs.source_commit) == "claude-code@1"
    finally:
        lw.close()


def test_codex_reviewer_session(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    script = {
        "openai": {
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "main_models": ["fake-codex"],
            "turns": [
                [
                    {
                        "tool": {"suffix": "review_submit"},
                        "input": {"changeset_id": CS_RE, "verdict": "approve", "note": "只新增一个工位"},
                    }
                ],
                [{"text": "已复核。"}],
            ],
            "fallback": "ok",
        }
    }
    lw = live_world(tmp_path, script)
    try:
        original = fixture_text("stations/upper.yaml")
        copy = original.replace(
            "metadata: {name: upper, version: 1}", "metadata: {name: upper-two, version: 1}"
        )
        assert copy != original
        source = commit_change(lw.w.repo, {"stations/upper-two.yaml": copy})
        cs = lw.pipeline.propose(
            changeset_doc(lw.w.base, [upsert("Station", "upper-two")]),
            source,
            by="claude-code@1",
            timeout_s=120,
        )
        cs = lw.pipeline.advance(cs.id)
        assert cs.state == "verified" and cs.reviews_required == 1
        WakeQueue(lw.w.ctx).file("review", cs.id, {"id": cs.id, "need": 1})
        sup = lw.supervisor()
        sup.start()
        record = sup.run_once()
        sup.shutdown()
        assert record.state == "ok", record.error
        assert (record.role, record.actor) == ("reviewer", "codex@1")
        assert lw.pipeline.approvals(cs.id) == ["OperatorRuntime:codex@1"]
    finally:
        lw.close()


def test_autonomy_loop_in_docker(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore, WakeQueue

    converter = fixture_text("stations/note-to-text.yaml")
    script = _anthropic(
        [
            [_bash(write_and_commit("stations/note-to-text.yaml", converter, "add note-to-text converter"))],
            [
                {
                    "tool": {"suffix": "changeset_propose"},
                    "input": {
                        "document": changeset_doc(
                            EPOCH_RE, [upsert("Station", "note-to-text")], rollout=CANARY
                        ),
                        "commit": "@last_result",
                    },
                }
            ],
            [{"text": "已提议。"}],
        ]
    )
    script["openai"] = {
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "main_models": ["fake-codex"],
        "turns": [
            [
                {
                    "tool": {"suffix": "review_submit"},
                    "input": {"changeset_id": CS_RE, "verdict": "approve", "note": "正反样例齐全"},
                }
            ],
            [{"text": "已复核。"}],
        ],
        "fallback": "ok",
    }
    lw = live_world(tmp_path, script, drop=("stations/note-to-text.yaml", "lines/note-pipeline.yaml"))
    try:
        draft = commit_change(
            lw.w.repo, {"lines/note-pipeline.yaml": fixture_text("lines/note-pipeline.yaml")}
        )
        human = lw.pipeline.propose(
            changeset_doc(lw.w.base, [upsert("Line", "note-pipeline")]),
            draft,
            by="human:acceptance",
            timeout_s=120,
        )
        assert lw.pipeline.advance(human.id).state == "rejected"
        [request] = ConversionStore(lw.w.ctx).list(status="open")
        sup = lw.supervisor()
        sup.start()
        lw.daemon.tick()
        record = sup.run_once()
        assert record is not None and record.state == "ok", record and record.error
        assert record.actor == "claude-code@1"
        [cs_id] = record.proposed
        assert lw.until(lambda: lw.pipeline.get(cs_id).state == "canary", supervisor=sup), lw.pipeline.get(
            cs_id
        ).report
        assert lw.pipeline.approvals(cs_id) == ["OperatorRuntime:codex@1"]
        bean = seal_input(lw.w.ctx, lw.w.snapshot, "note.text@1")
        lw.w.ctx.warehouse.label(bean, "line", "note-trigger", by="acceptance")
        assert lw.until(lambda: lw.pipeline.get(cs_id).state == "active", supervisor=sup), lw.pipeline.get(
            cs_id
        ).report
        sup.shutdown()
        cs = lw.pipeline.get(cs_id)
        assert lw.w.epochs.current() == cs.commit
        assert (
            next(item for item in ConversionStore(lw.w.ctx).list() if item.id == request.id).status.value
            == "resolved"
        )
        sessions = [(item.role, item.actor, item.state) for item in sup.sessions()]
        assert sessions == [("foreman", "claude-code@1", "ok"), ("reviewer", "codex@1", "ok")]
        signals = {
            w.signal: w.status for w in WakeQueue(lw.w.ctx).list() if w.signal in {"conversion", "review"}
        }
        assert signals == {"conversion": "done", "review": "done"}
    finally:
        lw.close()
