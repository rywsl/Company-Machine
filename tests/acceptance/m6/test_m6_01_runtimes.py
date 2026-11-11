"""M6-01 验收：真实 CLI 的接入与模型代理的生产路径（spec/system/runtimes.md 第 7 章）。"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("mao.operator")
pytest.importorskip("mao.egress")

from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402
from tests.fakes.fake_llm import FakeLlm  # noqa: E402
from tests.fakes.fakelib import REPO, render_registry  # noqa: E402

RUNTIMES = REPO / "spec" / "runtimes"
TRANSCRIPTS = REPO / "tests" / "fixtures" / "transcripts"
SECRET = "MODEL-SECRET-VALUE-9876543210"
LLM_SCRIPT = {"usage": {"input_tokens": 100, "output_tokens": 50}, "turns": [], "fallback": "ok"}


def _snapshot(tmp_path: Path, *, env: dict[str, str] | None = None, files: dict[str, str] | None = None):
    from mao.snapshot import Snapshot

    patches = [("runtimes/fake-agent.yaml", f"spec.env.{name}", value) for name, value in (env or {}).items()]
    return Snapshot.load(render_registry(tmp_path / "registry", patches=patches, files=files))


def _brief():
    from mao.operator import Brief

    return Brief("T", "C", "")


def _proxy_world(tmp_path: Path):
    from mao.base import FakeClock
    from mao.egress import ModelProxy, SecretStore, register_job_cap

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    script = tmp_path / "llm.json"
    script.write_text(json.dumps(LLM_SCRIPT), encoding="utf-8")
    llm = FakeLlm(script)
    llm_port = llm.start()
    egress = ctx.root / "egress"
    egress.mkdir(exist_ok=True)
    (egress / "prices.json").write_text(
        json.dumps(
            {
                "fake-opus": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
                "fake-codex": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
            }
        ),
        encoding="utf-8",
    )
    (egress / "upstreams.json").write_text(
        json.dumps(
            {
                "fake-opus": {
                    "url": f"http://127.0.0.1:{llm_port}",
                    "secret": "MODEL_TOKEN",
                    "header": "x-api-key",
                },
                "fake-codex": {
                    "url": f"http://127.0.0.1:{llm_port}/v1",
                    "secret": "MODEL_TOKEN",
                    "header": "x-api-key",
                },
                "*": {"url": f"http://127.0.0.1:{llm_port}", "secret": "MODEL_TOKEN", "header": "x-api-key"},
            }
        ),
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets-root"
    (secrets / "secrets" / "values").mkdir(parents=True)
    (secrets / "secrets" / "manifest.json").write_text('{"MODEL_TOKEN": "model"}', encoding="utf-8")
    (secrets / "secrets" / "values" / "MODEL_TOKEN").write_text(SECRET, encoding="utf-8")
    station = snapshot.key("Station", "writer@1")
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES ('j-meter', ?, ?, 'RUNNING', 1, '{}', '{}', '{}', ?, ?)",
            (station, snapshot.epoch, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )
    register_job_cap(ctx.db, "j-meter", station, 1.0)
    proxy = ModelProxy(SecretStore(secrets), FakeClock(), ctx.root)
    port = proxy.start(port=0)
    return ctx, llm, proxy, port


def _post(port: int, path: str, body: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "X-Factory-Job": "j-meter",
            "x-api-key": "factory-proxy",
            "Authorization": "Bearer factory-proxy",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_control_vars_kept_until_expanded(tmp_path: Path) -> None:
    from mao.operator import build_invocation, expand_control_vars

    snapshot = _snapshot(
        tmp_path / "a", env={"FAKE_AGENT_JOB": "${FACTORY_JOB_ID}", "FAKE_AGENT_PROXY": "${MODEL_PROXY}"}
    )
    inv = build_invocation(snapshot, "writer@1", _brief(), inputs={}, params={})
    assert inv.env["FAKE_AGENT_JOB"] == "${FACTORY_JOB_ID}"
    expanded = expand_control_vars(inv, {"FACTORY_JOB_ID": "j-0007", "MODEL_PROXY": "http://127.0.0.1:9"})
    assert expanded.env["FAKE_AGENT_JOB"] == "j-0007"
    assert expanded.env["FAKE_AGENT_PROXY"] == "http://127.0.0.1:9"
    assert expanded.argv == inv.argv
    with pytest.raises(FactoryError) as ei:
        expand_control_vars(inv, {"FACTORY_JOB_ID": "j-0007"})
    assert ei.value.code == "EGRESS_PROXY_UNAVAILABLE"
    assert ei.value.detail["var"] == "MODEL_PROXY"
    bad = _snapshot(tmp_path / "b", env={"FAKE_AGENT_HOME": "${HOME}"})
    with pytest.raises(FactoryError) as ei:
        build_invocation(bad, "writer@1", _brief(), inputs={}, params={})
    assert ei.value.code == "INVOCATION_INVALID"
    assert ei.value.detail["var"] == "HOME"


def test_reference_runtimes_compile(tmp_path: Path) -> None:
    from mao.operator import build_runtime_invocation

    files = {
        f"runtimes/{path.name}": path.read_text(encoding="utf-8") for path in sorted(RUNTIMES.glob("*.yaml"))
    }
    assert sorted(files) == ["runtimes/claude-code.yaml", "runtimes/codex.yaml"]
    snapshot = _snapshot(tmp_path, files=files)
    claude = build_runtime_invocation(
        snapshot,
        "claude-code@1",
        task="T",
        contract="C",
        model="claude-opus-5-5",
        max_turns=3,
        budget_usd=2.0,
        timeout_s=600,
    )
    assert claude.argv[:3] == ("/opt/operator/bin/claude", "-p", "T")
    assert claude.argv[claude.argv.index("--output-format") + 1] == "stream-json"
    assert claude.argv[claude.argv.index("--append-system-prompt") + 1] == "C"
    assert claude.transcript == "stream-json"
    assert claude.env["ANTHROPIC_BASE_URL"] == "${MODEL_PROXY}"
    assert claude.env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Factory-Job: ${FACTORY_JOB_ID}"
    assert snapshot.spec(snapshot.key("OperatorRuntime", "claude-code@1"))["invoke"]["mcp"] == {
        "format": "claude-json",
        "flag": "--mcp-config",
    }
    codex = build_runtime_invocation(
        snapshot,
        "codex@1",
        task="T",
        contract="C",
        model="gpt-5-codex",
        max_turns=3,
        budget_usd=2.0,
        timeout_s=600,
    )
    assert codex.argv[:3] == ("/opt/operator/bin/codex", "exec", "--json")
    assert codex.argv[-1] == "C\n\nT"
    assert codex.transcript == "codex-jsonl"
    assert any("${MODEL_PROXY}/v1" in item for item in codex.argv)
    assert any("${FACTORY_JOB_ID}" in item for item in codex.argv)
    assert snapshot.spec(snapshot.key("OperatorRuntime", "codex@1"))["invoke"]["mcp"] == {
        "format": "codex-toml"
    }


def test_parse_claude_stream() -> None:
    from mao.operator import parse_transcript

    summary = parse_transcript(TRANSCRIPTS / "claude-stream.jsonl", "stream-json")
    assert summary.turns == 3
    assert summary.cost_usd == 0.012346
    assert summary.result == "工作区是干净的。"
    assert summary.is_error is False
    assert (summary.events, summary.malformed) == (5, 0)


def test_parse_codex_jsonl() -> None:
    from mao.operator import parse_transcript

    ok = parse_transcript(TRANSCRIPTS / "codex-exec.jsonl", "codex-jsonl")
    assert ok.turns == 2
    assert ok.result == "已复核：approve"
    assert ok.cost_usd == 0.0
    assert ok.is_error is False
    assert (ok.events, ok.malformed) == (9, 0)
    failed = parse_transcript(TRANSCRIPTS / "codex-failed.jsonl", "codex-jsonl")
    assert failed.is_error is True
    assert failed.turns == 0
    assert (failed.events, failed.malformed) == (4, 1)


def test_meter_anthropic_stream(tmp_path: Path) -> None:
    ctx, llm, proxy, port = _proxy_world(tmp_path)
    try:
        status, body = _post(
            port,
            "/v1/messages",
            {"model": "fake-opus", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert status == 200, body
        assert b"message_stop" in body
        assert proxy.spent("j-meter") == 0.00105
        seen = llm.requests[-1]
        assert seen["path"] == "/v1/messages"
        assert seen["headers"]["x-api-key"] == SECRET
        assert "x-factory-job" not in seen["headers"]
        assert "authorization" not in seen["headers"]
    finally:
        proxy.close()
        llm.close()


def test_meter_openai_stream(tmp_path: Path) -> None:
    ctx, llm, proxy, port = _proxy_world(tmp_path)
    try:
        status, body = _post(
            port,
            "/v1/responses",
            {
                "model": "fake-codex",
                "stream": True,
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
                ],
            },
        )
        assert status == 200, body
        assert b"response.completed" in body
        assert proxy.spent("j-meter") == 0.0006
        assert llm.requests[-1]["path"] == "/v1/responses"
    finally:
        proxy.close()
        llm.close()


def test_count_tokens_not_metered(tmp_path: Path) -> None:
    ctx, llm, proxy, port = _proxy_world(tmp_path)
    try:
        status, body = _post(
            port,
            "/v1/messages/count_tokens",
            {"model": "fake-opus", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert status == 200, body
        assert json.loads(body) == {"input_tokens": 10}
        assert ctx.db.query("SELECT COUNT(*) AS n FROM model_usage WHERE job_id = 'j-meter'")[0]["n"] == 0
        assert ctx.db.query("SELECT COUNT(*) AS n FROM budget_stops WHERE job_id = 'j-meter'")[0]["n"] == 0
        assert proxy.spent("j-meter") == 0.0
    finally:
        proxy.close()
        llm.close()


def test_discover_proxies(tmp_path: Path) -> None:
    from mao.durable import LEASE_TTL_S, LeaseStore
    from mao.egress import ProxyEndpoints, discover_proxies

    ctx, _snapshot, _runner, clock = open_factory(tmp_path, FIXTURE)

    def listen() -> tuple[socket.socket, int]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        port = sock.getsockname()[1]
        (ctx.root / "serve.json").write_text(
            json.dumps({"proxies": {"host": "127.0.0.1", "egress_port": port, "model_port": port}}),
            encoding="utf-8",
        )
        return sock, port

    first, port = listen()
    assert discover_proxies(ctx) is None
    assert LeaseStore(ctx).acquire("serve", owner=ctx.owner)
    assert discover_proxies(ctx) == ProxyEndpoints(host="127.0.0.1", egress_port=port, model_port=port)
    first.close()
    assert discover_proxies(ctx) is None
    second, _port = listen()
    try:
        assert discover_proxies(ctx) is not None
        clock.advance(LEASE_TTL_S + 1)
        assert discover_proxies(ctx) is None
    finally:
        second.close()


def test_agent_station_reaches_model_proxy_only(tmp_path: Path) -> None:
    from mao.egress import SecretStore, build_egress

    secrets = tmp_path / "secrets-root"
    (secrets / "secrets" / "values").mkdir(parents=True)
    (secrets / "secrets" / "manifest.json").write_text('{"MODEL_TOKEN": "model"}', encoding="utf-8")
    (secrets / "secrets" / "values" / "MODEL_TOKEN").write_text(SECRET, encoding="utf-8")
    store = SecretStore(secrets)
    plain = _snapshot(tmp_path / "plain")
    assert build_egress(plain, plain.key("Station", "writer@1"), store).network == "none"
    proxied = _snapshot(tmp_path / "proxied", env={"FAKE_AGENT_BASE": "${MODEL_PROXY}"})
    agent = build_egress(proxied, proxied.key("Station", "writer@1"), store)
    assert agent.network == "egress"
    assert tuple(agent.allow) == ()
    assert SECRET not in agent.env.values()
    assert tuple(agent.secret_names) == ()
    script = build_egress(proxied, proxied.key("Station", "upper@1"), store)
    assert script.network == "none"


def test_proxy_bind_host() -> None:
    from mao.egress import proxy_bind_host

    assert proxy_bind_host("Docker Desktop", None) == "127.0.0.1"
    assert proxy_bind_host("Docker Desktop", "172.18.0.1") == "127.0.0.1"
    assert proxy_bind_host("Ubuntu 24.04.1 LTS", "172.18.0.1") == "172.18.0.1"
    with pytest.raises(FactoryError) as ei:
        proxy_bind_host("Ubuntu 24.04.1 LTS", None)
    assert ei.value.code == "EGRESS_PROXY_UNAVAILABLE"
    for name in ("Docker Desktop", "Ubuntu", "Alpine Linux v3.20"):
        assert proxy_bind_host(name, "10.0.0.1") != "0.0.0.0"
