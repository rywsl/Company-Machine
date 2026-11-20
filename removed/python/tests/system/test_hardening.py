"""M8-01 单元：hardening.md 第 5 章里没有验收的条目。"""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("mao.egress")
pytest.importorskip("mao.retention")
pytest.importorskip("mao.supervisor")

from mao.base import CounterIds, FakeClock  # noqa: E402
from mao.context import Context  # noqa: E402
from mao.egress import (  # noqa: E402
    ProxyEndpoints,
    forwarder_name,
    load_forwarder_image,
    register_job_egress,
    stop_owned_forwarder,
)
from mao.errors import FactoryError  # noqa: E402
from mao.jobs import JobState  # noqa: E402
from mao.runner import Runner  # noqa: E402
from mao.sandbox import DockerProvider, LocalProvider, SandboxSpec  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from mao.supervisor import _session_network  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402
from tests.fakes.fakelib import local_commands  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "registry-m1"
IMAGE = "example.invalid/fwd@sha256:" + "ab" * 32


def _open(tmp_path: Path):
    root = tmp_path / "factory"
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    ctx = Context.open(
        root,
        provider=LocalProvider(root, commands=local_commands(), clock=clock),
        clock=clock,
        ids=CounterIds(),
    )
    return ctx, Snapshot.load(FIXTURE), Runner(ctx, Snapshot.load(FIXTURE)), clock


