"""M17 execution diagnostics: expected contracts against the frozen M15 baseline.

These tests intentionally assert desired behaviour and fail where the baseline is
broken. The service fixture is local; no external provider or paid API is used.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from mao.gateway import openapi_to_tools
from mao.runs import InlineExecutor, LineRunner, RunState
from tests.acceptance.m9.closure_world import build_registry, node_map, open_world, seal_note
from tests.acceptance.m9.test_m9_03_gateway_runtime import Service, _commands


def _run_line(tmp_path: Path, registry: Path, commands: dict[str, list[str]]):
    ctx, snapshot, runner, _clock = open_world(tmp_path / "w", registry, commands=commands, gateway=True)
    lines = LineRunner(ctx, snapshot, runner)
    run = lines.start_run("note-render@1", {"note": seal_note(ctx, snapshot, "hello\n")})
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=200, wait_s=0)
    node = node_map(lines, done.id)["render"]
    return ctx, runner, done, node


def test_e01_real_factoryctl_runs_service_job(tmp_path: Path) -> None:
    """A service station must work through the installed, unmodified factoryctl."""
    registry = build_registry(tmp_path)
    root = tmp_path / "w" / "factory"
    real_ctl = root / "tools" / "factory" / "bin" / "factoryctl"
    commands = {"factoryctl": [sys.executable, str(real_ctl)]}
    service = Service(finish_after=0)
    try:
        from urllib.request import urlopen

        with urlopen(service.base + "/openapi.json", timeout=5) as response:
            assert response.status == 200
        # Point the machine to the actual local HTTP service. The installed
        # factoryctl is the system under test; only the upstream is a fixture.
        machine_path = registry / "machines" / "servicebox" / "machine.yaml"
        machine = yaml.safe_load(machine_path.read_text(encoding="utf-8"))
        machine["spec"]["runtime"]["service"]["port"] = service.server.server_address[1]
        machine_path.write_text(yaml.safe_dump(machine, sort_keys=False), encoding="utf-8")
        ctx, runner, done, node = _run_line(tmp_path, registry, commands)
        job = runner.get(node.job_id)
        stderr = ctx.root / "tmp" / "gateway" / f"{job.id}.stderr"
        detail = stderr.read_text(encoding="utf-8") if stderr.exists() else "missing stderr"
        assert done.state == RunState.SUCCEEDED, f"job={job.state.value}, error={job.error}, ctl={detail}"
        assert job.outputs and service.bodies == [{"chars": "6", "lang": "en"}]
    finally:
        service.close()


def test_e02_openapi_default_does_not_export_unrelated_operations() -> None:
    """A missing allowlist must fail closed instead of exposing arbitrary POSTs."""
    doc = {
        "openapi": "3.0.3",
        "paths": {
            "/videos": {"post": {"operationId": "submitVideo"}},
            "/tasks/{id}": {"get": {"operationId": "pollVideo"}},
            "/admin/reset": {"post": {"operationId": "wipeEverything"}},
        },
    }
    explicitly_allowed = openapi_to_tools(doc, allow=["submitVideo", "pollVideo"])
    assert {tool.name for tool in explicitly_allowed} == {"submitVideo", "pollVideo"}
    tools = openapi_to_tools(doc, allow=None)
    assert "wipeEverything" not in {tool.name for tool in tools}


def test_e03_agent_operator_runs_for_service_station(tmp_path: Path) -> None:
    """A service station declaring an agent operator must actually start it."""
    registry = build_registry(tmp_path)
    station_path = registry / "stations" / "render.yaml"
    station = yaml.safe_load(station_path.read_text(encoding="utf-8"))
    station["spec"]["operator"] = {"runtime": "fake-agent@1", "model": "fake-model-1", "max_turns": 5}
    station["spec"]["limits"]["budget_usd"] = 1.0
    station_path.write_text(yaml.safe_dump(station, sort_keys=False), encoding="utf-8")
    marker = tmp_path / "operator-started"
    agent = tmp_path / "record_agent.py"
    agent.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n"
        'print(\'{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0}\')\n',
        encoding="utf-8",
    )
    service = Service(finish_after=0)
    try:
        commands, _ = _commands(tmp_path, service, tmp_path / "w" / "factory")
        commands["/opt/operator/bin/fake-agent"] = [sys.executable, str(agent)]
        _ctx, runner, done, node = _run_line(tmp_path, registry, commands)
        job = runner.get(node.job_id)
        assert done.state == RunState.SUCCEEDED, f"job={job.state.value}, error={job.error}"
        assert marker.exists(), "service job was sealed without starting its declared agent operator"
    finally:
        service.close()


def test_e04_model_proxy_restart_keeps_job_budget(tmp_path: Path) -> None:
    """Restarting the proxy must not erase cost already recorded for a job."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mao.base import FakeClock
    from mao.egress import ModelProxy, register_job_cap
    from tests.acceptance.m2.test_m2_07_egress import _post_model, _running_job, _store
    from tests.acceptance.m2.world import EXAMPLE, open_factory

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.dumps({"cost_usd": 0.6}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    root = ctx.root
    (root / "egress").mkdir()
    (root / "egress" / "prices.json").write_text(
        json.dumps({"meter-model": {"input_per_mtok": 1.0, "output_per_mtok": 1.0}}), encoding="utf-8"
    )
    (root / "egress" / "upstreams.json").write_text(
        json.dumps(
            {
                "meter-model": {
                    "url": f"http://127.0.0.1:{upstream.server_address[1]}",
                    "secret": "TOOL_TOKEN",
                    "header": "x-api-key",
                }
            }
        ),
        encoding="utf-8",
    )
    _running_job(ctx, snapshot, "j-restart")
    register_job_cap(ctx.db, "j-restart", snapshot.key("Station", "explainer@3"), 1.0)
    store = _store(tmp_path)
    proxy = ModelProxy(store, FakeClock(), root)
    restarted = None
    try:
        port = proxy.start(port=0)
        assert _post_model(port, "j-restart", {"model": "meter-model"})[0] == 200
        proxy.close()
        restarted = ModelProxy(store, FakeClock(), root)
        port = restarted.start(port=0)
        assert _post_model(port, "j-restart", {"model": "meter-model"})[0] == 200
        rows = ctx.db.query("SELECT SUM(cost_usd) AS total FROM model_usage WHERE job_id = ?", ("j-restart",))
        assert abs(rows[0]["total"] - 1.2) < 0.000001
        stops = ctx.db.query("SELECT code FROM budget_stops WHERE job_id = ?", ("j-restart",))
        third_status, _body = _post_model(port, "j-restart", {"model": "meter-model"})
        assert third_status == 402, (
            f"recorded total after two calls={rows[0]['total']} > cap=1.0, "
            f"stops={stops}, but third request returned {third_status}"
        )
    finally:
        if restarted is not None:
            restarted.close()
        else:
            proxy.close()
        upstream.shutdown()
        upstream.server_close()


def test_e05_fetch_retry_reuses_known_external_task(tmp_path: Path) -> None:
    """A temporary fetch failure should resume the known task, not buy it again.

    This is an M17 design target: the existing M9 contract does not yet promise
    cross-attempt external task resume. The fake upstream counts submissions.
    """
    registry = build_registry(tmp_path)
    station_path = registry / "stations" / "render.yaml"
    station = yaml.safe_load(station_path.read_text(encoding="utf-8"))
    station["spec"]["limits"]["retries"] = {"rejected": 0, "failed": 1, "mode": "resume"}
    station_path.write_text(yaml.safe_dump(station, sort_keys=False), encoding="utf-8")
    service = Service(finish_after=0)
    try:
        commands, _ = _commands(tmp_path, service, tmp_path / "w" / "factory")
        fake_ctl = tmp_path / "factoryctl_fake.py"
        text = fake_ctl.read_text(encoding="utf-8")
        needle = '    elif op == "fetch":\n        dest = Path(flag("--dest"))\n'
        fail_once = tmp_path / "fetch-failed-once"
        replacement = (
            '    elif op == "fetch":\n'
            f"        failure = Path({str(fail_once)!r})\n"
            "        if not failure.exists():\n"
            '            failure.write_text("failed", encoding="utf-8")\n'
            "            sys.exit(1)\n"
            '        dest = Path(flag("--dest"))\n'
        )
        assert text.count(needle) == 1
        fake_ctl.write_text(text.replace(needle, replacement), encoding="utf-8")
        ctx, runner, done, node = _run_line(tmp_path, registry, commands)
        job = runner.get(node.job_id)
        assert done.state == RunState.SUCCEEDED, f"job={job.state.value}, error={job.error}"
        assert fail_once.exists(), "the transient fetch failure was not exercised"
        rows = ctx.db.query("SELECT data FROM events WHERE job_id = ? AND kind = 'service.submit'", (job.id,))
        assert len(service.bodies) == 1, (
            f"one external task completed but a transient fetch failure caused {len(service.bodies)} submissions "
            f"and {len(rows)} service.submit events"
        )
    finally:
        service.close()


def test_e06_factoryctl_budget_reports_live_balance(tmp_path: Path) -> None:
    """The real operator command must report the current budget file."""
    import json
    import os
    import subprocess

    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / "job.json").write_text(
        json.dumps({"job_id": "j-budget", "attempt": 1, "inputs": {}, "budget_usd": 1.0}), encoding="utf-8"
    )
    (ctl / "budget.json").write_text(
        json.dumps({"cap_usd": 1.0, "spent_usd": 0.6, "remaining_usd": 0.4}), encoding="utf-8"
    )
    factoryctl = Path(__file__).resolve().parents[4] / "src" / "mao" / "ctl" / "factoryctl.py"
    proc = subprocess.run(
        [sys.executable, str(factoryctl), "budget", "--json"],
        env={**os.environ, "FACTORY_CTL": str(ctl)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    assert observed.get("spent_usd") == 0.6, f"budget.json has spent_usd=0.6 but command returned {observed}"
    assert observed.get("remaining_usd") == 0.4
