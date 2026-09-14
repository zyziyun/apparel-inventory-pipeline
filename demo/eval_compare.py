#!/usr/bin/env python3
"""课堂演示：为什么 text-to-SQL 的 eval 不能 df1.equals(df2)。

抄的是 defog-ai/sql-eval 的 eval/eval.py：
  normalize_table()        比对前归一化
  compare_query_results()  返回两个布尔值，不是一个
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import psycopg

DSN = "host=/tmp port=55432 user=ziyun dbname=postgres"


@dataclass
class Result:
    exact_match: bool
    subset_match: bool

    def __str__(self):
        if self.exact_match:
            return "完全匹配"
        if self.subset_match:
            return "子集匹配，仍算对"
        return "不匹配"


def normalize(df: pd.DataFrame, order_matters: bool = False) -> pd.DataFrame:
    df = df.copy()
    df = df.loc[:, sorted(df.columns)]          # 列按名字重排
    df = df.drop_duplicates()                   # 去重行
    if not order_matters:                       # 行顺序只在问题要求排序时才算数
        df = df.sort_values(by=list(df.columns)).reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    return df


def compare(gold: pd.DataFrame, gen: pd.DataFrame, order_matters: bool = False) -> Result:
    g, p = normalize(gold, order_matters), normalize(gen, order_matters)

    if g.shape == p.shape and (g.columns == p.columns).all() and g.equals(p):
        return Result(True, True)

    # 标准答案是生成结果的子集：多 SELECT 了列，业务上仍然是正确回答
    if set(g.columns).issubset(set(p.columns)):
        sub = normalize(p[list(g.columns)], order_matters)
        if sub.equals(g):
            return Result(False, True)

    return Result(False, False)


CASES = [
    ("列顺序不同",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"qty": [10, 20], "sku_id": ["A", "B"]}), False),

    ("行顺序不同，问题没要求排序",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"sku_id": ["B", "A"], "qty": [20, 10]}), False),

    ("行顺序不同，但问题要求了排序",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"sku_id": ["B", "A"], "qty": [20, 10]}), True),

    ("多返回了一列",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20], "color": ["Black", "White"]}), False),

    ("少返回了一列",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"sku_id": ["A", "B"]}), False),

    ("数值真的不同",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 99]}), False),

    ("生成结果有重复行",
     pd.DataFrame({"sku_id": ["A", "B"], "qty": [10, 20]}),
     pd.DataFrame({"sku_id": ["A", "A", "B"], "qty": [10, 10, 20]}), False),
]


def main():
    print("=" * 76)
    print("eval 的结果比对：为什么 df1.equals(df2) 会把对的判成错")
    print("=" * 76)
    print(f"\n  {'场景':<28}{'裸 equals':<12}{'归一化后':<22}")
    print("  " + "-" * 66)

    naive_wrong = 0
    for name, gold, gen, om in CASES:
        naive = gold.equals(gen)
        r = compare(gold, gen, om)
        correct = r.exact_match or r.subset_match
        if naive != correct:
            naive_wrong += 1
        flag = "  <<<< 裸比对判错了" if naive != correct else ""
        print(f"  {name:<28}{str(naive):<12}{str(r):<22}{flag}")

    print(f"\n  7 个场景里，裸 equals 判错了 {naive_wrong} 个。")

    print("\n" + "=" * 76)
    print("同一个问题的两种正确写法，用真实数据库验证")
    print("=" * 76)

    q_a = """SELECT b.warehouse_id, SUM(b.actual_qty) AS total
             FROM analytics.bin b GROUP BY b.warehouse_id ORDER BY 1"""
    q_b = """WITH t AS (SELECT warehouse_id, actual_qty FROM analytics.bin)
             SELECT warehouse_id, SUM(actual_qty) AS total
             FROM t GROUP BY warehouse_id ORDER BY warehouse_id"""

    with psycopg.connect(DSN) as conn:
        a = pd.read_sql(q_a, conn)
        b = pd.read_sql(q_b, conn)

    print("\n  写法 A：直接 GROUP BY")
    print("  写法 B：套一层 CTE 再 GROUP BY")
    print(f"\n  SQL 文本相同吗        {q_a.strip() == q_b.strip()}")
    print(f"  结果集判定            {compare(a, b)}")
    print("\n  >> 比 SQL 文本会把这两条判成不同，而它们业务上完全等价。")
    print("     所以必须比结果，不能比文本。")

    print("\n" + "=" * 76)
    print("关键设计：compare 返回两个布尔值，不是一个")
    print("  exact_match   完全一致")
    print("  subset_match  标准答案是生成结果的子集，多几列业务上仍然算对")
    print("")
    print("  一刀切判错会低估模型，让你把改坏当成改好。")
    print("  而一个错的评估器，比没有评估器更糟。")
    print("=" * 76)


if __name__ == "__main__":
    main()
