"""M4-03 验收：服务网关（spec/system/gateway.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.gateway")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.gateway import ServiceGateway, mcp_manifest, openapi_to_tools, parse_long_task  # noqa: E402

DOC = {
    "openapi": "3.0.3",
    "paths": {
        "/videos": {"post": {"operationId": "submitVideo", "responses": {"200": {"description": "ok"}}}},
        "/tasks/{id}": {"get": {"operationId": "pollVideo", "responses": {"200": {"description": "ok"}}}},
        "/videos/{id}": {
            "delete": {"operationId": "deleteVideo", "responses": {"204": {"description": "ok"}}}
        },
    },
}


def test_m2_still_unsupported_without_gateway(tmp_path: Path) -> None:
    from mao.runner import Runner
    from tests.acceptance.m2.world import FIXTURE, open_factory

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    assert not isinstance(runner, ServiceGateway)
    with pytest.raises(FactoryError) as ei:
        Runner(ctx, snapshot).run(
            Runner(ctx, snapshot).submit(snapshot.key("Station", "service-source@1"), {}, {}).id
        )
    assert ei.value.code == "FORM_UNSUPPORTED"


def test_incomplete_long_task() -> None:
    with pytest.raises(FactoryError) as ei:
        parse_long_task({"submit": "POST /videos", "poll": "GET /tasks/{id}"})
    assert ei.value.code == "LONG_TASK_INVALID"


def test_submit_poll_fetch_order() -> None:
    task = parse_long_task(
        {
            "submit": "POST /videos",
            "poll": "GET /tasks/{id}",
            "task_id": "/id",
            "done": 'response.status == "ok"',
            "fetch": "response.files",
            "poll_every": "1s",
        }
    )
    assert task.submit == "POST /videos"
    assert task.poll == "GET /tasks/{id}"
    assert task.fetch == "response.files"


def test_done_expression() -> None:
    task = parse_long_task(
        {
            "submit": "POST /videos",
            "poll": "GET /tasks/{id}",
            "task_id": "/id",
            "done": 'response.status == "ok"',
            "fetch": "response.files",
            "poll_every": "1s",
        }
    )
    assert task.done == 'response.status == "ok"'
    assert task.poll_every == "1s"


def _patch_moneyprinter(registry: Path, poll_every: str) -> None:
    machine = registry / "machines" / "moneyprinter" / "machine.yaml"
    text = machine.read_text(encoding="utf-8")
    text = text.replace('reset: "rm -rf /app/storage/tasks/*"', "reset: [record-reset]")
    old = '    long_task:\n      submit: "POST /api/v1/videos"\n      poll:   "GET /api/v1/tasks/{task_id}"\n'
    new = (
        "    long_task:\n"
        '      submit: "POST /api/v1/videos"\n'
        '      poll: "GET /api/v1/tasks/{task_id}"\n'
        '      task_id: "/id"\n'
        "      done: 'response.status == \"ok\"'\n"
        "      fetch: response.files\n"
        f'      poll_every: "{poll_every}"\n'
    )
    if old not in text:
        raise AssertionError("moneyprinter long_task block missing")
    text = text.replace(old, new, 1)
    text = text.replace(
        "{{ inputs.brief.meta.topic }}",
        "acceptance",
    )
    text = text.replace("{{ inputs.brief.meta.aspect }}", "9:16")
    text = text.replace("{{ inputs.brief.meta.language }}", "en")
    machine.write_text(text, encoding="utf-8", newline="\n")


def _service_world(tmp_path: Path, *, poll_status: str, poll_every: str):
    import shutil
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.sandbox import LocalProvider, Mount, SandboxSpec
    from mao.snapshot import Snapshot
    from tests.acceptance.m2.world import EXAMPLE, seal_input
    from tests.fakes.fakelib import local_commands

    registry = tmp_path / "registry"
    shutil.copytree(EXAMPLE, registry)
    _patch_moneyprinter(registry, poll_every)
    log = tmp_path / "exec-log.txt"
    port_file = tmp_path / "port.txt"

    class Product(BaseHTTPRequestHandler):
        def _send(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/openapi"):
                self._send(b'{"openapi":"3.0.3","paths":{}}')
            elif self.path.startswith("/clip"):
                self._send(b"clip-bytes")
            else:
                files = f'["http://127.0.0.1:{port_file.read_text(encoding="utf-8")}/clip.bin"]'
                self._send(json.dumps({"status": poll_status, "files": json.loads(files)}).encode())

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._send(b'{"id":"task-acceptance"}')

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Product)
    port_file.write_text(str(server.server_address[1]), encoding="utf-8")
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctl = tmp_path / "factoryctl-service.py"
    reset = tmp_path / "record-reset.py"
    ctl.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from urllib.request import urlopen\n"
        f"LOG = Path({str(log)!r})\n"
        f"PORT = {server.server_address[1]}\n"
        "def note(line):\n"
        "    with LOG.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(line + '\\n')\n"
        "argv = sys.argv[1:]\n"
        "note('argv ' + json.dumps(argv))\n"
        "def flag(name):\n"
        "    if name not in argv:\n"
        "        return ''\n"
        "    return argv[argv.index(name) + 1]\n"
        "if 'openapi' in argv:\n"
        "    note('service openapi')\n"
        "    with urlopen(f'http://127.0.0.1:{PORT}/openapi.json', timeout=5) as resp:\n"
        "        Path(flag('--out')).write_bytes(resp.read())\n"
        "elif 'submit' in argv:\n"
        "    note('service submit')\n"
        "    body = Path(flag('--body')).read_bytes() if flag('--body') else b'{}'\n"
        "    from urllib.request import Request\n"
        "    req = Request(f'http://127.0.0.1:{PORT}/videos', data=body, method='POST')\n"
        "    with urlopen(req, timeout=5) as resp:\n"
        "        sys.stdout.write(resp.read().decode())\n"
        "elif 'poll' in argv:\n"
        "    note('service poll')\n"
        "    with urlopen(f'http://127.0.0.1:{PORT}/tasks/task-acceptance', timeout=5) as resp:\n"
        "        sys.stdout.write(resp.read().decode())\n"
        "elif 'fetch' in argv:\n"
        "    dest = flag('--dest')\n"
        "    note('service fetch')\n"
        "    note('fetch-dest ' + dest)\n"
        "    with urlopen(f'http://127.0.0.1:{PORT}/clip.bin', timeout=5) as resp:\n"
        "        data = resp.read()\n"
        "    target = Path(dest)\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        "    target.write_bytes(data)\n"
        "    canary = target.parent / 'job1-output-canary'\n"
        "    if not canary.exists():\n"
        "        canary.write_bytes(b'canary')\n"
        "        note('canary ' + str(canary))\n"
        "else:\n"
        "    note('service other')\n"
        "    sys.stdout.write('{}')\n",
        encoding="utf-8",
    )
    reset.write_text(
        "from pathlib import Path\n"
        f"LOG = Path({str(log)!r})\n"
        f"ROOT = Path({str(tmp_path)!r})\n"
        "with LOG.open('a', encoding='utf-8') as fh:\n"
        "    fh.write('reset\\n')\n"
        "for path in ROOT.rglob('job1-output-canary'):\n"
        "    path.unlink()\n",
        encoding="utf-8",
    )
    root = tmp_path / "factory"
    root.mkdir()
    from datetime import UTC, datetime

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    commands = local_commands()
    mapped = [sys.executable, str(ctl)]
    commands["factoryctl"] = mapped
    commands["/opt/factory/bin/factoryctl"] = mapped
    commands["record-reset"] = [sys.executable, str(reset)]
    provider = LocalProvider(root, commands=commands, clock=clock)
    ctx = Context.open(root, provider=provider, clock=clock, ids=CounterIds())
    snapshot = Snapshot.load(registry)
    gateway = ServiceGateway(ctx, snapshot)
    sandbox = provider.open(
        SandboxSpec(
            name="pooled-acceptance",
            image="registry.local/machines/moneyprinter@sha256:" + "a" * 64,
            mounts=(Mount(source=str(root), target="/factory", readonly=False),),
        )
    )
    brief = seal_input(ctx, snapshot, "brief.video@1")
    return ctx, snapshot, gateway, sandbox, server, log, {"brief": brief}


def test_timeout_uses_station_limit(tmp_path: Path) -> None:
    _ctx, snapshot, gateway, sandbox, server, _log, inputs = _service_world(
        tmp_path, poll_status="run", poll_every="31m"
    )
    limits = snapshot.spec(snapshot.key("Station", "quick-short@1"))["limits"]
    assert limits["timeout"] == "30m"
    try:
        with pytest.raises(FactoryError) as ei:
            gateway.run_recipe(
                station_key=snapshot.key("Station", "quick-short@1"),
                job_id="j-timeout",
                attempt=1,
                sandbox=sandbox,
                inputs=inputs,
                params={},
                timeout_s=1800,
            )
        assert ei.value.code == "SERVICE_TIMEOUT"
    finally:
        server.shutdown()


def test_fetch_rejects_public_host() -> None:
    task = parse_long_task(
        {
            "submit": "POST /videos",
            "poll": "GET /tasks/{id}",
            "task_id": "/id",
            "done": "true",
            "fetch": '["https://example.com/a"]',
            "poll_every": "1s",
        }
    )
    assert "example.com" in task.fetch


def test_openapi_allowlist() -> None:
    tools = openapi_to_tools(DOC, allow=[])
    names = {tool.name for tool in tools}
    assert names
    assert "deleteVideo" not in names
    assert names <= {"submitVideo", "pollVideo"}


def test_named_tool_missing() -> None:
    with pytest.raises(FactoryError) as ei:
        openapi_to_tools(DOC, allow=["missingTool"])
    assert ei.value.code == "OPENAPI_UNSUPPORTED"


def test_mcp_manifest_pure() -> None:
    tools = openapi_to_tools(DOC, allow=None)
    first = mcp_manifest(tools, fmt="claude-json")
    second = mcp_manifest(tools, fmt="claude-json")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_no_host_port_flag() -> None:
    from mao.sandbox import DockerProvider, SandboxSpec

    argv = DockerProvider(Path("."), allow_unproxied_egress=False).build_run_argv(
        SandboxSpec(
            name="mao-j-0001-1",
            image="registry.local/machines/ffmpeg@sha256:" + "a" * 64,
            mounts=(),
            network="none",
        )
    )
    assert "-p" not in argv
    assert "host" not in argv


def test_pooled_reset_between_jobs(tmp_path: Path) -> None:
    _ctx, snapshot, gateway, sandbox, server, log, inputs = _service_world(
        tmp_path, poll_status="ok", poll_every="1s"
    )
    station = snapshot.key("Station", "quick-short@1")
    try:
        first = gateway.run_recipe(
            station_key=station,
            job_id="j-pool-1",
            attempt=1,
            sandbox=sandbox,
            inputs=inputs,
            params={},
            timeout_s=1800,
        )
        second = gateway.run_recipe(
            station_key=station,
            job_id="j-pool-2",
            attempt=1,
            sandbox=sandbox,
            inputs=inputs,
            params={},
            timeout_s=1800,
        )
    finally:
        server.shutdown()
    assert first["task_id"]
    assert first["fetches"]
    assert second["task_id"]
    assert second["fetches"]
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    reset_at = [index for index, line in enumerate(lines) if line == "reset"]
    assert reset_at
    last_reset = reset_at[-1]
    assert any(line.startswith("service ") for line in lines[:last_reset])
    assert any(line.startswith("service ") for line in lines[last_reset + 1 :])
    first_dests = [line.split(" ", 1)[1] for line in lines[:last_reset] if line.startswith("fetch-dest ")]
    assert first_dests
    later_argv = "\n".join(line for line in lines[last_reset + 1 :] if line.startswith("argv "))
    for dest in first_dests:
        assert dest not in later_argv
        assert all(dest not in item for item in second["fetches"])
