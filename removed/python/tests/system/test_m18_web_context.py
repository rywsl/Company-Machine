from __future__ import annotations

import json
from pathlib import Path

from mao.context import Context
from mao.sandbox import LocalProvider
from mao.secrets import store_secret
from mao.snapshot import Snapshot
from mao.webapp.read_model import WebReadModel

FIXTURE = Path("spec/examples/registry")


def test_harnesses_expose_roles_and_allow_local_adapters(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    adapter_dir = root / ".factory" / "harnesses"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "custom.json").write_text(
        json.dumps(
            {
                "id": "zcode-local",
                "name": "Zcode",
                "version": "local",
                "roles": ["operator", "foreman"],
                "capabilities": {"tool_calls": True},
            }
        ),
        encoding="utf-8",
    )
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        harnesses = model.harnesses()

    names = {item["name"] for item in harnesses}
    assert "claude-code" in names
    assert "Zcode" in names
    assert any("foreman" in item["roles"] for item in harnesses)
    assert any("operator" in item["roles"] for item in harnesses)
    assert any(item["source"] == "local" for item in harnesses)


def test_secret_context_is_masked_and_tracks_bindings(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    store_secret(root, "GITHUB_PUSH_TOKEN", "publish", "do-not-return-this-value")
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        resources = model.secret_resources()

    item = next(resource for resource in resources if resource["key"] == "GITHUB_PUSH_TOKEN")
    encoded = json.dumps(resources, ensure_ascii=False)
    assert item["configured"] is True
    assert item["masked_value"] == "••••••••"
    assert item["bound_objects"]
    assert "do-not-return-this-value" not in encoded


def test_harness_profiles_keep_full_configuration_without_returning_api_key(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    payload = {
        "role": "operator",
        "runtime": "OperatorRuntime:claude-code@4",
        "base_url": "https://model.example.test/v1",
        "model": "claude-opus-5-5",
        "api_key": "operator-secret-value",
        "thinking_effort": "high",
        "context": 128000,
        "image": "registry.local/operators/claude-code@sha256:abc",
        "network": "egress",
        "session_timeout_s": 600,
        "session_budget_usd": 0,
        "daily_budget_usd": 10,
        "max_turns": 20,
        "max_attempts": 2,
        "backoff_s": 0,
        "mode": "session",
    }
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        saved = model.save_harness_profile(payload)
        reused = model.save_harness_profile({**payload, "api_key": ""})
        profiles = model.harness_profiles()

    assert saved["id"] == reused["id"]
    assert saved["api_key_configured"] is True
    assert saved["thinking_effort"] == "high"
    assert saved["session_budget_usd"] == 0
    assert "api_key_secret" not in saved
    assert "operator-secret-value" not in json.dumps(profiles, ensure_ascii=False)
    raw = json.loads((root / ".factory" / "harnesses" / "profiles.json").read_text(encoding="utf-8"))
    assert raw["profiles"][0]["api_key_secret"]


def test_foreman_harness_profile_writes_runtime_configuration(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    payload = {
        "role": "foreman",
        "runtime": "OperatorRuntime:claude-code@4",
        "base_url": "https://model.example.test/v1",
        "model": "claude-opus-5-5",
        "api_key": "foreman-secret-value",
        "thinking_effort": "medium",
        "context": 128000,
        "image": "registry.local/operators/claude-code@sha256:abc",
        "network": "egress",
        "session_timeout_s": 600,
        "session_budget_usd": 1,
        "daily_budget_usd": 10,
        "max_turns": 20,
        "max_attempts": 2,
        "backoff_s": 60,
        "mode": "session",
    }
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        profile = model.save_harness_profile(payload)
        foreman = model.foreman()

    factory = json.loads((root / "factory.json").read_text(encoding="utf-8"))
    assert profile["role"] == "foreman"
    assert foreman["configured"] is True
    assert factory["foreman"]["env"]["ANTHROPIC_API_KEY"].startswith("secret://")
    assert "foreman-secret-value" not in json.dumps(factory, ensure_ascii=False)
