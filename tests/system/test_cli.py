"""M1-09 单元：mao.cli。模块缺失时整文件跳过。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.cli")

from mao.cli import build_parser, main  # noqa: E402


def test_empty_argv_is_usage() -> None:
    assert main([]) == 2


def test_version_prints_text(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


def test_default_provider_is_docker() -> None:
    args = build_parser().parse_args(["check", "registry"])
    assert args.provider == "docker"


def test_provider_flag_is_local() -> None:
    args = build_parser().parse_args(["check", "registry", "--provider", "local"])
    assert args.provider == "local"


def test_check_invalid_registry_exits_3(tmp_path) -> None:
    (tmp_path / "types").mkdir()
    (tmp_path / "types" / "nope.yaml").write_text("kind: Nope\n", encoding="utf-8")
    assert main(["check", str(tmp_path), "--provider", "local"]) == 3
