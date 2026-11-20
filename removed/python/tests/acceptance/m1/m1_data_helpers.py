"""M1 数据面验收测试的共享夹具（受保护；只能由人修改）。

提供一个最小的“笔记”Registry（类型 note / note.text / card，机器 toolbox，工位 shout / split /
merge / stamp / vague），以及配套的 validator 脚本。Registry 必须能通过 `mrl registry check`
（零 error、零 warning），由 test_m1_02_snapshot.py::test_notes_registry_is_clean 守护。

本模块只用标准库，不 import mao，便于在实现之前就被导入。
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONSTITUTION = REPO / "spec" / "mrl" / "constitution" / "invariants.mrl"
EXAMPLE_REGISTRY = REPO / "spec" / "examples" / "registry"

COUNT_TEXT_IMAGE = "registry.local/validators/count-text@sha256:ef2e5482427c73262a46dfca223cefd2ae4f47d1c2bea898696b0e90fd272e16"
TOOLBOX_IMAGE = (
    "registry.local/machines/toolbox@sha256:dd221319b058fd80bf9028981e5d9e892d6102c48eec8b71b97863739a176874"
)

_NOTE_TYPES = """\
# 文本笔记。note@1 是抽象父类型：声明 facts 与属性，没有 validator。
apiVersion: mao/v1
kind: Type
metadata: {name: note, version: "1.0"}
spec:
  facts:
    chars: int
    lines: int
  properties:
    non_empty: "bean.facts.chars > 0"
---
apiVersion: mao/v1
kind: Type
metadata: {name: note.text, version: "1.0"}
spec:
  extends: note@1
  payload:
    - {path: note.txt, media_type: text/plain, required: true}
  validators:
    - name: count-text
      image: %(image)s
      run: [count-text, /bean/payload/note.txt]
      emits: [chars, lines]
      fixtures: {accept: [fixtures/ok.txt], reject: [fixtures/blank.txt]}
  properties:
    short: "bean.facts.chars <= 280"
---
apiVersion: mao/v1
kind: Type
metadata: {name: note.text, version: "1.1"%(lifecycle)s}
spec:
  extends: note@1
  payload:
    - {path: note.txt, media_type: text/plain, required: true}
    - {path: "attachments/*", media_type: text/plain, required: false}
  meta_schema:
    type: object
    properties:
      title: {type: string}
      chars: {type: integer}
    additionalProperties: false
  validators:
    - name: count-text
      image: %(image)s
      run: [count-text, /bean/payload/note.txt]
      emits: [chars, lines]
      fixtures: {accept: [fixtures/ok.txt], reject: [fixtures/blank.txt]}
  properties:
    short: "bean.facts.chars <= 280"
  always: [non_empty]
"""

_CARD_TYPE = """\
# 卡片：只有 meta 与一个 JSON 文件，没有 validator，也没有 facts。
apiVersion: mao/v1
kind: Type
metadata: {name: card, version: "1.0"}
spec:
  payload:
    - {path: card.json, media_type: application/json, required: true}
  meta_schema:
    type: object
    required: [title]
    properties:
      title: {type: string, minLength: 1}
"""

_TOOLBOX = """\
apiVersion: mao/v1
kind: Machine
metadata: {name: toolbox, version: 1}
spec:
  form: tool
  runtime:
    image: %(image)s
    instance: per_job
  needs: {egress: none}
  recipes:
    shout:
      inputs: {note: {type: note.text@1}}
      params:
        suffix: {type: string}
        times: {type: int, default: 1}
        ratio: {type: float}
        weight: {type: num}
        loud: {type: bool}
        lang: {type: string, default: en}
      outputs:
        note: {type: note.text@1}
        extra: {type: note.text@1, optional: true}
      run: [shout, /factory/in/note/note.txt, /factory/out/note/note.txt, "{{ params.suffix }}"]
    split:
      inputs: {note: {type: note.text@1}}
      outputs: {parts: {type: list<note.text@1>}}
      run: [split, /factory/in/note/note.txt, /factory/out/parts]
    merge:
      inputs: {notes: {type: list<note.text@1>}}
      outputs: {note: {type: note.text@1}}
      run: [merge, /factory/in/notes, /factory/out/note/note.txt]
    stamp:
      inputs: {card: {type: card@1}}
      outputs: {card: {type: card@1}}
      run: [stamp, /factory/in/card/card.json, /factory/out/card/card.json]
    vague:
      inputs: {note: {type: note.text@1}}
      outputs: {note: {type: note.text@1}}
      run: [vague]
"""

_RUNTIME = """\
apiVersion: mao/v1
kind: OperatorRuntime
metadata: {name: script, version: 1}
spec: {kind: script}
"""

_POLICY = """\
apiVersion: mao/v1
kind: Policy
metadata: {name: tool-default, version: 1}
spec:
  applies_to: [transform]
  egress: {none: true}
  secrets: []
