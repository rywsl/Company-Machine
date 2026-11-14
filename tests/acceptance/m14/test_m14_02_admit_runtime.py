"""M14-02 验收：admit 在来料检验时求值（spec/system/admit.md 第 5.3–5.6 节）。

物料经 `factory bean import` 入库，facts 由 validator json-facts 实测；单工位经 `Runner.submit` 与 `factory job run`，
产线经 `factory line run`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.acceptance.m14.admit_world import World  # noqa: E402


def _rejected(w: World, station: str, inputs: dict, params: dict | None = None) -> list[dict]:
    from mao.errors import FactoryError

    before = w.jobs()
    with pytest.raises(FactoryError) as info:
        w.submit(station, inputs, params)
    assert info.value.code == "INTAKE_REJECTED"
    assert w.jobs() == before, "拒收时不能建 job"
    return info.value.detail["problems"]


def _admit(index: int, detail: str | None = None) -> dict:
    return {
        "port": "",
        "index": index,
        "reason": "admit",
        "detail": detail or f"admit[{index}]",
        "bean_id": "",
    }


def test_job_run_admits_matching_pair(tmp_path: Path) -> None:
    w = World(tmp_path)
    car, fw = w.car(), w.firmware()
    job = w.cli.json("job", "run", "flash@1", "--input", f"car={car}", "--input", f"firmware={fw}")
    assert job["state"] == "SEALED", job
    [flashed] = job["outputs"]["flashed"]
    facts = w.ctx.warehouse.get(flashed).facts
    assert facts == {"vin": "LVIN0000000000001", "firmware_model": "X1"}


def test_job_run_rejects_mismatch_without_job(tmp_path: Path) -> None:
    w = World(tmp_path)
    car, fw = w.car(model="X1"), w.firmware(model="Y2")
    code, out, _ = w.cli("job", "run", "flash@1", "--input", f"car={car}", "--input", f"firmware={fw}")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "INTAKE_REJECTED"
    assert error["detail"]["problems"] == [_admit(0)]
    assert w.jobs() == 0
    events = w.ctx.db.query("SELECT kind FROM events WHERE kind = 'job.created'")
    assert events == []


def test_all_admit_failures_reported(tmp_path: Path) -> None:
    w = World(tmp_path)
    car, fw = w.car(model="X1", platform="A"), w.firmware(model="Y2", platform="B")
    assert _rejected(w, "flash@1", {"car": car, "firmware": fw}) == [_admit(0), _admit(1)]


def test_admit_eval_error_fails_closed(tmp_path: Path) -> None:
    w = World(tmp_path)
    car, fw = w.car(ecu_count=0), w.firmware()
    assert _rejected(w, "flash@1", {"car": car, "firmware": fw}) == [_admit(2, "admit[2]: MRL-DIV-ZERO")]
    big = w.firmware(size_kb=4096 * 4 + 1)
    assert _rejected(w, "flash@1", {"car": w.car(), "firmware": big}) == [_admit(2)]


def test_port_problems_skip_admit(tmp_path: Path) -> None:
    w = World(tmp_path)
    car = w.car(model="X1")
    unsigned = w.firmware(model="Y2", signed=False)
    [problem] = _rejected(w, "flash@1", {"car": car, "firmware": unsigned})
    assert (problem["port"], problem["reason"]) == ("firmware", "requires")
    other = w.firmware(model="Y2")
    [problem] = _rejected(w, "flash@1", {"car": other, "firmware": other})
    assert (problem["port"], problem["reason"]) == ("car", "type")


def test_admit_reads_params(tmp_path: Path) -> None:
    w = World(tmp_path)
    x1_car, x1_fw = w.car(model="X1"), w.firmware(model="X1")
    job = w.submit("flash-fixed@1", {"car": x1_car, "firmware": x1_fw})
    assert job.params == {"model": "X1"}
    assert _rejected(w, "flash-fixed@1", {"car": x1_car, "firmware": x1_fw}, {"model": "Y2"}) == [
        _admit(0),
        _admit(1),
    ]
    y2_car, y2_fw = w.car(vin="LVIN0000000000002", model="Y2"), w.firmware(model="Y2")
    assert w.submit("flash-fixed@1", {"car": y2_car, "firmware": y2_fw}, {"model": "Y2"}).params == {
        "model": "Y2"
    }


def test_admit_over_list_port(tmp_path: Path) -> None:
    w = World(tmp_path)
    car = w.car(platform="A", ecu_count=2)
    a1, a2 = w.firmware(platform="A"), w.firmware(platform="A", size_kb=1024)
    b1 = w.firmware(platform="B")
    assert w.submit("flash-many@1", {"car": car, "parts": [a1, a2]}).inputs == {
        "car": [car],
        "parts": [a1, a2],
    }
    assert _rejected(w, "flash-many@1", {"car": car, "parts": [a1, b1]}) == [_admit(0)]
    assert _rejected(w, "flash-many@1", {"car": car, "parts": [a1, a2, a1]}) == [_admit(1)]


def test_line_node_fails_on_admit(tmp_path: Path) -> None:
    w = World(tmp_path)
    car = w.car(model="X1")
    good, bad = w.firmware(model="X1"), w.firmware(model="Y2")
    run = w.cli.json(
        "line", "run", "vehicle-flash@1", "--in", f"car={car}", "--in", f"firmware={bad}", expect=1
    )
    assert run["state"] == "FAILED"
    node = w.cli.nodes(run["id"])["flash"]
    assert node["state"] == "FAILED" and node["job_id"] is None
    assert node["error"]["code"] == "INTAKE_REJECTED"
    assert node["error"]["detail"]["problems"] == [_admit(0)]
    assert w.jobs() == 0
    ok = w.cli.json("line", "run", "vehicle-flash@1", "--in", f"car={car}", "--in", f"firmware={good}")
    assert ok["state"] == "SUCCEEDED", ok["error"]
    assert w.cli.nodes(ok["id"])["flash"]["state"] == "DONE"


def test_source_material_joins_existing_bean(tmp_path: Path) -> None:
    w = World(tmp_path)
    car = w.car(model="X1", platform="A")
    run = w.cli.json("line", "run", "vehicle-pull-flash@1", "--in", f"car={car}")
    assert run["state"] == "SUCCEEDED", run["error"]
    nodes = w.cli.nodes(run["id"])
    assert nodes["pull"]["state"] == "DONE" and nodes["flash"]["state"] == "DONE"
    [fw] = nodes["pull"]["outputs"]["firmware"]
    assert w.ctx.warehouse.get(fw).facts["target_model"] == "X1"
    [flashed] = nodes["flash"]["outputs"]["flashed"]
    assert w.ctx.warehouse.get(flashed).facts == {"vin": "LVIN0000000000001", "firmware_model": "X1"}

    other = w.car(vin="LVIN0000000000003", model="X1", platform="B")
    failed = w.cli.json("line", "run", "vehicle-pull-flash@1", "--in", f"car={other}", expect=1)
    assert failed["state"] == "FAILED"
    nodes = w.cli.nodes(failed["id"])
    assert nodes["pull"]["state"] == "DONE"
    assert nodes["flash"]["state"] == "FAILED" and nodes["flash"]["job_id"] is None
    assert nodes["flash"]["error"]["detail"]["problems"] == [_admit(1)]
