"""合成服装 ERP 数据，并故意注入已知的不一致。

设计原则：每一种注入都返回它影响了哪些 key，
这样对账引擎查出来的东西可以和注入清单做精确集合比对。
没有这个，项目就是不可验证的。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

COLORS = ["Black", "White", "Navy", "Heather Grey", "Olive"]
SIZES = [("XS", 1), ("S", 2), ("M", 3), ("L", 4), ("XL", 5), ("XXL", 6)]
STYLES = [
    ("TEE-CREW", "Basic Crewneck Tee", "Tops", "SS26"),
    ("TEE-VNCK", "V-Neck Tee", "Tops", "SS26"),
    ("HOOD-PULL", "Pullover Hoodie", "Outerwear", "FW26"),
    ("SWEAT-CREW", "Crewneck Sweatshirt", "Outerwear", "FW26"),
    ("PANT-SWEAT", "Sweatpant", "Bottoms", "FW26"),
]
WAREHOUSES = [("LA01", "Los Angeles Main"), ("LA02", "Los Angeles Overflow"), ("NY01", "New York")]


@dataclass
class Dataset:
    styles: list = field(default_factory=list)
    attrs: list = field(default_factory=list)
    skus: list = field(default_factory=list)
    warehouses: list = field(default_factory=list)
    ledger: list = field(default_factory=list)   # dict rows
    bins: dict = field(default_factory=dict)     # (sku, wh) -> dict
    wms: dict = field(default_factory=dict)      # (sku, wh) -> Decimal
    injected: dict = field(default_factory=dict) # rule_id -> set of keys


def generate(seed: int = 42, n_movements: int = 4000) -> Dataset:
    rng = random.Random(seed)
    d = Dataset()

    d.styles = list(STYLES)
    d.attrs = [("color", c, i) for i, c in enumerate(COLORS)] + [
        ("size", s, o) for s, o in SIZES
    ]
    d.warehouses = list(WAREHOUSES)

    # SKU = style × color × size 的笛卡尔积，服装行业的特征
    for style_id, *_ in STYLES:
        for color in COLORS:
            for size, _ in SIZES:
                sku_id = f"{style_id}-{color[:3].upper()}-{size}"
                d.skus.append((sku_id, style_id, color, size))

    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    balances: dict[tuple[str, str], Decimal] = {}

    for i in range(n_movements):
        sku = rng.choice(d.skus)[0]
        wh = rng.choice(d.warehouses)[0]
        key = (sku, wh)
        bal = balances.get(key, Decimal(0))

        if bal <= 0 or rng.random() < 0.45:
            delta = Decimal(rng.randint(10, 200))      # 入库
            vtype, vno = "Purchase Receipt", f"PR-{rng.randint(1000,9999)}"
        else:
            delta = -Decimal(rng.randint(1, min(40, int(bal))))  # 出库
            vtype, vno = "Delivery Note", f"DN-{rng.randint(1000,9999)}"

        bal += delta
        balances[key] = bal

        # 故意让一部分流水共享完全相同的时间戳，制造 window frame 的 peer
        ts = t0 + timedelta(minutes=(i // 2) * 7)

        d.ledger.append(dict(
            sku_id=sku, warehouse_id=wh, posting_ts=ts,
            qty_delta=delta, qty_after_transaction=bal,
            voucher_type=vtype, voucher_no=vno, is_cancelled=False,
        ))

    # bin 是流水的物化汇总，正常情况下应该完全一致
    for (sku, wh), bal in balances.items():
        d.bins[(sku, wh)] = dict(
            sku_id=sku, warehouse_id=wh, actual_qty=bal,
            reserved_qty=Decimal(rng.randint(0, max(1, int(bal) // 4))),
            ordered_qty=Decimal(rng.randint(0, 120)),
            updated_at=t0 + timedelta(days=30),
        )
        d.wms[(sku, wh)] = bal          # 第二个系统，先做成一致的

    return d


# ---------------------------------------------------------------- corruptions

def drift_bin_qty(d: Dataset, rng: random.Random, rate: float = 0.03) -> None:
    """汇总表和流水累加对不上。真实 ERP 里最常见的库存问题。"""
    hit = set()
    for key, b in d.bins.items():
        if rng.random() < rate:
            b["actual_qty"] += Decimal(rng.choice([-37, -12, 5, 23, 61]))
            hit.add(key)
    d.injected["BIN_LEDGER_DRIFT"] = hit


def stale_qty_after_transaction(d: Dataset, rng: random.Random, rate: float = 0.015) -> None:
    """流水行上存的结存字段和重算结果不一致。"""
    hit = set()
    for row in d.ledger:
        if rng.random() < rate:
            row["qty_after_transaction"] += Decimal(rng.choice([-9, -3, 7, 15]))
            hit.add((row["sku_id"], row["warehouse_id"]))
    d.injected["LEDGER_SELF_DRIFT"] = hit


def drop_wms_row(d: Dataset, rng: random.Random, rate: float = 0.02) -> None:
    """SKU 在 ERP 里有，在仓库系统里整行丢失。"""
    hit = set()
    for key in list(d.wms):
        if rng.random() < rate:
            del d.wms[key]
            hit.add(key)
    d.injected["MISSING_IN_WMS"] = hit


def wms_qty_mismatch(d: Dataset, rng: random.Random, rate: float = 0.02) -> None:
    """两个系统都有这个 SKU，但数量对不上。"""
    hit = set()
    for key in d.wms:
        if rng.random() < rate:
            d.wms[key] += Decimal(rng.choice([-50, -8, 14, 33]))
            hit.add(key)
    d.injected["WMS_QTY_MISMATCH"] = hit


def soft_deleted_but_real(d: Dataset, rng: random.Random, n: int = 60) -> None:
    """把一批流水标记为已取消。

    这些行不该被任何统计算进去。忘了加 WHERE is_cancelled = FALSE 的查询
    会得到完全错误的数字，而且不会报错。
    """
    idx = rng.sample(range(len(d.ledger)), n)
    for i in idx:
        d.ledger[i]["is_cancelled"] = True
    # 注意：取消的流水不改 bin 也不改 wms，因为业务上它们本来就不算数


def _rebaseline(d: Dataset) -> None:
    """把 qty_after_transaction 和 bin 按「排除已取消流水」重算一遍。

    这一步让数据回到完全自洽的状态，之后注入的每一个差异
    才能和对账查出来的结果做精确集合比对。
    """
    running: dict[tuple[str, str], Decimal] = {}
    for row in d.ledger:
        if row["is_cancelled"]:
            continue
        key = (row["sku_id"], row["warehouse_id"])
        running[key] = running.get(key, Decimal(0)) + row["qty_delta"]
        row["qty_after_transaction"] = running[key]

    for key, b in d.bins.items():
        b["actual_qty"] = running.get(key, Decimal(0))
        d.wms[key] = running.get(key, Decimal(0))


def corrupt(d: Dataset, seed: int = 42) -> Dataset:
    rng = random.Random(seed + 1)
    soft_deleted_but_real(d, rng)   # 先标取消
    _rebaseline(d)                  # 重算到完全自洽
    # 然后注入已知差异，每一条都记录影响了哪些 key
    drift_bin_qty(d, rng)
    stale_qty_after_transaction(d, rng)
    drop_wms_row(d, rng)
    wms_qty_mismatch(d, rng)
    return d
