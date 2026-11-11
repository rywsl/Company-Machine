"""M5-05 验收：MCP 操作面与 factoryctl 邮箱桥（spec/system/mcp.md 第 7 章）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.mcp")

from tests.acceptance.m3.gov_world import (  # noqa: E402
    ROOT,
    changeset_doc,
    commit_change,
    open_world,
    toolbox_v2_image,
    upper_v2_limits,
    upsert,
)

FACTORYCTL = ROOT / "src" / "mao" / "ctl" / "factoryctl.py"


class _PassResult:
    def __init__(self, n: int) -> None:
        self.id = f"ct-{n:04d}"
        self.kind = "fake"
        self.subject = "fake"
        self.verdict = "pass"
        self.detail: dict = {}
        self.at = "2026-11-10T00:00:00.000Z"


class _PassContracts:
    def __init__(self) -> None:
        self.n = 0

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.n += 1
            return _PassResult(self.n)

        return call


def _server(w, role: str, actor: str, pipeline=None):
    from mao.mcp import McpServer, Session

    return McpServer(w.ctx, w.epochs, Session(role=role, actor=actor), pipeline=pipeline)


def _rpc(server, method: str, params: dict | None = None, rid: int = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def _init(server, version: str = "2025-06-18") -> dict:
    return _rpc(
        server,
        "initialize",
        {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    )


def _call(server, name: str, arguments: dict, rid: int = 9) -> dict:
    return _rpc(server, "tools/call", {"name": name, "arguments": arguments}, rid)


def test_initialize_and_tools_by_role(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    foreman = _server(w, "foreman", "fake-agent@1")
    result = _init(foreman)["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    names = [tool["name"] for tool in _rpc(foreman, "tools/list", rid=2)["result"]["tools"]]
    assert names == sorted(names)
    assert "changeset_propose" in names and "review_submit" not in names
    reviewer = _server(w, "reviewer", "script@1")
    _init(reviewer)
    rnames = [tool["name"] for tool in _rpc(reviewer, "tools/list", rid=2)["result"]["tools"]]
    assert "review_submit" in rnames and "changeset_propose" not in rnames
    for tool in _rpc(foreman, "tools/list", rid=3)["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False


def test_version_negotiation(tmp_path: Path) -> None:
    from mao.mcp import PROTOCOL_VERSIONS

    w = open_world(tmp_path)
    server = _server(w, "foreman", "fake-agent@1")
    assert _init(server, "1999-01-01")["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


def test_jsonrpc_errors(tmp_path: Path) -> None:
    import io

    w = open_world(tmp_path)
    server = _server(w, "foreman", "fake-agent@1")
    assert _rpc(server, "tools/list")["error"]["code"] == -32600
    _init(server)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc(server, "no/such/method", rid=4)["error"]["code"] == -32601
    assert (
        _call(server, "review_submit", {"changeset_id": "cs-0001", "verdict": "approve", "note": ""})[
            "error"
        ]["code"]
        == -32602
    )
    assert (
        _call(server, "registry_get", {"key": "Station:upper@1", "by": "human:x"})["error"]["code"] == -32602
    )
    out = io.StringIO()
    server.serve_stdio(io.StringIO("this is not json\n"), out)
    line = json.loads(out.getvalue().strip())
    assert line["error"]["code"] == -32700 and line["id"] is None


def test_tool_result_and_tool_error(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    server = _server(w, "foreman", "fake-agent@1")
    _init(server)
    ok = _call(server, "registry_get", {"key": "Station:upper@1"})["result"]
    assert ok["isError"] is False
    assert ok["structuredContent"]["result"]["kind"] == "Station"
    assert json.loads(ok["content"][0]["text"])["kind"] == "Station"
    bad = _call(server, "registry_get", {"key": "Station:nope@9"})["result"]
    assert bad["isError"] is True
    assert json.loads(bad["content"][0]["text"])["code"] == "QUERY_DENIED"


def test_propose_identity_from_session(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    server = _server(w, "foreman", "fake-agent@1")
    _init(server)
    source = commit_change(w.repo, upper_v2_limits())
    document = changeset_doc(w.base, [upsert("Station", "upper")])
    document["metadata"]["proposer"] = "human:pretender"
    result = _call(server, "changeset_propose", {"document": document, "commit": source})["result"]
    assert result["isError"] is False, result
    assert result["structuredContent"]["result"]["proposer"] == "fake-agent@1"


def test_review_runtime_from_session(tmp_path: Path) -> None:
    from mao.governance import ChangePipeline

    w = open_world(tmp_path)
    fake = _PassContracts()
    pipeline = ChangePipeline(w.ctx, w.epochs, contracts=lambda snapshot: fake)
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    assert pipeline.advance(cs.id).state == "verified"
    same = _server(w, "reviewer", "fake-agent@1", pipeline=pipeline)
    _init(same)
    refused = _call(same, "review_submit", {"changeset_id": cs.id, "verdict": "approve", "note": "ok"})[
        "result"
    ]
    assert refused["isError"] is True
    assert json.loads(refused["content"][0]["text"])["code"] == "REVIEW_SAME_RUNTIME"
    other = _server(w, "reviewer", "script@1", pipeline=pipeline)
    _init(other)
    accepted = _call(other, "review_submit", {"changeset_id": cs.id, "verdict": "approve", "note": "ok"})[
        "result"
    ]
    assert accepted["isError"] is False, accepted
    assert any(runtime.endswith("script@1") for runtime in pipeline.approvals(cs.id))


def test_factory_mcp_stdio(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    lines = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    env = {**os.environ, "FACTORY_ACTOR": "fake-agent@1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "mao", "--root", str(w.ctx.root), "mcp", "--role", "foreman"],
        input="".join(json.dumps(item) + "\n" for item in lines),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [item["id"] for item in responses] == [1, 2]
    assert responses[1]["result"]["tools"]


def test_factoryctl_bridge(tmp_path: Path) -> None:
    from mao.ctl import CtlServer, McpCtlHandler

    w = open_world(tmp_path)
    ctl = tmp_path / "ctl"
    (ctl / "requests").mkdir(parents=True)
    (ctl / "responses").mkdir()
    (ctl / "job.json").write_text(
        json.dumps(
            {
                "job_id": "fs-0001",
                "attempt": 1,
                "station": "foreman",
                "epoch": w.base,
                "inputs": {},
                "params": {},
                "budget_usd": 0,
                "timeout_s": 60,
            }
        ),
        encoding="utf-8",
    )
    server = CtlServer(ctl, McpCtlHandler(_server(w, "foreman", "fake-agent@1")), w.clock)
    server.start(interval_s=0.05)
    try:
        lines = [
            {
                "jsonrpc": "2.0",
                "id": "a",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": "b",
                "method": "tools/call",
                "params": {"name": "registry_list", "arguments": {"kind": "Station"}},
            },
        ]
        env = {**os.environ, "FACTORY_CTL": str(ctl), "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [sys.executable, str(FACTORYCTL), "mcp"],
            input="".join(json.dumps(item) + "\n" for item in lines),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
    finally:
        server.stop()
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [item["id"] for item in responses] == ["a", "b"]
    assert "Station:upper@1" in responses[1]["result"]["structuredContent"]["result"]


def test_ctl_handler_rejects_other_ops(tmp_path: Path) -> None:
    from mao.ctl import McpCtlHandler

    w = open_world(tmp_path)
    handler = McpCtlHandler(_server(w, "foreman", "fake-agent@1"))
    reply = handler.handle({"id": "r-00000001", "op": "check", "args": {"port": "note", "path": "x"}})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "CTL_REQUEST_INVALID"