def _sealed_note(ctx, tmp_path: Path) -> str:
    src = tmp_path / "note.txt"
    src.write_text("hello", encoding="utf-8")
    bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
    ctx.warehouse.seal_many(
        [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "cd" * 32
    )
    return bean.id


def test_runner_registers_egress_plan_during_provisioning(tmp_path: Path) -> None:
    ctx, _snap, runner, _clock = _open(tmp_path)
    try:
        job = runner.submit("upper@1", {"note": _sealed_note(ctx, tmp_path)}, {})
        done = runner.run(job.id)
        assert done.state == JobState.SEALED
        rows = ctx.db.query("SELECT plan FROM egress_plans WHERE job_id = ?", (job.id,))
        assert rows
        body = json.loads(rows[0]["plan"])
        assert body["network"] in {"none", "egress"}
        assert "env" not in body
        assert "secret_names" in body and "withheld" in body
    finally:
        ctx.close()


def test_forwarder_image_comes_from_serve_config(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    root.mkdir()
    (root / "factory.json").write_text(json.dumps({"serve": {}}), encoding="utf-8")
    with pytest.raises(FactoryError) as missing:
        load_forwarder_image(root)
    assert missing.value.code == "EGRESS_PROXY_UNAVAILABLE"
    assert missing.value.detail["reason"] == "forwarder-image"
    (root / "factory.json").write_text(
        json.dumps({"serve": {"forwarder_image": "node:22"}}), encoding="utf-8"
    )
    with pytest.raises(FactoryError) as bare:
        load_forwarder_image(root)
    assert bare.value.code == "SERVE_CONFIG_INVALID"
    assert bare.value.detail["field"] == "forwarder_image"
    (root / "factory.json").write_text(json.dumps({"serve": {"forwarder_image": IMAGE}}), encoding="utf-8")
    assert load_forwarder_image(root) == IMAGE


def test_forwarder_uses_configured_image_and_cleans_up(tmp_path: Path) -> None:
    from mao.egress import ensure_desktop_forwarder

    log = tmp_path / "docker.log"
    script = tmp_path / "docker-fake.py"
    script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        f"log = Path({str(log)!r})\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write('\\n'.join(sys.argv[1:]) + '\\n---\\n')\n"
        "if sys.argv[1:3] == ['network', 'connect'] and os.environ.get('FAIL_CONNECT') == '1':\n"
        "    sys.stderr.write('connect failed')\n"
        "    sys.exit(1)\n"
        "if sys.argv[1] == 'inspect':\n"
        "    sys.stdout.write('172.30.0.9')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "docker.cmd"
    launcher.write_text(f'@echo off\r\npython "{script}" %*\r\n', encoding="utf-8")
    owner = "serve-host:1:o-0001"
    alias = ensure_desktop_forwarder(
        str(launcher), ports=[51234, 8080], timeout_s=10, image=IMAGE, owner=owner
    )
    assert alias == "172.30.0.9"
    recorded = log.read_text(encoding="utf-8")
    assert IMAGE in recorded
    assert "node:22-bookworm-slim" not in recorded
    assert forwarder_name(owner) == "mao-fwd-" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:12]
    assert forwarder_name(owner) in recorded
    assert "--read-only" in recorded
    assert "--cap-drop" in recorded and "ALL" in recorded
    assert "no-new-privileges" in recorded
    assert "--rm" in recorded
    log.write_text("", encoding="utf-8")
    import os

    os.environ["FAIL_CONNECT"] = "1"
    try:
        with pytest.raises(FactoryError) as failed:
            ensure_desktop_forwarder(str(launcher), ports=[1, 2], timeout_s=10, image=IMAGE, owner=owner)
        assert failed.value.code == "EGRESS_PROXY_UNAVAILABLE"
    finally:
        os.environ.pop("FAIL_CONNECT", None)
    cleanup = log.read_text(encoding="utf-8")
    assert "rm" in cleanup and "-f" in cleanup and forwarder_name(owner) in cleanup


def test_provider_uses_discovered_alias(tmp_path: Path) -> None:
    provider = DockerProvider(tmp_path, allow_unproxied_egress=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = int(sock.getsockname()[1])
    try:
        provider.proxy_host = "127.0.0.1"
        provider.proxies = ProxyEndpoints("127.0.0.1", port, port, "172.30.0.2")
        argv = provider._network_argv(
            SandboxSpec(
                name="mao-j-0001-1",
                image="registry.local/machines/ffmpeg@sha256:" + "a" * 64,
                mounts=(),
                env={"HTTP_PROXY": f"http://egress:{port}"},
                network="egress",
            )
        )
    finally:
        sock.close()
    assert "--add-host=egress:172.30.0.2" in argv
    assert "--add-host=model-proxy:172.30.0.2" in argv
    assert argv.index("--add-host=egress:172.30.0.2") < argv.index("--add-host=egress:host-gateway")


def test_close_deletes_only_owned_forwarder(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    removed: list[str] = []

    class Provider:
        name = "docker"
        docker = "docker"
        egress_forwarder = "mao-fwd-owned"

    ctx = Context.open(root, provider=Provider(), clock=FakeClock(), ids=CounterIds())
    ctx.provider.docker = "missing-docker-binary"
    ctx.provider.egress_forwarder = "mao-fwd-owned"

    def _rm(argv, **kwargs):
        removed.append(argv[-1])
        return type("Proc", (), {"returncode": 0})()

    import mao.egress as egress

    original = egress.subprocess.run
    egress.subprocess.run = _rm  # type: ignore[method-assign]
    try:
        stop_owned_forwarder(ctx)
    finally:
        egress.subprocess.run = original  # type: ignore[method-assign]
        ctx.provider.egress_forwarder = None
        ctx.close()
    assert removed == ["mao-fwd-owned"]


def test_dead_digest_is_selected_inside_the_write_transaction(tmp_path: Path) -> None:
    from mao.retention import _sweep_blobs

    ctx, _snap, _runner, _clock = _open(tmp_path)
    try:
        bean_id = _sealed_note(ctx, tmp_path)
        ctx.warehouse.expire(bean_id, by="test", reason="retention")
        seen: dict[str, int] = {}

        def _trace(sql: str) -> None:
            if "payload_files" in sql and "expired_at" in sql:
                seen["depth"] = ctx.db._depth

        ctx.db._conn.set_trace_callback(_trace)
        deleted, freed = _sweep_blobs(ctx, set(), dry_run=False)
        assert deleted == 1
        assert freed > 0
        assert seen["depth"] >= 1
    finally:
        ctx.close()


def test_session_network_follows_model_proxy_reference() -> None:
    referenced = {"invoke": {"argv": ["--url", "${MODEL_PROXY}/v1"]}, "env": {}}
    via_env = {"invoke": {"argv": ["echo"]}, "env": {"BASE": "http://${MODEL_PROXY}"}}
    plain = {"invoke": {"argv": ["echo"]}, "env": {"BASE": "local"}}
    assert _session_network("none", referenced) == "egress"
    assert _session_network("none", via_env) == "egress"
    assert _session_network("none", plain) == "none"
    assert _session_network("egress", plain) == "egress"


def test_register_job_egress_omits_env(tmp_path: Path) -> None:
    from mao.egress import AllowRule, EgressPlan, plans_from_db

    ctx, _snap, _runner, _clock = _open(tmp_path)
    try:
        ctx.db.query(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES ('j-1', 'Station:upper@1', 'x', 'RUNNING', 1, '{}', '{}', '{}', ?, ?)",
            ("2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )
        plan = EgressPlan(
            "egress",
            (AllowRule("web-read", "example.invalid", frozenset({"GET", "HEAD"})),),
            ("TOOL",),
            ("MODEL",),
            {"HTTP_PROXY": "http://egress:1"},
        )
        register_job_egress(ctx.db, "j-1", plan)
        stored = json.loads(ctx.db.query("SELECT plan FROM egress_plans WHERE job_id = 'j-1'")[0]["plan"])
        assert stored["allow"][0]["methods"] == ["GET", "HEAD"]
        assert "env" not in stored
        loaded = plans_from_db(ctx.db)("j-1")
        assert loaded is not None
        assert loaded.allow[0].host == "example.invalid"
        assert loaded.env == {}
    finally:
        ctx.close()
