"""持久预算账本（spec/system/continuity.md 第 3.2、5.4 节）。"""

from __future__ import annotations

from typing import Any

from mao.base import utc_iso


class BudgetLedger:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def balance(self, scope: str) -> dict[str, Any]:
        rows = self.ctx.db.query(
            "SELECT kind, COALESCE(SUM(amount_usd), 0) AS amount FROM budget_entries "
            "WHERE scope = ? GROUP BY kind",
            (scope,),
        )
        amounts = {str(row["kind"]): float(row["amount"]) for row in rows}
        reserved = amounts.get("reserve", 0.0)
        spent = amounts.get("settle", 0.0)
        released = amounts.get("release", 0.0)
        unknown = amounts.get("unknown", 0.0)
        cap = self._cap(scope)
        remaining = None if cap is None else cap - spent - reserved + released
        seq = self.ctx.db.query("SELECT COALESCE(MAX(seq), 0) AS n FROM events")
        return {
            "scope": scope,
            "cap_usd": cap,
            "spent_usd": spent,
            "reserved_usd": max(0.0, reserved - released),
            "remaining_usd": remaining,
            "unknown_cost": unknown > 0,
            "as_of_seq": int(seq[0]["n"]) if seq else 0,
        }

    def _cap(self, scope: str) -> float | None:
        if scope.startswith("job:"):
            job_id = scope.split(":", 1)[1]
            rows = self.ctx.db.query(
                "SELECT cap_usd FROM model_caps WHERE job_id = ?",
                (job_id,),
            )
            if rows and rows[0]["cap_usd"] is not None:
                return float(rows[0]["cap_usd"])
        return None

    def reserve(self, operation_id: str, scope: str, amount_usd: float) -> None:
        self._write(operation_id, scope, "reserve", amount_usd)

    def settle(self, operation_id: str, scope: str, amount_usd: float) -> None:
        self._write(operation_id, scope, "settle", amount_usd)

    def release(self, operation_id: str, scope: str, amount_usd: float) -> None:
        self._write(operation_id, scope, "release", amount_usd)

    def _write(self, operation_id: str, scope: str, kind: str, amount_usd: float) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO budget_entries (id, operation_id, scope, kind, amount_usd, at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(operation_id, scope, kind) DO NOTHING""",
                (self.ctx.ids.new("be"), operation_id, scope, kind, float(amount_usd), now),
            )