"""

_STATIONS = {
    "stations/shout.yaml": """\
apiVersion: mao/v1
kind: Station
metadata: {name: shout, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: shout
  operator: {runtime: script@1}
  params:
    suffix: {type: string, default: "!"}
    times: {type: int, default: 2}
    ratio: {type: float, default: 0.5}
    weight: {type: num}
    loud: {type: bool, default: false}
  inputs:
    note: {type: note.text@1, requires: [non_empty]}
  outputs:
    note:
      type: note.text@1
      guarantees: [short]
      accept: ["out.facts.chars >= inputs.note.facts.chars"]
    extra: {type: note.text@1, optional: true}
  approval:
    in_run: {auto_if: "request.cost_usd <= 0.5", else: foreman}
  limits: {timeout: 5m}
  policy: tool-default@1
""",
    "stations/split.yaml": """\
apiVersion: mao/v1
kind: Station
metadata: {name: split, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: split
  operator: {runtime: script@1}
  inputs: {note: {type: note.text@1}}
  outputs:
    parts:
      type: list<note.text@1>
      guarantees: [short]
      accept: ["size(out) >= 2"]
  limits: {timeout: 5m}
  policy: tool-default@1
""",
    "stations/merge.yaml": """\
apiVersion: mao/v1
kind: Station
metadata: {name: merge, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: merge
  operator: {runtime: script@1}
  inputs:
    notes: {type: list<note.text@1>, requires: [non_empty]}
  outputs: {note: {type: note.text@1}}
  limits: {timeout: 5m}
  policy: tool-default@1
""",
    "stations/stamp.yaml": """\
apiVersion: mao/v1
kind: Station
metadata: {name: stamp, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: stamp
  operator: {runtime: script@1}
  inputs: {card: {type: card@1}}
  outputs:
    card:
      type: card@1
      accept: ["out.meta.title != 'forbidden'"]
  limits: {timeout: 5m}
  policy: tool-default@1
""",
    "stations/vague.yaml": """\
apiVersion: mao/v1
kind: Station
metadata: {name: vague, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: vague
  operator: {runtime: script@1}
  inputs: {note: {type: note.text@1}}
  outputs: {note: {type: note@1}}
  limits: {timeout: 5m}
  policy: tool-default@1
""",
}


def notes_registry_files(*, retire_note_text_1_1: bool = False) -> dict[str, str]:
    """笔记 Registry 的全部 Spec 文件（相对路径 → 文本），不含宪法。"""
    lifecycle = ", lifecycle: retired" if retire_note_text_1_1 else ""
    files = {
        "types/note.yaml": _NOTE_TYPES % {"image": COUNT_TEXT_IMAGE, "lifecycle": lifecycle},
        "types/card.yaml": _CARD_TYPE,
        "machines/toolbox/machine.yaml": _TOOLBOX % {"image": TOOLBOX_IMAGE},
        "runtimes/script.yaml": _RUNTIME,
        "policies/tool-default.yaml": _POLICY,
    }
    files.update(_STATIONS)
    return files


def write_registry(dest: Path, files: dict[str, str], *, constitution: bool = True) -> Path:
    """把 Spec 文件写到 dest（LF 换行、UTF-8），并复制参考宪法到 constitution/invariants.mrl。"""
    dest.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    if constitution:
        c = dest / "constitution"
        c.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONSTITUTION, c / "invariants.mrl")
    return dest


def write_notes_registry(dest: Path, *, retire_note_text_1_1: bool = False) -> Path:
    return write_registry(dest, notes_registry_files(retire_note_text_1_1=retire_note_text_1_1))


# count-text validator（gate.md 第 5.4 节的协议）。行为由文件内容中的标记决定，便于构造各种情况：
#   CRASH → 退出码 3（validator 故障）；REJECT → 退出码 1 并给出 reason；BADJSON → 输出不是 JSON；
#   MISSING → 缺少 fact lines；WRONGTYPE → lines 的值是字符串；EXTRA → 多出未声明的 fact。
# 其余情况：chars = 去掉所有空白后的字符数，lines = splitlines() 的行数。
COUNT_TEXT_SOURCE = """\
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
if "CRASH" in text:
    sys.exit(3)
if "REJECT" in text:
    print(json.dumps({"reason": "内容包含 REJECT"}, ensure_ascii=False))
    sys.exit(1)
if "BADJSON" in text:
    print("not json")
    sys.exit(0)
facts = {"chars": len("".join(text.split())), "lines": len(text.splitlines())}
if "MISSING" in text:
    del facts["lines"]
if "WRONGTYPE" in text:
    facts["lines"] = "many"
if "EXTRA" in text:
    facts["bogus"] = 1
print(json.dumps(facts))
"""


def write_count_text(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = dest_dir / "count_text.py"
    p.write_bytes(COUNT_TEXT_SOURCE.encode("utf-8"))
    return p
