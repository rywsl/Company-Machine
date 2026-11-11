"""M5-06 验收：厂长值守（spec/system/supervisor.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.supervisor")

from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    changeset_doc,
    commit_change,
    fixture_text,
    git,
    toolbox_v2_image,
    upper_v2_limits,
    upsert,
)
from tests.acceptance.m5.auto_world import (  # noqa: E402
    REVIEW_ACTION,
    auto_world,
    commit_actions,
    events,
    foreman_section,
    pass_pipeline,
    propose_action,
    session_dir,
    supervisor,
    write_script,
)


def _conversion_wake(ctx):
    from mao.foreman import ConversionStore, WakeQueue

    request = ConversionStore(ctx).open_or_merge(
        from_type="note.raw@1",
        guarantees=[],
        to_type="note.text@1",
        requires=[],
        found_at={"line": "acceptance"},
    )
    return request, WakeQueue(ctx).file("conversion", request.id, {"id": request.id})


def _verified_t2(w, pipeline):
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    cs = pipeline.advance(cs.id)
    assert cs.state == "verified" and cs.reviews_required == 1
    return cs


def test_config_required(tmp_path: Path) -> None:
    from mao.supervisor import SupervisorConfig

    root = tmp_path / "root"
    root.mkdir()
    config = root / "factory.json"
    config.write_text(json.dumps({"governance": {}}), encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        SupervisorConfig.load(root)
    assert ei.value.code == "SUPERVISOR_CONFIG_MISSING"
    section = foreman_section(tmp_path / "script.json")
    del section["image"]
    config.write_text(json.dumps({"foreman": section}), encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        SupervisorConfig.load(root)
    assert ei.value.code == "SUPERVISOR_CONFIG_INVALID"
    assert ei.value.detail["field"] == "image"
    section = foreman_section(tmp_path / "script.json", env={"FACTORY_ACTOR": "human:x"})
    config.write_text(json.dumps({"foreman": section}), encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        SupervisorConfig.load(root)
    assert ei.value.code == "SUPERVISOR_CONFIG_INVALID"
    config.write_text(json.dumps({"foreman": foreman_section(tmp_path / "script.json")}), encoding="utf-8")
    loaded = SupervisorConfig.load(root)
    assert loaded.reviewers == ("fake-agent@1", "fake-reviewer@1")
    assert loaded.max_attempts == 2


def test_single_instance(tmp_path: Path) -> None:
    from mao.base import RandomIds
    from mao.context import Context
    from mao.sandbox import LocalProvider
    from mao.supervisor import ForemanSupervisor, SupervisorConfig
    from tests.fakes.fakelib import local_commands

    w = auto_world(tmp_path)
    pipeline = pass_pipeline(w)
    first = supervisor(w, pipeline)
    assert first.start() == []
    other = Context.open(
        w.ctx.root,
        provider=LocalProvider(w.ctx.root, commands=local_commands(), clock=w.clock),
        clock=w.clock,
        ids=RandomIds(),
    )
    second = ForemanSupervisor(other, w.epochs, SupervisorConfig.load(w.ctx.root), pipeline=pipeline)
    with pytest.raises(FactoryError) as ei:
        second.start()
    assert ei.value.code == "SUPERVISOR_ALREADY_RUNNING"
    first.shutdown()
    second.start()
    assert second.run_once() is None
    assert second.sessions() == []
    second.shutdown()


def test_runtime_invocation(tmp_path: Path) -> None:
    from mao.operator import build_runtime_invocation

    w = auto_world(tmp_path)
    inv = build_runtime_invocation(
        w.snapshot,
        "fake-agent@1",
        task="T",
        contract="C",
        model="fake-model-1",
        max_turns=7,
        budget_usd=1.5,
        timeout_s=120,
    )
    assert inv.argv == (
        "/opt/operator/bin/fake-agent",
        "-p",
        "T",
        "--append-system-prompt",
        "C",
        "--output-format",
        "stream-json",
        "--max-turns",
        "7",
        "--model",
        "fake-model-1",
    )
    assert inv.env["FAKE_AGENT_RUNTIME"] == "fake-agent@1"
    assert inv.env["FAKE_AGENT_BUDGET"] == "1.5"
    assert inv.timeout_s == 120.0
    assert inv.transcript == "stream-json"
    with pytest.raises(FactoryError) as ei:
        build_runtime_invocation(
            w.snapshot,
            "script@1",
            task="T",
            contract="C",
            model="",
            max_turns=1,
            budget_usd=1.0,
            timeout_s=60,
        )
    assert ei.value.code == "INVOCATION_INVALID"
    with pytest.raises(FactoryError) as ei:
        build_runtime_invocation(
            w.snapshot,
            "fake-agent@1",
            task="T",
            contract="C",
            model="other-model",
            max_turns=1,
            budget_usd=1.0,
            timeout_s=60,
        )
    assert ei.value.code == "MODEL_NOT_ALLOWED"


def test_foreman_session_proposes(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore, WakeQueue

    write_script(
        tmp_path,
        {
            "default": commit_actions(upper_v2_limits(), "upper@2: longer timeout")
            + [propose_action([upsert("Station", "upper")])]
        },
    )
    w = auto_world(tmp_path)
    pipeline = pass_pipeline(w)
    request, wake = _conversion_wake(w.ctx)
    sup = supervisor(w, pipeline)
    sup.start()
    record = sup.run_once()
    sup.shutdown()
    assert record is not None
    assert record.state == "ok", record.error
    assert (record.role, record.actor, record.runtime) == (
        "foreman",
        "fake-agent@1",
        "OperatorRuntime:fake-agent@1",
    )
    assert record.wake_id == wake.id and record.epoch == w.base
    assert len(record.proposed) == 1
    cs = pipeline.get(record.proposed[0])
    assert cs.proposer == "fake-agent@1"
    assert cs.proposer_runtime == "OperatorRuntime:fake-agent@1"
    assert git(w.repo, "log", "-1", "--format=%an", cs.source_commit) == "fake-agent@1"
    updated = next(item for item in ConversionStore(w.ctx).list() if item.id == request.id)
    assert updated.status.value == "in_progress"
    assert updated.changeset_id == cs.id
    assert WakeQueue(w.ctx).get(wake.id).status == "done"
    folder = session_dir(w, record.id)
    brief = (folder / "in" / "brief.md").read_text(encoding="utf-8")
    assert wake.id in brief and w.base in brief
    assert (folder / "logs" / "transcript.jsonl").is_file()
    assert [item["session"] for item in events(w.ctx, "foreman.session.finished")] == [record.id]


def test_failure_backoff_then_give_up(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"attempts": {"1": [{"do": "exit", "code": 3}], "2": [{"do": "exit", "code": 4}]}})
    w = auto_world(tmp_path)
    queue = WakeQueue(w.ctx)
    wake = queue.file("other", "acceptance", {"id": "acceptance"})
    sup = supervisor(w, pass_pipeline(w))
    sup.start()
    first = sup.run_once()
    assert first.state == "failed"
    assert first.error["code"] == "SESSION_FAILED"
    assert first.exit_code == 3
    reopened = queue.get(wake.id)
    assert (reopened.status, reopened.attempts) == ("open", 1)
    assert reopened.not_before is not None
    assert sup.run_once() is None
    w.clock.advance(61)
    second = sup.run_once()
    assert second.state == "failed" and second.exit_code == 4
    assert queue.get(wake.id).status == "failed"
    assert queue.file("other", "acceptance", {"id": "acceptance"}).id == wake.id
    assert sup.run_once() is None
    assert [item["wake"] for item in events(w.ctx, "foreman.wake.failed")] == [wake.id]
    sup.shutdown()


def test_conversion_needs_proposal(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore, WakeQueue

    write_script(tmp_path, {"default": []})
    w = auto_world(tmp_path)
    request, wake = _conversion_wake(w.ctx)
    sup = supervisor(w, pass_pipeline(w))
    sup.start()
    record = sup.run_once()
    sup.shutdown()
    assert record.exit_code == 0
    assert record.state == "failed"
    assert record.error["code"] == "SESSION_NO_OUTCOME"
    assert WakeQueue(w.ctx).get(wake.id).status == "open"
    assert (
        next(item for item in ConversionStore(w.ctx).list() if item.id == request.id).status.value == "open"
    )


def test_reviewer_session(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"default": [REVIEW_ACTION]})
    w = auto_world(tmp_path)
    pipeline = pass_pipeline(w)
    cs = _verified_t2(w, pipeline)
    wake = WakeQueue(w.ctx).file("review", cs.id, {"id": cs.id, "need": 1})
    sup = supervisor(w, pipeline)
    sup.start()
    record = sup.run_once()
    sup.shutdown()
    assert record.state == "ok", record.error
    assert (record.role, record.actor) == ("reviewer", "fake-reviewer@1")
    assert record.reviewed == (cs.id,)
    assert pipeline.approvals(cs.id) == ["OperatorRuntime:fake-reviewer@1"]
    assert WakeQueue(w.ctx).get(wake.id).status == "done"


def test_reviewers_exhausted(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"default": [REVIEW_ACTION]})
    w = auto_world(tmp_path, reviewers=["fake-agent@1"], models={"fake-agent@1": "fake-model-1"})
    pipeline = pass_pipeline(w)
    cs = _verified_t2(w, pipeline)
    wake = WakeQueue(w.ctx).file("review", cs.id, {"id": cs.id, "need": 1})
    sup = supervisor(w, pipeline)
    sup.start()
    assert sup.run_once() is None
    sup.shutdown()
    assert sup.sessions() == []
    assert WakeQueue(w.ctx).get(wake.id).status == "failed"
    assert [item["code"] for item in events(w.ctx, "foreman.wake.failed")] == ["REVIEWERS_EXHAUSTED"]
    assert pipeline.approvals(cs.id) == []


def test_daily_budget(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"cost_usd": 2.0, "default": []})
    w = auto_world(tmp_path, daily_budget_usd=1.5)
    queue = WakeQueue(w.ctx)
    queue.file("other", "first", {"id": "first"})
    sup = supervisor(w, pass_pipeline(w))
    sup.start()
    record = sup.run_once()
    assert record.state == "ok" and record.cost_usd == 2.0
    second = queue.file("other", "second", {"id": "second"})
    assert sup.run_once() is None
    assert sup.run_once() is None
    sup.shutdown()
    assert queue.get(second.id).status == "open"
    assert len(events(w.ctx, "foreman.budget_exhausted")) == 1
    assert len(sup.sessions()) == 1


def test_restart_recovers_session(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    w = auto_world(tmp_path)
    queue = WakeQueue(w.ctx)
    wake = queue.file("other", "crashed", {"id": "crashed"})
    assert queue.next(by="supervisor").id == wake.id
    at = "2026-11-10T00:00:00.000Z"
    with w.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO foreman_sessions (id, wake_id, role, runtime, actor, model, epoch, state, started_at) "
            "VALUES ('fs-0900', ?, 'foreman', 'OperatorRuntime:fake-agent@1', 'fake-agent@1', 'fake-model-1', ?, "
            "'running', ?)",
            (wake.id, w.base, at),
        )
    sup = supervisor(w, pass_pipeline(w))
    assert sup.start() == ["fs-0900"]
    sup.shutdown()
    recovered = next(item for item in sup.sessions() if item.id == "fs-0900")
    assert recovered.state == "failed"
    assert recovered.error["code"] == "CONTROL_PLANE_RESTART"
    again = queue.get(wake.id)
    assert (again.status, again.attempts) == ("open", 1)


def test_transcript_redacted(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    leak = {"type": "assistant", "message": {"content": [{"type": "text", "text": "api_key=sk-test-123456"}]}}
    write_script(tmp_path, {"default": [{"do": "emit", "event": leak}]})
    w = auto_world(tmp_path)
    WakeQueue(w.ctx).file("other", "leak", {"id": "leak"})
    sup = supervisor(w, pass_pipeline(w))
    sup.start()
    record = sup.run_once()
    sup.shutdown()
    assert record.state == "ok", record.error
    logs = session_dir(w, record.id) / "logs"
    archived = (logs / "transcript.jsonl").read_text(encoding="utf-8")
    assert "[REDACTED]" in archived
    assert "sk-test-123456" not in archived
    assert {path.name for path in logs.iterdir()} <= {"transcript.jsonl", "stderr.txt"}


def test_no_converter_files_request(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore
    from mao.mcp import McpServer, Session

    w = auto_world(tmp_path, drop=("stations/note-to-text.yaml", "lines/note-pipeline.yaml"))
    pipeline = pass_pipeline(w)
    draft = commit_change(w.repo, {"lines/note-pipeline.yaml": fixture_text("lines/note-pipeline.yaml")})
    server = McpServer(w.ctx, w.epochs, Session(role="foreman", actor="fake-agent@1"), pipeline=pipeline)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
    )
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "registry_check", "arguments": {"commit": draft}},
        }
    )
    result = reply["result"]
    assert result["isError"] is False, result
    check = result["structuredContent"]["result"]
    assert check["ok"] is False
    assert any(item["code"] == "E-NO-CONVERTER" for item in check["diags"])
    assert len(check["conversions"]) == 1
    [request] = ConversionStore(w.ctx).list(status="open")
    assert request.id == check["conversions"][0]
    assert (request.from_type, request.to_type) == ("note.raw@1", "note.text@1")
    assert any(item.get("commit") == draft for item in request.found_at)
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Line", "note-pipeline")]), draft, by="human:acceptance", timeout_s=60
    )
    assert pipeline.advance(cs.id).state == "rejected"
    [merged] = ConversionStore(w.ctx).list(status="open")
    assert merged.id == request.id
    assert any(item.get("changeset") == cs.id for item in merged.found_at)


def test_model_proxy_accepts_running_session(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    from mao.base import FakeClock
    from mao.egress import ModelProxy, SecretStore, register_job_cap
    from tests.fakes.fake_llm import FakeLlm

    w = auto_world(tmp_path)
    script = tmp_path / "llm.json"
    script.write_text(
        json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 0}, "turns": []}), encoding="utf-8"
    )
    llm = FakeLlm(script)
    llm_port = llm.start()
    egress = w.ctx.root / "egress"
    egress.mkdir(exist_ok=True)
    (egress / "upstreams.json").write_text(
        json.dumps(
            {
                "fake-model-1": {
                    "url": f"http://127.0.0.1:{llm_port}/v1/messages",
                    "secret": "MODEL_TOKEN",
                    "header": "x-api-key",
                }
            }
        ),
        encoding="utf-8",
    )
    (egress / "prices.json").write_text(
        json.dumps({"fake-model-1": {"input_per_mtok": 2.0, "output_per_mtok": 4.0}}), encoding="utf-8"
    )
    secrets = tmp_path / "secrets-root"
    (secrets / "secrets" / "values").mkdir(parents=True)
    (secrets / "secrets" / "manifest.json").write_text('{"MODEL_TOKEN": "model"}', encoding="utf-8")
    (secrets / "secrets" / "values" / "MODEL_TOKEN").write_text("SESSION-SECRET-VALUE", encoding="utf-8")
    with w.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO foreman_sessions (id, wake_id, role, runtime, actor, model, epoch, state, started_at) "
            "VALUES ('fs-0901', 'wk-0001', 'foreman', 'OperatorRuntime:fake-agent@1', 'fake-agent@1', "
            "'fake-model-1', ?, 'running', '2026-11-10T00:00:00.000Z')",
            (w.base,),
        )
    register_job_cap(w.ctx.db, "fs-0901", "foreman", 1.0)
    proxy = ModelProxy(SecretStore(secrets), FakeClock(), w.ctx.root)
    port = proxy.start(port=0)

    def post() -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages",
            data=json.dumps(
                {"model": "fake-model-1", "messages": [{"role": "user", "content": "hi"}]}
            ).encode(),
            headers={"X-Factory-Job": "fs-0901", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code

    try:
        assert post() == 200
        assert proxy.spent("fs-0901") == 0.002
        with w.ctx.db.tx() as conn:
            conn.execute("UPDATE foreman_sessions SET state = 'ok' WHERE id = 'fs-0901'")
        assert post() == 403
    finally:
        proxy.close()
        llm.close()


def test_cli_sessions_and_reopen(tmp_path: Path, capsys) -> None:
    from mao.cli import main
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"default": [{"do": "exit", "code": 5}]})
    w = auto_world(tmp_path, max_attempts=1)
    queue = WakeQueue(w.ctx)
    wake = queue.file("other", "cli", {"id": "cli"})
    sup = supervisor(w, pass_pipeline(w))
    sup.start()
    record = sup.run_once()
    sup.shutdown()
    assert queue.get(wake.id).status == "failed"
    root = ["--root", str(w.ctx.root), "--provider", "local"]
    capsys.readouterr()
    assert main([*root, "--json", "foreman", "sessions", "--state", "failed"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [record.id]
    assert main([*root, "foreman", "reopen", wake.id]) == 0
    reopened = queue.get(wake.id)
    assert (reopened.status, reopened.attempts) == ("open", 0)
    assert main([*root, "foreman", "reopen", wake.id]) == 1
