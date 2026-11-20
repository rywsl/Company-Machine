"""M6-02 验收：从 Registry 构建镜像（spec/system/images.md 第 7 章）。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mao.images")

from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m3.gov_world import commit_change, git, open_world, write_factory_json  # noqa: E402

GOOD = "FROM mao-test/tool:dev\nRUN echo built > /tmp/built.txt\n"
REF_RE = re.compile(r"^mao-local/machines-toolbox-build@sha256:[0-9a-f]{64}$")


def _config(**overrides):
    from mao.images import ImageConfig

    values = {
        "repo": "mao-local",
        "network": "none",
        "timeout_s": 600.0,
        "max_context_mb": 200,
        "max_builds_per_day": 20,
        "allow_unpinned_bases": ("mao-test/tool:dev",),
    }
    values.update(overrides)
    return ImageConfig(**values)


def _builder(w, **overrides):
    from mao.images import ImageBuilder

    return ImageBuilder(w.ctx, w.epochs, _config(**overrides))


def _images_json(w) -> None:
    write_factory_json(w.ctx.root, w.repo)
    config = json.loads((w.ctx.root / "factory.json").read_text(encoding="utf-8"))
    config["images"] = {"allow_unpinned_bases": ["mao-test/tool:dev"]}
    (w.ctx.root / "factory.json").write_text(json.dumps(config), encoding="utf-8")


def test_build_argv_is_fixed(tmp_path: Path) -> None:
    from mao.images import build_argv

    ctx = tmp_path / "ctx"
    argv = build_argv(
        "docker",
        context=ctx,
        tag="mao-local/x:ctx-0123456789ab",
        iidfile=tmp_path / "ib.iid",
        network="none",
        labels={"mao.path": "machines/x/build", "mao.context": "sha256:" + "a" * 64, "mao.commit": "c" * 40},
    )
    assert argv[:6] == ["docker", "build", "--pull=false", "--network", "none", "--iidfile"]
    labels = [argv[i + 1] for i, item in enumerate(argv) if item == "--label"]
    assert labels == ["mao.commit=" + "c" * 40, "mao.context=sha256:" + "a" * 64, "mao.path=machines/x/build"]
    assert argv[-3:] == ["-f", str(ctx / "Dockerfile"), str(ctx)]
    joined = " ".join(argv)
    for forbidden in ("--secret", "--ssh", "--allow", "--build-arg", "host", "--privileged"):
        assert forbidden not in joined


def test_context_digest_stable(tmp_path: Path) -> None:
    from mao.images import context_digest

    a = tmp_path / "a"
    a.mkdir()
    (a / "Dockerfile").write_text(GOOD, encoding="utf-8", newline="\n")
    (a / "run.sh").write_text("echo hi\n", encoding="utf-8", newline="\n")
    first = context_digest(a)
    os.utime(a / "run.sh", (1_000_000, 1_000_000))
    assert context_digest(a) == first
    (a / "run.sh").write_text("echo bye\n", encoding="utf-8", newline="\n")
    assert context_digest(a) != first
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)


def test_unpinned_base_rejected(tmp_path: Path) -> None:
    from mao.images import dockerfile_bases

    assert dockerfile_bases(
        "FROM --platform=linux/amd64 a@sha256:" + "1" * 64 + " AS build\nFROM build\n"
    ) == [
        "a@sha256:" + "1" * 64,
        "build",
    ]
    w = open_world(tmp_path)
    builder = _builder(w)
    for text in ("FROM python:3.12-alpine\n", "ARG BASE=x\nFROM ${BASE}\n"):
        commit = commit_change(w.repo, {"machines/toolbox/build/Dockerfile": text})
        with pytest.raises(FactoryError) as ei:
            builder.build("machines/toolbox/build", commit=commit, by="human:acceptance")
        assert ei.value.code == "IMAGE_CONTEXT_INVALID"
        assert ei.value.detail["reason"] == "unpinned-base"
    assert builder.list() == []
    okay = "FROM scratch AS empty\nFROM mao-test/tool:dev\nFROM empty\nFROM b@sha256:" + "2" * 64 + "\n"
    assert dockerfile_bases(okay) == ["scratch", "mao-test/tool:dev", "empty", "b@sha256:" + "2" * 64]


def test_context_path_rejected(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    builder = _builder(w)
    commit = commit_change(w.repo, {"machines/toolbox/build/notes.txt": "no dockerfile here\n"})
    cases = {
        "../x": "path",
        "/abs/path": "path",
        "constitution": "path",
        "machines/nothing/here": "missing",
        "machines/toolbox/build": "no-dockerfile",
    }
    for path, reason in cases.items():
        with pytest.raises(FactoryError) as ei:
            builder.build(path, commit=commit, by="human:acceptance")
        assert ei.value.code == "IMAGE_CONTEXT_INVALID", path
        assert ei.value.detail["reason"] == reason, path


def test_build_and_reuse(tmp_path: Path) -> None:
    from mao.sandbox import DockerProvider

    w = open_world(tmp_path)
    builder = _builder(w)
    commit = commit_change(w.repo, {"machines/toolbox/build/Dockerfile": GOOD})
    first = builder.build("machines/toolbox/build", commit=commit, by="human:acceptance")
    assert first.state == "ok", first.error
    assert REF_RE.match(first.ref)
    assert first.ref.endswith(first.image_id.removeprefix("sha256:"))
    assert DockerProvider(w.ctx.root).resolve_image(first.ref) == first.image_id
    assert (w.ctx.root / first.log).is_file()
    second = builder.build("machines/toolbox/build", commit=commit, by="human:acceptance")
    assert second.state == "cached"
    assert second.image_id == first.image_id
    assert [item.state for item in builder.list()] == ["ok", "cached"]


def test_build_failure_recorded(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    _images_json(w)
    commit = commit_change(
        w.repo, {"machines/toolbox/build/Dockerfile": "FROM mao-test/tool:dev\nRUN exit 3\n"}
    )
    record = _builder(w).build("machines/toolbox/build", commit=commit, by="human:acceptance")
    assert record.state == "failed"
    assert record.error["code"] == "IMAGE_BUILD_FAILED"
    assert record.error["detail"]["exit_code"] != 0
    assert record.ref is None
    from mao.cli import main

    code = main(
        [
            "--root",
            str(w.ctx.root),
            "--provider",
            "local",
            "image",
            "build",
            "machines/toolbox/build",
            "--commit",
            commit,
        ]
    )
    assert code == 1


def test_daily_limit(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    builder = _builder(w, max_builds_per_day=1)
    first = commit_change(w.repo, {"machines/toolbox/build/Dockerfile": GOOD})
    assert builder.build("machines/toolbox/build", commit=first, by="human:acceptance").state == "ok"
    assert builder.build("machines/toolbox/build", commit=first, by="human:acceptance").state == "cached"
    other = commit_change(w.repo, {"machines/toolbox/build/Dockerfile": GOOD + "RUN echo again\n"})
    with pytest.raises(FactoryError) as ei:
        builder.build("machines/toolbox/build", commit=other, by="human:acceptance")
    assert ei.value.code == "IMAGE_BUILD_LIMITED"


def test_mcp_image_build(tmp_path: Path) -> None:
    from mao.mcp import McpServer, Session

    w = open_world(tmp_path)
    _images_json(w)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(w.repo), str(clone)], check=True, timeout=60)
    (clone / "machines" / "toolbox" / "build").mkdir(parents=True)
    (clone / "machines" / "toolbox" / "build" / "Dockerfile").write_text(GOOD, encoding="utf-8", newline="\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "toolbox build context")
    head = git(clone, "rev-parse", "HEAD")
    session = Session(role="foreman", actor="fake-agent@1", session_id="fs-0001", source_repo=clone)
    server = McpServer(w.ctx, w.epochs, session)
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "image_build",
                "arguments": {"path": "machines/toolbox/build", "commit": head},
            },
        }
    )
    result = reply["result"]
    assert result["isError"] is False, result
    built = result["structuredContent"]["result"]
    assert built["state"] == "ok"
    assert REF_RE.match(built["ref"])
    assert built["commit"] == head
    assert isinstance(built["log_tail"], str)
    reviewer = McpServer(w.ctx, w.epochs, Session(role="reviewer", actor="script@1"))
    reviewer.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    names = [
        tool["name"]
        for tool in reviewer.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    ]
    assert "image_build" not in names
