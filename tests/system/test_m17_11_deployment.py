"""M17-11 部署预检、幂等安装和外部操作边界。"""

from __future__ import annotations

import json
from pathlib import Path

from mao.base import CounterIds, FakeClock
from mao.context import Context
from mao.deployment import (
    apply_deployment,
    deployment_blockers,
    diagnose,
    effective_rollout,
    plan_deployment,
    rollout_evidence,
)
from mao.errors import FactoryError
from mao.operations import (
    handle_mailbox,
    issue_credential,
    revoke_credential,
    show_operation,
    submit_operation,
)
from mao.sandbox import LocalProvider
from tests.fakes.fakelib import local_commands

_MANIFEST = {
    "schema_version": "mao-deployment-1",
    "profile": "local-test",
    "root_id": "root-1",
    "registry": {"source": "local", "commit": "a" * 40},
    "config": {},
    "runtimes": [],
    "machines": [],
    "qualification_receipts": [],
}


def test_doctor_and_plan_are_read_only(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    report = diagnose(missing, profile="local-test", manifest=dict(_MANIFEST))
    plan = plan_deployment(missing, dict(_MANIFEST))
    assert not missing.exists()
    assert report["status"] == "degraded"
    assert plan["plan_digest"]
    assert "没有隔离" in " ".join(report["limitations"])


def test_init_repeat_preserves_registry_assets_secrets_and_runs(tmp_path: Path) -> None:
    root = _factory(tmp_path)
    registry = root / "registry" / "keep.yaml"
    registry.parent.mkdir()
    registry.write_text("keep\n", encoding="utf-8")
    secret = root / "secrets" / "values" / "TOKEN"
    secret.parent.mkdir(parents=True)
    secret.write_text("secret-value\n", encoding="utf-8")
    plan = plan_deployment(root, dict(_MANIFEST))
    first = apply_deployment(root, dict(_MANIFEST), plan_digest=plan["plan_digest"])
    second = apply_deployment(root, dict(_MANIFEST), plan_digest=plan["plan_digest"])
    assert first["stage"] == "committed" and second["noop"] is True
    assert registry.read_text(encoding="utf-8") == "keep\n"
    assert secret.read_text(encoding="utf-8") == "secret-value\n"
    assert (root / "factory.db").is_file()


def test_apply_crash_and_state_conflict_recover(tmp_path: Path) -> None:
    root = _factory(tmp_path)
    plan = plan_deployment(root, dict(_MANIFEST))
    journal = root / "deployment" / f"{plan['plan_digest']}.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps({**plan, "stage": "applying", "steps": ["staged"]}), encoding="utf-8")
    recovered = apply_deployment(root, dict(_MANIFEST), plan_digest=plan["plan_digest"])
    assert recovered["stage"] == "committed"
    (root / "extra.txt").write_text("changed\n", encoding="utf-8")
    fresh = plan_deployment(root, dict(_MANIFEST))
    assert fresh["plan_digest"] != plan["plan_digest"]
    try:
        apply_deployment(root, dict(_MANIFEST), plan_digest=plan["plan_digest"])
    except FactoryError as exc:
        assert exc.code == "DEPLOY_CONFLICT"
    else:
        raise AssertionError("stale plan was applied")


def test_missing_docker_or_qualification_blocks_resident(tmp_path: Path) -> None:
    blocked = deployment_blockers(
        "resident",
        {"docker_ok": False, "qualification_receipts": [], "governance": False},
    )
    assert {item["code"] for item in blocked} >= {"DOCKER_UNAVAILABLE", "RUNTIME_UNQUALIFIED"}
    report = diagnose(tmp_path / "no-root", profile="resident", manifest={**_MANIFEST, "profile": "resident"})
    assert report["status"] == "blocked"
    assert any(item["code"] == "RUNTIME_UNQUALIFIED" for item in report["blockers"])


def test_external_cli_cannot_impersonate_human_or_open_database(tmp_path: Path) -> None:
    ctx = _open(tmp_path)
    issued = issue_credential(ctx, "ada", "2026-01-02T00:00:00.000Z", tmp_path / "cred.json")
    before = (ctx.root / "factory.db").stat().st_mtime_ns
    submitted = submit_operation(
        tmp_path / "cred.json",
        ["talk", "send", "--by", "human:ada", "hello"],
        request_key="k1",
    )
    assert (ctx.root / "factory.db").stat().st_mtime_ns == before
    [reply] = handle_mailbox(ctx, issued["credential_id"])
    assert reply["state"] == "failed"
    assert reply["error"]["code"] == "OPERATION_FORBIDDEN"
    ok = submit_operation(tmp_path / "cred.json", ["talk", "send", "hello"], request_key="k2")
    [done] = [
        item
        for item in handle_mailbox(ctx, issued["credential_id"])
        if item["request_id"] == ok["request_id"]
    ]
    assert done["result"]["by"] == "external:ada"
    assert show_operation(ctx, issued["credential_id"], submitted["request_id"])["state"] == "failed"


def test_operator_credential_expiry_revoke_and_dedup(tmp_path: Path) -> None:
    ctx = _open(tmp_path)
    issued = issue_credential(ctx, "ada", "2026-01-02T00:00:00.000Z", tmp_path / "cred.json")
    first = submit_operation(tmp_path / "cred.json", ["talk", "send", "same"], request_key="same")
    second = submit_operation(tmp_path / "cred.json", ["talk", "send", "same"], request_key="same")
    assert first["request_id"] == second["request_id"]
    replies = handle_mailbox(ctx, issued["credential_id"])
    assert len(replies) == 1
    again = handle_mailbox(ctx, issued["credential_id"])
    assert again[0]["result"]["message_id"] == replies[0]["result"]["message_id"]
    other = submit_operation(tmp_path / "cred.json", ["talk", "send", "different"], request_key="same")
    conflict = [
        item
        for item in handle_mailbox(ctx, issued["credential_id"])
        if item["request_id"] == other["request_id"]
    ]
    assert conflict[0]["error"]["code"] == "OPERATION_CONFLICT"
    revoke_credential(ctx, issued["credential_id"])
    revoked = submit_operation(tmp_path / "cred.json", ["status"], request_key=None)
    found = [
        item
        for item in handle_mailbox(ctx, issued["credential_id"])
        if item["request_id"] == revoked["request_id"]
    ]
    assert found[0]["error"]["code"] == "OPERATION_CREDENTIAL_EXPIRED"
    expired = _open(tmp_path / "expired")
    issue_credential(expired, "ada", "2025-01-01T00:00:00.000Z", tmp_path / "old.json")
    submit_operation(tmp_path / "old.json", ["status"])
    assert (
        handle_mailbox(expired, json.loads((tmp_path / "old.json").read_text(encoding="utf-8"))["id"])[0][
            "error"
        ]["code"]
        == "OPERATION_CREDENTIAL_EXPIRED"
    )


def test_rollout_minimums_and_truthful_zero_evidence(tmp_path: Path) -> None:
    del tmp_path
    effective = effective_rollout(
        {"T2": {"shadow_runs": 3, "observe_s": 10}},
        {"T1": {"shadow_runs": 0, "observe_s": 0}, "T2": {"shadow_runs": 1, "observe_s": 1}},
    )
    assert effective["T2"] == {"shadow_runs": 3, "observe_s": 10}
    assert effective["T1"]["shadow_runs"] == 1
    zero = rollout_evidence(0, 0, {"shadow_runs": 0, "observe_s": 0})
    assert zero["not_performed"] is True and zero["observed"] is False
    try:
        rollout_evidence(0, 0, effective["T2"])
    except FactoryError as exc:
        assert exc.code == "ROLLOUT_EVIDENCE_REQUIRED"
    else:
        raise AssertionError("zero evidence satisfied a positive minimum")


def _factory(tmp_path: Path) -> Path:
    ctx = _open(tmp_path)
    ctx.close()
    return ctx.root


def _open(tmp_path: Path) -> Context:
    root = tmp_path / "factory"
    root.mkdir(parents=True, exist_ok=True)
    return Context.open(
        root,
        provider=LocalProvider(root, commands=local_commands()),
        clock=FakeClock(),
        ids=CounterIds(),
    )
