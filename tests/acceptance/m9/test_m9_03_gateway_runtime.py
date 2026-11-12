"""M9-03 验收：Runner 经 ServiceGateway 执行 form: service 的工位（spec/system/closure.md 5.3）。

假服务是测试进程里的标准库 HTTP 处理器，路径与示例 moneyprinter 不同（PUT /v2/renders、
GET /v2/renders/{task_id}/status、任务号在 /data/ref）。假 factoryctl 只替换 `service` 子命令：
按参数里的方法、路径、URL 转发到假服务；其他子命令交给 LocalProvider 安装的真实 factoryctl。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mao.gateway")

from mao.jobs import JobState  # noqa: E402
from mao.runs import InlineExecutor, LineRunner, NodeState, RunState  # noqa: E402
from tests.acceptance.m9.closure_world import (  # noqa: E402
    bean_text,
    build_registry,
    node_map,
    open_world,
    seal_note,
)

FAKE_CTL = """import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = {base!r}
LOG = Path({log!r})
REAL = Path({root!r}) / "tools" / "factory" / "bin" / "factoryctl"
argv = sys.argv[1:]
if argv[:1] != ["service"]:
    sys.exit(subprocess.call([sys.executable, str(REAL), *argv]))


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def note(entry):
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\\n")


def inside(url):
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "api"):
        raise SystemExit("factoryctl-fake: refusing " + url)
    return BASE + parts.path


def call(method, url, body=None):
    request = Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=10) as resp:
        return resp.read()


