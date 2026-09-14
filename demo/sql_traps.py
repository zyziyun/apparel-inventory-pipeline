#!/usr/bin/env python3
"""课堂演示：讲义 1.6 里的每一个坑，用真实数字砸出来。

用法：python demo/sql_traps.py          跑全部
     python demo/sql_traps.py 1        只跑第 1 个
"""
from __future__ import annotations

import os
import sys

import psycopg

DSN = os.getenv("PG_DSN", "host=/tmp port=55432 dbname=postgres")


def title(n, s):
    print(f"\n{'='*72}\n坑 {n}：{s}\n{'='*72}")


def show(cur, sql, label=""):
    cur.execute(sql)
    rows = cur.fetchall()
    cols = [d.name for d in cur.description]
    if label:
        print(f"\n  {label}")
    print("  " + " | ".join(f"{c:>22}" for c in cols))
    print("  " + "-" * (25 * len(cols)))
    for r in rows[:12]:
        print("  " + " | ".join(f"{str(v):>22}" for v in r))
    if len(rows) > 12:
        print(f"  ... 共 {len(rows)} 行")
    return rows


# ---------------------------------------------------------------------------
def trap1(cur):
    title(1, "窗口函数默认 frame 是 RANGE，running balance 会算错")

    print("""
  找一个同一时刻有多条流水的 SKU，对比两种写法。
  默认不写 frame  →  RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  RANGE 模式下 CURRENT ROW 指「所有 ORDER BY 值相等的 peer 行」""")

    cur.execute("""
        SELECT sku_id, warehouse_id, posting_ts
        FROM analytics.stock_ledger_entry
        WHERE is_cancelled = FALSE
        GROUP BY 1,2,3 HAVING count(*) > 1
        ORDER BY count(*) DESC, 1,2,3 LIMIT 1
    """)
    sku, wh, ts = cur.fetchone()
    print(f"\n  样本：{sku} @ {wh}，时刻 {ts} 上有并列流水")

    show(cur, f"""
        SELECT entry_id, qty_delta,
               SUM(qty_delta) OVER (PARTITION BY sku_id, warehouse_id
                                    ORDER BY posting_ts)                AS range_默认,
               SUM(qty_delta) OVER (PARTITION BY sku_id, warehouse_id
                                    ORDER BY posting_ts, entry_id
                                    ROWS BETWEEN UNBOUNDED PRECEDING
                                             AND CURRENT ROW)           AS rows_正确,
               qty_after_transaction                                     AS 表里存的
        FROM analytics.stock_ledger_entry
        WHERE is_cancelled = FALSE AND sku_id = '{sku}' AND warehouse_id = '{wh}'
        ORDER BY posting_ts, entry_id
        LIMIT 8
    """)

    cur.execute(f"""
        WITH x AS (
          SELECT SUM(qty_delta) OVER (PARTITION BY sku_id, warehouse_id
                                      ORDER BY posting_ts) AS a,
                 SUM(qty_delta) OVER (PARTITION BY sku_id, warehouse_id
                                      ORDER BY posting_ts, entry_id
                                      ROWS BETWEEN UNBOUNDED PRECEDING
                                               AND CURRENT ROW) AS b
          FROM analytics.stock_ledger_entry WHERE is_cancelled = FALSE
        ) SELECT count(*) FROM x WHERE a <> b
    """)
    n = cur.fetchone()[0]
    print(f"\n  >> 全表有 {n} 行两种写法结果不同。这个 bug 不报错，")
    print("     只会让你的对账查出一堆假差异。")


# ---------------------------------------------------------------------------
def trap2(cur):
    title(2, "NOT IN 遇到 NULL 返回零行")

    cur.execute("DROP TABLE IF EXISTS demo_wms; "
                "CREATE TEMP TABLE demo_wms AS "
                "SELECT sku_id FROM analytics.wms_inventory LIMIT 200")
    cur.execute("INSERT INTO demo_wms VALUES (NULL)")   # 只掺一个 NULL
    print("\n  构造：一张 201 行的表，其中恰好 1 行是 NULL")

    for name, sql in [
        ("NOT EXISTS", "SELECT count(*) FROM analytics.sku s "
                       "WHERE NOT EXISTS (SELECT 1 FROM demo_wms w WHERE w.sku_id = s.sku_id)"),
        ("LEFT JOIN IS NULL", "SELECT count(*) FROM analytics.sku s "
                              "LEFT JOIN demo_wms w ON w.sku_id = s.sku_id WHERE w.sku_id IS NULL"),
        ("NOT IN", "SELECT count(*) FROM analytics.sku s "
                   "WHERE s.sku_id NOT IN (SELECT sku_id FROM demo_wms)"),
    ]:
        cur.execute(sql)
        n = cur.fetchone()[0]
        flag = "  <<<< 零行，但数据里明明有差异" if n == 0 else ""
        print(f"    {name:<22} → {n:>4} 行{flag}")

    print("\n  原理：x <> NULL 结果是 UNKNOWN，TRUE AND UNKNOWN = UNKNOWN，")
    print("        WHERE 只放行 TRUE，所以条件永远不成立。")
    print("  >> 对账返回零条差异会被读成「数据很干净」，这是最危险的一类 bug。")

    print("\n  再看执行计划的差别：")
    for name, sql in [
        ("NOT EXISTS", "SELECT count(*) FROM analytics.sku s "
                       "WHERE NOT EXISTS (SELECT 1 FROM demo_wms w WHERE w.sku_id = s.sku_id)"),
        ("NOT IN", "SELECT count(*) FROM analytics.sku s "
                   "WHERE s.sku_id NOT IN (SELECT sku_id FROM demo_wms)"),
    ]:
        cur.execute("EXPLAIN " + sql)
        plan = [r[0].strip() for r in cur.fetchall()]
        node = next((p for p in plan if "Join" in p or "SubPlan" in p or "Anti" in p), plan[1])
        print(f"    {name:<22} → {node}")


