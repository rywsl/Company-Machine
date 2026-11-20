"""YAML 读取：采用 YAML 1.2 core 的标量解析规则，并拒绝重复的键。

PyYAML 默认按 YAML 1.1 解析，会把 on/off/yes/no 当作布尔值、把 2026-11-09 当作日期，
这会悄悄改变 Spec 的含义（例如 Line 的 trigger 写的是 {on: bean}）。这里统一改为：
- 布尔值只有 true/false（及首字母大写、全大写形式）；
- null 只有 ~、null（及大小写变体）和空值；
- 整数只有十进制；浮点数为十进制小数或指数形式；
- 不做时间戳解析；重复的键视为错误。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

import yaml


class YamlError(Exception):
    def __init__(self, message: str, line: int = 0, col: int = 0):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {}
for _tag, _regex, _first in (
    ("tag:yaml.org,2002:null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("tag:yaml.org,2002:bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("tag:yaml.org,2002:int", r"^[-+]?[0-9]+$", list("-+0123456789")),
    (
        "tag:yaml.org,2002:float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
):
    _Loader.add_implicit_resolver(_tag, re.compile(_regex), _first)


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise YamlError(
                f"映射的键必须是字符串，得到 {type(key).__name__}：{key!r}",
                key_node.start_mark.line + 1,
                key_node.start_mark.column + 1,
            )
        if key in result:
            raise YamlError(f"重复的键 {key!r}", key_node.start_mark.line + 1, key_node.start_mark.column + 1)
        result[key] = loader.construct_object(value_node, deep=True)
    return result


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    return int(loader.construct_scalar(node))


def _construct_float(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> float:
    text = str(loader.construct_scalar(node)).lower()
    if text.endswith("inf"):
        return float("-inf") if text.startswith("-") else float("inf")
    if text == ".nan":
        return float("nan")
    return float(text)


_Loader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)
_Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_Loader.add_constructor("tag:yaml.org,2002:float", _construct_float)


@dataclass
class Document:
    index: int  # 文件内的文档序号，从 0 开始
    line: int  # 文档起始行，从 1 开始
    data: Any
    positions: dict[str, tuple[int, int]] = field(default_factory=dict)  # 路径 → (行, 列)


def _positions(node: yaml.Node, path: str, out: dict[str, tuple[int, int]]) -> None:
    """记录每个节点的位置。路径写法与 JSON 路径一致：a.b[0].c；映射的值取其键的位置。"""
    out.setdefault(path, (node.start_mark.line + 1, node.start_mark.column + 1))
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if not isinstance(k, yaml.ScalarNode):
                continue
            child = f"{path}.{k.value}" if path else str(k.value)
            out.setdefault(child, (k.start_mark.line + 1, k.start_mark.column + 1))
            _positions(v, child, out)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _positions(item, f"{path}[{i}]", out)


# 一致性用例对同一份基准文本打补丁后反复解析。缓存原始文档，返回时复制 data，调用方可以改自己的那份。
_DOC_CACHE: dict[str, list[Document]] = {}
_DOC_CACHE_MAX = 4096


def _copy_document(doc: Document) -> Document:
    return Document(doc.index, doc.line, copy.deepcopy(doc.data), dict(doc.positions))


def load_documents(text: str) -> list[Document]:
    """读取多文档 YAML。空文档会被跳过（但序号仍按出现顺序计数）。"""
    cached = _DOC_CACHE.get(text)
    if cached is not None:
        return [_copy_document(doc) for doc in cached]
    docs = _parse_documents(text)
    if len(text) <= 1_000_000:
        if len(_DOC_CACHE) >= _DOC_CACHE_MAX:
            _DOC_CACHE.clear()
        _DOC_CACHE[text] = docs
    return [_copy_document(doc) for doc in docs]


def _parse_documents(text: str) -> list[Document]:
    docs: list[Document] = []
    try:
        loader = _Loader(text)
        try:
            index = 0
            while loader.check_node():
                node = loader.get_node()
                if node is None:
                    break
                positions: dict[str, tuple[int, int]] = {}
                _positions(node, "", positions)
                data = loader.construct_document(node)
                if data is not None:
                    docs.append(Document(index, node.start_mark.line + 1, data, positions))
                index += 1
        finally:
            loader.dispose()
    except YamlError:
        raise
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        line = mark.line + 1 if mark else 0
        col = mark.column + 1 if mark else 0
        raise YamlError(f"{e.problem or e}", line, col) from e
    except yaml.YAMLError as e:
        raise YamlError(str(e)) from e
    return docs


def load_one(text: str) -> Any:
    docs = load_documents(text)
    if len(docs) != 1:
        raise YamlError(f"期望 1 个 YAML 文档，得到 {len(docs)} 个")
    return docs[0].data


def load_file(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return load_one(f.read())
