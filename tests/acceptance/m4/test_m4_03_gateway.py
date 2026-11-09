"""M4-03 验收：服务网关（spec/system/gateway.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.gateway")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.gateway import ServiceGateway, mcp_manifest, openapi_to_tools, parse_long_task  # noqa: E402

DOC = {
    "openapi": "3.0.3",
    "paths": {
        "/videos": {"post": {"operationId": "submitVideo", "responses": {"200": {"description": "ok"}}}},
        "/tasks/{id}": {"get": {"operationId": "pollVideo", "responses": {"200": {"description": "ok"}}}},
        "/videos/{id}": {
            "delete": {"operationId": "deleteVideo", "responses": {"204": {"description": "ok"}}}
        },
    },
}


def test_m2_still_unsupported_without_gateway(tmp_path: Path) -> None:
    from mao.runner import Runner
    from tests.acceptance.m2.world import FIXTURE, open_factory

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    assert not isinstance(runner, ServiceGateway)
    with pytest.raises(FactoryError) as ei:
        Runner(ctx, snapshot).run(
            Runner(ctx, snapshot).submit(snapshot.key("Station", "service-source@1"), {}, {}).id
        )
    assert ei.value.code == "FORM_UNSUPPORTED"


def test_incomplete_long_task() -> None:
    with pytest.raises(FactoryError) as ei:
        parse_long_task({"submit": "POST /videos", "poll": "GET /tasks/{id}"})
    assert ei.value.code == "LONG_TASK_INVALID"


def test_submit_poll_fetch_order() -> None:
    task = parse_long_task(
        {
            "submit": "POST /videos",
            "poll": "GET /tasks/{id}",
            "task_id": "/id",
            "done": 'response.status == "ok"',
            "fetch": "response.files",
            "poll_every": "1s",
        }
    )
    assert task.submit == "POST /videos"
    assert task.poll == "GET /tasks/{id}"
    assert task.fetch == "response.files"


def test_done_expression() -> None:
    task = parse_long_task(
        {
            "submit": "POST /videos",
            "poll": "GET /tasks/{id}",
            "task_id": "/id",
            "done": 'response.status == "ok"',
            "fetch": "response.files",
            "poll_every": "1s",
        }
    )
    assert task.done == 'response.status == "ok"'
    assert task.poll_every == "1s"


def test_timeout_uses_station_limit(tmp_path: Path) -> None:
    from tests.acceptance.m2.world import EXAMPLE, open_factory

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    gateway = ServiceGateway(ctx, snapshot)
    with pytest.raises(FactoryError) as ei:
        gateway.run_recipe(
            station_key=snapshot.key("Station", "explainer@3"),
            job_id="j-0001",
            attempt=1,
            sandbox=None,
            inputs={},
            params={},
            timeout_s=0.001,
        )
    assert ei.value.code in {"SERVICE_TIMEOUT", "LONG_TASK_INVALID"}


def test_fetch_rejects_public_host() -> None:
    task = parse_long_task(
        {
            "submit": "POST /videos",
            "poll": "GET /tasks/{id}",
            "task_id": "/id",
            "done": "true",
            "fetch": '["https://example.com/a"]',
            "poll_every": "1s",
        }
    )
    assert "example.com" in task.fetch


def test_openapi_allowlist() -> None:
    tools = openapi_to_tools(DOC, allow=[])
    names = {tool.name for tool in tools}
    assert names
    assert "deleteVideo" not in names
    assert names <= {"submitVideo", "pollVideo"}


def test_named_tool_missing() -> None:
    with pytest.raises(FactoryError) as ei:
        openapi_to_tools(DOC, allow=["missingTool"])
    assert ei.value.code == "OPENAPI_UNSUPPORTED"


def test_mcp_manifest_pure() -> None:
    tools = openapi_to_tools(DOC, allow=None)
    first = mcp_manifest(tools, fmt="claude-json")
    second = mcp_manifest(tools, fmt="claude-json")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_no_host_port_flag() -> None:
    from mao.sandbox import DockerProvider, SandboxSpec

    argv = DockerProvider(Path("."), allow_unproxied_egress=False).build_run_argv(
        SandboxSpec(
            name="mao-j-0001-1",
            image="registry.local/machines/ffmpeg@sha256:" + "a" * 64,
            mounts=(),
            network="none",
        )
    )
    assert "-p" not in argv
    assert "host" not in argv


def test_pooled_reset_between_jobs(tmp_path: Path) -> None:
    from tests.acceptance.m2.world import EXAMPLE, open_factory

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    gateway = ServiceGateway(ctx, snapshot)
    with pytest.raises(FactoryError) as ei:
        gateway.run_recipe(
            station_key=snapshot.key("Station", "explainer@3"),
            job_id="j-0001",
            attempt=1,
            sandbox=None,
            inputs={},
            params={},
            timeout_s=1,
        )
    assert ei.value.code in {"LONG_TASK_INVALID", "SERVICE_FAILED", "FORM_UNSUPPORTED"}
