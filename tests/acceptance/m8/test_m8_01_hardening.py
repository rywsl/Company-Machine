"""M8-01 验收：M5–M7 审查后的纠偏（spec/system/hardening.md 第 7 章）。

出网测试经代理真实收发字节；值守与守护进程测试在线程里运行真实循环。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mao.supervisor")
pytest.importorskip("mao.daemon")

from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402
from tests.acceptance.m5.auto_world import auto_world, events, pass_pipeline, supervisor, write_script  # noqa: E402

NOW = "2026-01-01T00:00:00.000Z"
SCHEDULER = {
    "pools": {"cpu": None, "memory_mb": None, "gpu": None},
    "compartments": [],
    "breakers": [],
    "wip": [],
    "rates": [],
    "run_cap": None,
    "toxic_failures": None,
    "tick_s": None,
}


# ---------------------------------------------------------------- 出网


class Upstream:
    """本机明文 HTTP 上游，记录收到的请求。"""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                hits.append((self.command, self.path))
                body = b"upstream-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _reply

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _plan(methods=("GET",)):
    from mao.egress import AllowRule, EgressPlan

    rule = AllowRule(tier="web-read", host="127.0.0.1", methods=frozenset(methods))
    return EgressPlan("egress", (rule,), (), (), {})


def _job(ctx, job_id: str, state: str) -> None:
    ctx.db.query(
        "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
        "VALUES (?, 'Station:upper@1', 'x', ?, 1, '{}', '{}', '{}', ?, ?)",
        (job_id, state, NOW, NOW),
    )


def _via_proxy(port: int, method: str, target: str, job: str | None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": target.split("/")[2] if "://" in target else target}
    if job is not None:
        headers["X-Factory-Job"] = job
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _proxy(ctx):
    from mao.egress import EgressProxy, SecretStore, plans_from_db

    proxy = EgressProxy(plans_from_db(ctx.db), SecretStore(ctx.root), ctx.clock, db=ctx.db)
    return proxy, proxy.start("127.0.0.1", 0)


def test_egress_forwards_allowed(tmp_path: Path) -> None:
    from mao.egress import register_job_egress

    ctx, _snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    _job(ctx, "j-run", "RUNNING")
    register_job_egress(ctx.db, "j-run", _plan())
    up = Upstream()
    proxy, port = _proxy(ctx)
    try:
        status, body = _via_proxy(port, "GET", f"http://127.0.0.1:{up.port}/data?token=abc123", "j-run")
        assert (status, body) == (200, b"upstream-ok")
        assert up.hits == [("GET", "/data?token=abc123")]
        rows = ctx.db.query("SELECT data FROM events WHERE kind = 'egress.allowed'")
        assert [json.loads(r["data"]) for r in rows] == [
            {"job_id": "j-run", "method": "GET", "host": "127.0.0.1"}
        ]
    finally:
        proxy.close()
        up.close()


def test_egress_denials(tmp_path: Path) -> None:
    from mao.egress import register_job_egress

    ctx, _snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    _job(ctx, "j-run", "RUNNING")
    _job(ctx, "j-done", "SEALED")
    register_job_egress(ctx.db, "j-run", _plan())
    register_job_egress(ctx.db, "j-done", _plan())
    up = Upstream()
    proxy, port = _proxy(ctx)
    url = f"http://127.0.0.1:{up.port}/x"
    try:
        status, body = _via_proxy(port, "POST", url, "j-run", body=b"payload")
        assert (status, json.loads(body)) == (403, {"code": "EGRESS_DENIED"})
        status, body = _via_proxy(port, "GET", "http://denied.example.invalid/secret-path", "j-run")
        assert (status, json.loads(body)) == (403, {"code": "EGRESS_DENIED"})
        assert b"denied.example.invalid" not in body and b"secret-path" not in body
        status, body = _via_proxy(port, "GET", url, None)
        # M11（capabilities.md 5.1）：不带任何身份的请求回 407，要求代理认证
        assert (status, json.loads(body)) == (407, {"code": "EGRESS_AUTH_REQUIRED"})
        status, body = _via_proxy(port, "GET", url, "j-done")
        assert (status, json.loads(body)) == (403, {"code": "EGRESS_JOB_INVALID"})
        status, body = _via_proxy(port, "CONNECT", "127.0.0.1:443", "j-run")
        assert (status, json.loads(body)) == (403, {"code": "EGRESS_METHOD_OPAQUE"})
        assert up.hits == []
        denied = [
            json.loads(r["data"])
            for r in ctx.db.query("SELECT data FROM events WHERE kind = 'egress.denied'")
        ]
        assert {"job_id": "j-run", "method": "POST", "host": "127.0.0.1", "code": "EGRESS_DENIED"} in denied
        assert "secret-path" not in json.dumps(denied)
    finally:
        proxy.close()
        up.close()


def test_owned_proxy_reads_registered_plan(tmp_path: Path) -> None:
    from mao.egress import register_job_egress, start_owned_proxies

    ctx, _snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    _job(ctx, "j-run", "RUNNING")
    _job(ctx, "j-unregistered", "RUNNING")
    register_job_egress(ctx.db, "j-run", _plan())
    up = Upstream()
    egress, model = start_owned_proxies(ctx, model_port=0, timeout_s=10)
    try:
        port = ctx.proxies.egress_port
        url = f"http://127.0.0.1:{up.port}/ok"
        assert _via_proxy(port, "GET", url, "j-run") == (200, b"upstream-ok")
        status, body = _via_proxy(port, "GET", url, "j-unregistered")
        assert (status, json.loads(body)) == (403, {"code": "EGRESS_DENIED"})
    finally:
        egress.close()
        model.close()
        up.close()


def test_open_context_discovers_serve_proxies(tmp_path: Path) -> None:
    from mao.cli import build_parser
    from mao.commands.common import open_context
    from mao.context import Context
    from mao.durable import LeaseStore
    from mao.egress import ProxyEndpoints, start_owned_proxies
    from mao.sandbox import LocalProvider

    root = tmp_path / "factory"
    root.mkdir()
    owner_ctx = Context.open(root, provider=LocalProvider(root, commands={}))
    egress, model = start_owned_proxies(owner_ctx, model_port=0, timeout_s=10)
    leases = LeaseStore(owner_ctx)
    try:
        assert leases.acquire("serve", owner="serve-host:1:o-0001", ttl_s=600)
        endpoints = owner_ctx.proxies
        body = {
            "owner": "serve-host:1:o-0001",
            "tick": 1,
            "proxies": {
                "host": endpoints.host,
                "egress_port": endpoints.egress_port,
                "model_port": endpoints.model_port,
            },
        }
        (root / "serve.json").write_text(json.dumps(body), encoding="utf-8")
        args = build_parser().parse_args(["--root", str(root), "--provider", "local", "status"])
        with open_context(args) as ctx:
            assert ctx.proxies == ProxyEndpoints(endpoints.host, endpoints.egress_port, endpoints.model_port)
        assert leases.release("serve", owner="serve-host:1:o-0001")
        with open_context(args) as ctx:
            assert ctx.proxies is None
    finally:
        egress.close()
        model.close()
        owner_ctx.close()


# ---------------------------------------------------------------- 值守与守护进程


def _until(predicate, limit_s: float = 120.0) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _config(**overrides):
    from mao.daemon import ServeConfig

    values = {
        "tick_s": 0.05,
        "max_parallel_jobs": 2,
        "audit_every_s": None,
        "canary_every_s": None,
        "docker_timeout_s": 60.0,
        "supervise": False,
    }
    values.update(overrides)
    return ServeConfig(**values)


def _executor(w):
    from mao.daemon import InlineJobExecutor
    from mao.jobs import load_job
    from mao.runner import Runner

    (w.ctx.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    return InlineJobExecutor(
        lambda job_id: Runner(w.ctx, w.epochs.materialize(load_job(w.ctx.db, job_id).epoch))
    )


def test_supervisor_serve_loop(tmp_path: Path) -> None:
    from mao.durable import LeaseStore
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"default": [{"do": "exit", "code": 0}]})
    w = auto_world(tmp_path)
    queue = WakeQueue(w.ctx)
    first = queue.file("other", "loop-a", {"id": "loop-a"})
    second = queue.file("other", "loop-b", {"id": "loop-b"})
    sup = supervisor(w, pass_pipeline(w))
    stop = threading.Event()
    thread = threading.Thread(target=sup.serve, args=(stop,), kwargs={"idle_s": 0.05}, daemon=True)
    thread.start()
    try:
        assert _until(lambda: {queue.get(first.id).status, queue.get(second.id).status} == {"done"})
    finally:
        stop.set()
        thread.join(60)
    assert not thread.is_alive()
    assert sorted(item.state for item in sup.sessions()) == ["ok", "ok"]
    assert LeaseStore(w.ctx).valid("supervisor") is None


def test_run_serve_hosts_supervisor(tmp_path: Path) -> None:
    from mao.daemon import run_serve
    from mao.durable import LeaseStore
    from mao.foreman import WakeQueue

    write_script(tmp_path, {"default": [{"do": "exit", "code": 0}]})
    w = auto_world(tmp_path)
    wake = WakeQueue(w.ctx).file("other", "hosted", {"id": "hosted"})
    stop = threading.Event()
    thread = threading.Thread(
        target=run_serve,
        args=(w.ctx, _config(supervise=True)),
        kwargs={"executor": _executor(w), "stop": stop, "epochs": w.epochs, "pipeline": pass_pipeline(w)},
        daemon=True,
    )
    thread.start()
    try:
        assert _until(lambda: WakeQueue(w.ctx).get(wake.id).status == "done")
        assert LeaseStore(w.ctx).valid("serve") is not None
    finally:
        stop.set()
        thread.join(120)
    assert not thread.is_alive()
    leases = LeaseStore(w.ctx)
    assert leases.valid("serve") is None and leases.valid("supervisor") is None
    assert events(w.ctx, "serve.started") and events(w.ctx, "serve.stopped")


def test_rejected_proposal_reopens_conversion(tmp_path: Path) -> None:
    from mao.daemon import Daemon
    from mao.foreman import ConversionStore, WakeQueue

    w = auto_world(tmp_path)
    store = ConversionStore(w.ctx)
    request = store.open_or_merge(
        from_type="note.raw@1",
        guarantees=[],
        to_type="note.text@1",
        requires=[],
        found_at={"line": "acceptance"},
    )
    w.ctx.db.query(
        'INSERT INTO changesets (id, base_epoch, source_commit, "commit", state, proposer, document, '
        "activated_at, created_at, updated_at) VALUES ('cs-rej', ?, ?, ?, 'rejected', 'fake-agent@1', '{}', "
        "NULL, ?, ?)",
        (w.base, w.base, "f" * 40, NOW, NOW),
    )
    store.mark(request.id, "in_progress", changeset_id="cs-rej")
    daemon = Daemon(w.ctx, _config(), executor=_executor(w), epochs=w.epochs, pipeline=pass_pipeline(w))
    daemon.start()
    try:
        daemon.tick()
    finally:
        daemon.shutdown(timeout_s=10)
    [again] = [item for item in store.list() if item.id == request.id]
    assert again.status.value == "open"
    assert again.changeset_id is None
    assert ("conversion", request.id) in {(x.signal, x.subject) for x in WakeQueue(w.ctx).list(status="open")}
    assert [e["id"] for e in events(w.ctx, "conversion.reopened")] == [request.id]


class FailingPipeline:
    """治理流水线的 tick 每轮抛出设施错误；其余方法转给真实流水线。"""

    def __init__(self, inner) -> None:
        self.inner = inner

    def tick(self):
        from mao.errors import FactoryError

        raise FactoryError("DOCKER_FAILED", "acceptance: docker 不可用")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_tick_failure_does_not_stop_serve(tmp_path: Path) -> None:
    from mao.daemon import Daemon
    from mao.durable import LeaseStore

    w = auto_world(tmp_path)
    daemon = Daemon(
        w.ctx, _config(), executor=_executor(w), epochs=w.epochs, pipeline=FailingPipeline(pass_pipeline(w))
    )
    stop = threading.Event()
    thread = threading.Thread(target=daemon.serve, args=(stop,), daemon=True)
    thread.start()
    try:
        assert _until(lambda: len(events(w.ctx, "serve.tick_failed")) >= 2, limit_s=60)
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(60)
    assert not thread.is_alive()
    assert {e["code"] for e in events(w.ctx, "serve.tick_failed")} == {"DOCKER_FAILED"}
    assert LeaseStore(w.ctx).valid("serve") is None
    assert events(w.ctx, "serve.stopped")
