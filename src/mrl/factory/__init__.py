"""MRL 工厂 profile：把 Registry 中的 Spec 抽取为关系事实，用内核规则与宪法检查系统边界。

流水线（spec/mrl/factory-profile.md 第 2 章）：
  读取（E-YAML、E-LAYOUT）→ schema 校验（E-SCHEMA）→ 抽取事实 → 第一遍求值
  → 表达式静态检查（E-EXPR、W-EXPR）→ 第二遍求值 → 诊断
"""

from .check import CheckResult, check_registry

__all__ = ["CheckResult", "check_registry"]