op = argv[1] if len(argv) > 1 else ""
note({{"op": op, "argv": argv}})
try:
    if op in ("submit", "poll"):
        body = Path(flag("--body")).read_bytes() if flag("--body") else None
        sys.stdout.write(call(flag("--method"), BASE + flag("--path"), body).decode("utf-8"))
    elif op == "fetch":
        dest = Path(flag("--dest"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(call("GET", inside(flag("--url"))))
    elif op == "openapi":
        out = Path(flag("--out"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(call("GET", inside(flag("--url"))))
    else:
        sys.exit(2)
except HTTPError as exc:
    sys.stderr.write("http %d\\n" % exc.code)
    sys.exit(1)
"""


class Service:
    """假的长任务服务。finish_after 次 poll 之后报告完成；None 表示永不完成。"""

    def __init__(self, *, finish_after: int | None = 1, outputs: list[str] | None = None) -> None:
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.polls = 0
        self.finish_after = finish_after
        self.outputs = outputs
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self) -> None:  # noqa: N802
                service.requests.append(("PUT", self.path))
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                if self.path != "/v2/renders":
                    self._send(404, b"{}")
                    return
                service.bodies.append(json.loads(raw.decode("utf-8")))
                self._send(200, b'{"data": {"ref": "rnd-7"}}')

            def do_POST(self) -> None:  # noqa: N802
                service.requests.append(("POST", self.path))
                self._send(404, b"{}")

            def do_GET(self) -> None:  # noqa: N802
                service.requests.append(("GET", self.path))
                if self.path == "/openapi.json":
                    self._send(200, b'{"openapi": "3.0.3", "paths": {}}')
                elif self.path == "/v2/renders/rnd-7/status":
                    service.polls += 1
                    if service.finish_after is not None and service.polls > service.finish_after:
                        urls = service.outputs or [f"{service.base}/v2/files/rnd-7/final.txt"]
                        self._send(200, json.dumps({"state": "finished", "outputs": urls}).encode("utf-8"))
                    else:
                        self._send(200, b'{"state": "queued"}')
                elif self.path == "/v2/files/rnd-7/final.txt":
                    body = service.bodies[-1]
                    self._send(200, f"RENDERED {body['chars']} {body['lang']}\n".encode())
                else:
                    self._send(404, b"{}")

            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _commands(tmp_path: Path, service: Service, root: Path) -> tuple[dict[str, list[str]], Path]:
    log = tmp_path / "ctl-log.jsonl"
    script = tmp_path / "factoryctl_fake.py"
    script.write_text(FAKE_CTL.format(base=service.base, log=str(log), root=str(root)), encoding="utf-8")
    mapped = [sys.executable, str(script)]
    return {"factoryctl": mapped, "/opt/factory/bin/factoryctl": mapped}, log


def _ops(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [json.loads(line)["op"] for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_render(tmp_path: Path, service: Service):
    registry = build_registry(tmp_path)
    commands, log = _commands(tmp_path, service, tmp_path / "w" / "factory")
    ctx, snapshot, runner, _clock = open_world(tmp_path / "w", registry, commands=commands, gateway=True)
    lines = LineRunner(ctx, snapshot, runner)
    run = lines.start_run("note-render@1", {"note": seal_note(ctx, snapshot, "hello\n")})
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=200, wait_s=0)
    return ctx, runner, lines, done, log


def test_line_runs_service_station(tmp_path: Path) -> None:
    service = Service(finish_after=1)
    try:
        ctx, runner, lines, done, log = _run_render(tmp_path, service)
    finally:
        service.close()
    assert done.state == RunState.SUCCEEDED
    node = node_map(lines, done.id)["render"]
    assert node.state == NodeState.DONE
    assert bean_text(ctx, done.outputs["clip"][0]) == "RENDERED 6 en\n"
    assert service.bodies == [{"chars": "6", "lang": "en"}]
    calls = [item for item in service.requests if item != ("GET", "/openapi.json")]
    assert calls[0] == ("PUT", "/v2/renders")
    assert calls[1:3] == [("GET", "/v2/renders/rnd-7/status")] * 2
    assert calls[3:] == [("GET", "/v2/files/rnd-7/final.txt")]
    submits = [
        json.loads(row["data"])
        for row in ctx.db.query("SELECT data FROM events WHERE kind = 'service.submit' ORDER BY seq")
    ]
    assert submits == [{"job": node.job_id, "task_id": "rnd-7", "method": "PUT", "path": "/v2/renders"}]
    assert runner.get(node.job_id).state == JobState.SEALED


def test_service_timeout_marks_dead(tmp_path: Path) -> None:
    service = Service(finish_after=None)
    try:
        _ctx, runner, lines, done, log = _run_render(tmp_path, service)
    finally:
        service.close()
    assert done.state == RunState.FAILED
    node = node_map(lines, done.id)["render"]
    job = runner.get(node.job_id)
    assert job.state == JobState.DEAD
    assert job.error["code"] == "SERVICE_TIMEOUT"
    assert "fetch" not in _ops(log)
    assert 25 <= service.polls <= 35


def test_fetch_outside_service_rejected(tmp_path: Path) -> None:
    service = Service(finish_after=0, outputs=["http://203.0.113.9/v2/files/rnd-7/final.txt"])
    try:
        _ctx, runner, lines, done, log = _run_render(tmp_path, service)
    finally:
        service.close()
    assert done.state == RunState.FAILED
    job = runner.get(node_map(lines, done.id)["render"].job_id)
    assert job.state == JobState.DEAD
    assert job.error["code"] == "SERVICE_FAILED"
    assert "fetch" not in _ops(log)


def test_cli_job_run_uses_gateway(tmp_path: Path, capsys) -> None:
    from mao.cli import main

    registry = build_registry(tmp_path)
    root = tmp_path / "cli-root"
    service = Service(finish_after=0)
    try:
        commands, _log = _commands(tmp_path, service, root)
        from tests.fakes.fakelib import local_commands

        mapping = local_commands()
        mapping.update(commands)
        command_file = tmp_path / "commands.json"
        command_file.write_text(json.dumps(mapping), encoding="utf-8")
        base = ["--root", str(root), "--registry", str(registry), "--provider", "local"]
        base += ["--local-commands", str(command_file)]
        src = tmp_path / "in" / "note.txt"
        src.parent.mkdir(parents=True)
        src.write_text("hello\n", encoding="utf-8")
        capsys.readouterr()
        assert main([*base, "bean", "import", "--type", "note.text@1", "--file", f"note.txt={src}"]) == 0
        note = capsys.readouterr().out.strip()
        assert main([*base, "--json", "job", "run", "render@1", "--input", f"note={note}"]) == 0
        job = json.loads(capsys.readouterr().out)
    finally:
        service.close()
    assert job["state"] == "SEALED"
    assert ("PUT", "/v2/renders") in service.requests
