"""M1-03 单元：mao.warehouse。模块缺失时整文件跳过。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.warehouse")
pytest.importorskip("mao.context")
pytest.importorskip("mao.sandbox")

from mao.context import Context  # noqa: E402
from mao.sandbox import LocalProvider  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402


def _ctx(tmp_path):
    root = tmp_path / "factory"
    root.mkdir()
    return Context.open(root, provider=LocalProvider(root))


def test_put_get_roundtrip(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    try:
        blob = tmp_path / "blob"
        blob.write_bytes(b"abc")
        digest = ctx.warehouse.put_file(blob)
        assert ctx.warehouse.blob_path(digest).read_bytes() == b"abc"
    finally:
        ctx.close()


def test_same_bytes_same_digest(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    try:
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.write_bytes(b"same")
        second.write_bytes(b"same")
        assert ctx.warehouse.put_file(first) == ctx.warehouse.put_file(second)
    finally:
        ctx.close()


def test_draft_is_unsealed(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    try:
        draft = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={}))
        assert draft.sealed is False
        stored = ctx.warehouse.get(draft.id)
        assert stored.sealed is False
    finally:
        ctx.close()


def test_missing_digest_is_absent(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    try:
        assert ctx.warehouse.blob_path("sha256:" + "ab" * 32).is_file() is False
    finally:
        ctx.close()


def test_distinct_bytes_distinct_digest(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    try:
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.write_bytes(b"one")
        two.write_bytes(b"two")
        assert ctx.warehouse.put_file(one) != ctx.warehouse.put_file(two)
    finally:
        ctx.close()
