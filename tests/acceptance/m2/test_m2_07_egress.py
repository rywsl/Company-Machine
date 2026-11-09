"""M2-07 验收：出网（spec/system/egress.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.egress")
pytest.importorskip("mao.errors")
pytest.importorskip("mao.snapshot")

from mao.egress import SecretStore, build_egress, egress_run_args  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from tests.acceptance.m2.world import EXAMPLE  # noqa: E402

CANARY = "CANARY-SECRET-VALUE-0123456789"


def _store(tmp_path: Path) -> SecretStore:
    root = tmp_path / "secrets-root"
    (root / "secrets" / "values").mkdir(parents=True)
    (root / "secrets" / "manifest.json").write_text(
        '{"TOOL_TOKEN": "other", "MODEL_TOKEN": "model"}',
        encoding="utf-8",
        newline="\n",
    )
    (root / "secrets" / "values" / "TOOL_TOKEN").write_text(CANARY, encoding="utf-8", newline="\n")
    (root / "secrets" / "values" / "MODEL_TOKEN").write_text(CANARY, encoding="utf-8", newline="\n")
    return SecretStore(root)


def test_none_stays_none(tmp_path: Path) -> None:
    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "subs-convert@1"), _store(tmp_path))
    assert plan.network == "none"
    assert egress_run_args("none") == ["--network", "none"]
    assert CANARY not in plan.env.values()


def test_allow_exact_host_and_method(tmp_path: Path) -> None:
    from mao.egress import EgressProxy

    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "explainer@3"), _store(tmp_path))
    proxy = EgressProxy(
        lambda _job: plan, _store(tmp_path), __import__("mao.base", fromlist=["FakeClock"]).FakeClock()
    )
    port = proxy.start()
    assert port > 0
    proxy.close()
    assert any(rule.host == "fal.run" for rule in plan.allow)


def test_no_suffix_match(tmp_path: Path) -> None:
    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "explainer@3"), _store(tmp_path))
    hosts = {rule.host for rule in plan.allow}
    assert "fal.run" in hosts
    assert "api.fal.run" not in hosts
    assert not any(host.endswith(".fal.run") and host != "fal.run" for host in hosts)


def test_machine_needs_do_not_expand_policy(tmp_path: Path) -> None:
    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "explainer@3"), _store(tmp_path))
    hosts = {rule.host for rule in plan.allow}
    assert {"fal.run", "api.pexels.com", "api.elevenlabs.io"} <= hosts
    assert "example.invalid" not in hosts


def test_connect_without_ca_rejected_when_methods_limited(tmp_path: Path) -> None:
    from mao.egress import EgressProxy

    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "publish-shorts@1"), _store(tmp_path))
    proxy = EgressProxy(
        lambda _job: plan, _store(tmp_path), __import__("mao.base", fromlist=["FakeClock"]).FakeClock()
    )
    assert proxy.start() > 0
    proxy.close()
    assert plan.network == "egress"
    limited = [rule for rule in plan.allow if "*" not in rule.methods]
    assert limited


def test_argv_has_no_host_network() -> None:
    argv = egress_run_args("egress")
    assert argv == [
        "--network",
        "mao-egress",
        "--add-host=egress:host-gateway",
        "--add-host=model-proxy:host-gateway",
    ]
    assert "--privileged" not in argv
    assert "host" not in argv[argv.index("--network") + 1]


def test_unproxied_still_denied(tmp_path: Path) -> None:
    from mao.sandbox import DockerProvider, SandboxSpec

    provider = DockerProvider(tmp_path, allow_unproxied_egress=False)
    with pytest.raises(FactoryError) as ei:
        provider.open(
            SandboxSpec(
                name="mao-j-0001-1",
                image="registry.local/machines/ffmpeg@sha256:" + "a" * 64,
                mounts=(),
                network="egress",
            )
        )
    assert ei.value.code == "EGRESS_UNSUPPORTED"


def test_model_secret_not_in_sandbox_env(tmp_path: Path) -> None:
    store = _store(tmp_path)
    injected = store.inject(["TOOL_TOKEN"])
    assert "MODEL_TOKEN" not in injected
    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "explainer@3"), store)
    assert "MODEL_TOKEN" not in plan.env
    assert store.secret_class("MODEL_TOKEN") == "model"
    assert "MODEL_TOKEN" in plan.withheld


def _post_model(port: int, job_id: str, body: dict) -> tuple[int, dict]:
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(body).encode(),
        headers={"X-Factory-Job": job_id, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _running_job(ctx, snapshot, job_id: str, state: str = "RUNNING") -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, '{}', '{}', '{}', ?, ?)",
            (
                job_id,
                snapshot.key("Station", "explainer@3"),
                snapshot.epoch,
                state,
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
            ),
        )


def test_model_proxy_meters(tmp_path: Path) -> None:
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mao.base import FakeClock
    from mao.egress import ModelProxy
    from tests.acceptance.m2.world import open_factory

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 0}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading = __import__("threading")
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    root = ctx.root
    (root / "egress").mkdir()
    (root / "egress" / "prices.json").write_text(
        json.dumps({"meter-model": {"input_per_mtok": 2.0, "output_per_mtok": 4.0}}),
        encoding="utf-8",
    )
    (root / "egress" / "upstreams.json").write_text(
        json.dumps(
            {
                "meter-model": {
                    "url": f"http://127.0.0.1:{upstream.server_address[1]}/v1/messages",
                    "secret": "TOOL_TOKEN",
                    "header": "x-api-key",
                }
            }
        ),
        encoding="utf-8",
    )
    _running_job(ctx, snapshot, "j-meter")
    proxy = ModelProxy(_store(tmp_path), FakeClock(), root)
    port = proxy.start(port=0)
    try:
        status, _body = _post_model(port, "j-meter", {"model": "meter-model"})
        assert status == 200
        assert proxy.spent("j-meter") == 0.002
    finally:
        proxy.close()
        upstream.shutdown()


def test_missing_price_does_not_zero(tmp_path: Path) -> None:
    import json

    from mao.base import FakeClock
    from mao.egress import ModelProxy
    from tests.acceptance.m2.world import open_factory

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    (ctx.root / "egress").mkdir()
    (ctx.root / "egress" / "upstreams.json").write_text(
        json.dumps({"*": {"url": "http://127.0.0.1:9/v1", "secret": "TOOL_TOKEN", "header": "x-api-key"}}),
        encoding="utf-8",
    )
    _running_job(ctx, snapshot, "j-noprice")
    before = proxy_spent = 0.0
    proxy = ModelProxy(_store(tmp_path), FakeClock(), ctx.root)
    port = proxy.start(port=0)
    try:
        status, body = _post_model(port, "j-noprice", {"model": "unpriced"})
        assert status == 402
        assert body["code"] == "BUDGET_PRICE_MISSING"
        assert proxy.spent("j-noprice") == before == proxy_spent
    finally:
        proxy.close()


def test_budget_stop_prevents_seal(tmp_path: Path) -> None:
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mao.base import FakeClock
    from mao.egress import ModelProxy
    from tests.acceptance.m2.world import open_factory

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 0}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading = __import__("threading")
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    root = ctx.root
    (root / "egress").mkdir()
    (root / "egress" / "prices.json").write_text(
        json.dumps({"meter-model": {"input_per_mtok": 6000.0, "output_per_mtok": 0.0}}),
        encoding="utf-8",
    )
    (root / "egress" / "upstreams.json").write_text(
        json.dumps(
            {
                "meter-model": {
                    "url": f"http://127.0.0.1:{upstream.server_address[1]}/v1/messages",
                    "secret": "TOOL_TOKEN",
                    "header": "x-api-key",
                }
            }
        ),
        encoding="utf-8",
    )
    _running_job(ctx, snapshot, "j-stop")
    proxy = ModelProxy(_store(tmp_path), FakeClock(), root)
    port = proxy.start(port=0)
    try:
        first_status, _first = _post_model(port, "j-stop", {"model": "meter-model"})
        assert first_status == 200
        assert proxy.spent("j-stop") == 6.0
        stops = ctx.db.query("SELECT code FROM budget_stops WHERE job_id = ?", ("j-stop",))
        assert [row["code"] for row in stops] == ["BUDGET_EXCEEDED"]
        status, body = _post_model(port, "j-stop", {"model": "meter-model"})
        assert status == 402
        assert body["code"] == "BUDGET_EXCEEDED"
        row = ctx.db.query("SELECT state FROM jobs WHERE id = ?", ("j-stop",))
        assert row[0]["state"] != "SEALED"
    finally:
        proxy.close()
        upstream.shutdown()


def test_sealed_job_header_rejected(tmp_path: Path) -> None:
    from mao.base import FakeClock
    from mao.egress import ModelProxy
    from tests.acceptance.m2.world import open_factory

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    _running_job(ctx, snapshot, "j-sealed", state="SEALED")
    proxy = ModelProxy(_store(tmp_path), FakeClock(), ctx.root)
    port = proxy.start(port=0)
    try:
        status, body = _post_model(port, "j-sealed", {"model": "meter-model"})
        assert status == 403
        assert body["code"] == "EGRESS_JOB_INVALID"
    finally:
        proxy.close()


def test_secret_absent_from_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    redacted = store.redact(f"token={CANARY}")
    assert CANARY not in redacted
    try:
        raise FactoryError("SECRET_NOT_FOUND", store.redact(CANARY))
    except FactoryError as exc:
        assert CANARY not in str(exc)
