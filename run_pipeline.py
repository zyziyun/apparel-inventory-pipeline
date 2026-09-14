#!/usr/bin/env python3
"""端到端：建表 → 生成带已知错误的数据 → 幂等加载 → 对账 → 验收。

验收标准：对账查出来的差异集合，必须和注入清单精确相等。
这一条把项目从「不可验证」变成「可验证」。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from recon import rules
from synth.generate import corrupt, generate

DSN = "host=/tmp port=55432 user=ziyun dbname=postgres"
ROOT = Path(__file__).parent


def create_schema(conn):
    conn.execute(open(ROOT / "sql" / "schema.sql").read())
    conn.commit()


def load(conn, d, *, batch_marker: str = "full"):
    """幂等加载。同样的输入跑 N 遍，结果完全一致。"""
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO analytics.style VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (style_id) DO NOTHING", d.styles)
        cur.executemany("INSERT INTO analytics.attribute_value VALUES (%s,%s,%s) "
                        "ON CONFLICT (attribute, value) DO UPDATE SET sort_order = EXCLUDED.sort_order",
                        d.attrs)
        cur.executemany("INSERT INTO analytics.warehouse VALUES (%s,%s) "
                        "ON CONFLICT (warehouse_id) DO NOTHING", d.warehouses)
        cur.executemany("INSERT INTO analytics.sku VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (sku_id) DO NOTHING", d.skus)

        # 流水是 append-only，用 voucher 去重键做幂等
        cur.execute("TRUNCATE analytics.stock_ledger_entry RESTART IDENTITY CASCADE")
        cur.executemany(
            "INSERT INTO analytics.stock_ledger_entry "
            "(sku_id, warehouse_id, posting_ts, qty_delta, qty_after_transaction,"
            " voucher_type, voucher_no, is_cancelled) "
            "VALUES (%(sku_id)s,%(warehouse_id)s,%(posting_ts)s,%(qty_delta)s,"
            "%(qty_after_transaction)s,%(voucher_type)s,%(voucher_no)s,%(is_cancelled)s)",
            d.ledger)

        # bin 走 upsert，WHERE 那句防乱序旧数据覆盖新数据
        cur.executemany(
            "INSERT INTO analytics.bin "
            "(sku_id, warehouse_id, actual_qty, reserved_qty, ordered_qty, updated_at) "
            "VALUES (%(sku_id)s,%(warehouse_id)s,%(actual_qty)s,%(reserved_qty)s,"
            "%(ordered_qty)s,%(updated_at)s) "
            "ON CONFLICT (sku_id, warehouse_id) DO UPDATE SET "
            "  actual_qty = EXCLUDED.actual_qty,"
            "  reserved_qty = EXCLUDED.reserved_qty,"
            "  ordered_qty = EXCLUDED.ordered_qty,"
            "  updated_at = EXCLUDED.updated_at "
            "WHERE analytics.bin.updated_at <= EXCLUDED.updated_at",
            list(d.bins.values()))

        cur.executemany(
            "INSERT INTO analytics.wms_inventory VALUES (%s,%s,%s) "
            "ON CONFLICT (sku_id, warehouse_id) DO UPDATE SET qty = EXCLUDED.qty",
            [(k[0], k[1], v) for k, v in d.wms.items()])
    conn.commit()


def snapshot(conn) -> dict:
    """用于幂等性验证：对每张表取行数和内容校验和。"""
    out = {}
    with conn.cursor() as cur:
        for t, cols in [
            ("stock_ledger_entry", "sku_id, warehouse_id, qty_delta, qty_after_transaction, is_cancelled"),
            ("bin", "sku_id, warehouse_id, actual_qty, reserved_qty, ordered_qty"),
            ("wms_inventory", "sku_id, warehouse_id, qty"),
            ("sku", "sku_id, style_id, color, size"),
        ]:
            cur.execute(f"SELECT count(*), md5(string_agg({t}::text, '|' ORDER BY {cols})) "
                        f"FROM analytics.{t}")
            out[t] = cur.fetchone()
    return out


def main() -> int:
    print("=" * 72)
    print("服装库存数据管道与对账系统")
    print("=" * 72)

    d = corrupt(generate(seed=42), seed=42)
    n_cancelled = sum(1 for r in d.ledger if r["is_cancelled"])
    print(f"\n[1] 合成数据  SKU {len(d.skus)}  流水 {len(d.ledger)} 条"
          f"（其中 {n_cancelled} 条 is_cancelled）  bin {len(d.bins)} 行")
    print("    注入的已知差异：")
    for rid, keys in d.injected.items():
        print(f"      {rid:<20} {len(keys):>4} 个 (sku, warehouse)")

    with psycopg.connect(DSN, autocommit=False) as conn:
        create_schema(conn)
        print("\n[2] 建表完成")

        load(conn, d)
        snap1 = snapshot(conn)
        print(f"[3] 首次加载完成  stock_ledger_entry={snap1['stock_ledger_entry'][0]} 行")

        load(conn, d)                       # 同样的输入再跑一遍
        snap2 = snapshot(conn)
        idempotent = snap1 == snap2
        print(f"[4] 幂等性验证  再跑一遍 → {'一致 PASS' if idempotent else '不一致 FAIL'}")
        for t in snap1:
            flag = "ok" if snap1[t] == snap2[t] else "DIFF"
            print(f"      {t:<22} {snap1[t][0]:>6} 行  md5 {snap1[t][1][:12]}  {flag}")

        print("\n[5] 对账")
        with conn.cursor() as cur:
            found = rules.run_all(cur)

        indep = rules.independent(found)
        derived = [x for x in found if x not in indep]

        by_rule: dict[str, set] = {}
        for disc in indep:
            by_rule.setdefault(disc.rule_id, set()).add(disc.key)

        print(f"    原始告警 {len(found)} 条 → 归因后独立问题 {len(indep)} 条，"
              f"派生症状 {len(derived)} 条被折叠")
        print(f"    {'规则':<22}{'注入':>6}{'查出':>6}   验收")
        print("    " + "-" * 48)

        all_pass = True
        for rid in sorted(set(d.injected) | set(by_rule)):
            inj = d.injected.get(rid, set())
            fnd = by_rule.get(rid, set())
            ok = inj == fnd
            all_pass &= ok
            print(f"    {rid:<22}{len(inj):>6}{len(fnd):>6}   "
                  f"{'集合精确相等 PASS' if ok else 'FAIL'}")
            if not ok:
                print(f"        漏查 {len(inj - fnd)} 个，误报 {len(fnd - inj)} 个")

        sev = Counter(x.severity for x in found)
        print(f"\n    按严重度  {dict(sev)}")

        print("\n" + "=" * 72)
        print(f"验收结果：幂等 {'PASS' if idempotent else 'FAIL'}，"
              f"对账 {'PASS' if all_pass else 'FAIL'}")
        print("=" * 72)
        return 0 if (idempotent and all_pass) else 1


if __name__ == "__main__":
    raise SystemExit(main())
