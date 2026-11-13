"""M13-01：Agent kind、agent_config、invoke.resume、transcript 增补（spec/system/agents.md 3.1、3.3、4.1）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.acceptance.m13.agent_world import SCOUT, World


def test_agent_kind_compiles(tmp_path: Path) -> None:
    w = World(tmp_path)
    assert w.snapshot.keys("Agent") == ["Agent:helper@1", "Agent:scout@1"]
    assert w.snapshot.spec("Agent:scout@1")["lines"] == ["note-trigger@1"]
    broken = tmp_path / "broken"
    shutil.copytree(w.repo, broken, ignore=shutil.ignore_patterns(".git"))
    (broken / "agents" / "bad.yaml").write_text(
        SCOUT.replace("name: scout", "name: bad")
        .replace("[note-trigger@1]", "[no-such-line@1]")
        .replace("scout-in", "bad-in"),
        encoding="utf-8",
        newline="\n",
    )
    code, body, err = w.cli("check", str(broken))
    assert code == 3, (body, err)
    assert "Agent:bad@1" in json.dumps(body, ensure_ascii=False) + err


def test_agent_config_rules(tmp_path: Path) -> None:
    from mao.agents import agent_config
    from mao.errors import FactoryError

    w = World(tmp_path)
    cfg = agent_config(w.snapshot, "scout")
    assert (cfg.name, cfg.key, cfg.runtime, cfg.model, cfg.policy) == (
        "scout",
        "Agent:scout@1",
        "OperatorRuntime:fake-agent@1",
        "fake-model-1",
        "Policy:agent-default@1",
    )
    assert cfg.lines == ("Line:note-trigger@1",)
    assert cfg.schedule == ((3600.0, "整理笔记"),)
    assert cfg.beans[0][1] == "src=scout"
    assert (cfg.daily_usd, cfg.turn_usd, cfg.turn_timeout_s) == (1.0, 1.0, 60.0)
    assert (cfg.batch, cfg.rotate_turns, cfg.rotate_tokens, cfg.max_turns) == (3, 3, 1000, 5)
    helper = agent_config(w.snapshot, "helper")
    assert (helper.batch, helper.rotate_turns, helper.rotate_tokens, helper.max_turns) == (
        20,
        200,
        150000,
        50,
    )
    assert helper.schedule == () and helper.beans == () and helper.lines == ()

    base = w.snapshot.spec("Agent:scout@1")

    def variant(**changes):
        spec = json.loads(json.dumps(base))
        for key, value in changes.items():
            spec[key] = value
        return spec

    cases = {
        "runtime": variant(runtime="fake-reviewer@1"),
        "model": variant(model="other-model"),
        "charter": variant(charter=""),
        "schedule": variant(subscribe={"schedule": [{"every": "30s"}]}),
        "label": variant(subscribe={"beans": [{"type": "note.text@1", "label": "nolabel"}]}),
        "budget": variant(budget={"daily_usd": 1.0, "turn_usd": 0, "turn_timeout": "60s"}),
        "context": variant(context={"batch": 0}),
    }
    for reason, spec in cases.items():
        with pytest.raises(FactoryError) as ei:
            agent_config(w.snapshot, "scout", spec=spec)
        assert (ei.value.code, ei.value.detail.get("reason")) == ("AGENT_INVALID", reason), reason
    with pytest.raises(FactoryError) as ei:
        agent_config(w.snapshot, "nobody")
    assert ei.value.code == "AGENT_NOT_FOUND"

    (w.repo / "agents" / "scout-2.yaml").write_text(
        SCOUT.replace("version: 1", "version: 2").replace("scout-in", "scout-in-2"),
        encoding="utf-8",
        newline="\n",
    )
    from tests.acceptance.m3.gov_world import git

    git(w.repo, "add", "-A")
    git(w.repo, "commit", "-q", "-m", "second scout")
    head = git(w.repo, "rev-parse", "HEAD")
    w.epochs.build(head)
    with pytest.raises(FactoryError) as ei:
        agent_config(w.epochs.materialize(head), "scout")
    assert (ei.value.code, ei.value.detail.get("reason")) == ("AGENT_INVALID", "versions")


def test_resume_invocation(tmp_path: Path) -> None:
    from mao.errors import FactoryError
    from mao.operator import build_runtime_invocation

    w = World(tmp_path)
    common = dict(
        task="do it", contract="rules", model="fake-model-1", max_turns=5, budget_usd=1.0, timeout_s=60
    )
    first = build_runtime_invocation(w.snapshot, "fake-agent@1", **common)
    assert first.argv[1:3] == ("-p", "do it")
    again = build_runtime_invocation(w.snapshot, "fake-agent@1", session="fake-at-0001-1", **common)
    assert again.argv[1:5] == ("--resume", "fake-at-0001-1", "-p", "do it")
    with pytest.raises(FactoryError) as ei:
        build_runtime_invocation(w.snapshot, "fake-reviewer@1", session="s-1", **common)
    assert (ei.value.code, ei.value.detail.get("reason")) == ("INVOCATION_INVALID", "resume")


def test_transcript_session_and_tokens(tmp_path: Path) -> None:
    from mao.operator import parse_transcript

    path = tmp_path / "transcript.raw"
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-early"},
        {"type": "assistant", "session_id": "s-late", "message": {"role": "assistant", "content": []}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "result": "ok",
            "session_id": "s-late",
            "total_cost_usd": 0.25,
            "usage": {"input_tokens": 1234},
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    summary = parse_transcript(path, "stream-json")
    assert (summary.session_id, summary.input_tokens, summary.cost_usd) == ("s-late", 1234, 0.25)
    plain = tmp_path / "plain.raw"
    plain.write_text(
        json.dumps({"type": "result", "subtype": "success", "is_error": False}) + "\n", encoding="utf-8"
    )
    bare = parse_transcript(plain, "stream-json")
    assert (bare.session_id, bare.input_tokens) == (None, 0)
