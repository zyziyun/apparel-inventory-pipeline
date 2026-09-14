#!/usr/bin/env bash
# 下载课上要打开的开源参考文件。
#
# 这些文件不进 git：ERPNext 是 GPL-3.0，把它的文件提交进来会给本仓库
# 带来 copyleft 义务。读设计没问题，分发副本是另一回事。
# 其余两个虽然是 MIT 和 Apache-2.0，一并走下载，保持一致。
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p reference/{erpnext,vanna,sql_eval}
B=https://raw.githubusercontent.com

# ERPNext  GPL-3.0  只读它的表定义，学数据模型
for f in bin stock_ledger_entry item_variant item_variant_attribute stock_reconciliation; do
    curl -sfo "reference/erpnext/$f.json" \
        "$B/frappe/erpnext/develop/erpnext/stock/doctype/$f/$f.json"
done

# vanna  MIT  已归档，读三类知识的那三对方法和 get_sql_prompt
curl -sfo reference/vanna/base.py "$B/vanna-ai/vanna/main/src/vanna/legacy/base/base.py"

# defog sql-eval  Apache-2.0  读 normalize_table 和 compare_query_results
curl -sfo reference/sql_eval/eval.py "$B/defog-ai/sql-eval/main/eval/eval.py"

echo "下载完成："
find reference -type f | sort | sed 's/^/  /'
