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
