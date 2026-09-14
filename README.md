# 服装库存数据管道与对账系统，教学可跑版

> **这是教学参考实现，不要 fork 当作品集。**
> 自己重写一遍，每一行都要能解释为什么。整包 fork 然后说是自己做的，面试官问两层就穿。


一个用来讲数据工程的参考实现。每一个论断都有一条能跑出真实数字的命令：
窗口函数的默认 frame 会怎么算错、`NOT IN` 为什么返回零行、
对账报告为什么会把差异数虚高。

数据全部合成，不含任何真实业务数据。

## 首次准备

```bash
python3 -m venv .venv
./.venv/bin/pip install "psycopg[binary]" sqlglot faker pandas
make reference   # 下载开源参考文件
```

需要本机有 Postgres 14 以上。`make up` 会用 `initdb` 在 `./pgdata`
建一个独立集群跑在 55432，**不碰系统上已有的服务**。

## 课上怎么跑

```bash
make run      # 端到端，看验收表
make traps    # 5 个 SQL 坑，每个都有真实数字
make guard    # AST 级 SQL 校验，12 条攻击
make eval     # 结果比对，为什么不能 df1.equals(df2)
```

`make up` 会在端口 55432 起一个独立的 Postgres 集群，数据目录就在
`./pgdata`，**不碰你系统上的任何服务**。讲完 `make down` 停掉。

## 跑出来的真实数字

### `make run`

```
合成 150 SKU，4000 条流水（60 条 is_cancelled），450 行 bin

幂等性      同样输入跑两遍，4 张表 md5 完全一致        PASS

对账        原始告警 83 条 → 归因后独立问题 66 条
                            派生症状 17 条被折叠

  规则                    注入    查出    验收
  BIN_LEDGER_DRIFT         17     17    集合精确相等
  LEDGER_SELF_DRIFT        42     42    集合精确相等
  MISSING_IN_WMS            2      2    集合精确相等
  WMS_QTY_MISMATCH          5      5    集合精确相等
```

**验收标准是集合精确相等，不是数量相等。** 数量对上但找错了对象也是 bug。

### `make traps`

| 坑 | 跑出来的事实 |
|---|---|
| 窗口函数默认 frame 是 `RANGE` | entry 3187 上 `RANGE` 给 206，`ROWS` 给 211。全表 8 行两种写法结果不同 |
| `NOT IN` 遇到 NULL | `NOT EXISTS` 查出 31 行，`NOT IN` 查出 **0 行**。执行计划一个是 `Hash Anti Join`，一个是 `NOT (hashed SubPlan 1)` |
| `ON CONFLICT` 批内重复键 | 真实报错 `ON CONFLICT DO UPDATE command cannot affect row a second time`，`DISTINCT ON` 修好 |
| 忘了 `is_cancelled = FALSE` | 总量 179983 对 177232，差 1.55%，**不报错不警告** |
| `EXPLAIN` | 估算行数比实际大一个数量级，且估算值随统计信息状态变动 |

### `make guard`

```
攻击拦截率  12/12 = 100%
正常通过率   6/6 = 100%
```

**两个都要报。只测攻击不测误杀是评估里最常见的错误。**

`SELECT 'DROP TABLE sku' AS note FROM sku` 被正确放行，正则会误杀它。
`SELECT pg_read_file('/etc/passwd')` 被函数黑名单拦住，只读权限拦不住它。

### `make eval`

```
7 个比对场景，裸 df1.equals(df2) 判错了 4 个
```

列顺序不同、行顺序不同且问题没要求排序、多返回一列、结果有重复行，
这四种裸比对都判错，归一化加子集判定之后全对。

## 跑起来才发现的一件事，值得单独讲

第一次跑，`WMS_QTY_MISMATCH` 注入 5 个却查出 22 个，多出的 17 个
恰好是 `BIN_LEDGER_DRIFT` 的那 17 个 key。

**因为改坏的是 `bin.actual_qty`，而 WMS 那边是对的，所以跨系统对账也看到了差异。
一个根因在多条规则上同时报警。**

没有归因，业务方看到 83 条会以为天塌了，实际只有 66 个独立问题。
**对账系统最常见的死法，就是因为重复计数把差异数系统性放大，然后没人再信这份报告。**

解法在 `recon/rules.py` 的 `attribute_root_cause()`：规则按「离根因有多近」
排优先级，同一个实体上的表层症状标成 `派生症状`，降级为 info 不计入独立问题。

## 目录

```
sql/schema.sql          数据模型，抄 ERPNext 的 item/variant/ledger/bin
synth/generate.py       合成数据 + 故意注入已知错误，每种都记录影响了哪些 key
recon/rules.py          三条对账规则 + 根因归因
run_pipeline.py         端到端加验收
demo/sql_traps.py       5 个 SQL 坑的现场实证
demo/agent_guard.py     Project 2 的 guard.py，纯函数不需要 API key
demo/eval_compare.py    Project 2 的结果比对
reference/              开源参考文件，`make reference` 下载，不进 git
                        ERPNext 是 GPL-3.0，本仓库不分发它的副本
  erpnext/              bin / stock_ledger_entry / item_variant 的表定义
  vanna/base.py         三类知识的那三对方法
  sql_eval/eval.py      normalize_table 和 compare_query_results
```

## reference 里该看什么

| 文件 | 看什么 |
|---|---|
| `erpnext/bin.json` | `actual_qty` `reserved_qty` `ordered_qty` `projected_qty`，注意全是 `Float` |
| `erpnext/stock_ledger_entry.json` | `qty_after_transaction` 物化结存、`voucher_type` 加 `voucher_no` 多态外键、`is_cancelled` 软删除、`posting_date` 和 `posting_time` 分两列 |
| `erpnext/stock_reconciliation.json` | ERPNext 自己也需要一个对账 doctype，证明漂移是真问题 |
| `vanna/base.py` | `add_ddl` / `add_documentation` / `add_question_sql` 三对方法，以及 `get_sql_prompt()` |
| `sql_eval/eval.py` | `normalize_table()` 和 `compare_query_results()` 返回两个布尔值 |
