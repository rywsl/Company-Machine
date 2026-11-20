"""M1-05 验收：factoryctl 与控制通道（spec/system/factoryctl.md 第 7 章）。

factoryctl 用 sys.executable 直接运行仓库里的 src/mao/ctl/factoryctl.py，环境变量指向临时目录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.ctl")

from mao.base import FakeClock  # noqa: E402
from mao.ctl import CtlServer  # noqa: E402
from tests.acceptance.m1.m1_exec_helpers import START  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[3] / "src" / "mao" / "ctl" / "factoryctl.py"
JOB = {
    "job_id": "j-0001",
    "attempt": 1,
    "station": "Station:writer@1",
    "epoch": "e-0123456789abcdef",
    "inputs": {
        "task": {"id": "sha256:" + "1" * 64, "type": "agent.task@1.0", "facts": {}, "meta": {}, "payload": []}
    },
    "params": {"topic": "工厂"},
    "budget_usd": 1.0,
    "timeout_s": 120.0,
}


def _dirs(tmp: Path) -> dict[str, Path]:
    dirs = {name: tmp / name for name in ("in", "out", "ctl", "workspace")}
    for path in dirs.values():
        path.mkdir()
    for sub in ("requests", "responses", "submissions"):
        (dirs["ctl"] / sub).mkdir()
    return dirs


def _env(dirs: dict[str, Path]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        FACTORY_IN=str(dirs["in"]),
        FACTORY_OUT=str(dirs["out"]),
        FACTORY_CTL=str(dirs["ctl"]),
        FACTORY_WORKSPACE=str(dirs["workspace"]),
        PYTHONIOENCODING="utf-8",
    )
    return env


def _ctl(env: dict[str, str], *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class Handler:
    def __init__(self, *, defer: int = 0) -> None:
        self.defer = defer
        self.seen: list[dict] = []

    def handle(self, request):
        self.seen.append(request)
        if self.defer > 0:
            self.defer -= 1
            return None
        return {
            "id": request["id"],
            "ok": True,
            "result": {"verdict": "reject", "feedback": "行数不够"},
            "error": None,
        }


def _request(ctl: Path, rid: str, body: dict | None = None) -> None:
    body = (
        body
        if body is not None
        else {"id": rid, "op": "check", "args": {"port": "note", "path": "/workspace/a.txt"}}
    )
    (ctl / "requests" / f"{rid}.json").write_text(json.dumps(body), encoding="utf-8")


def test_poll_once_writes_response_and_skips_done(tmp_path: Path) -> None:
    ctl = _dirs(tmp_path)["ctl"]
    _request(ctl, "r-0000000a")
    (ctl / "requests" / "r-0000000b.json.part").write_text("{", encoding="utf-8")
    handler = Handler()
    server = CtlServer(ctl, handler, FakeClock(START))
    assert server.poll_once() == 1
    response = _read(ctl / "responses" / "r-0000000a.json")
    assert response == {
        "id": "r-0000000a",
        "ok": True,
        "result": {"verdict": "reject", "feedback": "行数不够"},
        "error": None,
    }
    assert server.poll_once() == 0
    assert len(handler.seen) == 1
    assert handler.seen[0]["op"] == "check"
    assert not (ctl / "responses" / "r-0000000b.json").exists()
    assert not list((ctl / "responses").glob("*.part"))


def test_poll_once_rejects_invalid_request(tmp_path: Path) -> None:
    ctl = _dirs(tmp_path)["ctl"]
    _request(ctl, "r-0000000c", {"id": "r-0000000c", "args": {}})
    _request(ctl, "r-0000000d", {"id": "r-ffffffff", "op": "check", "args": {}})
    handler = Handler()
    assert CtlServer(ctl, handler, FakeClock(START)).poll_once() == 2
    for rid in ("r-0000000c", "r-0000000d"):
        response = _read(ctl / "responses" / f"{rid}.json")
        assert response["ok"] is False
        assert response["error"]["code"] == "CTL_REQUEST_INVALID"
    assert handler.seen == []


def test_poll_once_defers_when_handler_returns_none(tmp_path: Path) -> None:
    ctl = _dirs(tmp_path)["ctl"]
    _request(ctl, "r-0000000e")
    server = CtlServer(ctl, Handler(defer=1), FakeClock(START))
    assert server.poll_once() == 0
    assert not (ctl / "responses" / "r-0000000e.json").exists()
    assert server.poll_once() == 1
    assert (ctl / "responses" / "r-0000000e.json").exists()


def test_factoryctl_harvest_places_single_match(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["workspace"] / "drafts").mkdir()
    (dirs["workspace"] / "drafts" / "a.txt").write_text("a\nb\n", encoding="utf-8")
    harvest = {
        "outputs": {
            "note": {"harvest": "drafts/*.txt", "place_as": "note.txt", "optional": False, "list": False}
        }
    }
    (dirs["ctl"] / "harvest.json").write_text(json.dumps(harvest), encoding="utf-8")
    proc = _ctl(_env(dirs), "harvest")
    assert proc.returncode == 0, proc.stderr
    assert (dirs["out"] / "note" / "note.txt").read_text(encoding="utf-8") == "a\nb\n"
    result = _read(dirs["ctl"] / "harvest-result.json")
    assert result == {
        "ok": True,
        "ports": {"note": {"ok": True, "files": ["note.txt"], "matches": ["drafts/a.txt"]}},
    }


def test_factoryctl_harvest_ambiguous_empty_and_submitted(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["workspace"] / "drafts").mkdir()
    for name in ("a.txt", "b.txt"):
        (dirs["workspace"] / "drafts" / name).write_text(name, encoding="utf-8")
    (dirs["ctl"] / "submissions" / "done.json").write_text("{}", encoding="utf-8")
    harvest = {
        "outputs": {
            "many": {"harvest": "drafts/*.txt", "place_as": "x.txt", "optional": False, "list": False},
            "none": {"harvest": "missing/*.txt", "place_as": "y.txt", "optional": False, "list": False},
            "maybe": {"harvest": "missing/*.txt", "place_as": "z.txt", "optional": True, "list": False},
            "listed": {"harvest": "drafts/*.txt", "place_as": "p.txt", "optional": False, "list": True},
            "done": {"harvest": "drafts/*.txt", "place_as": "d.txt", "optional": False, "list": False},
        }
    }
    (dirs["ctl"] / "harvest.json").write_text(json.dumps(harvest), encoding="utf-8")
    proc = _ctl(_env(dirs), "harvest")
    assert proc.returncode == 0, proc.stderr
    result = _read(dirs["ctl"] / "harvest-result.json")
    assert result["ok"] is False
    assert result["ports"]["many"]["code"] == "HARVEST_AMBIGUOUS"
    assert result["ports"]["none"]["code"] == "HARVEST_EMPTY"
    assert result["ports"]["maybe"] == {"ok": True, "files": [], "matches": []}
    assert result["ports"]["listed"]["files"] == ["0/p.txt", "1/p.txt"]
    assert "done" not in result["ports"]
    assert not (dirs["out"] / "many").exists()
    assert (dirs["out"] / "listed" / "1" / "p.txt").read_text(encoding="utf-8") == "b.txt"


def test_factoryctl_submit_and_inputs(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    env = _env(dirs)
    (dirs["ctl"] / "job.json").write_text(json.dumps(JOB, ensure_ascii=False), encoding="utf-8")
    (dirs["workspace"] / "drafts").mkdir()
    (dirs["workspace"] / "drafts" / "a.txt").write_text("天\n", encoding="utf-8")
    proc = _ctl(env, "submit", "note", "/workspace/drafts/a.txt", "--meta", "title=天")
    assert proc.returncode == 0, proc.stderr
    assert (dirs["out"] / "note" / "a.txt").read_text(encoding="utf-8") == "天\n"
    submission = _read(dirs["ctl"] / "submissions" / "note.json")
    assert submission["port"] == "note"
    assert submission["source"] == "/workspace/drafts/a.txt"
    assert submission["files"] == ["a.txt"]
    assert submission["meta"] == {"title": "天"}
    assert submission["at"].endswith("Z")
    assert _read(dirs["out"] / "note" / "meta.json") == {"title": "天"}

    shown = _ctl(env, "inputs", "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == {"job_id": "j-0001", "attempt": 1, "inputs": JOB["inputs"]}

    assert _ctl(env, "submit", "note", "/workspace/drafts/missing.txt").returncode == 1
    assert _ctl(env, "submit", "note", "/workspace/drafts/a.txt", "--meta", "novalue").returncode == 2
    assert _ctl(env, "receipt", "note").returncode == 2
    assert _ctl(env, "submit", "note", "/workspace/drafts/a.txt", "--label", "k=v").returncode == 2
    budget = _ctl(env, "budget", "--json")
    assert budget.returncode == 0
    assert json.loads(budget.stdout)["spent_usd"] is None


def test_factoryctl_fail_and_log(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    env = _env(dirs)
    assert _ctl(env, "log", "hello {x}").returncode == 0
    assert _ctl(env, "log", "第二行").returncode == 0
    lines = (dirs["ctl"] / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["hello {x}", "第二行"]
    failed = _ctl(env, "fail", "坏了")
    assert failed.returncode == 1
    assert _read(dirs["ctl"] / "fail.json") == {"reason": "坏了"}
    no_ctl = dict(env)
    no_ctl.pop("FACTORY_CTL")
    assert _ctl(no_ctl, "log", "x").returncode == 4
    assert _ctl(env, "contract").returncode == 4


def test_check_roundtrip_with_server(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    contract = {"station": "Station:writer@1", "outputs": {}, "approval": {"timeout_s": 5.0}, "limits": {}}
    (dirs["ctl"] / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
    handler = Handler()
    server = CtlServer(dirs["ctl"], handler, FakeClock(START))
    server.start(0.05)
    try:
        proc = _ctl(_env(dirs), "check", "note", "/workspace/a.txt", timeout=60)
    finally:
        server.stop()
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"verdict": "reject", "feedback": "行数不够"}
    assert [r["op"] for r in handler.seen] == ["check"]
    assert handler.seen[0]["args"] == {"port": "note", "path": "/workspace/a.txt"}