# ---------------------------------------------------------------------------
def trap3(cur):
    title(3, "ON CONFLICT 不处理同一批次内部的重复键")

    cur.execute("DROP TABLE IF EXISTS demo_dw; "
                "CREATE TEMP TABLE demo_dw (k TEXT PRIMARY KEY, v INT, updated_at TIMESTAMPTZ)")
    dup = [("A", 1, "2026-01-01"), ("A", 2, "2026-01-02"), ("B", 3, "2026-01-01")]
    print("\n  一条 INSERT 里塞两行同样的主键 'A'：")
    try:
        cur.execute("SAVEPOINT sp")
        cur.executemany("INSERT INTO demo_dw VALUES (%s,%s,%s) "
                        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", dup)
        print("    executemany 是逐条执行，不触发。改成单条多值：")
        cur.execute("ROLLBACK TO sp")
        cur.execute("INSERT INTO demo_dw VALUES ('A',1,'2026-01-01'),('A',2,'2026-01-02') "
                    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v")
    except psycopg.errors.CardinalityViolation as e:
        cur.execute("ROLLBACK TO sp")
        print(f"    ERROR: {str(e).splitlines()[0]}")
        print("\n  原理：ON CONFLICT 只处理「新行 vs 表里已有行」，")
        print("        不处理「同一批新行之间」的冲突，因为批内顺序是未定义的。")

    print("\n  解法：入库前用 DISTINCT ON 去重，每个 key 只留最新一条")
    cur.execute("""
        WITH raw(k, v, updated_at) AS (VALUES
            ('A', 1, '2026-01-01'::timestamptz),
            ('A', 2, '2026-01-02'::timestamptz),
            ('B', 3, '2026-01-01'::timestamptz))
        SELECT DISTINCT ON (k) k, v, updated_at
        FROM raw ORDER BY k, updated_at DESC
    """)
    for r in cur.fetchall():
        print(f"    {r}")
    print("  >> DISTINCT ON 是 Postgres 专有语法，取每组第一行，")
    print("     比 ROW_NUMBER() = 1 快，因为不用给每行算窗口值。")


# ---------------------------------------------------------------------------
def trap4(cur):
    title(4, "忘了 WHERE is_cancelled = FALSE，数字全错且不报错")

    cur.execute("SELECT count(*) FROM analytics.stock_ledger_entry WHERE is_cancelled")
    n_cancel = cur.fetchone()[0]
    cur.execute("SELECT SUM(qty_delta) FROM analytics.stock_ledger_entry")
    wrong = cur.fetchone()[0]
    cur.execute("SELECT SUM(qty_delta) FROM analytics.stock_ledger_entry WHERE is_cancelled = FALSE")
    right = cur.fetchone()[0]

    print(f"\n  流水 4000 条，其中 {n_cancel} 条 is_cancelled = TRUE")
    print(f"    忘了加条件  SUM(qty_delta) = {wrong:>12}")
    print(f"    正确        SUM(qty_delta) = {right:>12}")
    print(f"    差额                        {wrong - right:>12}"
          f"   ({abs(wrong-right)/right*100:.2f}%)")
    print("\n  >> 没有报错，没有警告。只有一个静默错误的数字。")
    print("     这就是为什么部分索引要带上这个条件：")
    print("     CREATE INDEX ... WHERE is_cancelled = FALSE")


# ---------------------------------------------------------------------------
def trap5(cur):
    title(5, "EXPLAIN 先看估算行数和实际行数差多少")

    sql = """
        SELECT b.sku_id, b.warehouse_id, b.actual_qty, l.ledger_qty
        FROM analytics.bin b
        LEFT JOIN (SELECT sku_id, warehouse_id, SUM(qty_delta) AS ledger_qty
                   FROM analytics.stock_ledger_entry WHERE is_cancelled = FALSE
                   GROUP BY 1,2) l
          ON b.sku_id = l.sku_id AND b.warehouse_id = l.warehouse_id
        WHERE abs(b.actual_qty - COALESCE(l.ledger_qty,0)) > 0.0001
    """
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
    for line in cur.fetchall():
        print("   " + line[0])

    print("\n  读法：")
    print("    ① rows=估算  vs  actual rows=实际。差几百倍说明统计信息过期，")
    print("       这时候加索引没用，要先 ANALYZE。")
    print("    ② Seq Scan 不等于有问题。小表全表扫比走索引快。")
    print("    ③ FULL OUTER JOIN 在 PG 里只能走 Hash 或 Merge，走不了 Nested Loop。")
    print("    ④ BUFFERS 里出现 Disk: 说明排序溢出，调 work_mem 比加索引管用。")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    traps = {"1": trap1, "2": trap2, "3": trap3, "4": trap4, "5": trap5}
    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            for k, fn in traps.items():
                if only in (None, k):
                    fn(cur)
        conn.rollback()
    print()


if __name__ == "__main__":
    main()
