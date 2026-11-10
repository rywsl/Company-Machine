"""M1-10 验收：端到端（spec/system/testing.md 第 7 章）。

Local 与 Docker 跑同一个夹具工位，facts 与正文必须相同。Docker 测试在 MAO_REQUIRE_DOCKER=1 时不得跳过；
测试镜像不存在时失败并提示 tools/build_test_images.py（不跳过）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.runner")

from mao.jobs import JobState  # noqa: E402
from tests.acceptance.m1.m1_exec_helpers import open_factory, read_payload, seal_note, seal_task  # noqa: E402
from tests.fakes.fakelib import REFERENCE_CONSTITUTION, REGISTRY_M1, docker_image_ids  # noqa: E402

TEXT = "hello\nfactory\n"


def _run_upper(tmp: Path, **kwargs):
    ctx, _snapshot, runner = open_factory(tmp, **kwargs)
    note = seal_note(ctx, tmp, TEXT)
    done = runner.run(runner.submit("upper@1", {"note": note}, {}).id)
    assert done.state == JobState.SEALED, done.error
    [out] = done.outputs["note"]
    return ctx, note, out


def test_local_upper_seals(tmp_path: Path) -> None:
    ctx, _note, out = _run_upper(tmp_path)
    bean = ctx.warehouse.get(out)
    assert bean.sealed is True
    assert bean.type.startswith("note.text@1")
    assert read_payload(ctx, out, "note.txt") == TEXT.upper()
    assert bean.facts == {"chars": len("".join(TEXT.split())), "lines": 2, "has_lower": False}
    assert ctx.warehouse.verify() == []


def test_local_lineage(tmp_path: Path) -> None:
    ctx, note, out = _run_upper(tmp_path)
    down = ctx.warehouse.lineage(note, "down")
    assert down["beans"] == [out]
    assert [job["job_id"] for job in down["jobs"]] == ["j-0001"]
    assert ctx.warehouse.lineage(out, "up")["beans"] == [note]


@pytest.mark.docker
def test_docker_upper_matches_local(tmp_path: Path) -> None:
    from mao.sandbox import DockerProvider

    images = docker_image_ids()
    local_ctx, _n1, local_out = _run_upper(tmp_path / "local")
    root = tmp_path / "docker" / "factory"
    root.mkdir(parents=True)
    docker_ctx, _n2, docker_out = _run_upper(
        tmp_path / "docker", provider=DockerProvider(root), images=images
    )
    assert docker_ctx.warehouse.get(docker_out).facts == local_ctx.warehouse.get(local_out).facts
    assert read_payload(docker_ctx, docker_out, "note.txt") == read_payload(local_ctx, local_out, "note.txt")


@pytest.mark.docker
def test_docker_agent_workspace_harvest(tmp_path: Path) -> None:
    from mao.sandbox import DockerProvider

    images = docker_image_ids()
    root = tmp_path / "factory"
    root.mkdir()
    ctx, _snapshot, runner = open_factory(tmp_path, provider=DockerProvider(root), images=images)
    script = {"cost_usd": 0.1, "default": [{"do": "write", "path": "drafts/a.txt", "text": "a\nb\n"}]}
    task = seal_task(ctx, tmp_path, script)
    done = runner.run(runner.submit("writer@1", {"task": task}, {}).id)
    assert done.state == JobState.SEALED, done.error
    assert done.cost_usd == 0.1
    [out] = done.outputs["note"]
    assert read_payload(ctx, out, "note.txt") == "a\nb\n"


def test_registry_m1_matches_constitution() -> None:
    copy = REGISTRY_M1 / "constitution" / "invariants.mrl"
    assert copy.read_bytes() == REFERENCE_CONSTITUTION.read_bytes()
