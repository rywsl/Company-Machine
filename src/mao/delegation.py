"""授权模式：bypass 只改审批路由。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mao.errors import FactoryError

Mode = Literal["standard", "bypass"]
SignoffMode = Literal["auto", "review", "human"]
TIERS = ("T0", "T1", "T2", "T3")
_SIGNOFF = ("auto", "review", "human")
_ROUTES = ("auto", "foreman", "review", "human")


@dataclass(frozen=True)
class AutonomyConfig:
    mode: Mode
    changesets: Mapping[str, SignoffMode]

    @classmethod
    def load(cls, root: Path) -> AutonomyConfig:
        path = Path(root) / "factory.json"
        body: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FactoryError(
                    "AUTONOMY_CONFIG_INVALID", "factory.json 无法解析", field="autonomy"
                ) from exc
            if not isinstance(loaded, dict):
                raise FactoryError("AUTONOMY_CONFIG_INVALID", "factory.json 不是对象", field="autonomy")
            body = loaded
        section = body.get("autonomy")
        if section is None:
            return cls("standard", {tier: "review" for tier in TIERS})
        if not isinstance(section, dict):
            raise FactoryError("AUTONOMY_CONFIG_INVALID", "autonomy 段不合法", field="autonomy")
        extra = sorted(key for key in section if key not in {"mode", "changesets"})
        if extra:
            raise FactoryError("AUTONOMY_CONFIG_INVALID", "autonomy 有未知字段", field=extra[0])
        mode = section.get("mode", "standard")
        if mode not in ("standard", "bypass"):
            raise FactoryError("AUTONOMY_CONFIG_INVALID", "mode 不合法", field="mode")
        raw = section.get("changesets", {})
        if not isinstance(raw, dict):
            raise FactoryError("AUTONOMY_CONFIG_INVALID", "changesets 不是对象", field="changesets")
        changesets: dict[str, SignoffMode] = {}
        for key in sorted(raw):
            value = raw[key]
            if key not in TIERS or value not in _SIGNOFF:
                raise FactoryError("AUTONOMY_CONFIG_INVALID", "签核方式不合法", field=f"changesets.{key}")
            changesets[str(key)] = value
        for tier in TIERS:
            changesets.setdefault(tier, "review")
        return cls(mode, changesets)

    def route(self, route: str) -> str:
        if route not in _ROUTES:
            return route
        if self.mode == "bypass" and route in ("review", "human"):
            return "foreman"
        return route

    def signoff(self, tier: str) -> SignoffMode:
        if tier not in TIERS:
            raise FactoryError("AUTONOMY_CONFIG_INVALID", "未知风险等级", field=tier)
        if self.mode == "bypass":
            return "auto"
        return self.changesets[tier]

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "changesets": {tier: self.signoff(tier) for tier in TIERS},
            "configured": {tier: self.changesets[tier] for tier in TIERS},
        }
