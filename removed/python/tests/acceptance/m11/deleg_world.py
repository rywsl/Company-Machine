"""M11 验收的夹具（spec/system/delegation.md、inbox.md、capabilities.md 第 7 章）。导入本模块不会加载 mao。

在 tests/fixtures/registry-m2 的副本上增加：
- 机器 deskbox@1（配方都是测试运行时写出的标准库脚本，经 LocalProvider 的命令映射执行）、deskbig@1 与
  deskbad@1（声明沙箱资源）；
- 演示审批路由、向厂长申请、人工协助、出网、按需放行、监控、秘密核验与回执核验的工位和策略。
Registry 是 Git 仓库，fake-agent@1 带 invoke.mcp（同 m5 的 auto_world）。factory.json 带 governance、foreman、
autonomy、inbox、console 段。Runner 由 open_runner 在 active 快照上构造。时钟是从当前时间开始的 FakeClock，
因此命令行进程（真实时钟）看到的审批期限仍在将来。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import json
import shutil
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from tests.acceptance.m3.gov_world import FIXTURE, git
from tests.acceptance.m5.auto_world import PassContracts, PassShadow, _patch_runtimes, foreman_section

IMAGE = "mao-test/tool@sha256:09b42648636a8405354592f2ae8985670dfda5b61377ac6c75e3cebbd22e0500"

_HEAD = """import json
import os
import subprocess
import sys
from pathlib import Path

ctl = Path(os.environ["FACTORY_CTL"])
out = Path(os.environ["FACTORY_OUT"])


