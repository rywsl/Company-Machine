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


def test_explainer_workarea_is_a_business_aggregate(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        model = WebReadModel(ctx, Snapshot.load(FIXTURE))
        graph = model.workarea_graph("explainer-multilang")

        assert graph is not None
        assert len(graph["nodes"]) == 2
        assert sum(node["type"] == "operator" for node in graph["nodes"]) == 1
        assert sum(node["type"] == "machine" for node in graph["nodes"]) == 1
        assert len(graph["edges"]) == 1
        assert {edge["kind"] for edge in graph["edges"]} == {"aggregate"}
        assert all("binding" not in edge["metadata"] for edge in graph["edges"])
        assert all(node["metadata"]["step_count"] == 6 for node in graph["nodes"])
        assert {item["name"] for item in graph["nodes"][0]["metadata"]["internal_steps"]} == {
            "explainer",
            "dub_en",
            "vertical",
            "to_s3",
            "to_github",
            "to_shorts",
        }
        assert all(
            node["metadata"]["provenance"]["static_source"]
            == "registry Line, Station, Machine and OperatorRuntime"
            for node in graph["nodes"]
        )
        assert graph["metadata"]["projection"] == "web_read_model.aggregate"
        assert graph["metadata"]["foreman"]["graph_generated"] is False
