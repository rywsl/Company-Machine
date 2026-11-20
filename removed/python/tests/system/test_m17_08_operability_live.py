"""M17-08 四格 live witness。资源缺口由共享执行器按格诊断，不能 skip 或改记 passed。"""

from __future__ import annotations

from pathlib import Path


def test_live_cross_domain_onboarding_and_recovery(tmp_path: Path) -> None:
    from tests.system.m17_08_live import diagnosis, execute_witness

    index = Path(".factory/m17-repro/operability-live/index.json")
    result = execute_witness("four_cell", tmp_path, live=True, report_path=index)
    if result.get("live_passed") is not True:
        raise AssertionError("operability live witness " + diagnosis(result))
