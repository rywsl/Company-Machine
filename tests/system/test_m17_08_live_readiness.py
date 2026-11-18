"""M17-13 四项见证准备。夹具只证明编排，scope=witness_protocol，不冒充 live passed。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tests.system import m17_08_live as live

CANARY = "CANARY-SECRET-VALUE-m17"
PRODUCTS = live.HISTORICAL_PRODUCTS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _event_command(secret: str = CANARY) -> list[str]:
    script = (
        "from pathlib import Path\n"
        "Path('events.jsonl').write_text("
        '\'{"seq": 2, "kind": "protocol-observed"}\\n\', encoding=\'utf-8\')\n'
        "Path('ran').write_text('ok', encoding='utf-8')\n"
        f"assert {secret!r} not in Path('events.jsonl').read_text(encoding='utf-8')\n"
    )
    return [sys.executable, "-c", script]


def _grant(path: Path, *, expires: str = "2099-01-01T00:00:00+00:00", cap: float | None = 20) -> None:
    body: dict[str, object] = {"status": "approved", "expires_at": expires, "total_usd": 20}
    if cap is not None:
        body["cost_cap_usd"] = cap
    _write(path, json.dumps(body))


def _auth(path: Path, runtime: str) -> None:
    _write(path, f"runtime_ref: {runtime}\nsecret: {CANARY}\n")


def _manifest(path: Path, **extra: object) -> None:
    body = {
        "schema_version": "mao-m17-witness-manifest-1",
        "scope": "witness_protocol",
        "command": _event_command(),
        "docker_required": False,
        "paid": True,
        "line_products": [],
    }
    body.update(extra)
    _write(path, json.dumps(body))


def _cell(root: Path, name: str, runtime: str, domain: str, mode: str | None = None) -> dict[str, str]:
    base = root / name
    _manifest(base / "manifest.json")
    _grant(base / "grant.json")
    _auth(base / "auth.txt", runtime)
    _write(base / "objective.txt", "raw material only\n")
    _write(base / "contract.txt", "contract\n")
    _write(base / "schedule.txt", "drop raw notes at t0\n")
    cell = {
        "deployment_manifest": str(base / "manifest.json"),
        "runtime_ref": runtime,
        "domain_id": domain,
        "objective_asset": str(base / "objective.txt"),
        "grant_file": str(base / "grant.json"),
        "contract_ref": str(base / "contract.txt"),
        "source_schedule": str(base / "schedule.txt"),
    }
    if mode:
        cell["entry_mode"] = mode
    return cell


def _config(
    root: Path, *, c10: dict[str, str] | None, cells: list[dict[str, str]] | None, products: dict[str, str]
) -> Path:
    auth = {}
    runtimes = []
    if c10:
        runtimes.append(c10["runtime_ref"])
    if cells:
        runtimes.extend(cell["runtime_ref"] for cell in cells)
    for runtime in dict.fromkeys(runtimes):
        path = root / f"auth-{runtime.replace(':', '-')}.txt"
        if not path.is_file():
            _auth(path, runtime)
        auth[runtime] = {"source": "native_config", "path": str(path)}
    grant = root / "grant.json"
    if not grant.is_file():
        _grant(grant)
    body = {
        "schema_version": live.SCHEMA,
        "project_root": str(root),
        "authentication": auth,
        "authorization": {"grant_file": str(grant), "deployment_budget_file": None},
        "products": products,
    }
    if c10:
        body["c10"] = c10
    if cells is not None:
        body["cells"] = cells
    path = root / "witness.json"
    _write(path, json.dumps(body))
    return path


def _run(entry: str, root: Path, config: Path, env: dict[str, str] | None = None) -> dict:
    merged = dict(os.environ)
    merged.pop("XAI_API_KEY", None)
    merged.pop("OPENAI_API_KEY", None)
    merged.pop("MAO_BUDGET_USD", None)
    if env:
        merged.update(env)
    merged[live.CONFIG_ENV] = str(config)
    return live.execute_witness(entry, root / "run", env=merged)


def _four(root: Path) -> list[dict[str, str]]:
    pairs = (
        ("a-help", "runtime-a@1", "domain-a", "help_only"),
        ("a-skill", "runtime-b@1", "domain-a", "skill"),
        ("b-help", "runtime-b@1", "domain-b", "help_only"),
        ("b-skill", "runtime-a@1", "domain-b", "skill"),
    )
    return [_cell(root, name, runtime, domain, mode) for name, runtime, domain, mode in pairs]


def test_ready_resources_execute_witness_not_permanent_blocker(tmp_path: Path) -> None:
    cell = _cell(tmp_path, "c10", "runtime-a@1", "domain-a")
    config = _config(tmp_path, c10=cell, cells=None, products={})
    result = _run("c10", tmp_path, config)
    assert result["status"] == "protocol"
    assert result["scope"] == "witness_protocol"
    assert result["live_passed"] is False
    assert result["status"] != "blocked"
    process = result["processes"][0]
    assert process["pid"] > 0
    assert process["exit"] == 0
    assert (tmp_path / "run" / "cell-0" / "ran").read_text(encoding="utf-8") == "ok"
    assert "launch" in result["stages"]
    assert "observe" in result["stages"]
    assert CANARY not in (tmp_path / "run" / "witness.log").read_text(encoding="utf-8")
    assert CANARY not in json.dumps(result)


def test_native_auth_and_grant_without_vendor_env_are_accepted(tmp_path: Path) -> None:
    cell = _cell(tmp_path, "c10", "runtime-a@1", "domain-a")
    config = _config(tmp_path, c10=cell, cells=None, products={})
    result = _run(
        "c10", tmp_path, config, env={"XAI_API_KEY": "", "OPENAI_API_KEY": "", "MAO_BUDGET_USD": ""}
    )
    assert result["status"] == "protocol"
    assert result["processes"][0]["pid"] > 0
    assert CANARY not in json.dumps(result)

    wrong = tmp_path / "wrong"
    bad_cell = _cell(wrong, "c10", "runtime-a@1", "domain-a")
    _write(wrong / "auth-runtime-a@1.txt", "runtime_ref: other@1\nsecret: " + CANARY + "\n")
    bad_config = _config(wrong, c10=bad_cell, cells=None, products={})
    rejected = _run("c10", wrong, bad_config)
    assert rejected["status"] == "failed"
    assert rejected["reason_kind"] == "configuration"
    assert "upstream" in rejected["detail"]

    missing = tmp_path / "missing-ref"
    missing_cell = _cell(missing, "c10", "runtime-a@1", "domain-a")
    missing_config = _config(missing, c10=missing_cell, cells=None, products={})
    (missing / "auth-runtime-a@1.txt").unlink()
    invalid = _run("c10", missing, missing_config)
    assert invalid["status"] == "failed"
    assert invalid["reason_kind"] == "configuration"
    assert "invalid reference" in invalid["detail"]

    expired = tmp_path / "expired"
    expired_cell = _cell(expired, "c10", "runtime-a@1", "domain-a")
    _grant(expired / "grant.json", expires="2000-01-01T00:00:00+00:00")
    expired_config = _config(expired, c10=expired_cell, cells=None, products={})
    denied = _run("c10", expired, expired_config)
    assert denied["status"] == "blocked"
    assert denied["reason_kind"] == "authorization"
    assert "expired" in denied["detail"]


def test_resource_blockers_are_scoped_and_recoverable(tmp_path: Path) -> None:
    blocked_root = tmp_path / "missing-manifest"
    cell = _cell(blocked_root, "c10", "runtime-a@1", "domain-a")
    (blocked_root / "c10" / "manifest.json").unlink()
    config = _config(blocked_root, c10=cell, cells=None, products={})
    missing = _run("c10", blocked_root, config)
    assert missing["status"] == "blocked"
    assert missing["reason_kind"] == "resource"
    assert "deployment_manifest" in missing["field"]

    _manifest(blocked_root / "c10" / "manifest.json")
    recovered = _run("c10", blocked_root, config)
    assert recovered["status"] == "protocol"
    assert recovered["processes"][0]["pid"] > 0

    basic = _run("basic", blocked_root, config)
    assert basic["status"] == "protocol"
    assert all(key not in basic["detail"] for key in PRODUCTS)

    four_root = tmp_path / "four"
    cells = _four(four_root)
    four_config = _config(four_root, c10=None, cells=cells, products={})
    four_blocked = _run("four_cell", four_root, four_config)
    assert four_blocked["status"] == "blocked"
    assert four_blocked["reason_kind"] == "resource"
    assert "Machine:openmontage@1" in four_blocked["detail"]

    docker_root = tmp_path / "docker"
    docker_cell = _cell(docker_root, "c10", "runtime-a@1", "domain-a")
    _manifest(
        docker_root / "c10" / "manifest.json",
        docker_required=True,
        docker_command=[sys.executable, "-c", "raise SystemExit(7)"],
    )
    docker_config = _config(docker_root, c10=docker_cell, cells=None, products={})
    no_docker = _run("c10", docker_root, docker_config)
    assert no_docker["status"] == "blocked"
    assert no_docker["reason_kind"] == "resource"
    assert no_docker["field"] == "docker"

    scope_root = tmp_path / "scope"
    scope_cell = _cell(scope_root, "c10", "runtime-a@1", "domain-a")
    _manifest(scope_root / "c10" / "manifest.json", scope="substituted")
    scope_config = _config(scope_root, c10=scope_cell, cells=None, products={})
    bad_scope = _run("c10", scope_root, scope_config)
    assert bad_scope["status"] == "failed"
    assert bad_scope["reason_kind"] == "configuration"
    assert "scope" in bad_scope["field"]

    cap_root = tmp_path / "cap"
    cap_cell = _cell(cap_root, "c10", "runtime-a@1", "domain-a")
    _grant(cap_root / "grant.json", cap=None)
    cap_body = json.loads((cap_root / "grant.json").read_text(encoding="utf-8"))
    cap_body.pop("total_usd", None)
    _write(cap_root / "grant.json", json.dumps(cap_body))
    cap_config = _config(cap_root, c10=cap_cell, cells=None, products={})
    no_cap = _run("c10", cap_root, cap_config)
    assert no_cap["status"] == "blocked"
    assert no_cap["reason_kind"] == "authorization"
    assert "cost cap" in no_cap["detail"]


def test_live_reports_require_current_observed_execution(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    handwritten = {"status": "passed", "scope": "live", "watermark_seq": 1, "snapshot_sha256": "0" * 64}
    checked = live.validate_observed_execution(handwritten, old)
    assert checked["ok"] is False
    assert any("events" in item or "snapshot" in item for item in checked["issues"])

    protocol_claim = {"status": "passed", "scope": "witness_protocol", "watermark_seq": 1}
    claimed = live.validate_observed_execution(protocol_claim, old)
    assert claimed["ok"] is False
    assert any("live passed" in item for item in claimed["issues"])

    cell = _cell(tmp_path, "c10", "runtime-a@1", "domain-a")
    config = _config(tmp_path, c10=cell, cells=None, products={})
    result = _run("c10", tmp_path, config)
    assert result["status"] == "protocol"
    assert result["live_passed"] is False
    assert result["scope"] == "witness_protocol"
    assert result["event_count"] >= 1
    assert result["integrity"]["ok"] is False or result["status"] != "passed"
    live_gap = live.execute_witness("c10", tmp_path / "live-gap", live=True, env={})
    assert live_gap["entry"] == "c10"
    assert live_gap["live_passed"] is False
    assert live_gap["reason_kind"] == "authorization"
    assert live_gap["field"] == "authorization.grant_file"
    assert "qualification_receipts" not in live_gap["detail"]
    assert "Machine:openmontage@1" not in live_gap["detail"]
    assert "Machine:minimax-h3@1" not in live_gap["detail"]
    grant = tmp_path / "full-grant.json"
    grant.write_text(
        json.dumps({"is_full_m17_live_acceptance": True, "total_usd": 20}),
        encoding="utf-8",
    )
    observed = live.execute_witness(
        "c10",
        tmp_path / "live-observed",
        live=True,
        env={live.GRANT_ENV: str(grant)},
    )
    assert observed["entry"] == "c10"
    assert "qualification_receipts" not in observed["detail"]
    assert "Machine:openmontage@1" not in observed["detail"]
    assert "Machine:minimax-h3@1" not in observed["detail"]
    assert observed["reason_kind"] != "authorization"
    if observed.get("live_passed") is True:
        assert observed["status"] == "passed"
        assert observed["scope"] == "live"
        assert observed.get("event_count", 0) >= 4
        assert observed.get("model_spent_usd", 0) > 0
        assert observed["cycles"][0] == [1, "idle"] or observed["cycles"][0] == (1, "idle")
    else:
        assert observed["field"] == "observe"
        assert observed["reason_kind"] == "implementation"