def factoryctl(*args):
    done = subprocess.run(
        [sys.executable, os.environ["FACTORYCTL"], *args], capture_output=True, text=True, encoding="utf-8"
    )
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def parse(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def requests():
    return len([p for p in (ctl / "requests").glob("*.json")]) if (ctl / "requests").is_dir() else 0


def emit(body):
    (out / "note").mkdir(parents=True, exist_ok=True)
    (out / "note" / "note.txt").write_text(json.dumps(body, sort_keys=True) + "\\n", encoding="utf-8")
"""

_PNG = """import struct
import zlib


def png(width, height):
    raw = b"".join(b"\\x00" + b"\\x80\\x80\\x80" * width for _ in range(height))

    def chunk(kind, data):
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
"""

_FETCH = """import urllib.error
import urllib.request


def fetch(url, token=None):
    request = urllib.request.Request(url, headers={"X-Token": token} if token else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    try:
        with opener.open(request, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, type(exc).__name__
"""

TOOLS = {
    "desk-request": _HEAD
    + "code, stdout, err = factoryctl('approval', 'request', '--kind', sys.argv[1], '--summary', sys.argv[2],\n"
    + "                               '--cost-usd', sys.argv[3])\n"
    + "emit({'code': code, 'response': parse(stdout)})\n",
    "desk-ask": _HEAD
    + "detail = Path(os.environ['FACTORY_WORKSPACE']) / 'detail.txt'\n"
    + "detail.write_text(sys.argv[3], encoding='utf-8')\n"
    + "code, stdout, err = factoryctl('ask', '--kind', sys.argv[1], '--summary', sys.argv[2],\n"
    + "                               '--detail-file', str(detail))\n"
    + "bad = factoryctl('ask', '--kind', 'Bad Kind', '--summary', 'x')[0]\n"
    + "missing = factoryctl('ask', '--kind', 'help')[0]\n"
    + "emit({'code': code, 'response': parse(stdout), 'bad': bad, 'missing': missing, 'requests': requests()})\n",
    "desk-handoff": _HEAD
    + _PNG
    + "mode = sys.argv[3]\n"
    + "shot = ctl / 'handoff' / 'shot.png'\n"
    + "shot.parent.mkdir(parents=True, exist_ok=True)\n"
    + "if mode == 'png':\n    shot.write_bytes(png(40, 30))\n"
    + "elif mode == 'txt':\n    shot.write_text('not a png', encoding='utf-8')\n"
    + "args = ['handoff', '--kind', sys.argv[1], '--summary', 'solve the captcha', '--answer', sys.argv[2],\n"
    + "        '--hint', 'click the cat']\n"
    + "if mode != 'none':\n    args += ['--screenshot', '/factory/ctl/handoff/shot.png']\n"
    + "code, stdout, err = factoryctl(*args)\n"
    + "missing = factoryctl('handoff', '--kind', 'captcha', '--summary', 'x')[0]\n"
    + "emit({'code': code, 'response': parse(stdout), 'stderr': err, 'missing_answer': missing})\n",
    "desk-fetch": _HEAD
    + _FETCH
    + "results = [[url, *fetch(url)] for url in sys.argv[1].split(',') if url]\n"
    + "emit({'results': results, 'user': os.environ.get('FACTORY_EGRESS_USER'),\n"
    + "      'has_token': bool(os.environ.get('FACTORY_EGRESS_TOKEN'))})\n",
    "desk-check": "import os\nimport sys\n\nsys.exit(0 if os.environ.get(sys.argv[1]) == sys.argv[2] else 1)\n",
    "desk-check-json": "import json\nimport os\nimport sys\n\nvalue = os.environ.get(sys.argv[1], '')\n"
    + "ok = '\\n' not in value and 'cookies' in json.loads(value or '{}')\nsys.exit(0 if ok else 1)\n",
    "desk-check-up": "import json\nimport os\nimport sys\nfrom pathlib import Path\n\n"
    + _FETCH
    + "creds = {'user': os.environ.get('FACTORY_EGRESS_USER'), 'token': os.environ.get('FACTORY_EGRESS_TOKEN'),\n"
    + "         'proxy': os.environ.get('HTTP_PROXY')}\n"
    + "(Path(os.environ['FACTORY_WORKSPACE']) / 'creds.json').write_text(json.dumps(creds), encoding='utf-8')\n"
    + "status, _ = fetch(sys.argv[1], os.environ.get('UP_TOKEN'))\n"
    + "sys.exit(0 if status == 200 else 1)\n",
    "desk-probe": _HEAD
    + "import base64\nimport http.client\nimport urllib.parse\n\n"
    + "proxy = urllib.parse.urlsplit(os.environ['HTTP_PROXY'])\n"
    + "user, token = os.environ['FACTORY_EGRESS_USER'], os.environ['FACTORY_EGRESS_TOKEN']\n\n\n"
    + "def basic(name, password):\n"
    + "    return 'Basic ' + base64.b64encode((name + ':' + password).encode()).decode()\n\n\n"
    + "def ask(headers):\n"
    + "    conn = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=30)\n"
    + "    conn.request('GET', sys.argv[1], headers=headers)\n"
    + "    resp = conn.getresponse()\n"
    + "    text = resp.read().decode('utf-8', 'replace')\n"
    + "    code = parse(text).get('code', '') if text.startswith('{') else text\n"
    + "    return [resp.status, code, resp.getheader('Proxy-Authenticate') or '']\n\n\n"
    + "emit({'ok': ask({'Proxy-Authorization': basic(user, token)}), 'none': ask({}),\n"
    + "      'wrong': ask({'Proxy-Authorization': basic(user, '0' * 32)}), 'header_only': ask({'X-Factory-Job': user}),\n"
    + "      'user': user, 'token': token})\n",
    "desk-upload": _HEAD
    + "code, stdout, err = factoryctl('handoff', '--kind', 'captcha', '--summary', 'login captcha', '--answer', 'text')\n"
    + "if not isinstance(parse(stdout), dict) or parse(stdout).get('decision') != 'answered':\n"
    + "    sys.exit(factoryctl('fail', 'captcha not solved')[0] or 1)\n"
    + "body = {'target': sys.argv[1], 'locator': {'url': sys.argv[1] + '/receipt'}, 'items': []}\n"
    + "(ctl / 'receipt.json.part').write_text(json.dumps(body), encoding='utf-8')\n"
    + "(ctl / 'receipt.json.part').replace(ctl / 'receipt.json')\n",
    "desk-sink-pub": _HEAD
    + "body = {'target': sys.argv[1], 'locator': {'url': sys.argv[1] + '/receipt'}, 'items': []}\n"
    + "(ctl / 'receipt.json.part').write_text(json.dumps(body), encoding='utf-8')\n"
    + "(ctl / 'receipt.json.part').replace(ctl / 'receipt.json')\n",
    "desk-sink-check": "import os\nimport sys\n\n"
    + _FETCH
    + "status, _ = fetch(sys.argv[1], os.environ.get(sys.argv[2]))\n"
    + "sys.exit(0 if status == 200 else 1)\n",
    "notify-ok": "import sys\nfrom pathlib import Path\n\n"
    + "with Path(sys.argv[1]).open('a', encoding='utf-8') as f:\n    f.write(sys.stdin.read() + '\\n')\n",
    "notify-fail": "import sys\n\nsys.stdin.read()\nsys.exit(3)\n",
}

_NOTE_IN = "      inputs:  {note: {type: note.text@1}}\n"
_NOTE_OUT = "      outputs: {note: {type: note.text@1}}\n"

_PARAMS = {
    "request": "{kind: {type: string, default: cost}, summary: {type: string, default: buy assets}, "
    'cost: {type: string, default: "1.0"}}',
    "ask": "{kind: {type: string, default: access}, summary: {type: string, default: need a host}, "
    "detail: {type: string, default: please allow api.example.test}}",
    "handoff": "{kind: {type: string, default: captcha}, answer: {type: string, default: point}, "
    "shot: {type: string, default: png}}",
    "fetch": '{urls: {type: string, default: ""}}',
    "probe": '{target: {type: string, default: ""}}',
}

DESKBOX = f"""# M11 验收的工具型机器：每个配方的命令都是测试运行时写出的标准库脚本。
apiVersion: mao/v1
kind: Machine
metadata: {{name: deskbox, version: 1}}
spec:
  form: tool
  runtime:
    image: {IMAGE}
    instance: per_job
    resources: {{cpus: 1, memory: 256Mi}}
  needs:
    egress: none
    secrets: [DESK_TOKEN]
    secret_checks:
      DESK_TOKEN: {{run: [desk-check, DESK_TOKEN, good-token], timeout_s: 30}}
      JSON_TOKEN: {{run: [desk-check-json, JSON_TOKEN], timeout_s: 30}}
      UP_TOKEN: {{run: [desk-check-up, "@UPSTREAM@/check"], timeout_s: 30}}
  recipes:
    request:
{_NOTE_IN}      params: {_PARAMS["request"]}
{_NOTE_OUT}      run: [desk-request, "{{{{ params.kind }}}}", "{{{{ params.summary }}}}", "{{{{ params.cost }}}}"]
    ask:
{_NOTE_IN}      params: {_PARAMS["ask"]}
{_NOTE_OUT}      run: [desk-ask, "{{{{ params.kind }}}}", "{{{{ params.summary }}}}", "{{{{ params.detail }}}}"]
    handoff:
{_NOTE_IN}      params: {_PARAMS["handoff"]}
{_NOTE_OUT}      run: [desk-handoff, "{{{{ params.kind }}}}", "{{{{ params.answer }}}}", "{{{{ params.shot }}}}"]
    fetch:
{_NOTE_IN}      params: {_PARAMS["fetch"]}
{_NOTE_OUT}      run: [desk-fetch, "{{{{ params.urls }}}}"]
    probe:
{_NOTE_IN}      params: {_PARAMS["probe"]}
{_NOTE_OUT}      run: [desk-probe, "{{{{ params.target }}}}"]
    sink-net:
{_NOTE_IN}      outputs: {{}}
      run: [desk-sink-pub, "@UPSTREAM@"]
    sink-net-check:
{_NOTE_IN}      outputs: {{}}
      run: [desk-sink-check, "@UPSTREAM@/receipt", SINK_TOKEN]
"""


def _sized_machine(name: str, resources: str) -> str:
    return f"""apiVersion: mao/v1
kind: Machine
metadata: {{name: {name}, version: 1}}
spec:
  form: tool
  runtime:
    image: {IMAGE}
    instance: per_job
    resources: {resources}
  needs: {{egress: none}}
  recipes:
    echo:
{_NOTE_IN}{_NOTE_OUT}      run: [upper-text, /factory/in/note/note.txt, /factory/out/note/note.txt]
"""


MACHINES = {
    "deskbox": DESKBOX,
    "deskbig": _sized_machine(
        "deskbig", "{cpus: 1, memory: 256Mi, shm_mb: 512, tmp_mb: 128, pids: 1024, writable: [/home/app]}"
    ),
    "deskbad": _sized_machine("deskbad", "{cpus: 1, memory: 256Mi, pids: 0}"),
    "deskbadw": _sized_machine("deskbadw", "{cpus: 1, memory: 256Mi, writable: [/workspace/x]}"),
}


def _policy(name: str, body: str, applies: str = "transform") -> str:
    return (
        f"apiVersion: mao/v1\nkind: Policy\nmetadata: {{name: {name}, version: 1}}\n"
        f"spec:\n  applies_to: [{applies}]\n{body}"
    )


POLICIES = {
    "desk-none": _policy("desk-none", "  egress: {none: true}\n  secrets: []\n"),
    "desk-web": _policy(
        "desk-web", '  egress: {web_read: [127.0.0.1, "*.example.invalid"]}\n  secrets: []\n'
    ),
    "desk-ask": _policy(
        "desk-ask", "  egress: {web_read: [listed.example.invalid], on_unlisted: ask}\n  secrets: []\n"
    ),
    "desk-handoff": _policy(
        "desk-handoff",
        "  egress: {none: true}\n  secrets: []\n  handoff: {kinds: [captcha], timeout: 5m}\n",
    ),
    "desk-secret": _policy(
        "desk-secret", "  egress: {none: true}\n  secrets: [DESK_TOKEN, OTHER_TOKEN, JSON_TOKEN]\n"
    ),
    "desk-badwild1": _policy("desk-badwild1", '  egress: {web_read: ["*.invalid"]}\n  secrets: []\n'),
    "desk-badwild2": _policy(
        "desk-badwild2", '  egress: {web_read: ["a.*.example.invalid"]}\n  secrets: []\n'
    ),
    "desk-badask": _policy("desk-badask", "  egress: {none: true, on_unlisted: ask}\n  secrets: []\n"),
    "desk-secret-web": _policy(
        "desk-secret-web", "  egress: {web_read: [127.0.0.1]}\n  secrets: [UP_TOKEN]\n"
    ),
    "desk-sink": _policy(
        "desk-sink",
        '  egress: {publish: {hosts: [127.0.0.1], methods: ["*"]}}\n  secrets: [SINK_TOKEN]\n',
        applies="sink",
    ),
}


def _station(
    name: str,
    recipe: str,
    policy: str,
    *,
    machine: str = "deskbox@1",
    role: str = "transform",
    extra: str = "",
    timeout: str = "120s",
) -> str:
    params = f"  params: {_PARAMS[recipe]}\n" if recipe in _PARAMS else ""
    outputs = "{}" if role == "sink" else "{note: {type: note.text@1}}"
    return f"""apiVersion: mao/v1
kind: Station
metadata: {{name: {name}, version: 1}}
spec:
  role: {role}
  machine: {machine}
  recipe: {recipe}
  operator: {{runtime: script@1}}
{params}  inputs:  {{note: {{type: note.text@1}}}}
  outputs: {outputs}
{extra}  limits: {{timeout: {timeout}, budget_usd: 0, retries: {{rejected: 0, failed: 0, mode: fresh}}}}
  policy: {policy}
"""


_FOREMAN_RULE = (
    '  approval:\n    in_run: {auto_if: "request.cost_usd <= 0.5", else: foreman}\n    timeout: 10m\n'
)
_ASK_RULE = (
    "  approval:\n"
    '    in_run: {auto_if: \'request.kind == "egress" && request.summary == "GET 127.0.0.1"\', else: foreman}\n'
    "    timeout: 10m\n"
)


def _monitor(name: str, action: str, when: str = 'request.summary == "GET 127.0.0.1"') -> str:
    return f"  monitors:\n    {name}: {{on: egress, when: '{when}', action: {action}, timeout: 10m}}\n"


STATIONS = {
    "desk-foreman": _station("desk-foreman", "request", "desk-none@1", extra=_FOREMAN_RULE),
    "desk-human": _station(
        "desk-human", "request", "desk-none@1", extra="  approval: {in_run: human, timeout: 10m}\n"
    ),
    "desk-review": _station(
        "desk-review", "request", "desk-none@1", extra="  approval: {in_run: review, timeout: 10m}\n"
    ),
    "desk-ask": _station("desk-ask", "ask", "desk-none@1", extra="  approval: {in_run: auto}\n"),
    "desk-handoff": _station("desk-handoff", "handoff", "desk-handoff@1"),
    "desk-handoff-none": _station("desk-handoff-none", "handoff", "desk-none@1"),
    "desk-fetch": _station("desk-fetch", "fetch", "desk-web@1"),
    "desk-probe": _station("desk-probe", "probe", "desk-web@1"),
    "desk-badwild1": _station("desk-badwild1", "fetch", "desk-badwild1@1"),
    "desk-badwild2": _station("desk-badwild2", "fetch", "desk-badwild2@1"),
    "desk-badask": _station("desk-badask", "fetch", "desk-badask@1"),
    "desk-asknet": _station("desk-asknet", "fetch", "desk-ask@1", extra=_ASK_RULE),
    "desk-asknorule": _station("desk-asknorule", "fetch", "desk-ask@1"),
    "desk-secret": _station("desk-secret", "request", "desk-secret@1"),
    "desk-upcheck": _station("desk-upcheck", "fetch", "desk-secret-web@1"),
    "desk-sink": _station(
        "desk-sink",
        "sink-net",
        "desk-sink@1",
        role="sink",
        extra='  idempotency_key: "{{ inputs.note.id }}"\n  verify_receipt: sink-net-check\n',
    ),
    "desk-monitor-hold": _station(
        "desk-monitor-hold", "fetch", "desk-web@1", extra=_monitor("hold-up", "hold")
    ),
    "desk-monitor-deny": _station(
        "desk-monitor-deny", "fetch", "desk-web@1", extra=_monitor("block", "deny")
    ),
    "desk-monitor-wake": _station("desk-monitor-wake", "fetch", "desk-web@1", extra=_monitor("ping", "wake")),
    "desk-big": _station("desk-big", "echo", "desk-none@1", machine="deskbig@1"),
    "desk-badres": _station("desk-badres", "echo", "desk-none@1", machine="deskbad@1"),
    "desk-badwritable": _station("desk-badwritable", "echo", "desk-none@1", machine="deskbadw@1"),
}


def write_registry(repo: Path, upstream: str) -> None:
    """把本文件的机器、策略、工位写进 repo（不提交）。"""
    for name, text in MACHINES.items():
        path = repo / "machines" / name / "machine.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace("@UPSTREAM@", upstream), encoding="utf-8", newline="\n")
    for name, text in POLICIES.items():
        (repo / "policies" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
    for name, text in STATIONS.items():
        (repo / "stations" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")


def commands(tmp: Path) -> dict[str, list[str]]:
    """LocalProvider 的命令映射：夹具工具、fake agent，加上本文件的脚本。"""
    from tests.fakes.fakelib import local_commands

    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    mapping = local_commands()
    for name, text in TOOLS.items():
        script = tools / (name.replace("-", "_") + ".py")
        script.write_text(text, encoding="utf-8")
        mapping[name] = [sys.executable, str(script)]
    return mapping


def tool_path(tmp: Path, name: str) -> str:
    return str(tmp / "tools" / (name.replace("-", "_") + ".py"))


class Upstream:
    """进程内的明文 HTTP 上游。记录每个请求的方法、路径与请求头。

    tokens：路径 → 要求的 X-Token 值；不匹配时回 403。其他路径一律 200 "upstream-ok"。
    """

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self.hits: list[dict[str, Any]] = []
        self.tokens = dict(tokens or {})
        hits, tokens_ = self.hits, self.tokens

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                hits.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers.items())}
                )
                want = tokens_.get(self.path.split("?", 1)[0])
                ok = want is None or self.headers.get("X-Token") == want
                body = b"upstream-ok" if ok else b"forbidden"
                self.send_response(200 if ok else 403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_HEAD = _reply  # noqa: N815

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Background:
    """后台线程里跑 Runner.run(job_id)。"""

    def __init__(self, runner: Any, job_id: str) -> None:
        self.job_id = job_id
        self.result: dict[str, Any] = {}
        self.thread = threading.Thread(target=self._go, args=(runner,), daemon=True)
        self.thread.start()

    def _go(self, runner: Any) -> None:
        try:
            self.result["job"] = runner.run(self.job_id)
        except BaseException as exc:  # noqa: BLE001 — 交给 join 的调用方
            self.result["error"] = exc

    def join(self, timeout: float = 120.0) -> Any:
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "Runner.run 没有结束"
        if "error" in self.result:
            raise self.result["error"]
        return self.result["job"]


class World:
    def __init__(
        self,
        tmp: Path,
        *,
        autonomy: dict[str, Any] | None = None,
        inbox: dict[str, Any] | None = None,
        console: dict[str, Any] | None = None,
        upstream: str = "http://127.0.0.1:9",
        secrets: dict[str, tuple[str, str]] | None = None,
        script: dict[str, Any] | None = None,
        images: dict[str, Any] | None = None,
        record: bool = False,
    ) -> None:
        from mao.base import CounterIds, FakeClock
        from mao.context import Context
        from mao.governance import EpochStore
        from mao.runner import open_runner
        from mao.sandbox import LocalProvider

        self.tmp = tmp
        self.repo = tmp / "registry"
        shutil.copytree(FIXTURE, self.repo)
        _patch_runtimes(self.repo)
        write_registry(self.repo, upstream)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "checkout", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial registry")
        self.base = git(self.repo, "rev-parse", "HEAD")

        self.mapping = commands(tmp)
        self.script_path = tmp / "agent-script.json"
        self.script_path.write_text(json.dumps(script or {"default": []}), encoding="utf-8")
        self.root = tmp / "factory"
        self.root.mkdir()
        config: dict[str, Any] = {
            "governance": {"registry": str(self.repo), "git_timeout_s": 60},
            "foreman": foreman_section(self.script_path),
        }
        for key, value in (
            ("autonomy", autonomy),
            ("inbox", inbox),
            ("console", console),
            ("images", images),
        ):
            if value is not None:
                config[key] = value
        self.write_config(config)
        for name, (klass, value) in (secrets or {}).items():
            self.put_secret(name, klass, value)

        self.clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
        self.specs: list[Any] = []
        specs = self.specs

        class Recording(LocalProvider):
            def open(self, spec):  # noqa: ANN001, ANN201 — 记录每个 SandboxSpec
                specs.append(spec)
                return super().open(spec)

        provider_cls = Recording if record else LocalProvider
        provider = provider_cls(self.root, commands=self.mapping, clock=self.clock)
        self.ctx = Context.open(self.root, provider=provider, clock=self.clock, ids=CounterIds())
        self.epochs = EpochStore(self.ctx)
        self.epochs.init(self.base, by="human:acceptance")
        self.snapshot = self.epochs.materialize(self.base)
        self.runner = open_runner(self.ctx, self.snapshot)

    # ------------------------------------------------------------ 配置与秘密

    def write_config(self, config: dict[str, Any]) -> None:
        self.config = config
        (self.root / "factory.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def set_config(self, key: str, value: Any) -> None:
        config = dict(self.config)
        config[key] = value
        self.write_config(config)

    def put_secret(self, name: str, klass: str, value: str) -> None:
        folder = self.root / "secrets"
        (folder / "values").mkdir(parents=True, exist_ok=True)
        manifest = folder / "manifest.json"
        body = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        body[name] = klass
        manifest.write_text(json.dumps(body), encoding="utf-8")
        (folder / "values" / name).write_text(value, encoding="utf-8", newline="")

    # ------------------------------------------------------------ 运行

    def key(self, ref: str) -> str:
        return self.snapshot.key("Station", ref)

    def note(self, text: str = "hello factory\n") -> str:
        from mao.warehouse import BeanDraft

        type_id = self.snapshot.type_key("note.text@1").split(":", 1)[1]
        incoming = self.root / "incoming" / self.ctx.ids.new("in")
        incoming.mkdir(parents=True)
        (incoming / "note.txt").write_text(text, encoding="utf-8")
        bean = self.ctx.warehouse.add(
            BeanDraft(type=type_id, meta={}, files={"note.txt": incoming / "note.txt"})
        )
        facts = {
            "chars": len(text),
            "lines": len(text.splitlines()),
            "has_lower": any(c.islower() for c in text),
        }
        self.ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest="sha256:" + "0" * 64)
        return bean.id

    def submit(self, station: str, params: dict[str, Any] | None = None) -> str:
        return self.runner.submit(self.key(station), {"note": self.note()}, params or {}).id

    def run(self, station: str, params: dict[str, Any] | None = None):
        return self.runner.run(self.submit(station, params))

    def start(self, station: str, params: dict[str, Any] | None = None) -> Background:
        return Background(self.runner, self.submit(station, params))

    def output(self, job) -> dict[str, Any]:
        """job 的 note 产物（操作工写的 JSON）。"""
        found = job.outputs["note"]
        bean = self.ctx.warehouse.get(found[0] if isinstance(found, list) else found)
        [ref] = [item for item in bean.payload if item.path.endswith("note.txt")]
        return json.loads(self.ctx.warehouse.blob_path(ref.digest).read_text(encoding="utf-8"))

    def wait(self, sql: str, args: tuple = (), timeout: float = 60.0) -> list[Any]:
        deadline = time.monotonic() + timeout
        while True:
            rows = self.ctx.db.query(sql, args)
            if rows or time.monotonic() > deadline:
                assert rows, f"等待超时：{sql} {args}"
                return rows
            time.sleep(0.05)

    def pending(self, job_id: str) -> dict[str, Any]:
        rows = self.wait("SELECT * FROM approvals WHERE job_id = ? AND status = 'pending'", (job_id,))
        return dict(rows[0])

    def awaiting(self, job_id: str) -> None:
        self.wait("SELECT 1 FROM jobs WHERE id = ? AND state = 'AWAITING_APPROVAL'", (job_id,))

    def job_state(self, job_id: str) -> str:
        return str(self.ctx.db.query("SELECT state FROM jobs WHERE id = ?", (job_id,))[0]["state"])

    def events(self, kind: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT data FROM events WHERE kind = ? ORDER BY seq", (kind,))
        return [json.loads(row["data"]) for row in rows]

    def wakes(self, signal: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT * FROM wakes WHERE signal = ? ORDER BY at, id", (signal,))
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 操作面

    def pipeline(self, shadow: Any = None):
        from mao.governance import ChangePipeline

        fake = PassContracts()
        return ChangePipeline(
            self.ctx,
            self.epochs,
            contracts=lambda snapshot: fake,
            shadow=shadow or PassShadow(),
            rebuild=lambda r, s: r,
        )

    def supervisor(self):
        from mao.supervisor import ForemanSupervisor, SupervisorConfig

        return ForemanSupervisor(
            self.ctx, self.epochs, SupervisorConfig.load(self.root), pipeline=self.pipeline()
        )

    def mcp(self, name: str, arguments: dict[str, Any], *, actor: str = "fake-agent@1") -> tuple[Any, Any]:
        """经 McpServer.handle 调用厂长的工具。返回 (结果, None) 或 (None, 错误对象)。"""
        from mao.mcp import McpServer, Session

        server = McpServer(
            self.ctx, self.epochs, Session(role="foreman", actor=actor), pipeline=self.pipeline()
        )
        init = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        }
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert "result" in reply, reply
        result = reply["result"]
        if result.get("isError"):
            return None, json.loads(result["content"][0]["text"])
        return result["structuredContent"]["result"], None

    def script(self, body: dict[str, Any]) -> None:
        self.script_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def cli(self, *args: str, stdin: str | None = None, actor: str | None = None) -> tuple[int, Any, str]:
        """经 mao.cli.main 操作工厂目录。返回 (退出码, 解析后的 JSON 或原文, stderr)。"""
        import os

        from mao.cli import main

        mapping = self.tmp / "commands.json"
        mapping.write_text(json.dumps(self.mapping), encoding="utf-8")
        base = ["--root", str(self.root), "--provider", "local", "--local-commands", str(mapping), "--json"]
        out, err = io.StringIO(), io.StringIO()
        saved_stdin, saved_actor = sys.stdin, os.environ.get("FACTORY_ACTOR")
        if stdin is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        if actor is not None:
            os.environ["FACTORY_ACTOR"] = actor
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    code = main([*base, *args])
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 2
        finally:
            sys.stdin = saved_stdin
            if actor is not None:
                if saved_actor is None:
                    os.environ.pop("FACTORY_ACTOR", None)
                else:
                    os.environ["FACTORY_ACTOR"] = saved_actor
        text = out.getvalue()
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = text
        return code, body, err.getvalue()

    def error_code(self, body: Any, err: str = "") -> str:
        """--json 下错误是 stdout 上的 {"error": {...}}（commands.md）；否则在 stderr。"""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return str(body["error"].get("code", ""))
        return err


TOKEN = "console-token-" + "x" * 32


class Console:
    """真实的 ConsoleServer（127.0.0.1，系统分配端口），请求经 http.client 发出。"""

    def __init__(self, w: World) -> None:
        from mao.console import ConsoleServer

        self.w = w
        self.server = ConsoleServer(w.ctx, token=TOKEN, epochs=w.epochs, pipeline=w.pipeline())
        self.port = self.server.start(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def csrf(request_id: str) -> str:
        return hmac.new(TOKEN.encode(), ("inbox:" + request_id).encode(), hashlib.sha256).hexdigest()

    def request(
        self, method: str, path: str, form: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {TOKEN}"}
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def close(self) -> None:
        self.server.close()
        self.thread.join(5)
