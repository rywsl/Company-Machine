"""M16 离线验收夹具；复用既有执行器，实际运行 Gate、脚本和假 CLI。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.acceptance.m13 import agent_world

FILES_TYPE = """apiVersion: mao/v1
kind: Type
metadata: {name: org.files, version: "1.0"}
spec:
  payload:
    - {path: '*', required: false}
    - {path: '*/*', required: false}
    - {path: '*/*/*', required: false}
    - {path: '*/*/*/*', required: false}
    - {path: '*/*/*/*/*', required: false}
    - {path: '*/*/*/*/*/*', required: false}
    - {path: '*/*/*/*/*/*/*', required: false}
    - {path: '*/*/*/*/*/*/*/*', required: false}
  facts: {}
  validators: []
  properties: {}
"""


class World(agent_world.World):
    def __init__(self, tmp: Path, *, enabled: bool = True) -> None:
        original = agent_world.patch_registry

        def patch(repo: Path) -> None:
            original(repo)
            (repo / "types" / "org-files.yaml").write_text(FILES_TYPE, encoding="utf-8")
            scout = repo / "agents" / "scout.yaml"
            scout.write_text(
                scout.read_text(encoding="utf-8")
                .replace("rotate_turns: 3", "rotate_turns: 100")
                .replace("rotate_tokens: 1000", "rotate_tokens: 100000"),
                encoding="utf-8",
            )

        agent_world.patch_registry = patch
        try:
            super().__init__(tmp, foreman={"mode": "resident"})
        finally:
            agent_world.patch_registry = original
        if enabled:
            self.config["assets"] = {"environment": {"toolchain": "acceptance-v1"}}
            self.save_config()
        self._host = None

    def save_config(self) -> None:
        (self.root / "factory.json").write_text(json.dumps(self.config), encoding="utf-8")

    @property
    def assets(self):
        from mao.assets import AssetStore

        return AssetStore(self.ctx)

    @property
    def learning(self):
        from mao.learning import LearningStore

        return LearningStore(self.ctx, self.epochs)

    def host(self, pipeline: Any = None):
        if self._host is None:
            self._host = super().host(pipeline)
        return self._host

    def note(self, text: str = "hello factory\n") -> str:
        """有真实准入报告的输入；不沿用 M13 中零摘要的简化封存。"""
        from mao.base import canonical_json
        from mao.gate import Gate

        folder = self.root / "incoming" / self.ctx.ids.new("in")
        folder.mkdir(parents=True)
        source = folder / "note.txt"
        source.write_text(text, encoding="utf-8")
        report, bean = Gate(
            self.snapshot,
            self.ctx.warehouse,
            self.ctx.provider,
            self.ctx.clock,
            self.ctx.ids,
            workdir=self.root / "gates",
        ).admit_external("note.text@1.0", {"note.txt": source}, {})
        assert report.verdict == "pass" and bean is not None, report
        proof = folder / "report.json"
        proof.write_text(canonical_json(report.to_json()), encoding="utf-8")
        digest = self.ctx.warehouse.put_file(proof)
        self.ctx.warehouse.seal_many(
            [(bean.id, report.facts[bean.id])],
            job_id=None,
            report_digest=digest,
        )
        return bean.id

    def asset(
        self,
        *,
        key: str = "method.notes",
        kind: str = "knowledge",
        subject: str = "station:upper@1",
        beans: list[str] | None = None,
        requires: list[str] | None = None,
        data: dict | None = None,
        evidence: list[dict] | None = None,
        summary: str = "保留原始材料的整理方法",
    ) -> dict:
        if data is None and kind in {"knowledge", "skill", "tool", "workflow"}:
            data = {
                "claim": {
                    "kind": "method",
                    "statement": "按材料完成整理",
                    "limits": "仅适用于约定输入",
                    "revalidate": ["工具版本变化"],
                }
            }
        return self.assets.put(
            {
                "key": key,
                "kind": kind,
                "summary": summary,
                "scope": {"subject": subject},
                "beans": beans or [],
                "requires": requires or [],
                "evidence": evidence or [],
                "data": data or {},
            },
            by="ada",
        )

    def contract(
        self,
        *,
        subject: str = "station:upper@1",
        cases: list[dict] | None = None,
        authorize: bool = True,
        key: str = "contract.notes",
    ) -> dict:
        if cases is None:
            cases = [
                {
                    "name": "normal",
                    "kind": "job",
                    "subject": subject,
                    "tags": ["normal"],
                    "uses_target": True,
                    "assert": 'out.state == "SEALED" && out.outputs.note[0].facts.lines >= 2',
                }
            ]
        result = self.asset(
            key=key, kind="contract", subject=subject, data={"registry": [], "files": [], "cases": cases}
        )
        if authorize:
            self.learning.authorize(result["id"], by="ada")
        return result

    def trial(self, target: dict, contract: dict, purpose: str = "knowledge") -> dict:
        return self.learning.open_trial(target["id"], contract["id"], purpose=purpose, by="ada")

    def job(self, bean: str):
        from mao.runner import open_runner

        runner = open_runner(self.ctx, self.snapshot)
        job = runner.submit("upper@1", {"note": bean}, {})
        return runner.run(job.id)

    def turn(self, text: str = "正常任务", *, agent: str = "scout"):
        self.post(agent, text)
        return self.host().run_once(agent)

    def handoff_document(
        self, *, agent: str = "scout", knowledge: list[str] | None = None, workspace: list[dict] | None = None
    ) -> dict:
        return {
            "key": "handoff." + agent,
            "kind": "handoff",
            "summary": "继续已有工作，先核对回执",
            "scope": {"subject": "agent:" + agent},
            "beans": [],
            "requires": knowledge or [],
            "evidence": [],
            "data": {
                "agent": agent,
                "tasks": [],
                "decisions": [],
                "uncertainties": [],
                "knowledge": knowledge or [],
                "workspace": workspace or [],
                "external": [],
            },
        }

    def handoff(self, **kw: Any) -> dict:
        from mao.assets import capture_handoff

        document = self.handoff_document(**kw)
        return capture_handoff(self.ctx, document["data"]["agent"], document, expected=None, by="ada")

    def takeover_contract(self) -> dict:
        """领域替身用固定输入、动作断言区分四类场景；不只是进程退出码。"""
        from mao.base import canonical_bytes, digest_bytes

        cases = []
        for name, tool in [
            ("normal", "agent_send"),
            ("exception", "ask_human"),
            ("recovery", "remind"),
            ("uncertainty", "ask_human"),
        ]:
            stimulus = digest_bytes(canonical_bytes({"text": name}))
            cases.append(
                {
                    "name": name,
                    "kind": "turn",
                    "subject": "agent:scout",
                    "tags": [name],
                    "uses_target": True,
                    "assert": (
                        f'out.state == "ok" && out.actions[0].tool == "{tool}"'
                        f' && out.message_digests[0] == "{stimulus}"'
                    ),
                }
            )
        return self.contract(subject="agent:scout", cases=cases, key="contract.scout")

    def takeover_work(self) -> dict:
        evidence = {}
        for name, tool, args in [
            ("normal", "agent_send", {"to": "helper", "text": "已整理"}),
            ("exception", "ask_human", {"summary": "输入缺失", "options": ["补充"]}),
            ("recovery", "remind", {"after": "2h", "note": "按交接继续检查"}),
            ("uncertainty", "ask_human", {"summary": "证据不足", "options": ["补证据"]}),
        ]:
            self.script({"default": [{"do": "mcp", "calls": [{"name": tool, "arguments": args}]}]})
            turn = self.turn(name)
            assert turn is not None and turn.state == "ok", turn
            evidence[name] = {"kind": "turn", "id": turn.id}
        return evidence

    def registry_asset(self) -> dict:
        from mao.base import canonical_json
        from mao.gate import Gate

        files = {
            p.relative_to(self.repo).as_posix(): p
            for p in self.repo.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.repo).parts
        }
        report, bean = Gate(
            self.snapshot,
            self.ctx.warehouse,
            self.ctx.provider,
            self.ctx.clock,
            self.ctx.ids,
            workdir=self.root / "gates",
        ).admit_external("org.files@1.0", files, {})
        assert report.verdict == "pass" and bean is not None, report
        proof = self.root / "incoming" / "registry-proof.json"
        proof.parent.mkdir(exist_ok=True)
        proof.write_text(canonical_json(report.to_json()), encoding="utf-8")
        self.ctx.warehouse.seal_many(
            [(bean.id, report.facts[bean.id])], job_id=None, report_digest=self.ctx.warehouse.put_file(proof)
        )
        return self.asset(key="registry.public", kind="registry", subject="factory", beans=[bean.id])

    def resources(self) -> list[dict]:
        import yaml

        images = set()
        secrets = set()

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"image", "volume"} and isinstance(item, str) and "@sha256:" in item:
                        images.add(item)
                    if key == "secret" and isinstance(item, str):
                        secrets.add(item)
                    if key == "secrets" and isinstance(item, list):
                        secrets.update(x for x in item if isinstance(x, str))
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        for path in self.repo.rglob("*.yaml"):
            visit(yaml.safe_load(path.read_text(encoding="utf-8")))
        visit(self.config)
        result = [
            {
                "key": "image-" + str(n),
                "kind": "image",
                "required": True,
                "fingerprint": value,
                "state": None,
                "transfer": "bind",
                "license": "test-fixture",
            }
            for n, value in enumerate(sorted(images))
        ]
        result.extend(
            {
                "key": "secret:" + name,
                "kind": "secret",
                "name": name,
                "required": True,
                "fingerprint": "buyer-provided",
                "state": None,
                "transfer": "bind",
                "license": "buyer-provided",
            }
            for name in sorted(secrets)
        )
        return result

    def factory_asset(
        self, requires: list[str] | None = None, *, resources: list[dict] | None = None
    ) -> dict:
        return self.asset(
            key="factory.notes",
            kind="factory",
            subject="factory",
            requires=requires,
            data={
                "workspaces": {
                    name: {"business": ["notebook"], "ephemeral": []}
                    for name in ["scout", "helper", "foreman"]
                },
                "resources": self.resources() if resources is None else resources,
            },
        )

    def release(self, root: dict, registry: dict, contracts: list[str] | None = None) -> dict:
        from mao.base import canonical_bytes, digest_bytes

        bindings = [
            {"key": r["key"], "kind": r["kind"], "required": r["required"]}
            for r in root["document"]["data"]["resources"]
        ]
        parts = {
            "closure": self.assets.closure([root["id"], registry["id"], *(contracts or [])]),
            "registry": registry["id"],
            "contracts": contracts or [],
            "bindings": bindings,
            "omitted": [],
        }
        return {
            "root": root["id"],
            "registry": registry["id"],
            "contracts": contracts or [],
            "bindings": bindings,
            "omitted": [],
            "review": {
                "closure_digest": digest_bytes(canonical_bytes(parts)),
                "by": "ada",
                "note": "仅分享明示测试材料",
            },
        }

    def buyer_bindings(self, root: dict) -> dict:
        resources = {}
        for resource in root["document"]["data"]["resources"]:
            item = {"kind": resource["kind"], "fingerprint": resource["fingerprint"]}
            if resource["kind"] == "secret":
                name = resource["name"]
                ref = self.tmp / ("buyer-" + name + ".txt")
                ref.write_text("buyer-" + name + "-value-v1", encoding="utf-8")
                item.update({"ref": str(ref), "secret_class": "other", "fingerprint": "buyer-secret-v1"})
            resources[resource["key"]] = item
        return {"resources": resources, "factory_config": self.config}


def error_code(code: str):
    import pytest

    from mao.errors import FactoryError

    return pytest.raises(FactoryError, match=code)
