from __future__ import annotations

from pathlib import Path

from mao.context import Context
from mao.sandbox import LocalProvider
from mao.snapshot import Snapshot
from mao.webapp.read_model import WebReadModel

FIXTURE = Path("spec/examples/registry")


def test_graph_projection_keeps_factory_and_workarea_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        factory = model.factory_graph()
        assert factory["scope"] == "factory"
        assert factory["nodes"]
        assert {node["type"] for node in factory["nodes"]} == {"workarea"}
        assert all(node["group"] == "factory" for node in factory["nodes"])

        workarea = model.workarea_graph(factory["nodes"][0]["id"])
        assert workarea is not None
        assert workarea["scope"] == "workarea"
        assert workarea["nodes"]
        assert {node["type"] for node in workarea["nodes"]} <= {"operator", "machine"}
        assert all(node["type"] in {"operator", "machine"} for node in workarea["nodes"])
        node_ids = {node["id"] for node in workarea["nodes"]}
        assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in workarea["edges"])
        assert all(
            edge["kind"] in {"material", "condition", "default", "aggregate"} for edge in workarea["edges"]
        )


def test_graph_does_not_expose_sink_control_or_secret_values(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        graph = model.workarea_graph("explainer-multilang")
        assert graph is not None
        serialized = str(graph).lower()
        assert "sink_toggle" not in serialized
        assert "secret_value" not in serialized
        assert "api_key=" not in serialized
        assert "delivery" in serialized
        assert "foreman" in serialized
