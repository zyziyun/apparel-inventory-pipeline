"""对账规则。每条规则一个函数，统一返回 Discrepancy 列表。

统一返回格式是关键：加一条新规则不用改报告、改 dashboard、改测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Discrepancy:
    rule_id: str
    severity: str
    entity_key: dict[str, Any]
    expected: Decimal | None
    actual: Decimal | None
    delta: Decimal | None
    detail: str

    @property
    def key(self) -> tuple:
        return (self.entity_key.get("sku_id"), self.entity_key.get("warehouse_id"))


# ---------------------------------------------------------------------------
# 规则一：流水表内部自洽。存的结存 vs 用 window function 重算的结存
#
# 两个必须的写法：
#   ORDER BY posting_ts, entry_id   加唯一列打破 peer
#   ROWS BETWEEN ...                显式写 ROWS，默认的 RANGE 会算错
# ---------------------------------------------------------------------------
LEDGER_SELF_DRIFT = """
WITH computed AS (
    SELECT
        sku_id, warehouse_id, entry_id, posting_ts,
        qty_after_transaction AS stored_balance,
        SUM(qty_delta) OVER (
            PARTITION BY sku_id, warehouse_id
            ORDER BY posting_ts, entry_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS computed_balance
    FROM analytics.stock_ledger_entry
    WHERE is_cancelled = FALSE
)
SELECT sku_id, warehouse_id, min(entry_id) AS first_bad_entry,
       count(*) AS bad_rows,
       max(abs(stored_balance - computed_balance)) AS max_drift
FROM computed
WHERE abs(stored_balance - computed_balance) > 0.0001
GROUP BY sku_id, warehouse_id
ORDER BY sku_id, warehouse_id;
"""

# ---------------------------------------------------------------------------
# 规则二：汇总表 vs 流水表。bin 是 ledger 的物化聚合，冗余就会漂移
# ---------------------------------------------------------------------------
BIN_LEDGER_DRIFT = """
SELECT
    b.sku_id, b.warehouse_id,
    b.actual_qty                             AS bin_qty,
    COALESCE(l.ledger_qty, 0)                AS ledger_qty,
    b.actual_qty - COALESCE(l.ledger_qty, 0) AS drift
FROM analytics.bin b
LEFT JOIN (
    SELECT sku_id, warehouse_id, SUM(qty_delta) AS ledger_qty
    FROM analytics.stock_ledger_entry
    WHERE is_cancelled = FALSE
    GROUP BY sku_id, warehouse_id
) l ON b.sku_id = l.sku_id AND b.warehouse_id = l.warehouse_id
WHERE abs(b.actual_qty - COALESCE(l.ledger_qty, 0)) > 0.0001
ORDER BY 1, 2;
"""

# ---------------------------------------------------------------------------
# 规则三：跨系统对账。FULL OUTER JOIN + WHERE IS NULL，一次查出三类差异
# ---------------------------------------------------------------------------
CROSS_SYSTEM = """
SELECT
    COALESCE(b.sku_id, w.sku_id)             AS sku_id,
    COALESCE(b.warehouse_id, w.warehouse_id) AS warehouse_id,
    b.actual_qty                             AS erp_qty,
    w.qty                                    AS wms_qty,
    CASE
        WHEN w.sku_id IS NULL          THEN 'MISSING_IN_WMS'
        WHEN b.sku_id IS NULL          THEN 'MISSING_IN_ERP'
        ELSE                                'WMS_QTY_MISMATCH'
    END                                      AS discrepancy_type,
    COALESCE(b.actual_qty, 0) - COALESCE(w.qty, 0) AS delta
FROM analytics.bin b
FULL OUTER JOIN analytics.wms_inventory w
  ON b.sku_id = w.sku_id AND b.warehouse_id = w.warehouse_id
WHERE w.sku_id IS NULL
   OR b.sku_id IS NULL
   OR abs(b.actual_qty - w.qty) > 0.0001
ORDER BY 1, 2;
"""


def run_all(cur) -> list[Discrepancy]:
    out: list[Discrepancy] = []

    cur.execute(LEDGER_SELF_DRIFT)
    for sku, wh, first_bad, bad_rows, max_drift in cur.fetchall():
        out.append(Discrepancy(
            "LEDGER_SELF_DRIFT", "critical", {"sku_id": sku, "warehouse_id": wh},
            None, None, max_drift,
            f"{bad_rows} 行存储结存与重算不符，首个 entry_id={first_bad}",
        ))

    cur.execute(BIN_LEDGER_DRIFT)
    for sku, wh, bin_qty, ledger_qty, drift in cur.fetchall():
        out.append(Discrepancy(
            "BIN_LEDGER_DRIFT", "critical", {"sku_id": sku, "warehouse_id": wh},
            ledger_qty, bin_qty, drift, "汇总表与流水累加不符",
        ))

    cur.execute(CROSS_SYSTEM)
    for sku, wh, erp, wms, dtype, delta in cur.fetchall():
        out.append(Discrepancy(
            dtype, "warning" if dtype == "WMS_QTY_MISMATCH" else "critical",
            {"sku_id": sku, "warehouse_id": wh}, erp, wms, delta, "跨系统对账",
        ))

    return attribute_root_cause(out)


# ---------------------------------------------------------------------------
# 根因归因
#
# 跑起来才发现的问题：一个根因会在多条规则上同时报警。
# bin.actual_qty 被改坏之后，BIN_LEDGER_DRIFT 会报，
# 跨系统对账拿 bin 和 wms 比也会报，同一个问题被计了两次。
#
# 没有归因，业务方看到的差异数会被系统性放大，
# 然后他们会不再相信这份报告。这是对账系统最常见的死法。
#
# 规则的优先级 = 离根因有多近。
#   LEDGER_SELF_DRIFT  流水自己就不自洽，最底层
#   BIN_LEDGER_DRIFT   汇总表偏离流水
#   跨系统的三条        最表层，它依赖上面两层的输入
# ---------------------------------------------------------------------------
ROOT_PRECEDENCE = ["LEDGER_SELF_DRIFT", "BIN_LEDGER_DRIFT"]
DERIVED_RULES = {"WMS_QTY_MISMATCH"}   # 数量不符可能是 bin 侧的锅
                                       # MISSING_IN_WMS / MISSING_IN_ERP 是整行缺失，
                                       # 和 bin 数量错不是同一回事，不做抑制


def attribute_root_cause(discs: list[Discrepancy]) -> list[Discrepancy]:
    roots: dict[tuple, str] = {}
    for d in discs:
        if d.rule_id in ROOT_PRECEDENCE and d.key not in roots:
            roots[d.key] = d.rule_id

    out = []
    for d in discs:
        if d.rule_id in DERIVED_RULES and d.key in roots:
            out.append(Discrepancy(
                d.rule_id, "info", d.entity_key, d.expected, d.actual, d.delta,
                f"派生症状，根因是 {roots[d.key]}",
            ))
        else:
            out.append(d)
    return out


def independent(discs: list[Discrepancy]) -> list[Discrepancy]:
    """只保留独立问题，供报告给业务方时用。"""
    return [d for d in discs if not d.detail.startswith("派生症状")]
