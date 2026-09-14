#!/usr/bin/env python3
"""课堂演示：Project 2 的 guard.py，用 sqlglot 做 AST 级校验。

不需要任何 API key，纯函数，跑得飞快。
课上先跑这个，学生立刻看懂为什么正则不行。
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop,
             exp.Create, exp.Alter, exp.TruncateTable)

# 只读权限拦不住这些，它们语法上是完全合法的 SELECT
DANGEROUS_FUNCS = {
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "dblink", "dblink_exec", "lo_import", "lo_export", "pg_sleep",
}

ALLOWED_TABLES = {"sku", "style", "bin", "stock_ledger_entry",
                  "wms_inventory", "warehouse", "attribute_value"}


class GuardError(Exception):
    pass


def validate(sql: str, allowed=ALLOWED_TABLES, max_rows: int = 1000) -> str:
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as e:
        raise GuardError(f"解析失败 {e}")

    if len(statements) != 1:
        raise GuardError(f"只允许单条语句，收到 {len(statements)} 条")

    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise GuardError(f"只允许 SELECT，收到 {type(stmt).__name__}")

    # 递归遍历，危险语句会藏在 CTE 和子查询里，只看顶层不够
    for node in stmt.walk():
        if isinstance(node, FORBIDDEN):
            raise GuardError(f"禁止的语句类型 {type(node).__name__}")
        if isinstance(node, exp.Anonymous) and node.name.lower() in DANGEROUS_FUNCS:
            raise GuardError(f"禁止的函数 {node.name}")
        if type(node).__name__.lower() in DANGEROUS_FUNCS:
            raise GuardError(f"禁止的函数 {type(node).__name__}")

    # 从 AST 里提取真实的表引用。字符串里的表名不会被误判
    cte_names = {c.alias_or_name for c in stmt.find_all(exp.CTE)}
    referenced = {t.name for t in stmt.find_all(exp.Table)} - cte_names
    if unknown := referenced - allowed:
        raise GuardError(f"引用了不允许的表 {sorted(unknown)}")

    if not stmt.args.get("limit"):
        stmt = stmt.limit(max_rows)          # 能自动修的就别打断用户

    return stmt.sql(dialect="postgres")


ATTACKS = [
    ("分号堆叠",        "SELECT 1; DROP TABLE sku"),
    ("直球 DROP",       "DROP TABLE sku"),
    ("藏在 CTE 里",     "WITH x AS (DELETE FROM sku RETURNING *) SELECT * FROM x"),
    ("藏在子查询里",     "SELECT * FROM (SELECT * FROM salary_table) t"),
    ("黑名单表",        "SELECT * FROM salary_table"),
    ("系统表",          "SELECT * FROM pg_shadow"),
    ("读服务器文件",     "SELECT pg_read_file('/etc/passwd')"),
    ("列目录",          "SELECT pg_ls_dir('/')"),
    ("外连执行",        "SELECT dblink_exec('dbname=x', 'DROP TABLE sku')"),
    ("拖死连接",        "SELECT pg_sleep(10000)"),
    ("UPDATE",         "UPDATE bin SET actual_qty = 0"),
    ("注释干扰",        "/* SELECT */ DROP TABLE sku"),
]

BENIGN = [
    ("普通查询",        "SELECT sku_id, actual_qty FROM bin WHERE warehouse_id = 'LA01'"),
    ("带 LIMIT",        "SELECT * FROM sku LIMIT 10"),
    ("多表 join",       "SELECT s.style_id, b.actual_qty FROM sku s "
                       "JOIN bin b ON b.sku_id = s.sku_id"),
    ("CTE 聚合",        "WITH t AS (SELECT sku_id, SUM(qty_delta) q "
                       "FROM stock_ledger_entry WHERE is_cancelled = FALSE GROUP BY 1) "
                       "SELECT * FROM t WHERE q > 100"),
    ("窗口函数",        "SELECT sku_id, SUM(qty_delta) OVER (PARTITION BY sku_id "
                       "ORDER BY posting_ts ROWS BETWEEN UNBOUNDED PRECEDING "
                       "AND CURRENT ROW) FROM stock_ledger_entry"),
    ("表名出现在字符串里", "SELECT 'DROP TABLE sku' AS note FROM sku"),
]


def main():
    print("=" * 74)
    print("guard.py  AST 级 SQL 校验")
    print("=" * 74)

    print("\n【攻击样本】必须全部被拦")
    print(f"  {'':<18}{'结果':<6}原因")
    print("  " + "-" * 70)
    blocked = 0
    for name, sql in ATTACKS:
        try:
            validate(sql)
            print(f"  {name:<18}{'放行':<6}<<<< 漏了")
        except GuardError as e:
            blocked += 1
            print(f"  {name:<18}{'拦截':<6}{e}")

    print(f"\n  攻击拦截率  {blocked}/{len(ATTACKS)} = {blocked/len(ATTACKS)*100:.0f}%")

    print("\n【正常查询】必须全部放行，只测攻击不测误杀是评估里最常见的错误")
    print("  " + "-" * 70)
    passed = 0
    for name, sql in BENIGN:
        try:
            out = validate(sql)
            passed += 1
            added = "  [自动加了 LIMIT]" if "LIMIT" not in sql.upper() else ""
            print(f"  {name:<20}放行{added}")
        except GuardError as e:
            print(f"  {name:<20}误杀 <<<< {e}")

    print(f"\n  正常通过率  {passed}/{len(BENIGN)} = {passed/len(BENIGN)*100:.0f}%")

    print("\n" + "=" * 74)
    print("为什么必须 AST 不能正则：")
    print("  正则查 DROP 关键字，下面这条会被误杀，因为 DROP 出现在字符串里")
    print("    SELECT 'DROP TABLE sku' AS note FROM sku")
    print("  而下面这条会被漏过，因为关键字被注释和结构藏起来了")
    print("    WITH x AS (DELETE FROM sku RETURNING *) SELECT * FROM x")
    print("  >> 正则看的是字符串，SQL 的语义由语法结构决定，不在一个层面上。")
    print("\n只读权限的盲区：")
    print("  SELECT pg_read_file('/etc/passwd')  语法完全合法，只读也拦不住")
    print("  >> 所以函数黑名单是必须的一层。")
    print("=" * 74)


if __name__ == "__main__":
    main()
