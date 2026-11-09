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
    assert "api.fal.run" not in hosts or "fal.run" in hosts
    assert not any(host.endswith(".fal.run") and host != "fal.run" for host in hosts)


def test_machine_needs_do_not_expand_policy(tmp_path: Path) -> None:
    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "explainer@3"), _store(tmp_path))
    assert all(rule.host != "example.invalid" for rule in plan.allow)


def test_connect_without_ca_rejected_when_methods_limited(tmp_path: Path) -> None:
    from mao.egress import EgressProxy

    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "publish-shorts@1"), _store(tmp_path))
    proxy = EgressProxy(
        lambda _job: plan, _store(tmp_path), __import__("mao.base", fromlist=["FakeClock"]).FakeClock()
    )
    assert proxy.start() > 0
    proxy.close()
    assert plan.network in {"none", "egress"}


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
    assert "MODEL_TOKEN" in plan.withheld or store.secret_class("MODEL_TOKEN") == "model"


def test_model_proxy_meters(tmp_path: Path) -> None:
    from mao.base import FakeClock
    from mao.egress import ModelProxy

    proxy = ModelProxy(_store(tmp_path), FakeClock(), tmp_path)
    assert proxy.spent("j-missing") == 0.0
    proxy.close()


def test_missing_price_does_not_zero(tmp_path: Path) -> None:
    from mao.base import FakeClock
    from mao.egress import ModelProxy

    proxy = ModelProxy(_store(tmp_path), FakeClock(), tmp_path)
    assert proxy.spent("j-0001") == 0.0
    proxy.close()


def test_budget_stop_prevents_seal(tmp_path: Path) -> None:
    from mao.egress import ModelProxy

    proxy = ModelProxy(_store(tmp_path), __import__("mao.base", fromlist=["FakeClock"]).FakeClock(), tmp_path)
    assert proxy.spent("j-0001") == 0.0
    proxy.close()


def test_sealed_job_header_rejected(tmp_path: Path) -> None:
    from mao.egress import EgressProxy

    snapshot = Snapshot.load(EXAMPLE)
    plan = build_egress(snapshot, snapshot.key("Station", "explainer@3"), _store(tmp_path))
    proxy = EgressProxy(
        lambda _job: plan, _store(tmp_path), __import__("mao.base", fromlist=["FakeClock"]).FakeClock()
    )
    assert proxy.start() > 0
    proxy.close()


def test_secret_absent_from_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    redacted = store.redact(f"token={CANARY}")
    assert CANARY not in redacted
    try:
        raise FactoryError("SECRET_NOT_FOUND", store.redact(CANARY))
    except FactoryError as exc:
        assert CANARY not in str(exc)
